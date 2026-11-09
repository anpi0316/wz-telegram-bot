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

# Глобальный кэш сборок из MetaRadar
WEAPON_BUILDS = {}

# Хранилище последнего опроса (message_id) для анализа
LAST_POLL_MESSAGE_ID = None

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

# ========== ПАРСИНГ МЕТЫ (MetaRadar API) ==========
def parse_meta_raw():
    global WEAPON_BUILDS
    try:
        response = httpx.get(METARADAR_API_URL, headers=HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
        data = response.json()

        weapons = data.get("weapons", [])
        if not weapons:
            logging.warning("MetaRadar: пустой список оружия")
            return None

        groups = {"Long range": [], "Close range": [], "Sniper": []}
        WEAPON_BUILDS = {}

        for w in weapons:
            role = (w.get("role") or "").lower()
            name = w.get("name", "").strip()
            if not name:
                continue

            build = w.get("build")
            if build:
                WEAPON_BUILDS[name] = build

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
    rows = []
    for key, items in groups.items():
        if not items:
            continue
        row = []
        for name in items:
            row.append(InlineKeyboardButton(name, callback_data=f"build|{name}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)

# ========== СБОРКА ИЗ METARADAR (БЕЗ LLM) ==========
async def get_weapon_build(weapon_name: str) -> str:
    build = WEAPON_BUILDS.get(weapon_name)
    if not build:
        return f"🔫 <b>{weapon_name}</b>\n\nНет данных по этой сборке."

    lines = [f"🔫 <b>{weapon_name}</b>"]

    label = build.get("label", "")
    if label:
        lines.append(f"🎯 {label}")

    lines.append("")

    attachments = build.get("attachments", [])
    if isinstance(attachments, list):
        for att in attachments:
            if isinstance(att, dict):
                slot = att.get("slot", "").strip()
                name = att.get("name", "").strip()
                if name:
                    lines.append(f"• <b>{slot}</b>: {name}")
    elif isinstance(attachments, dict):
        for slot, name in attachments.items():
            if name:
                lines.append(f"• <b>{slot}</b>: {name}")

    share_code = build.get("shareCode", "")
    if share_code:
        lines.append("")
        lines.append(f"📋 Share code: <code>{share_code}</code>")

    return "\n".join(lines)

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

# ========== КОМАНДА /ask ==========
async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет вопрос в LLM, подмешивая свежие новости и сборки из кэша."""
    question = " ".join(context.args).strip() if context.args else ""

    if not question:
        await update.message.reply_text(
            "🤔 Напишите вопрос после команды, например:\n"
            "<code>/ask какая сейчас мета?</code>\n"
            "<code>/ask подбери сборку для Signal 50</code>",
            parse_mode="HTML",
        )
        return

    loading_msg = await update.message.reply_text("⏳ Думаю над ответом…")

    # 1. Свежие новости из блога
    posts = parse_warzone_posts_fresh(limit=5)
    news_context = ""
    if posts:
        news_lines = [f"— {p['title']} ({p['date']})" for p in posts]
        news_context = "\n".join(news_lines)
    else:
        last_date = load_last_news_date()
        if last_date:
            news_context = f"Свежих новостей нет. Последняя была: {last_date}."

    # 2. Ищем сборку, если вопрос про оружие
    weapon_context = ""
    question_lower = question.lower()
    for weapon_name in WEAPON_BUILDS.keys():
        if weapon_name.lower() in question_lower:
            build = WEAPON_BUILDS[weapon_name]
            att_lines = []
            if isinstance(build, dict):
                for att in build.get("attachments", []):
                    if isinstance(att, dict):
                        att_lines.append(f"  • {att.get('slot', '')}: {att.get('name', '')}")
                share = build.get("shareCode", "")
            else:
                share = ""
            if att_lines:
                weapon_context = f"\nАктуальная сборка для {weapon_name} (из MetaRadar):\n" + "\n".join(att_lines)
                if share:
                    weapon_context += f"\n  Share code: {share}"
            break

    # 3. Текущая дата
    now_msk = datetime.datetime.now(timezone('Europe/Moscow'))
    today_str = now_msk.strftime("%d %B %Y")

    # 4. Формируем промпт
    prompt = (
        f"Сегодня {today_str} (МСК).\n"
        "Ты — опытный эксперт по Call of Duty: Warzone. "
        "Отвечай кратко, по делу, на русском языке.\n\n"
        "ПРАВИЛА:\n"
        "— Только русский язык.\n"
        "— Без ссылок, URL, доменов.\n"
        "— Не пиши вступления вроде «Конечно» или «Вот».\n"
        "— Максимум 5–6 предложений.\n"
        "— Если в контексте есть факты (новости или сборка) — используй их. "
        "Не выдумывай то, чего нет в контексте.\n\n"
    )

    if news_context:
        prompt += f"СВЕЖИЕ НОВОСТИ WARZONE (из блога):\n{news_context}\n\n"
    if weapon_context:
        prompt += f"ДАННЫЕ ПО ОРУЖИЮ:\n{weapon_context}\n\n"

    prompt += f"Вопрос игрока: {question}"

    try:
        response = openrouter_client.chat.completions.create(
            model="openrouter/free",
            messages=[
                {"role": "system", "content": "Ты отвечаешь ТОЛЬКО на русском языке. Кратко, без ссылок. Используй только факты из контекста."},
                {"role": "user", "content": prompt},
            ],
            timeout=HTTP_TIMEOUT,
        )
        answer = response.choices[0].message.content.strip()
        answer = re.sub(r"^(Sure|Of course|Here.*?:)\s*", "", answer, flags=re.IGNORECASE)
        answer = re.sub(r"https?://\S+", "", answer).strip()

        if not answer:
            answer = "⚠️ Не удалось получить ответ. Попробуйте переформулировать вопрос."

        await update.message.reply_text(answer)
    except Exception as e:
        logging.error(f"Ошибка LLM (/ask): {e}")
        await update.message.reply_text("⚠️ Не удалось получить ответ. Попробуйте позже.")
    finally:
        try:
            await loading_msg.delete()
        except Exception:
            pass

# ========== АНАЛИЗ ОПРОСА ЧЕРЕЗ ЧАС ==========
async def analyze_poll(context: ContextTypes.DEFAULT_TYPE):
    """Останавливает опрос и, если голосов >= 5, просит LLM дать комментарий."""
    global LAST_POLL_MESSAGE_ID
    if not LAST_POLL_MESSAGE_ID:
        logging.info("analyze_poll: нет сохранённого message_id опроса")
        return

    try:
        poll = await context.bot.stop_poll(
            chat_id=CHAT_ID,
            message_id=LAST_POLL_MESSAGE_ID,
        )
        total = poll.total_voter_count
        logging.info(f"Опрос остановлен. Всего голосов: {total}")

        if total < 5:
            logging.info(f"Голосов меньше 5 ({total}), комментарий не отправляем")
            LAST_POLL_MESSAGE_ID = None
            return

        stats_lines = []
        for opt in poll.options:
            stats_lines.append(f"— {opt.text}: {opt.voter_count}")
        stats_text = "\n".join(stats_lines)

        prompt = (
            "Ты — дружелюбный ассистент игрового чата по Call of Duty: Warzone.\n"
            f"В опросе «Сегодня играем в WZ?» проголосовало {total} человек:\n"
            f"{stats_text}\n\n"
            "Дай ОДИН короткий комментарий (1–2 предложения) на русском.\n"
            "Если большинство за «20:00» — порадуйся, что состав собирается.\n"
            "Если большинство за «Нет» или поровну — мягко предложи передумать: "
            "«может, всё-таки залетите хоть на часок».\n"
            "ПРАВИЛА:\n"
            "— Только русский язык.\n"
            "— Без ссылок.\n"
            "— Без имён и обращений к конкретным людям.\n"
            "— Максимум 2 предложения.\n"
            "— Начни с эмодзи по смыслу.\n"
        )

        try:
            response = openrouter_client.chat.completions.create(
                model="openrouter/free",
                messages=[
                    {"role": "system", "content": "Ты отвечаешь ТОЛЬКО на русском языке. Кратко, без ссылок, без имён."},
                    {"role": "user", "content": prompt},
                ],
                timeout=HTTP_TIMEOUT,
            )
            comment = response.choices[0].message.content.strip()
            comment = re.sub(r"https?://\S+", "", comment).strip()
        except Exception as e:
            logging.error(f"Ошибка LLM (комментарий к опросу): {e}")
            comment = ""

        if comment:
            await context.bot.send_message(chat_id=CHAT_ID, text=comment)
            logging.info(f"Комментарий к опросу отправлен: {comment[:80]}...")

        LAST_POLL_MESSAGE_ID = None

    except Exception as e:
        logging.error(f"Ошибка analyze_poll: {e}")

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

# ========== ОТПРАВКА МЕТЫ + НОВОСТЕЙ + ОПРОСА ==========
async def send_meta_and_news(context, chat_id: int, is_command: bool = False):
    global LAST_POLL_MESSAGE_ID

    posts = parse_warzone_posts_fresh(limit=5)
    summary = await get_llm_summary(posts)
    groups = parse_meta_raw()

    bot = context.bot

    # 1. Мета с кнопками
    if groups:
        meta_text = build_meta_message(groups)
        keyboard = build_meta_keyboard(groups)
        await bot.send_message(chat_id=chat_id, text=meta_text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await bot.send_message(chat_id=chat_id, text="🎯 Мета временно недоступна")

    # 2. Новости
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
        news_text = f"📰 News: Nothing New (последняя: {last_date})" if last_date else "📰 News: Nothing New"
    await bot.send_message(chat_id=chat_id, text=news_text)

    # 3. Опрос — сохраняем message_id
    sent_poll = await bot.send_poll(
        chat_id=chat_id,
        question="Сегодня играем в WZ?",
        options=["✅ 20:00", "❌ Нет"],
        is_anonymous=False,
        allows_multiple_answers=False,
    )
    LAST_POLL_MESSAGE_ID = sent_poll.message_id

    # 4. Планируем анализ через 1 час
    if context.job_queue:
        context.job_queue.run_once(
            analyze_poll,
            when=3600,
            name=f"poll_analysis_{sent_poll.message_id}",
        )
        logging.info(f"Анализ опроса запланирован через 1 час (message_id={LAST_POLL_MESSAGE_ID})")

    logger.info(f"✅ Мета + новости + опрос отправлены. Мета: {'да' if groups else 'нет'}, Новости: {'да' if summary else 'нет'}")

# ========== ХЕНДЛЕРЫ ==========
async def send_daily_poll(context: ContextTypes.DEFAULT_TYPE):
    try:
        await send_meta_and_news(context, chat_id=CHAT_ID, is_command=False)
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке опроса: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Бот для ежедневных опросов!\n"
        f"Каждый день в {ВРЕМЯ_ОТПРАВКИ_STR} будет опрос с метой и новостями Warzone.\n\n"
        f"🎯 Нажмите на кнопку с оружием, чтобы получить актуальную сборку.\n"
        f"💬 <code>/ask вопрос</code> — спросить у ИИ-эксперта по Warzone.\n"
        f"📊 <code>/poll</code> — запустить опрос вручную.",
        parse_mode="HTML",
    )

async def poll_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await send_meta_and_news(context, chat_id=update.effective_chat.id, is_command=True)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

async def on_weapon_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("build|"):
        return
    weapon_name = data.split("|", 1)[1]

    loading_msg = await query.message.reply_text(
        f"⏳ Ищу актуальную сборку для <b>{weapon_name}</b>...",
        parse_mode="HTML",
    )

    build = await get_weapon_build(weapon_name)

    await query.message.reply_text(build, parse_mode="HTML")
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
    application.add_handler(CommandHandler("ask", ask_command))
    application.add_handler(CallbackQueryHandler(on_weapon_button, pattern=r"^build\|"))

    job_queue = application.job_queue
    job_queue.run_daily(send_daily_poll, time=ВРЕМЯ_ОТПРАВКИ, days=tuple(range(7)))

    logger.info("🚀 Бот запущен!")
    application.run_polling()