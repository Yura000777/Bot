import os
import json
from datetime import datetime, timedelta
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL")

KYIV_TZ = pytz.timezone("Europe/Kyiv")
DATA_FILE = "reminders.json"
reminders = {}

# ===== Збереження / завантаження нагадувань =====
def load_reminders():
    global reminders
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for chat_id, rems in data.items():
                reminders[int(chat_id)] = [
                    {
                        "task": r["task"],
                        "time": KYIV_TZ.localize(datetime.fromisoformat(r["time"])),
                        "repeat": r["repeat"]
                    }
                    for r in rems
                ]

def save_reminders():
    data = {}
    for chat_id, rems in reminders.items():
        data[chat_id] = [
            {
                "task": r["task"],
                "time": r["time"].isoformat(),
                "repeat": r["repeat"]
            }
            for r in rems
        ]
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ===== Форматування часу =====
def format_time_delta(td):
    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days > 0: parts.append(f"{days} дн")
    if hours > 0: parts.append(f"{hours} год")
    if minutes > 0: parts.append(f"{minutes} хв")
    return " ".join(parts) if parts else "менше хвилини"

# ===== Меню =====
def main_menu():
    keyboard = [
        [InlineKeyboardButton("➕ Додати нагадування", callback_data="set_reminder")],
        [InlineKeyboardButton("📋 Список нагадувань", callback_data="list_reminders")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def safe_edit_message_text(query, text, **kwargs):
    try:
        if query.message.text != text:
            await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

# ===== Команди =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Вітаю! Оберіть дію:", reply_markup=main_menu())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    if query.data == "main_menu":
        await safe_edit_message_text(query, "Головне меню:", reply_markup=main_menu())

    elif query.data == "set_reminder":
        context.user_data["step"] = "waiting_for_task"
        await safe_edit_message_text(query, "Введіть текст нагадування:",
                                     reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Назад", callback_data="main_menu")]]))

    elif query.data == "list_reminders":
        user_reminders = reminders.get(chat_id, [])
        if not user_reminders:
            await safe_edit_message_text(query, "У вас немає активних нагадувань.", reply_markup=main_menu())
            return
        text = "📋 Ваші нагадування:\n"
        keyboard = []
        now = datetime.now(KYIV_TZ)
        for i, r in enumerate(user_reminders):
            remaining = format_time_delta(r["time"] - now)
            text += f"{i+1}. {r['task']} ⏳ {remaining} ({r['repeat']})\n"
            keyboard.append([InlineKeyboardButton(f"❌ Видалити {i+1}", callback_data=f"delete_{i}")])
        keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="main_menu")])
        await safe_edit_message_text(query, text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data.startswith("delete_"):
        idx = int(query.data.split("_")[1])
        if chat_id in reminders and 0 <= idx < len(reminders[chat_id]):
            reminders[chat_id].pop(idx)
            save_reminders()
            await safe_edit_message_text(query, "Нагадування видалено.", reply_markup=main_menu())

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("step")

    if step == "waiting_for_task":
        context.user_data["task"] = update.message.text
        context.user_data["step"] = "waiting_for_time"
        await update.message.reply_text("Введіть час у форматі HH:MM (24-годинний):",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Назад", callback_data="main_menu")]]))

    elif step == "waiting_for_time":
        try:
            chosen_time = datetime.strptime(update.message.text, "%H:%M").time()
            now = datetime.now(KYIV_TZ)
            remind_datetime = KYIV_TZ.localize(datetime.combine(now.date(), chosen_time))
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
    chat_id = update.effective_chat.id

    task = context.user_data["task"]
    remind_time = context.user_data["time"]
    repeat_type = query.data.replace("repeat_", "")

    job_id = schedule_reminder(context, chat_id, remind_time, task, repeat_type)

    if chat_id not in reminders:
        reminders[chat_id] = []
    reminders[chat_id].append({"task": task, "time": remind_time, "repeat": repeat_type})
    save_reminders()

    await safe_edit_message_text(query, "Нагадування створено ✅", reply_markup=main_menu())
    context.user_data.clear()

def schedule_reminder(context, chat_id, remind_time, task, repeat_type):
    now = datetime.now(KYIV_TZ)
    delay = (remind_time - now).total_seconds()
    context.job_queue.run_once(job_send, delay, data={"chat_id": chat_id, "task": task, "repeat_type": repeat_type})
    return f"{chat_id}_{int(remind_time.timestamp())}"

async def job_send(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data["chat_id"]
    task = context.job.data["task"]
    repeat_type = context.job.data["repeat_type"]

    await context.bot.send_message(chat_id, text=f"🔔 Нагадування: {task}")

    now = datetime.now(KYIV_TZ)

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
    else:  # once
        return

    schedule_reminder(context, chat_id, next_time, task, repeat_type)

def restore_jobs(app):
    now = datetime.now(KYIV_TZ)
    for chat_id, rems in reminders.items():
        for r in rems:
            if r["time"] > now:
                app.job_queue.run_once(job_send, (r["time"] - now).total_seconds(),
                                       data={"chat_id": chat_id, "task": r["task"], "repeat_type": r["repeat"]})

def run_app():
    load_reminders()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^(set_reminder|list_reminders|main_menu|delete_\\d+)$"))
    app.add_handler(CallbackQueryHandler(repeat_handler, pattern="^repeat_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    restore_jobs(app)

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        url_path="",
        webhook_url=WEBHOOK_URL
    )

if __name__ == "__main__":
    run_app()
