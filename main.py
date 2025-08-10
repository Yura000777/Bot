from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request
import datetime
import asyncio
import os

# Токен беремо з Environment Variables
TOKEN = os.environ.get("TELEGRAM_TOKEN")

# Flask сервер
flask_app = Flask(__name__)

scheduler = BackgroundScheduler()
scheduler.start()

# Зберігаємо нагадування у пам'яті
reminders = {}

# 🔔 Відправка нагадування
async def send_reminder(bot, chat_id, text):
    await bot.send_message(chat_id=chat_id, text=f"⏰ Нагадування: {text}")

# 📌 Головне меню
async def show_main_menu(update, context):
    keyboard = [
        [InlineKeyboardButton("🕒 Встановити нагадування", callback_data="set_reminder")],
        [InlineKeyboardButton("📋 Список нагадувань", callback_data="list_reminders")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text("📌 Головне меню:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("📌 Головне меню:", reply_markup=reply_markup)

# 📌 /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

# 📌 Обробка кнопок
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "set_reminder":
        await query.edit_message_text("⏰ Введи час у форматі HH:MM:", reply_markup=back_button())
        context.user_data["step"] = "waiting_for_time"

    elif data == "list_reminders":
        user_reminders = reminders.get(chat_id, [])
        if not user_reminders:
            await query.edit_message_text("📋 У тебе немає нагадувань.", reply_markup=back_button())
            return

        keyboard = []
        for idx, r in enumerate(user_reminders):
            keyboard.append([InlineKeyboardButton(f"🗑 {r['time']} - {r['text']} ({r['repeat']})", callback_data=f"delete_{idx}")])
        keyboard.append([InlineKeyboardButton("↩ На початок", callback_data="main_menu")])

        await query.edit_message_text("📋 Твої нагадування:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("delete_"):
        idx = int(data.split("_")[1])
        if chat_id in reminders and 0 <= idx < len(reminders[chat_id]):
            job_id = reminders[chat_id][idx]['job_id']
            scheduler.remove_job(job_id)
            reminders[chat_id].pop(idx)
        await show_main_menu(update, context)

    elif data == "main_menu":
        await show_main_menu(update, context)

    elif data.startswith("repeat_"):
        repeat_type = data.split("_")[1]
        chosen_time = context.user_data.get("chosen_time")
        chosen_task = context.user_data.get("chosen_task")

        now = datetime.datetime.now()
        hours, minutes = map(int, chosen_time.split(":"))
        run_time = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if run_time < now:
            run_time += datetime.timedelta(days=1)

        job_id = f"{chat_id}_{chosen_time}_{chosen_task}_{repeat_type}"
        if repeat_type == "once":
            scheduler.add_job(lambda: asyncio.run(send_reminder(context.bot, chat_id, chosen_task)),
                              "date", run_date=run_time, id=job_id)
        elif repeat_type == "daily":
            scheduler.add_job(lambda: asyncio.run(send_reminder(context.bot, chat_id, chosen_task)),
                              "cron", hour=hours, minute=minutes, id=job_id)
        elif repeat_type == "weekdays":
            scheduler.add_job(lambda: asyncio.run(send_reminder(context.bot, chat_id, chosen_task)),
                              "cron", day_of_week="mon-fri", hour=hours, minute=minutes, id=job_id)
        elif repeat_type == "weekends":
            scheduler.add_job(lambda: asyncio.run(send_reminder(context.bot, chat_id, chosen_task)),
                              "cron", day_of_week="sat,sun", hour=hours, minute=minutes, id=job_id)

        reminders.setdefault(chat_id, []).append({
            "time": chosen_time,
            "text": chosen_task,
            "repeat": repeat_type,
            "job_id": job_id
        })

        context.user_data.clear()
        await show_main_menu(update, context)

# 📌 Обробка введеного тексту
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("step")
    chat_id = update.effective_chat.id

    if step == "waiting_for_time":
        try:
            chosen_time = update.message.text.strip()
            datetime.datetime.strptime(chosen_time, "%H:%M")
            context.user_data["chosen_time"] = chosen_time
            context.user_data["step"] = "waiting_for_task"
            await update.message.reply_text("✏️ Що потрібно зробити?", reply_markup=back_button())
        except ValueError:
            await update.message.reply_text("⚠ Невірний формат часу! Використай HH:MM")

    elif step == "waiting_for_task":
        chosen_task = update.message.text.strip()
        context.user_data["chosen_task"] = chosen_task
        context.user_data["step"] = None

        keyboard = [
            [InlineKeyboardButton("🔁 Щодня", callback_data="repeat_daily")],
            [InlineKeyboardButton("1️⃣ Один раз", callback_data="repeat_once")],
            [InlineKeyboardButton("📅 По буднях", callback_data="repeat_weekdays")],
            [InlineKeyboardButton("🏖 По вихідних", callback_data="repeat_weekends")],
            [InlineKeyboardButton("↩ На початок", callback_data="main_menu")]
        ]
        await update.message.reply_text("🔄 Як повторювати нагадування?", reply_markup=InlineKeyboardMarkup(keyboard))

# 📌 Кнопка повернення
def back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩ На початок", callback_data="main_menu")]])

# 🚀 Створення бота
application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

# 📌 Flask webhook endpoint
@flask_app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put(update)
    return "ok"

# 📌 Запуск на Render
if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5000))
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url and not render_url.startswith("https://"):
        render_url = f"https://{render_url}"

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
        webhook_url=f"{render_url}/{TOKEN}"
    )

