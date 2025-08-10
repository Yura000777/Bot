import os
import logging
from datetime import datetime, timedelta, time as dtime
from functools import partial

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.base import JobLookupError

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ------------- logging -------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ------------- env checks -------------
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set in environment variables.")

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
if not RENDER_EXTERNAL_URL:
    # we allow missing here to let local testing but in Render you must set it
    logger.warning("RENDER_EXTERNAL_URL not set — webhook URL will be invalid on Render.")

PORT = int(os.getenv("PORT", "5000"))

# ------------- scheduler & storage -------------
scheduler = BackgroundScheduler()
scheduler.start()

# reminders structure:
# reminders[chat_id] = [
#   { "job_id": str, "text": str, "time_str": "HH:MM", "repeat": "once|daily|weekdays|weekends" }
# ]
reminders = {}

# ------------- helpers -------------
def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩ На початок", callback_data="main_menu")]])

def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("➕ Встановити нагадування", callback_data="set_reminder")],
        [InlineKeyboardButton("📋 Список нагадувань", callback_data="list_reminders")],
    ]
    return InlineKeyboardMarkup(keyboard)

def format_timedelta(delta: timedelta) -> str:
    if delta.total_seconds() <= 0:
        return "0 хвилин"
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days} дн")
    if hours:
        parts.append(f"{hours} год")
    if minutes:
        parts.append(f"{minutes} хв")
    return " ".join(parts) if parts else "менше 1 хв"

def get_job_next_run(job_id):
    job = scheduler.get_job(job_id)
    if job and job.next_run_time:
        return job.next_run_time
    return None

# ------------- message sending (async) -------------
async def send_reminder(bot, chat_id: int, text: str):
    try:
        await bot.send_message(chat_id=chat_id, text=f"⏰ Нагадування: {text}")
    except Exception as e:
        logger.exception("Failed to send reminder: %s", e)

# ------------- handlers -------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("📌 Головне меню:", reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text("📌 Головне меню:", reply_markup=main_menu_keyboard())

# Entry from callback buttons
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "main_menu":
        await query.edit_message_text("📌 Головне меню:", reply_markup=main_menu_keyboard())
        return

    if data == "set_reminder":
        # ask for time first (HH:MM)
        await query.edit_message_text("⏰ Введи час у форматі HH:MM (наприклад, 14:30):", reply_markup=back_keyboard())
        context.user_data["step"] = "waiting_for_time"
        return

    if data == "list_reminders":
        user_reminders = reminders.get(chat_id, [])
        if not user_reminders:
            await query.edit_message_text("📋 У тебе немає нагадувань.", reply_markup=back_keyboard())
            return

        keyboard = []
        for idx, r in enumerate(user_reminders):
            # find next run time from scheduler
            next_run = get_job_next_run(r["job_id"])
            if next_run:
                delta = next_run - datetime.utcnow()
                time_left = format_timedelta(delta)
                next_run_str = next_run.strftime("%Y-%m-%d %H:%M UTC")
            else:
                time_left = "—"
                next_run_str = "—"

            btn_text = f"{r['time_str']} — {r['text']} ({r['repeat']})\nзалишилось: {time_left}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"noop_{idx}")])
        keyboard.append([InlineKeyboardButton("↩ На початок", callback_data="main_menu")])
        await query.edit_message_text("📋 Твої нагадування:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("delete_"):
        # callback format delete_IDX
        try:
            idx = int(data.split("_", 1)[1])
        except Exception:
            await query.edit_message_text("Невірний індекс.", reply_markup=back_keyboard())
            return
        user_reminders = reminders.get(chat_id, [])
        if 0 <= idx < len(user_reminders):
            job_id = user_reminders[idx]["job_id"]
            try:
                scheduler.remove_job(job_id)
            except JobLookupError:
                logger.warning("Job not found when deleting: %s", job_id)
            user_reminders.pop(idx)
            reminders[chat_id] = user_reminders
            await query.edit_message_text("✅ Видалено.", reply_markup=main_menu_keyboard())
        else:
            await query.edit_message_text("Нічого не знайдено.", reply_markup=back_keyboard())
        return

    if data.startswith("noop_"):
        # no operation — just keep message (used to display reminders)
        await query.answer()
        return

# handle plain text messages (time and task)
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    step = context.user_data.get("step")

    if step == "waiting_for_time":
        text = update.message.text.strip()
        if text == "/start":
            await start(update, context)
            context.user_data.pop("step", None)
            return

        try:
            hh_mm = datetime.strptime(text, "%H:%M").time()
        except Exception:
            await update.message.reply_text("⚠ Невірний формат. Використай HH:MM", reply_markup=back_keyboard())
            return

        context.user_data["chosen_time"] = hh_mm.strftime("%H:%M")
        context.user_data["chosen_time_obj"] = hh_mm
        context.user_data["step"] = "waiting_for_task"
        await update.message.reply_text("✏️ Що потрібно нагадати?", reply_markup=back_keyboard())
        return

    if step == "waiting_for_task":
        task_text = update.message.text.strip()
        context.user_data["chosen_task"] = task_text
        # ask repeat type via inline buttons
        keyboard = [
            [InlineKeyboardButton("🔁 Щодня", callback_data="repeat_daily")],
            [InlineKeyboardButton("1️⃣ Один раз", callback_data="repeat_once")],
            [InlineKeyboardButton("📅 По буднях", callback_data="repeat_weekdays")],
            [InlineKeyboardButton("🏖 По вихідних", callback_data="repeat_weekends")],
            [InlineKeyboardButton("↩ На початок", callback_data="main_menu")],
        ]
        context.user_data["step"] = None
        await update.message.reply_text("🔄 Як повторювати нагадування?", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # default: show main menu
    await start(update, context)

# handle repeat callbacks (after entering task and time)
async def repeat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # e.g., repeat_daily
    if data == "main_menu":
        await query.edit_message_text("📌 Головне меню:", reply_markup=main_menu_keyboard())
        return

    if not data.startswith("repeat_"):
        # route to normal button handler for other callbacks
        await button_handler(update, context)
        return

    repeat_type = data.split("_", 1)[1]  # 'daily' or 'once' etc.
    chat_id = query.message.chat_id

    chosen_time_str = context.user_data.get("chosen_time")
    chosen_task = context.user_data.get("chosen_task")
    chosen_time_obj = context.user_data.get("chosen_time_obj")  # datetime.time

    if not (chosen_time_str and chosen_task and chosen_time_obj):
        await query.edit_message_text("Щось пішло не так — спробуй ще раз.", reply_markup=main_menu_keyboard())
        context.user_data.clear()
        return

    # compute first run (use UTC times for scheduler; assume user gives local time server's timezone)
    now = datetime.utcnow()
    run_dt = datetime.combine(now.date(), chosen_time_obj)
    if run_dt < now:
        run_dt += timedelta(days=1)

    hours = chosen_time_obj.hour
    minutes = chosen_time_obj.minute

    # ensure unique job id
    base_job_id = f"{chat_id}_{chosen_time_str}_{abs(hash(chosen_task))}_{repeat_type}"
    job_id = base_job_id
    i = 1
    while scheduler.get_job(job_id) is not None:
        job_id = f"{base_job_id}_{i}"
        i += 1

    # job function that schedules the async send in the bot's loop
    def job_func_wrapper(bot, c_chat_id, c_text):
        # use create_task on the app to run async send_reminder
        # NOTE: we use context.application here via closure of outer handler's context
        try:
            context.application.create_task(send_reminder(bot, c_chat_id, c_text))
        except Exception:
            # in case context.application is not available in the closure, get by building from env:
            logger.exception("Failed to create task for reminder")

    # create partial with the correct args (bot will be passed at runtime? scheduler doesn't pass bot,
    # so we capture the application.bot now)
    bot = context.bot
    job_callable = partial(job_func_wrapper, bot, chat_id, chosen_task)

    if repeat_type == "once":
        scheduler.add_job(job_callable, "date", run_date=run_dt, id=job_id)
    elif repeat_type == "daily":
        scheduler.add_job(job_callable, "cron", hour=hours, minute=minutes, id=job_id)
    elif repeat_type == "weekdays":
        scheduler.add_job(job_callable, "cron", day_of_week="mon-fri", hour=hours, minute=minutes, id=job_id)
    elif repeat_type == "weekends":
        scheduler.add_job(job_callable, "cron", day_of_week="sat,sun", hour=hours, minute=minutes, id=job_id)
    else:
        await query.edit_message_text("Невідомий тип повтору.", reply_markup=main_menu_keyboard())
        context.user_data.clear()
        return

    # store reminder
    reminders.setdefault(chat_id, []).append({
        "job_id": job_id,
        "text": chosen_task,
        "time_str": chosen_time_str,
        "repeat": repeat_type
    })

    context.user_data.clear()
    await query.edit_message_text(f"✅ Нагадування встановлено: {chosen_time_str} — {chosen_task} ({repeat_type})", reply_markup=main_menu_keyboard())

# allow deletion UI: show list with delete buttons
async def delete_list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    user_reminders = reminders.get(chat_id, [])
    if not user_reminders:
        await query.edit_message_text("У тебе немає нагадувань.", reply_markup=back_keyboard())
        return

    keyboard = []
    for idx, r in enumerate(user_reminders):
        next_run = get_job_next_run(r["job_id"])
        if next_run:
            delta = next_run - datetime.utcnow()
            time_left = format_timedelta(delta)
        else:
            time_left = "—"
        txt = f"{r['time_str']} — {r['text']} ({r['repeat']})\nзалишилось: {time_left}"
        keyboard.append([InlineKeyboardButton(txt, callback_data=f"delete_{idx}")])
    keyboard.append([InlineKeyboardButton("↩ На початок", callback_data="main_menu")])
    await query.edit_message_text("Вибери нагадування, щоб видалити:", reply_markup=InlineKeyboardMarkup(keyboard))

# unknown text / fallback
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Не розумію. Використай меню.", reply_markup=main_menu_keyboard())

# ------------- main -------------
def build_application():
    app = ApplicationBuilder().token(TOKEN).build()

    # command /start shows main menu
    app.add_handler(CommandHandler("start", start))

    # callback handlers
    app.add_handler(CallbackQueryHandler(repeat_handler, pattern=r"^repeat_"))
    app.add_handler(CallbackQueryHandler(delete_list_handler, pattern="^delete_list$"))
    app.add_handler(CallbackQueryHandler(button_handler, pattern="^(set_reminder|list_reminders|main_menu|delete_\d+|noop_\d+)$"))
    app.add_handler(CallbackQueryHandler(button_handler))  # catch-all for other buttons (delete_ handled inside)

    # text handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # fallback
    app.add_handler(MessageHandler(filters.ALL, unknown))

    return app

def run_app():
    app = build_application()

    # Prepare webhook URL
    render_url = RENDER_EXTERNAL_URL
    if render_url:
        if not render_url.startswith("https://") and not render_url.startswith("http://"):
            render_url = "https://" + render_url
        webhook_url = f"{render_url}/{TOKEN}"
    else:
        webhook_url = f"https://{os.getenv('HOST', 'localhost')}:{PORT}/{TOKEN}"
        logger.warning("Using fallback webhook URL: %s", webhook_url)

    # start webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TOKEN,
        webhook_url=webhook_url,
    )

if __name__ == "__main__":
    run_app()
