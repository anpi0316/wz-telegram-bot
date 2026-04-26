import os
import logging
import threading
import time
import datetime
import random
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from pytz import timezone

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ========== МОТИВАЦИОННЫЕ ФРАЗЫ ==========
MOTIVATIONAL_PHRASES = [
    "🔥 Варзона без тебя — не варзона! Заходи!",
    "🎯 Кто сегодня килов раздаст? Твой выход!",
    "💪 Кто настреляет 10к урона? Покажи им!",
    "🏆 Кто вынесет кабину врагам? Пора устроить зачистку!",
    "🎮 Контроллер/мышь уже заждались твоего фрага!",
    "⚡ Без тебя мы не полный состав!",
    "🍺 Пивко, друзья, вертушка — что ещё нужно?",
    "🔥 Твоя задача — вывезти ближний бой!",
    "🎲 Го дропаться в матчмейкинг и рвать лобби!",
    "💀 Кто из нас сегодня будет нести (или нестись)?",
    "🎙️ Дискорд без твоего голоса — как тир без мишеней!",
    "🪂 Пристегни парашют — летим закрывать фраги!",
    "🏃‍♂️ Твоя задача сегодня — хиллить и возить!",
    "🤣 Покажи им, как мы умеем угарать и раздавать!",
    "🎧 Микрофон настраивай — будет смешно и опасно!",
    "🍕 Матч без перекуса — не матч. Заодно и поешь.",
    "🚁 Кто сегодня утюжит кабины? Пора загрузить ракет!",
    "🎯 Напоминаем: каждый фраг удваивается!",
    "📊 Твой процент винрейта падает. Исправляй выстрелом!",
    "🎬 Сегодня ты — главный киллер. Бери пушку!",
    "🔥 Нам нужен твой урон и пару клатчей!",
    "🤝 Состав почти собран. Где твои демаги?",
    "💣 C4 уже заряжен на твой приход!",
    "😎 Кто разнесёт команду врага? Как обычно — ты!",
    "🫡 Друзья по оружию ждут твоего выстрела!",
    "🏆 Время настрелять на грудь и на хилли!",
    "🕶️ Твоя военная тень уже загрузилась в вертушку!",
    "🎮 Как тебе вчерашняя обнова? Сегодня проверим в бою!",
    "🍻 Один вечер, много демага. Решайся!",
    "💥 Бро, наша кабина врагов не вывезут себя сами!",
    "🎲 Го покажем молодым, как выносить пачками!",
    "💣 Сколько фрагов ты унесёшь сегодня?",
    "🎯 10к урона — слабо? Докажи!",
    "🚀 Кто поведёт команду к эксфилу?",
    "🇷🇺 За родину! И за фраги тоже.",
    "🔥 Покажи, почему ты главный страх лобби!",
    "🎮 Решающий матч вечера — без тебя никак!",
    "🏆 Челлендж: кто сделает тройной килл первым?",
    "⚡ Хватит апать оружие в меню, пора апать скилл!",
    "😎 Без тебя у нас только 3 патрона на команду.",
    "🛸 Давай, покажи скиллы, которые мы не заслужили",
    "🎙️ Врубай мемы в микро и выноси пачками!",
    "🤜 Кто здесь главный соло-квестер? А в команде соберёмся.",
    "🔥 Сегодня твоя очередь быть живой легендой!"
]
# ============================================================

# ========== ФАЙЛ ДЛЯ ХРАНЕНИЯ ИСТОРИИ ФРАЗ ==========
HISTORY_FILE = "used_phrases.json"

def load_used_phrases():
    """Загружает список уже использованных фраз за текущий месяц"""
    if not os.path.exists(HISTORY_FILE):
        return {}
    
    try:
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data
    except Exception as e:
        logger.warning(f"Не удалось загрузить историю фраз: {e}")
        return {}

def save_used_phrases(data):
    """Сохраняет список использованных фраз"""
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Не удалось сохранить историю фраз: {e}")

def get_daily_phrase():
    """
    Возвращает случайную фразу, которая ещё не использовалась в этом месяце.
    Если все фразы использованы — сбрасывает историю и начинает заново.
    """
    today = datetime.datetime.today()
    current_month = today.strftime("%Y-%m")  # Например: "2026-04"
    
    # Загружаем историю
    history = load_used_phrases()
    used_phrases = history.get(current_month, [])
    
    # Доступные фразы (которые ещё не использовались)
    available_phrases = [p for p in MOTIVATIONAL_PHRASES if p not in used_phrases]
    
    # Если все фразы использованы в этом месяце — сбрасываем
    if not available_phrases:
        logger.info(f"📅 Все фразы использованы в месяце {current_month}. Сброс истории!")
        used_phrases = []
        available_phrases = MOTIVATIONAL_PHRASES.copy()
    
    # Выбираем случайную фразу
    chosen_phrase = random.choice(available_phrases)
    
    # Добавляем в использованные и сохраняем
    used_phrases.append(chosen_phrase)
    history[current_month] = used_phrases
    save_used_phrases(history)
    
    logger.info(f"📝 Выбрана фраза #{len(used_phrases)}/{len(MOTIVATIONAL_PHRASES)}: {chosen_phrase[:50]}...")
    
    return chosen_phrase
# ============================================================

# ========== ПРОСТОЙ HTTP-СЕРВЕР ДЛЯ RENDER ==========
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    
    def log_message(self, format, *args):
        pass

def run_http_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHandler)
    server.serve_forever()

threading.Thread(target=run_http_server, daemon=True).start()
# ====================================================

# ========== KEEP-ALIVE ПИНГИ ДЛЯ ПРЕДОТВРАЩЕНИЯ ЗАСЫПАНИЯ ==========
def keep_alive():
    """Пингует Render и Telegram каждые 15 минут, чтобы бот не засыпал"""
    while True:
        time.sleep(900)  # 15 минут (экономит часы)
        try:
            render_url = os.environ.get("RENDER_URL", "https://wz-telegram-bot.onrender.com")
            requests.get(render_url, timeout=10)
        except Exception:
            pass
        
        try:
            token = os.environ.get("BOT_TOKEN")
            if token:
                requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        except Exception:
            pass

threading.Thread(target=keep_alive, daemon=True).start()
# ====================================================

# ========== НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==========
TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID", 0))
ВРЕМЯ_ОТПРАВКИ_STR = os.environ.get("POLL_TIME", "12:45")
# ======================================================

try:
    час, минута = map(int, ВРЕМЯ_ОТПРАВКИ_STR.split(':'))
    ВРЕМЯ_ОТПРАВКИ = datetime.time(hour=час, minute=минута, tzinfo=timezone('Europe/Moscow'))
except:
    ВРЕМЯ_ОТПРАВКИ = datetime.time(hour=12, minute=45, tzinfo=timezone('Europe/Moscow'))
    logging.warning(f"Неверный формат времени '{ВРЕМЯ_ОТПРАВКИ_STR}', используется 12:45")

if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан! Добавьте его в переменные окружения.")
if not CHAT_ID:
    raise ValueError("❌ CHAT_ID не задан! Добавьте его в переменные окружения.")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

async def send_daily_poll(context: ContextTypes.DEFAULT_TYPE):
    """Отправляет опрос каждый день с разной мотивационной фразой"""
    try:
        phrase = get_daily_phrase()
        await context.bot.send_poll(
            chat_id=CHAT_ID,
            question=f"Сегодня играем в WZ?\n{phrase}",
            options=["✅ 20:00", "❌ Нет"],
            is_anonymous=False,
            allows_multiple_answers=False,
        )
        logger.info(f"✅ Опрос успешно отправлен в чат {CHAT_ID}")
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке опроса: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    await update.message.reply_text(
        f"👋 Бот для ежедневных опросов!\n"
        f"Каждый день в {ВРЕМЯ_ОТПРАВКИ_STR} будет опрос 'Сегодня играем в WZ?'\n"
        f"Варианты: ✅ 20:00, ❌ Нет\n\n"
        f"✨ У меня {len(MOTIVATIONAL_PHRASES)} мотивационных фраз, и все уникальны в месяц!"
    )

async def poll_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск опроса командой /poll"""
    try:
        phrase = get_daily_phrase()
        await update.message.reply_poll(
            question=f"Сегодня играем в WZ?\n{phrase}",
            options=["✅ 20:00", "❌ Нет"],
            is_anonymous=False,
            allows_multiple_answers=False,
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

if __name__ == "__main__":
    logger.info(f"🚀 Запуск бота... Время опроса: {ВРЕМЯ_ОТПРАВКИ_STR}")
    logger.info("🔋 Keep-alive поток запущен (пинг каждые 15 минут)")
    logger.info(f"📅 Загружено {len(MOTIVATIONAL_PHRASES)} мотивационных фраз")
    
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("poll", poll_now))

    job_queue = application.job_queue
    job_queue.run_daily(
        send_daily_poll, 
        time=ВРЕМЯ_ОТПРАВКИ, 
        days=tuple(range(7))
    )
    
    logger.info("🚀 Бот запущен и готов к работе! HTTP-сервер слушает порт 10000")
    
    application.run_polling()