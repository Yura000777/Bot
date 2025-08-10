import os
import json
import logging
from datetime import datetime, date, time as dtime, timedelta
from functools import partial
from typing import Dict, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------- logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------- env ----------------
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set in environment variables.")

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")  # e.g. myservice.onrender.com
PORT = int(os.getenv("PORT", "5000"))

# ---------------- persistence ----------------
REMINDERS_FILE = "reminders.json"
# reminders structure in json:
# {
#   "<chat_id>": [
#       {
#           "id": "<job_id>",
#           "time_str": "HH:MM",
#           "text": "...",
#           "repeat": "once"|"daily"|"weekdays"|"weekends",
#           "next_run_ts": 169xxx (optional, for info)
#       }, ...
#   ],
#   ...
# }

def load_reminders() -> Dict[str, Any]:
    if not os.path.exists(REMINDERS_FILE):
        return {}
    try:
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.exception("Failed to load reminders.json: %s", e)
        return {}

def save_reminders(data: Dict[str, Any]):
    try:
        with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.exception("Failed to write reminders.json")

# ---------------- runtime job mapping ----------------
# will store job objects: JOBS[job_id] = Job instance
JOBS: Dict[str, Any] = {}

# ---------------- helpers: keyboards & formatting ----------------
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("➕ Встановити нагадування", callback_data="set_reminder")],
        [InlineKeyboardButton("📋 Список нагадувань", callback_data="list_reminders")],
        [InlineKeyboardButton("❌ Видалити нагадування", callback_data="delete_list")],
    ]
    return InlineKeyboardMarkup(keyboard)

def back_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩ На початок", callback_data="main_menu")]])

def format_timedelta(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "0 хв"
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} дн")
    if hours:
        parts.append(f"{hours} год")
    if minutes:
        parts.append(f"{minutes} хв")
    return " ".join(parts) if parts else "менше 1 хв"

def make_job_id(chat_id: int, time_str: str, text: str, repeat: str) -> str:
    # stable-ish id
    base = f"{chat_id}_{time_str}_{abs(hash(text))}_{repeat}"
    # ensure uniqueness by adding suffix if exists in loaded reminders
    return base

# ---------------- the actual send job callback ----------------
async def job_send(context: ContextTypes.DEFAULT_TYPE):
    """
    Called by job_queue. context.job.data contains a dict with:
    { "chat_id": int, "text": str, "id": job_id, "repeat": "once|daily|..."}
    """
    try:
        job_data = context.job.data
        chat_id = job_data["chat_id"]
        text = job_data["text"]
        job_id = job_data.get("id")
        # Before sending, verify that reminder still exists in reminders.json (not deleted)
        reminders_all = load_reminders()
        user_list = reminders_all.get(str(chat_id), [])
        still_exists = any(r.get("id") == job_id for r in user_list)
        if not still_exists:
            # nothing to do (deleted)
            logger.info("Job %s fired but was deleted from storage; skipping", job_id)
            return
        await context.bot.send_message(chat_id=chat_id, text=f"⏰ Нагадування: {text}")
    except Exception:
        logger.exception("Exception in job_send")

# ---------------- schedule helpers ----------------
def seconds_until_next(time_obj: dtime) -> float:
    """Return seconds from now (UTC) until the next occurrence of time_obj (treating today if later)."""
    now = datetime.utcnow()
    target = datetime.combine(now.date(), time_obj)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()

def schedule_reminder(app, chat_id: int, time_str: str, text: str, repeat: str):
    """
    Create a job in app.job_queue and store mapping in JOBS.
    repeat: once, daily, weekdays, weekends
    Returns job_id
    """
    hh, mm = map(int, time_str.split(":"))
    time_obj = dtime(hour=hh, minute=mm)
    job_id = make_job_id(chat_id, time_str, text, repeat)
    # ensure uniqueness
    i = 1
    existing = app.bot_data.get("job_ids", set())
    while job_id in JOBS or any(job_id == r.get("id") for r in load_reminders().get(str(chat_id), [])):
        job_id = f"{job_id}_{i}"; i += 1

    data = {"chat_id": chat_id, "text": text, "id": job_id, "repeat": repeat}

    # compute first run seconds
    first_seconds = seconds_until_next(time_obj)

    # choose scheduling strategy
    if repeat == "once":
        job = app.job_queue.run_once(job_send, when=first_seconds, data=data, name=job_id)
    else:
        # For repeating, schedule repeating with interval=86400 and decide inside job_send whether to send (for weekdays/weekends)
        def _filter_and_send(context):
            # wrapper for sync->async; but we will not use wrapper; instead we schedule job_send which checks storage and then
            # for weekdays/weekends we decide here synchronously whether to create the async task
            jd = context.job.data
            rtype = jd.get("repeat")
            # check weekday/weekend conditions before creating async task
            if rtype == "weekdays":
                if datetime.utcnow().weekday() >= 5:  # 5,6 -> sat,sun
                    return
            if rtype == "weekends":
                if datetime.utcnow().weekday() < 5:
                    return
            # schedule actual async send via app.create_task
            try:
                app.create_task(job_send(context))
            except Exception:
                logger.exception("Failed to create task from repeating wrapper")

        # PTB JobQueue supports run_repeating; use interval=86400 (1 day)
        job = app.job_queue.run_repeating(_filter_and_send, interval=86400, first=first_seconds, name=job_id, data=data)

    JOBS[job_id] = job
    return job_id

def cancel_job(job_id: str):
    job = JOBS.get(job_id)
    if job:
        try:
            job.schedule_removal()
        except Exception:
            try:
                job.remove()
            except Exception:
                logger.exception("Failed to remove job %s", job_id)
        JOBS.pop(job_id, None)
    else:
        # no in-memory job (maybe restarted) — we try to find in job_queue by name
        # best-effort: iterate jobs in job_queue if available
        logger.info("No in-memory job for %s", job_id)

# ---------------- handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("📌 Головне меню:", reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text("📌 Головне меню:", reply_markup=main_menu_keyboard())

# callback buttons main handler
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if data == "main_menu":
        await query.edit_message_text("📌 Головне меню:", reply_markup=main_menu_keyboard())
        return

    if data == "set_reminder":
        await query.edit_message_text("⏰ Введи час у форматі HH:MM (наприклад 14:30):", reply_markup=back_keyboard())
        context.user_data["step"] = "waiting_for_time"
        return

    if data == "list_reminders":
        reminders_all = load_reminders()
        user_rem = reminders_all.get(str(chat_id), [])
        if not user_rem:
            await query.edit_message_text("📋 У тебе немає нагадувань.", reply_markup=back_keyboard())
            return
        keyboard = []
        for idx, r in enumerate(user_rem):
            job_id = r["id"]
            # find next run: if JOBS has job -> next_run from job, else unknown
            job = JOBS.get(job_id)
            if job and getattr(job, "next_run_time", None):
                next_run = job.next_run_time
            else:
                next_run = None
            time_left = format_timedelta(next_run - datetime.utcnow()) if next_run else "—"
            btn_text = f"{r['time_str']} — {r['text']} ({r['repeat']})\nзалишилось: {time_left}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"noop_{idx}")])
        keyboard.append([InlineKeyboardButton("↩ На початок", callback_data="main_menu")])
        await query.edit_message_text("📋 Твої нагадування:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "delete_list":
        # show delete options
        reminders_all = load_reminders()
        user_rem = reminders_all.get(str(chat_id), [])
        if not user_rem:
            await query.edit_message_text("📋 У тебе немає нагадувань.", reply_markup=back_keyboard())
            return
        keyboard = []
        for idx, r in enumerate(user_rem):
            job_id = r["id"]
            job = JOBS.get(job_id)
            if job and getattr(job, "next_run_time", None):
                next_run = job.next_run_time
                time_left = format_timedelta(next_run - datetime.utcnow())
            else:
                time_left = "—"
            txt = f"{r['time_str']} — {r['text']} ({r['repeat']})\nзалишилось: {time_left}"
            keyboard.append([InlineKeyboardButton(txt, callback_data=f"delete_{idx}")])
        keyboard.append([InlineKeyboardButton("↩ На початок", callback_data="main_menu")])
        await query.edit_message_text("Вибери нагадування, щоб видалити:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("delete_"):
        try:
            idx = int(data.split("_", 1)[1])
        except Exception:
            await query.edit_message_text("Невірний індекс.", reply_markup=back_keyboard())
            return
        reminders_all = load_reminders()
        user_rem = reminders_all.get(str(chat_id), [])
        if 0 <= idx < len(user_rem):
            job_id = user_rem[idx]["id"]
            # cancel job in-memory
            cancel_job(job_id)
            # remove from storage and save
            user_rem.pop(idx)
            reminders_all[str(chat_id)] = user_rem
            save_reminders(reminders_all)
            await query.edit_message_text("✅ Видалено.", reply_markup=main_menu_keyboard())
        else:
            await query.edit_message_text("Нічого не знайдено.", reply_markup=back_keyboard())
        return

    # noop callbacks (just informational buttons)
    if data.startswith("noop_"):
        await query.answer()
        return

# text handler for time and task
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    step = context.user_data.get("step")

    if step == "waiting_for_time":
        txt = update.message.text.strip()
        try:
            hh_mm = datetime.strptime(txt, "%H:%M").time()
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
        # ask repeat
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

    # default fallback
    await start(update, context)

# repeat handler (buttons like repeat_daily)
async def repeat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "main_menu":
        await query.edit_message_text("📌 Головне меню:", reply_markup=main_menu_keyboard())
        context.user_data.clear()
        return

    if not data.startswith("repeat_"):
        await button_handler(update, context)
        return

    repeat_type = data.split("_", 1)[1]  # daily, once, weekdays, weekends
    chat_id = query.message.chat_id
    chosen_time = context.user_data.get("chosen_time")
    chosen_task = context.user_data.get("chosen_task")
    chosen_time_obj = context.user_data.get("chosen_time_obj")
    if not (chosen_time and chosen_task and chosen_time_obj):
        await query.edit_message_text("Щось пішло не так — спробуй ще.", reply_markup=main_menu_keyboard())
        context.user_data.clear()
        return

    # schedule job via job_queue
    app = context.application
    job_id = schedule_reminder(app, chat_id, chosen_time, chosen_task, repeat_type)

    # save to JSON
    reminders_all = load_reminders()
    reminders_all.setdefault(str(chat_id), []).append({
        "id": job_id,
        "time_str": chosen_time,
        "text": chosen_task,
        "repeat": repeat_type
    })
    save_reminders(reminders_all)

    context.user_data.clear()
    await query.edit_message_text(f"✅ Нагадування встановлено: {chosen_time} — {chosen_task} ({repeat_type})", reply_markup=main_menu_keyboard())

# fallback unknown
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Не розумію. Використай меню.", reply_markup=main_menu_keyboard())

# ---------------- restore stored reminders on startup ----------------
def restore_reminders(app):
    reminders_all = load_reminders()
    for chat_id_str, items in reminders_all.items():
        chat_id = int(chat_id_str)
        for r in items:
            # schedule only if not already scheduled
            jid = r.get("id")
            if jid in JOBS:
                continue
            try:
                schedule_reminder(app, chat_id, r["time_str"], r["text"], r["repeat"])
                logger.info("Restored reminder %s for chat %s", r.get("id"), chat_id)
            except Exception:
                logger.exception("Failed to restore reminder %s", r)

# ---------------- application building & run ----------------
def build_app():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    # repeat buttons (repeat_daily etc.)
    app.add_handler(CallbackQueryHandler(repeat_handler, pattern=r"^repeat_"))
    # main menu and other buttons (set_reminder, list_reminders, delete_list, delete_IDX, noop)
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(set_reminder|list_reminders|main_menu|delete_list|delete_\d+|noop_\d+)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(MessageHandler(filters.ALL, unknown))
    return app

def run():
    app = build_app()
    # restore persisted reminders into job_queue
    restore_reminders(app)

    # prepare webhook url
    if RENDER_EXTERNAL_URL:
        render_url = RENDER_EXTERNAL_URL
        if not render_url.startswith("http"):
            render_url = "https://" + render_url
        webhook_url = f"{render_url}/{TOKEN}"
    else:
        logger.warning("RENDER_EXTERNAL_URL not set; building local webhook url")
        webhook_url = f"https://{os.getenv('HOST', 'localhost')}:{PORT}/{TOKEN}"

    logger.info("Starting webhook with URL %s", webhook_url)
    app.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN, webhook_url=webhook_url)

if __name__ == "__main__":
    run()
