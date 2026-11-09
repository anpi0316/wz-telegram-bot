import os
import logging
import threading
import time
import datetime
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from pytz import timezone

import httpx
from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
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
META_URL = "https://warzoneloadout.games/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ========== HTTP-СЕРВЕР ДЛЯ RENDER ==========
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
        except: pass
        try:
            token = os.environ.get("BOT_TOKEN")
            if token:
                httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        except: pass

threading.Thread(target=keep_alive, daemon=True).start()

# ========== ПАРСИНГ БЛОГА (только за последние 24 часа) ==========
def parse_warzone_posts_fresh(limit: int = 5):
    """
    Парсит страницу блога Warzone и возвращает ТОЛЬКО посты,
    опубликованные за последние 24 часа (по дате на странице).
    """
    try:
        response = httpx.get(WARZONE_BLOG_URL, headers=HEADERS, timeout=25, follow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        cards = soup.select("div.blog-card-item")
        logging.info(f"Warzone blog: найдено карточек {len(cards)}")

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
            date_str = date_div.get("data-date", "")  # например "November 05, 2026"
            if not date_str:
                continue

            try:
                # Парсим дату вида "November 05, 2026"
                post_date = datetime.datetime.strptime(date_str, "%B %d, %Y").replace(tzinfo=datetime.timezone.utc)
                age = now - post_date
                if age.total_seconds() > 24 * 3600:
                    # Пост старше 24 часов — пропускаем
                    continue
            except Exception as e:
                logging.warning(f"Не удалось распарсить дату '{date_str}': {e}")
                continue

            sub = card.select_one(".subtitle")
            summary = sub.get_text(strip=True) if sub else ""

            fresh_posts.append({
                "title": title,
                "date": date_str,
                "summary": summary,
                "url": url,
            })
            if len(fresh_posts) >= limit:
                break

        logging.info(f"Свежих постов (за 24ч): {len(fresh_posts)}")
        return fresh_posts
    except Exception as e:
        logging.warning(f"Ошибка парсинга Warzone блога: {e}")
        return []

# ========== ПАРСИНГ МЕТЫ ==========
def parse_meta():
    """
    Парсит warzoneloadout.games и извлекает топ-оружие из S-тира.
    Возвращает строку вида: "Cronen Osprey (Long Range), VEL 46 (Close Range)"
    """
    try:
        response = httpx.get(META_URL, headers=HEADERS, timeout=25, follow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Ищем заголовки оружия в S-тире. По HTML структуре это <h2> внутри блоков
        # с классом, содержащим "Absolute Meta" или "S Tier".
        # Универсальный подход: ищем все <h2>, в тексте которых есть "#1"
        meta_items = []
        for h2 in soup.find_all("h2"):
            text = h2.get_text(" ", strip=True)
            # Пример: "Cronen Osprey#1 Long RangeAssault Riflemw4"
            # Или: "VEL 46#1 Close RangeSMGModern Warfare 2"
            if "#1" in text and ("Long Range" in text or "Close Range" in text or "Sniper" in text):
                # Вытаскиваем имя оружия (до #1) и категорию (Long Range / Close Range / Sniper)
                name_match = re.match(r"^([^#]+)#1\s*(Long Range|Close Range|Sniper)", text)
                if name_match:
                    weapon_name = name_match.group(1).strip()
                    category = name_match.group(2)
                    meta_items.append(f"{weapon_name} ({category})")

        if meta_items:
            # Берём первые 3 (Long Range, Close Range, Sniper)
            unique = list(dict.fromkeys(meta_items))[:3]
            result = ", ".join(unique)
            logging.info(f"Мета спарсена: {result}")
            return result
        else:
            logging.warning("Мета не найдена на странице")
            return None
    except Exception as e:
        logging.warning(f"Ошибка парсинга меты: {e}")
        return None

# ========== LLM ==========
async def get_llm_summary(posts):
    """
    Отправляет посты в LLM. Возвращает краткую сводку на русском без ссылок
    и без искусственных оценок «обновление есть/нет».
    Если постов нет — возвращает None.
    """
    if not posts:
        return None

    lines = []
    for i, p in enumerate(posts, 1):
        lines.append(
            f"{i}. [{p['date']}] {p['title']}\n"
            f"   Описание: {p['summary'][:400]}"
        )
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
        "— Начни с одного эмодзи по смыслу (🎮, 🗺️, 🔫, 🏆, 📰 и т.п.).\n\n"
        f"Посты:\n{news_text}"
    )

    try:
        response = openrouter_client.chat.completions.create(
            model="openrouter/free",
            messages=[
                {"role": "system", "content": "Ты отвечаешь ТОЛЬКО на русском языке. Кратко, без ссылок, без оценок «есть/нет»."},
                {"role": "user", "content": prompt}
            ],
            timeout=40
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
except:
    ВРЕМЯ_ОТПРАВКИ = datetime.time(hour=20, minute=0, tzinfo=timezone('Europe/Moscow'))

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== ОТПРАВКА ОПРОСА ==========
async def send_daily_poll(context: ContextTypes.DEFAULT_TYPE):
    try:
        posts = parse_warzone_posts_fresh(limit=5)
        summary = await get_llm_summary(posts)
        meta = parse_meta()

        lines = ["Сегодня играем в WZ?"]
        if meta:
            lines.append(f"🎯 Мета: {meta}")
        if summary:
            lines.append(f"📰 {summary}")

        question_text = "\n\n".join(lines)

        await context.bot.send_poll(
            chat_id=CHAT_ID,
            question=question_text[:300],
            options=["✅ 20:00", "❌ Нет"],
            is_anonymous=False,
            allows_multiple_answers=False,
        )
        logger.info(f"✅ Опрос отправлен. Мета: {'да' if meta else 'нет'}, Новости: {'да' if summary else 'нет'}")
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке опроса: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Бот для ежедневных опросов!\n"
        f"Каждый день в {ВРЕМЯ_ОТПРАВКИ_STR} будет опрос с метой и новостями Warzone (если есть за сутки)."
    )

async def poll_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        posts = parse_warzone_posts_fresh(limit=5)
        summary = await get_llm_summary(posts)
        meta = parse_meta()

        lines = ["Сегодня играем в WZ?"]
        if meta:
            lines.append(f"🎯 Мета: {meta}")
        if summary:
            lines.append(f"📰 {summary}")

        question_text = "\n\n".join(lines)

        await update.message.reply_poll(
            question=question_text[:300],
            options=["✅ 20:00", "❌ Нет"],
            is_anonymous=False,
            allows_multiple_answers=False,
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

if __name__ == "__main__":
    logger.info(f"🚀 Запуск бота... Время опроса: {ВРЕМЯ_ОТПРАВКИ_STR}")

    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("poll", poll_now))

    job_queue = application.job_queue
    job_queue.run_daily(send_daily_poll, time=ВРЕМЯ_ОТПРАВКИ, days=tuple(range(7)))

    logger.info("🚀 Бот запущен!")
    application.run_polling()