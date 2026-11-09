import os
import logging
import threading
import time
import datetime
import json
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from pytz import timezone

import httpx
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from openai import OpenAI

# ========== НАСТРОЙКИ ==========
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
if not OPENROUTER_API_KEY:
    raise ValueError("❌ OPENROUTER_API_KEY не задан!")

openrouter_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

WARZONE_BLOG_URL = "https://www.callofduty.com/blog/warzone"
METARADAR_API_URL = "https://metaradar.gg/api/v1/meta/warzone"

HTTP_TIMEOUT = 80

HEADERS = {
    "User-Agent": "WZ-Poll-Bot/1.0 (https://github.com/anpi0316/wz-telegram-bot)",
    "Accept": "application/json",
}

BLOG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

LAST_NEWS_FILE = "last_news.json"

# ========== HTTP-СЕРВЕР ДЛЯ RENDER (с HEAD) ==========
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass

def run_http_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_http_server, daemon=True).start()

# ========== KEEP-ALIVE ==========
def keep_alive():
    while True:
        time.sleep(900)
        try:
            httpx.get(os.environ.get("RENDER_URL", "https://wz-telegram-bot.onrender.com"), timeout=10)
        except Exception:
            pass
        try:
            token = os.environ.get("BOT_TOKEN")
            if token:
                httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        except Exception:
            pass

threading.Thread(target=keep_alive, daemon=True).start()

# ========== ФАЙЛ ДЛЯ ДАТЫ ПОСЛЕДНЕЙ НОВОСТИ ==========
def save_last_news_date(date_str: str):
    try:
        with open(LAST_NEWS_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_date": date_str}, f, ensure_ascii=False)
    except Exception as e:
        logging.warning(f"Не удалось сохранить дату новости: {e}")

def load_last_news_date():
    try:
        if os.path.exists(LAST_NEWS_FILE):
            with open(LAST_NEWS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("last_date")
    except Exception as e:
        logging.warning(f"Не удалось загрузить дату новости: {e}")
    return None

def get_latest_post_date():
    """Возвращает дату самого свежего поста на блоге (даже если он старше 24ч)."""
    try:
        response = httpx.get(WARZONE_BLOG_URL, headers=BLOG_HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        first_card = soup.select_one("div.blog-card-item")
        if not first_card:
            return None
        date_div = first_card.select_one(".news-published")
        if date_div:
            return date_div.get("data-date")
    except Exception as e:
        logging.warning(f"Не удалось получить дату последнего поста: {e}")
    return None

# ========== ПАРСИНГ БЛОГА ==========
def parse_warzone_posts_fresh(limit: int = 5):
    try:
        response = httpx.get(WARZONE_BLOG_URL, headers=BLOG_HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        cards = soup.select("div.blog-card-item")
        now = datetime.datetime.now(datetime.timezone.utc)
        fresh_posts = []

        for card in cards:
            title_tag = card.select_one(".title a")
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            href = title_tag.get("href", "").strip()
            url = "https://www.callofduty.com" + href if href.startswith("/") else href

            date_div = card.select_one(".news-published")
            if not date_div:
                continue
            date_str = date_div.get("data-date", "")
            if not date_str:
                continue

            try:
                post_date = datetime.datetime.strptime(date_str, "%B %d, %Y").replace(tzinfo=datetime.timezone.utc)
                if (now - post_date).total_seconds() > 24 * 3600:
                    continue
            except Exception:
                continue

            sub = card.select_one(".subtitle")
            summary = sub.get_text(strip=True) if sub else ""

            fresh_posts.append({"title": title, "date": date_str, "summary": summary, "url": url})
            if len(fresh_posts) >= limit:
                break

        logging.info(f"Свежих постов (за 24ч): {len(fresh_posts)}")
        return fresh_posts
    except Exception as e:
        logging.warning(f"Ошибка парсинга Warzone блога: {e}")
        return []

# ========== ПАРСИНГ МЕТЫ ==========
def parse_meta_raw():
    """
    Возвращает словарь {категория: [список имён]}.
    Используется и для текста, и для кнопок.
    """
    try:
        response = httpx.get(METARADAR_API_URL, headers=HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        data = response.json()

        weapons = data.get("weapons", [])
        if not weapons:
            logging.warning("MetaRadar: пустой список оружия")
            return None

        groups = {"Long range": [], "Close range": [], "Sniper": []}
        for w in weapons:
            role = (w.get("role") or "").lower()
            name = w.get("name", "").strip()
            if not name:
                continue
            if "long range" in role:
                groups["Long range"].append(name)
            elif "close range" in role:
                groups["Close range"].append(name)
            elif "sniper" in role:
                groups["Sniper"].append(name)

        return {
            "Long range": groups["Long range"][:3],
            "Close range": groups["Close range"][:3],
            "Sniper": groups["Sniper"][:3],
        }
    except Exception as e:
        logging.warning(f"Ошибка MetaRadar API: {e}")
        return None

def build_meta_message(groups: dict) -> str:
    """Собирает HTML-сообщение с жирными названиями оружия."""
    if not groups:
        return "🎯 Мета временно недоступна"

    labels = {
        "Long range": "🎯 Long Range",
        "Close range": "🔫 Close Range",
        "Sniper": "🔭 Sniper",
    }
    lines = []
    for key, items in groups.items():
        if items:
            bold_names = ", ".join(f"<b>{n}</b>" for n in items)
            lines.append(f"{labels[key]}: {bold_names}")
    return "\n".join(lines)

def build_meta_keyboard(groups: dict) -> InlineKeyboardMarkup:
    """Создаёт inline-кнопки: одна строка — одна категория, кнопки по оружиям."""
    rows = []
    for key, items in groups.items():
        if not items:
            continue
        row = []
        for name in items:
            # callback_data ограничен 64 байтами — названия оружия влезают
            row.append(InlineKeyboardButton(name, callback_data=f"build|{name}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)

# ========== LLM ДЛЯ СБОРКИ ОРУЖИЯ ==========
async def get_weapon_build(weapon_name: str) -> str:
    """
    Спрашивает LLM об актуальной сборке для оружия.
    Ответ на русском, без ссылок, кратко.
    """
    prompt = (
        f"Ты — эксперт по Call of Duty: Warzone. Дай актуальную мета-сборку для оружия «{weapon_name}».\n\n"
        "ФОРМАТ ОТВЕТА (строго):\n"
        "🔫 <b>{weapon_name}</b>\n"
        "• Дуло: ...\n"
        "• Ствол: ...\n"
        "• Прицел: ...\n"
        "• Магазин: ...\n"
        "• Рукоятка/приклад: ...\n\n"
        "ПРАВИЛА:\n"
        "— Отвечай ТОЛЬКО на русском языке.\n"
        "— НЕ добавляй ссылки, URL.\n"
        "— Не пиши вступления.\n"
        "— Максимум 6 строк.\n"
        "— Если не знаешь актуальную сборку — напиши «Нет данных по этой сборке».\n"
    )

    try:
        response = openrouter_client.chat.completions.create(
            model="openrouter/free",
            messages=[
                {"role": "system", "content": "Ты отвечаешь ТОЛЬКО на русском языке. Кратко, без ссылок."},
                {"role": "user", "content": prompt},
            ],
            timeout=HTTP_TIMEOUT,
        )
        answer = response.choices[0].message.content.strip()
        answer = re.sub(r"https?://\S+", "", answer).strip()
        return answer
    except Exception as e:
        logging.error(f"Ошибка LLM (сборка): {e}")
        return f"⚠️ Не удалось получить сборку для «{weapon_name}». Попробуйте позже."

# ========== LLM ДЛЯ НОВОСТЕЙ ==========
async def get_llm_summary(posts):
    if not posts:
        return None

    lines = []
    for i, p in enumerate(posts, 1):
        lines.append(f"{i}. [{p['date']}] {p['title']}\n   Описание: {p['summary'][:400]}")
    news_text = "\n\n".join(lines)

    prompt = (
        "Ты — краткий новостной ассистент для русскоязычного игрового чата по Call of Duty: Warzone.\n"
        "Ниже — свежие посты с официального блога Warzone (только за последние 24 часа).\n\n"
        "Твоя задача — выбрать ОДИН самый важный пост и пересказать его суть в 1–2 коротких предложениях.\n\n"
        "ПРАВИЛА:\n"
        "— Отвечай ТОЛЬКО на русском языке.\n"
        "— НЕ добавляй ссылки, URL, домены.\n"
        "— НЕ пиши «Обновление: есть/нет» и подобные оценки.\n"
        "— Не пиши вступления вроде «Конечно» или «Вот».\n"
        "— Максимум 2 предложения.\n"
        "— Начни с одного эмодзи по смыслу.\n\n"
        f"Посты:\n{news_text}"
    )

    try:
        response = openrouter_client.chat.completions.create(
            model="openrouter/free",
            messages=[
                {"role": "system", "content": "Ты отвечаешь ТОЛЬКО на русском языке. Кратко, без ссылок, без оценок «есть/нет»."},
                {"role": "user", "content": prompt},
            ],
            timeout=HTTP_TIMEOUT,
        )
        answer = response.choices[0].message.content.strip()
        answer = re.sub(r"^(Sure|Of course|Here.*?:)\s*", "", answer, flags=re.IGNORECASE)
        answer = re.sub(r"https?://\S+", "", answer).strip()
        answer = re.sub(r"🔧?\s*Обновление\s*:\s*(есть|нет)\s*[—\-–]?\s*", "", answer, flags=re.IGNORECASE).strip()
        return answer
    except Exception as e:
        logging.error(f"Ошибка OpenRouter: {e}")
        return None

# ========== НАСТРОЙКИ БОТА ==========
TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID", 0))
ВРЕМЯ_ОТПРАВКИ_STR = os.environ.get("POLL_TIME", "20:00")

try:
    час, минута = map(int, ВРЕМЯ_ОТПРАВКИ_STR.split(':'))
    ВРЕМЯ_ОТПРАВКИ = datetime.time(hour=час, minute=минута, tzinfo=timezone('Europe/Moscow'))
except Exception:
    ВРЕМЯ_ОТПРАВКИ = datetime.time(hour=20, minute=0, tzinfo=timezone('Europe/Moscow'))
    logging.warning(f"Неверный формат времени '{ВРЕМЯ_ОТПРАВКИ_STR}', используется 20:00")

if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан!")
if not CHAT_ID:
    raise ValueError("❌ CHAT_ID не задан!")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== ОТПРАВКА МЕТЫ + НОВОСТЕЙ ==========
async def send_meta_and_news(context_or_update, is_command: bool = False):
    """
    Отправляет два сообщения:
    1. Мета с кнопками (HTML, жирные названия).
    2. Новости (обычное сообщение).
    Плюс опрос отдельно.
    """
    posts = parse_warzone_posts_fresh(limit=5)
    summary = await get_llm_summary(posts)
    groups = parse_meta_raw()

    bot = context_or_update.bot if hasattr(context_or_update, "bot") else context_or_update.get_bot()
    chat_id = CHAT_ID if not is_command else context_or_update.effective_chat.id

    # 1. Сообщение с метой и кнопками
    if groups:
        meta_text = build_meta_message(groups)
        keyboard = build_meta_keyboard(groups)
        await bot.send_message(
            chat_id=chat_id,
            text=meta_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        await bot.send_message(chat_id=chat_id, text="🎯 Мета временно недоступна")

    # 2. Сообщение с новостями
    if summary and posts:
        latest_date = posts[0].get("date", "")
        if latest_date:
            save_last_news_date(latest_date)
        news_text = f"📰 News: {summary}"
    else:
        last_date = load_last_news_date()
        if not last_date:
            last_date = get_latest_post_date()
            if last_date:
                save_last_news_date(last_date)
        if last_date:
            news_text = f"📰 News: Nothing New (последняя: {last_date})"
        else:
            news_text = "📰 News: Nothing New"

    await bot.send_message(chat_id=chat_id, text=news_text)

    # 3. Опрос
    await bot.send_poll(
        chat_id=chat_id,
        question="Сегодня играем в WZ?",
        options=["✅ 20:00", "❌ Нет"],
        is_anonymous=False,
        allows_multiple_answers=False,
    )
    logger.info(f"✅ Мета + новости + опрос отправлены. Мета: {'да' if groups else 'нет'}, Новости: {'да' if summary else 'нет'}")

# ========== ХЕНДЛЕРЫ ==========
async def send_daily_poll(context: ContextTypes.DEFAULT_TYPE):
    try:
        await send_meta_and_news(context, is_command=False)
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке опроса: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Бот для ежедневных опросов!\n"
        f"Каждый день в {ВРЕМЯ_ОТПРАВКИ_STR} будет опрос с метой и новостями Warzone.\n"
        f"Нажмите на кнопку с оружием, чтобы получить актуальную сборку."
    )

async def poll_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await send_meta_and_news(update, is_command=True)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def on_weapon_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатия на кнопку с оружием."""
    query = update.callback_query
    await query.answer()  # убираем "часики" на кнопке

    data = query.data or ""
    if not data.startswith("build|"):
        return
    weapon_name = data.split("|", 1)[1]

    # Отдельным сообщением отправляем "загрузка..."
    loading_msg = await query.message.reply_text(
        f"⏳ Ищу актуальную сборку для <b>{weapon_name}</b>...",
        parse_mode="HTML",
    )

    build = await get_weapon_build(weapon_name)

    # Отправляем ответ отдельным сообщением
    await query.message.reply_text(
        f"<b>{weapon_name}</b>\n\n{build}",
        parse_mode="HTML",
    )
    # Удаляем "загрузку"
    try:
        await loading_msg.delete()
    except Exception:
        pass

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    logger.info(f"🚀 Запуск бота... Время опроса: {ВРЕМЯ_ОТПРАВКИ_STR}")

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("poll", poll_now))
    application.add_handler(CallbackQueryHandler(on_weapon_button, pattern=r"^build\|"))

    job_queue = application.job_queue
    job_queue.run_daily(send_daily_poll, time=ВРЕМЯ_ОТПРАВКИ, days=tuple(range(7)))

    logger.info("🚀 Бот запущен!")
    application.run_polling()