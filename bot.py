import os
import logging
from datetime import time
from pytz import timezone

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ========== НАСТРОЙКИ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ==========
TOKEN = os.environ.get("BOT_TOKEN")  # Токен будет на Render
CHAT_ID = int(os.environ.get("CHAT_ID", 0))  # ID группы (преобразуем в число)
ВРЕМЯ_ОТПРАВКИ = time(hour=12, minute=0, tzinfo=timezone('Europe/Moscow'))  # Можно тоже вынести в переменные при желании
# ======================================================

# Проверка, что переменные загружены
if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не задан! Добавьте его в переменные окружения.")
if not CHAT_ID:
    raise ValueError("❌ CHAT_ID не задан! Добавьте его в переменные окружения.")

# Включаем логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

async def send_daily_poll(context: ContextTypes.DEFAULT_TYPE):
    """Отправляет опрос каждый день"""
    try:
        await context.bot.send_poll(
            chat_id=CHAT_ID,
            question="Сегодня играем в WZ?",
            options=["Да", "Нет"],
            is_anonymous=False,
            allows_multiple_answers=False,
        )
        logger.info("✅ Опрос успешно отправлен в чат %s", CHAT_ID)
    except Exception as e:
        logger.error(f"❌ Ошибка при отправке опроса: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    await update.message.reply_text(
        "👋 Бот для ежедневных опросов!\n"
        "Каждый день в 12:00 будет опрос 'Сегодня играем в WZ?'"
    )

async def poll_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск опроса командой /poll"""
    try:
        await update.message.reply_poll(
            question="Сегодня играем в WZ?",
            options=["Да", "Нет"],
            is_anonymous=False,
            allows_multiple_answers=False,
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

if __name__ == "__main__":
    logger.info("🚀 Запуск бота...")
    
    # Создаём приложение
    application = Application.builder().token(TOKEN).build()

    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("poll", poll_now))

    # Настраиваем ежедневную отправку
    job_queue = application.job_queue
    job_queue.run_daily(
        send_daily_poll, 
        time=ВРЕМЯ_ОТПРАВКИ, 
        days=tuple(range(7))  # Все дни недели
    )
    
    logger.info("🚀 Бот запущен и готов к работе!")
    
    # Запускаем бота
    application.run_polling()