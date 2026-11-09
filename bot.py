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

# ========== ПАРСИНГ WARZONE БЛОГА ==========
def parse_warzone_posts(limit: int = 5):
    """
    Парсит страницу блога Warzone и возвращает список постов:
    [{"title": "...", "date": "...", "summary": "...", "url": "..."}, ...]
    """
    try:
        response = httpx.get(WARZONE_BLOG_URL, headers=HEADERS, timeout=25, follow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        cards = soup.select("div.blog-card-item")
        logging.info(f"Warzone blog: найдено карточек {len(cards)}")

        posts = []
        for card in cards:
            title_tag = card.select_one(".title a")
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            href = title_tag.get("href", "").strip()
            url = "https://www.callofduty.com" + href if href.startswith("/") else href

            date_div = card.select_one(".news-published")
            date = date_div.get("data-date-loc") if date_div else ""

            sub = card.select_one(".subtitle")
            summary = sub.get_text(strip=True) if sub else ""

            posts.append({
                "title": title, "date": date, "summary": summary, "url": url,
            })
            if len(posts) >= limit:
                break
        return posts
    except Exception as e:
        logging.warning(f"Ошибка парсинга Warzone блога: {e}")
        return []

# ========== LLM ==========
async def get_llm_summary(posts):
    """
    Отправляет посты в LLM. Возвращает красивый ответ на русском БЕЗ ссылок.
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
        "Ниже — свежие посты с официального блога Warzone.\n\n"
        "Твоя задача — выбрать ОДИН самый важный пост (обновление, новый сезон, патч, важная новость) "
        "и дать краткую сводку в таком формате:\n\n"
        "🔧 Обновление: [есть/нет] — если есть, одна короткая фраза о сути (без даты, без ссылки).\n"
        "📰 Новость: одна короткая фраза о самом интересном (без ссылки).\n\n"
        "ПРАВИЛА:\n"
        "— Отвечай ТОЛЬКО на русском языке.\n"
        "— НЕ добавляй ссылки, URL, домены.\n"
        "— Не пиши вступления вроде «Конечно» или «Вот».\n"
        "— Максимум 3 строки.\n"
        "— Используй эмодзи 🔧 и 📰 строго как в шаблоне выше.\n\n"
        f"Посты:\n{news_text}"
    )

    try:
        response = openrouter_client.chat.completions.create(
            model="openrouter/free",
            messages=[
                {"role": "system", "content": "Ты отвечаешь ТОЛЬКО на русском языке. Кратко, без ссылок, без вступлений."},
                {"role": "user", "content": prompt}
            ],
            timeout=40
        )
        answer = response.choices[0].message.content.strip()
        answer = re.sub(r"^(Sure|Of course|Here.*?:)\s*", "", answer, flags=re.IGNORECASE)
        answer = re.sub(r"https?://\S+", "", answer).strip()
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
        posts = parse_warzone_posts(limit=5)
        summary = await get_llm_summary(posts)

        question_text = "Сегодня играем в WZ?"
        if summary:
            question_text = f"Сегодня играем в WZ?\n\n{summary}"

        await context.bot.send_poll(
            chat_id=CHAT_ID,
            question=question_text[:300],
            options=["✅ 20:00", "❌ Нет"],
            is_anonymous=False,
            allows_multiple_answers=False,
        )
        logger.info(f"✅ Опрос отправлен. Сводка: {'да' if summary else 'нет'}")
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке опроса: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Бот для ежедневных опросов!\n"
        f"Каждый день в {ВРЕМЯ_ОТПРАВКИ_STR} будет опрос с новостями Warzone."
    )

async def poll_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        posts = parse_warzone_posts(limit=5)
        summary = await get_llm_summary(posts)

        question_text = "Сегодня играем в WZ?"
        if summary:
            question_text = f"Сегодня играем в WZ?\n\n{summary}"

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