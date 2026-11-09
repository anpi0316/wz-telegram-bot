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

BLOG_URL = "https://www.callofduty.com/ru/blog"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

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
        except: pass
        try:
            token = os.environ.get("BOT_TOKEN")
            if token:
                httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        except: pass

threading.Thread(target=keep_alive, daemon=True).start()

# ========== ПАРСИНГ HTML ==========
def parse_blog_posts(limit: int = 5):
    """
    Парсит страницу блога Call of Duty и возвращает список постов:
    [{ "title": "...", "date": "...", "summary": "...", "url": "...", "games": ["WZ", "MW4"] }, ...]
    """
    try:
        response = httpx.get(BLOG_URL, headers=HEADERS, timeout=25, follow_redirects=True)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        cards = soup.select("div.blog-card-item")
        logging.info(f"HTML спарсен: найдено карточек {len(cards)}")

        posts = []
        for card in cards:
            # Заголовок
            title_tag = card.select_one(".title a")
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            href = title_tag.get("href", "").strip()
            if href.startswith("/"):
                url = "https://www.callofduty.com" + href
            else:
                url = href

            # Игры (WZ, MW4 и т.д.) из блока game-list
            games = []
            for g in card.select(".game-list .game-tile a"):
                g_text = g.get_text(strip=True)
                if g_text:
                    games.append(g_text)

            # Дата
            date_div = card.select_one(".news-published")
            date = date_div.get("data-date-loc") if date_div else ""

            # Подзаголовок
            sub = card.select_one(".subtitle")
            summary = sub.get_text(strip=True) if sub else ""

            posts.append({
                "title": title,
                "date": date,
                "summary": summary,
                "url": url,
                "games": games,
            })
            if len(posts) >= limit:
                break

        return posts
    except Exception as e:
        logging.warning(f"Не удалось спарсить HTML блога: {e}")
        return []

def filter_warzone_posts(posts):
    """Оставляем посты, где есть WZ или в заголовке упоминается Warzone."""
    wz = []
    for p in posts:
        games_lower = [g.lower() for g in p.get("games", [])]
        title_lower = p.get("title", "").lower()
        if "wz" in games_lower or "warzone" in title_lower:
            wz.append(p)
    return wz

# ========== LLM (OpenRouter, ответ только на русском) ==========
async def get_llm_summary(posts):
    """
    Отправляет посты в LLM и просит краткую сводку на русском.
    Если постов нет — возвращает None.
    """
    if not posts:
        return None

    # Формируем текст для LLM
    lines = []
    for i, p in enumerate(posts, 1):
        lines.append(
            f"{i}. [{p['date']}] {p['title']}\n"
            f"   Игры: {', '.join(p['games']) or '—'}\n"
            f"   Описание: {p['summary'][:400]}\n"
            f"   Ссылка: {p['url']}"
        )
    news_text = "\n\n".join(lines)

    prompt = (
        "Ты — краткий новостной ассистент для русскоязычного игрового чата по Call of Duty: Warzone.\n"
        "Ниже — свежие посты с официального блога Call of Duty. Выбери ОДИН самый важный пост, "
        "который касается Warzone (обновление, новый сезон, патч, важная новость).\n"
        "Дай ответ СТРОГО в формате:\n"
        "1) Одна короткая фраза о том, есть ли обновление или важная новость (что именно).\n"
        "2) Одно короткое предложение — суть новости.\n"
        "3) Ссылка на пост.\n\n"
        "ВАЖНО: Отвечай ТОЛЬКО на русском языке. Не пиши ничего на английском. "
        "Не добавляй вступления вроде 'Конечно' или 'Вот'. Максимум 3 строки.\n\n"
        f"Посты:\n{news_text}"
    )

    try:
        response = openrouter_client.chat.completions.create(
            model="openrouter/free",
            messages=[
                {
                    "role": "system",
                    "content": "Ты отвечаешь ТОЛЬКО на русском языке. Кратко, по делу, без вступлений."
                },
                {"role": "user", "content": prompt}
            ],
            timeout=40
        )
        answer = response.choices[0].message.content.strip()
        # На всякий случай чистим возможные английские вступления
        answer = re.sub(r"^(Sure|Of course|Here.*?:)\s*", "", answer, flags=re.IGNORECASE)
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
        posts = parse_blog_posts(limit=5)
        wz_posts = filter_warzone_posts(posts)
        # Если WZ-постов нет — берём все, чтобы хоть что-то было
        posts_for_llm = wz_posts if wz_posts else posts
        summary = await get_llm_summary(posts_for_llm)

        question_text = "Сегодня играем в WZ?"
        if summary:
            question_text = f"Сегодня играем в WZ?\n\n📰 {summary}"

        await context.bot.send_poll(
            chat_id=CHAT_ID,
            question=question_text[:300],  # Telegram ограничивает длину вопроса
            options=["✅ 20:00", "❌ Нет"],
            is_anonymous=False,
            allows_multiple_answers=False,
        )
        logger.info(f"✅ Опрос отправлен. Новости: {'да' if summary else 'нет'}")
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке опроса: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Бот для ежедневных опросов!\n"
        f"Каждый день в {ВРЕМЯ_ОТПРАВКИ_STR} будет опрос с новостями Warzone."
    )

async def poll_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        posts = parse_blog_posts(limit=5)
        wz_posts = filter_warzone_posts(posts)
        posts_for_llm = wz_posts if wz_posts else posts
        summary = await get_llm_summary(posts_for_llm)

        question_text = "Сегодня играем в WZ?"
        if summary:
            question_text = f"Сегодня играем в WZ?\n\n📰 {summary}"

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