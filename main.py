import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = f"{os.environ.get('RENDER_EXTERNAL_URL')}/{TOKEN}"

reminders = {}

def format_time_delta(td):
    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days > 0:
        parts.append(f"{days} дн")
    if hours > 0:
        parts.append(f"{hours} год")
    if minutes > 0:
        parts.append(f"{minutes} хв")
    return " ".join(parts) if parts else "менше хвилини"

def main_menu():
    keyboard = [
        [InlineKeyboardButton("➕ Додати нагадування", callback_data="set_reminder")],
        [InlineKeyboardButton("📋 Список нагадувань", callback_data="list_reminders")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Вітаю! Оберіть дію:", reply_markup=main_menu())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "main_menu":
        await query.edit_message_text("Головне меню:", reply_markup=main_menu())

    elif query.data == "set_reminder":
        context.user_data["step"] = "waiting_for_task"
        await query.edit_message_text("Введіть текст нагадування:", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅ Назад", callback_data="main_menu")]]
        ))

    elif query.data == "list_reminders":
        user_reminders = reminders.get(chat_id, [])
        if not user_reminders:
            await query.edit_message_text("У вас немає активних нагадувань.", reply_markup=main_menu())
            return
        text = "📋 Ваші нагадування:\n"
        keyboard = []
        for i, r in enumerate(user_reminders):
            remaining = format_time_delta(r["time"] - datetime.now())
            text += f"{i+1}. {r['task']} ⏳ {remaining}\n"
            keyboard.append([InlineKeyboardButton(f"❌ Видалити {i+1}", callback_data=f"delete_{i}")])
        keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="main_menu")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith("delete_"):
        idx = int(query.data.split("_")[1])
        if chat_id in reminders and 0 <= idx < len(reminders[chat_id]):
            reminders[chat_id].pop(idx)
            await query.edit_message_text("Нагадування видалено.", reply_markup=main_menu())

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    step = context.user_data.get("step")

    if step == "waiting_for_task":
        context.user_data["task"] = update.message.text
        context.user_data["step"] = "waiting_for_time"
        await update.message.reply_text(
            "Введіть час у форматі HH:MM (24-годинний):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Назад", callback_data="main_menu")]])
        )

    elif step == "waiting_for_time":
        try:
            chosen_time = datetime.strptime(update.message.text, "%H:%M").time()
            now = datetime.now()
            remind_datetime = datetime.combine(now.date(), chosen_time)
            if remind_datetime < now:
                remind_datetime += timedelta(days=1)
            context.user_data["time"] = remind_datetime
            context.user_data["step"] = "waiting_for_repeat"

            keyboard = [
                [InlineKeyboardButton("Один раз", callback_data="repeat_once")],
                [InlineKeyboardButton("Будні", callback_data="repeat_weekdays")],
                [InlineKeyboardButton("Вихідні", callback_data="repeat_weekends")],
                [InlineKeyboardButton("Щодня", callback_data="repeat_daily")],
                [InlineKeyboardButton("⬅ Назад", callback_data="main_menu")]
            ]
            await update.message.reply_text("Оберіть тип повтору:", reply_markup=InlineKeyboardMarkup(keyboard))
        except ValueError:
            await update.message.reply_text("Невірний формат. Спробуйте ще раз.")

async def repeat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    task = context.user_data["task"]
    remind_time = context.user_data["time"]
    repeat_type = query.data.replace("repeat_", "")

    job_id = schedule_reminder(context, chat_id, remind_time, task, repeat_type)

    if chat_id not in reminders:
        reminders[chat_id] = []
    reminders[chat_id].append({"task": task, "time": remind_time, "repeat": repeat_type, "job_id": job_id})

    await query.edit_message_text("Нагадування створено ✅", reply_markup=main_menu())
    context.user_data.clear()

def schedule_reminder(context, chat_id, remind_time, task, repeat_type):
    now = datetime.now()
    delay = (remind_time - now).total_seconds()
    job_id = f"reminder_{chat_id}_{int(remind_time.timestamp())}"
    context.job_queue.run_once(job_send, delay, data={"chat_id": chat_id, "task": task, "repeat_type": repeat_type}, name=job_id)
    return job_id

async def job_send(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    task = context.job.data["task"]
    repeat_type = context.job.data["repeat_type"]

    await context.bot.send_message(chat_id, text=f"🔔 Нагадування: {task}")

    now = datetime.now()
    if repeat_type == "daily":
        next_time = now + timedelta(days=1)
    elif repeat_type == "weekdays":
        next_time = now + timedelta(days=1)
        while next_time.weekday() >= 5:
            next_time += timedelta(days=1)
    elif repeat_type == "weekends":
        next_time = now + timedelta(days=1)
        while next_time.weekday() < 5:
            next_time += timedelta(days=1)
    else:
        return

    schedule_reminder(context, chat_id, next_time, task, repeat_type)

def run_app():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^(set_reminder|list_reminders|main_menu|delete_\\d+)$"))
    app.add_handler(CallbackQueryHandler(repeat_handler, pattern="^repeat_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        url_path=TOKEN,
        webhook_url=WEBHOOK_URL
    )

if __name__ == "__main__":
    run_app()
