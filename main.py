import os
import logging
import asyncio
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters
)

# ------------------ Налаштування логування ------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------ Змінні середовища ------------------
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN не знайдено у змінних середовища!")

# ------------------ Планувальник ------------------
scheduler = BackgroundScheduler(timezone="Europe/Kyiv")
scheduler.start()

# ------------------ Стан розмови ------------------
CHOOSING_TIME, CHOOSING_REPEAT = range(2)
user_tasks = {}

# ------------------ Відправка нагадування ------------------
async def send_reminder(bot, chat_id, task):
    try:
        await bot.send_message(chat_id=chat_id, text=f"🔔 Нагадування: {task}")
    except Exception as e:
        logger.error(f"Помилка при відправці нагадування: {e}")

# ------------------ Старт ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привіт! Напиши, яке завдання треба нагадати.")
    return CHOOSING_TIME

# ------------------ Отримання завдання ------------------
async def set_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    task = update.message.text
    user_tasks[chat_id] = {"task": task}
    await update.message.reply_text("⏰ Вкажи час у форматі ГГ:ХХ")
    return CHOOSING_REPEAT

# ------------------ Отримання часу ------------------
async def set_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    time_str = update.message.text
    try:
        chosen_time = datetime.strptime(time_str, "%H:%M").time()
        user_tasks[chat_id]["time"] = chosen_time

        keyboard = [
            [InlineKeyboardButton("Одноразово", callback_data="repeat_once")],
            [InlineKeyboardButton("Щодня", callback_data="repeat_daily")],
            [InlineKeyboardButton("Будні", callback_data="repeat_weekdays")],
            [InlineKeyboardButton("Вихідні", callback_data="repeat_weekends")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("📅 Як повторювати?", reply_markup=reply_markup)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ Невірний формат. Вкажи час як 14:30.")
        return CHOOSING_REPEAT

# ------------------ Обробка вибору повтору ------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    repeat_type = query.data
    chosen_task = user_tasks[chat_id]["task"]
    chosen_time = user_tasks[chat_id]["time"]

    now = datetime.now()
    run_time = datetime.combine(now.date(), chosen_time)
    if run_time < now:
        run_time += timedelta(days=1)

    hours = chosen_time.hour
    minutes = chosen_time.minute
    job_id = f"{chat_id}_{chosen_time}_{chosen_task}_{repeat_type}"

    def job_func():
        context.application.create_task(
            send_reminder(context.bot, chat_id, chosen_task)
        )

    if repeat_type == "repeat_once":
        scheduler.add_job(job_func, "date", run_date=run_time, id=job_id)
    elif repeat_type == "repeat_daily":
        scheduler.add_job(job_func, "cron", hour=hours, minute=minutes, id=job_id)
    elif repeat_type == "repeat_weekdays":
        scheduler.add_job(job_func, "cron", day_of_week="mon-fri", hour=hours, minute=minutes, id=job_id)
    elif repeat_type == "repeat_weekends":
        scheduler.add_job(job_func, "cron", day_of_week="sat,sun", hour=hours, minute=minutes, id=job_id)

    await query.edit_message_text(
        text=f"✅ Нагадування '{chosen_task}' встановлено на {chosen_time.strftime('%H:%M')} ({repeat_type})"
    )

# ------------------ Запуск ------------------
def main():
    application = ApplicationBuilder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_task)],
            CHOOSING_REPEAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_time)],
        },
        fallbacks=[]
    )

    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(button_handler))

    # Запуск через webhook (Render)
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not render_url:
        raise ValueError("❌ RENDER_EXTERNAL_URL не знайдено у змінних середовища!")

    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        url_path=TOKEN,
        webhook_url=f"{render_url}/{TOKEN}"
    )

if __name__ == "__main__":
    main()
