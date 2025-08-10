import os
import json
from datetime import datetime, timedelta
import pytz
from typing import Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)

TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_URL = os.environ.get("RENDER_EXTERNAL_URL")  # just host; webhook builder uses it below

KYIV_TZ = pytz.timezone("Europe/Kyiv")
DATA_FILE = "reminders.json"

# in-memory: { chat_id: [ { "id": str, "task": str, "time": datetime(tz=KYIV_TZ), "repeat": str } , ... ] }
reminders: dict = {}

# ---------------- persistence ----------------
def save_reminders():
    out = {}
    for chat_id, lst in reminders.items():
        out[str(chat_id)] = [
            {"id": r.get("id"), "task": r["task"], "time": r["time"].astimezone(KYIV_TZ).isoformat(), "repeat": r["repeat"]}
            for r in lst
        ]
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

def load_reminders():
    global reminders
    reminders = {}
    if not os.path.exists(DATA_FILE):
        return
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    now = datetime.now(KYIV_TZ)
    for chat_id_str, lst in data.items():
        parsed = []
        for item in lst:
            # parse ISO datetime safely
            dt = datetime.fromisoformat(item["time"])
            if dt.tzinfo is None:
                dt = KYIV_TZ.localize(dt)
            else:
                dt = dt.astimezone(KYIV_TZ)
            repeat = item.get("repeat", "once")
            # if once and already passed -> skip
            if repeat == "once" and dt <= now:
                continue
            # if repeating and time already passed -> compute next occurrence
            if repeat != "once" and dt <= now:
                next_dt = find_next_time(now, dt.timetz(), repeat)
                if next_dt is None:
                    continue
                dt = next_dt
            parsed.append({"id": item.get("id"), "task": item["task"], "time": dt, "repeat": repeat})
        if parsed:
            reminders[int(chat_id_str)] = parsed

# ---------------- helpers ----------------
def format_time_delta(td: timedelta) -> str:
    if td.total_seconds() <= 0:
        return "0 хв"
    days = td.days
    hours, rem = divmod(td.seconds, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days} дн")
    if hours:
        parts.append(f"{hours} год")
    if minutes:
        parts.append(f"{minutes} хв")
    return " ".join(parts) if parts else "менше 1 хв"

def main_menu() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("➕ Додати нагадування", callback_data="set_reminder")],
        [InlineKeyboardButton("📋 Список нагадувань", callback_data="list_reminders")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def safe_edit_message_text(query, text, **kwargs):
    try:
        cur = query.message.text or ""
        if cur != text or ("reply_markup" in kwargs and query.message.reply_markup != kwargs.get("reply_markup")):
            await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

def find_next_time(start: datetime, time_of_day, repeat: str) -> Optional[datetime]:
    """
    start: aware datetime in KYIV_TZ
    time_of_day: a datetime.time object (can be from .time() or .timetz())
    repeat: 'daily'|'weekdays'|'weekends'|'once'
    Returns next datetime (aware KYIV_TZ) strictly > start that matches repeat.
    """
    for i in range(0, 14):  # search up to 2 weeks to be safe
        candidate_date = (start.date() + timedelta(days=i))
        naive = datetime.combine(candidate_date, time_of_day.replace(tzinfo=None) if hasattr(time_of_day, "tzinfo") else time_of_day)
        candidate = KYIV_TZ.localize(naive)
        if candidate <= start:
            continue
        dow = candidate.weekday()  # 0..6 Mon..Sun
        if repeat == "daily":
            return candidate
        if repeat == "weekdays" and dow < 5:
            return candidate
        if repeat == "weekends" and dow >= 5:
            return candidate
        if repeat == "once":
            return candidate
    return None

def make_job_id(chat_id: int, dt: datetime, task: str) -> str:
    # small unique-ish id
    return f"{chat_id}_{int(dt.timestamp())}_{abs(hash(task))%100000}"

# ---------------- scheduling ----------------
def schedule_reminder(job_queue, chat_id: int, remind_dt: datetime, task: str, repeat: str, reminder_obj: dict):
    """
    job_queue: app.job_queue or context.job_queue
    reminder_obj: dict object from reminders[chat_id] list (will be mutated to set 'id')
    """
    now = datetime.now(KYIV_TZ)
    delay = (remind_dt - now).total_seconds()
    if delay < 0:
        delay = 0.1
    job_id = make_job_id(chat_id, remind_dt, task)
    # store id into object
    reminder_obj["id"] = job_id
    # schedule with job data containing our job_id
    job_queue.run_once(job_send, delay, data={"chat_id": chat_id, "job_id": job_id})
    return job_id

# ---------------- handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Вітаю! Оберіть дію:", reply_markup=main_menu())

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    if query.data == "main_menu":
        await safe_edit_message_text(query, "Головне меню:", reply_markup=main_menu())
        return

    if query.data == "set_reminder":
        context.user_data["step"] = "waiting_for_task"
        await safe_edit_message_text(
            query,
            "Введіть текст нагадування:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Назад", callback_data="main_menu")]])
        )
        return

    if query.data == "list_reminders":
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
        return

    if query.data.startswith("delete_"):
        idx = int(query.data.split("_", 1)[1])
        if chat_id in reminders and 0 <= idx < len(reminders[chat_id]):
            rem = reminders[chat_id].pop(idx)
            save_reminders()
            await safe_edit_message_text(query, "Нагадування видалено.", reply_markup=main_menu())
        else:
            await safe_edit_message_text(query, "Нічого не знайдено.", reply_markup=main_menu())
        return

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    step = context.user_data.get("step")

    if step == "waiting_for_task":
        context.user_data["task"] = update.message.text
        context.user_data["step"] = "waiting_for_time"
        await update.message.reply_text(
            "Введіть час у форматі HH:MM (24-годинний, київський час):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Назад", callback_data="main_menu")]])
        )
        return

    if step == "waiting_for_time":
        text = update.message.text.strip()
        try:
            chosen_time = datetime.strptime(text, "%H:%M").time()
        except ValueError:
            await update.message.reply_text("Невірний формат. Використай HH:MM (наприклад 21:00).")
            return
        now = datetime.now(KYIV_TZ)
        # build candidate datetime for today at chosen_time
        naive = datetime.combine(now.date(), chosen_time)
        remind_dt = KYIV_TZ.localize(naive)
        # if already passed -> consider next day
        if remind_dt <= now:
            remind_dt += timedelta(days=1)
        context.user_data["time"] = remind_dt
        context.user_data["step"] = "waiting_for_repeat"

        keyboard = [
            [InlineKeyboardButton("Один раз", callback_data="repeat_once")],
            [InlineKeyboardButton("Будні", callback_data="repeat_weekdays")],
            [InlineKeyboardButton("Вихідні", callback_data="repeat_weekends")],
            [InlineKeyboardButton("Щодня", callback_data="repeat_daily")],
            [InlineKeyboardButton("⬅ Назад", callback_data="main_menu")]
        ]
        await update.message.reply_text("Оберіть тип повтору:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # fallback
    await update.message.reply_text("Використай меню.", reply_markup=main_menu())

async def repeat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    task = context.user_data.get("task")
    chosen_dt: datetime = context.user_data.get("time")
    repeat_type = query.data.replace("repeat_", "")

    if not task or not chosen_dt:
        await safe_edit_message_text(query, "Щось пішло не так. Почни заново.", reply_markup=main_menu())
        context.user_data.clear()
        return

    now = datetime.now(KYIV_TZ)

    if repeat_type == "once":
        scheduled_dt = chosen_dt  # we already made it > now in message_handler
    else:
        # find next matching datetime (could be today or later)
        scheduled_dt = find_next_time(now, chosen_dt.timetz(), repeat_type)
        if scheduled_dt is None:
            await safe_edit_message_text(query, "Не вдалося знайти підходящу дату.", reply_markup=main_menu())
            context.user_data.clear()
            return

    # create reminder object and persist
    rem = {"id": None, "task": task, "time": scheduled_dt, "repeat": repeat_type}
    reminders.setdefault(chat_id, []).append(rem)
    # schedule and set id
    job_id = schedule_reminder(context.job_queue, chat_id, scheduled_dt, task, repeat_type, rem)
    # persist id into file
    save_reminders()

    await safe_edit_message_text(query, "Нагадування створено ✅", reply_markup=main_menu())
    context.user_data.clear()

# ---------------- job callback ----------------
async def job_send(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data or {}
    chat_id = job_data.get("chat_id")
    job_id = job_data.get("job_id")

    if chat_id is None or job_id is None:
        return

    # find reminder
    user_list = reminders.get(chat_id, [])
    rem = next((r for r in user_list if r.get("id") == job_id), None)
    if rem is None:
        # maybe removed; nothing to do
        return

    # send notification
    try:
        await context.bot.send_message(chat_id, text=f"🔔 Нагадування: {rem['task']}")
    except Exception:
        # if sending fails, keep reminder as is
        pass

    # handle repeat logic
    if rem["repeat"] == "once":
        # remove it
        reminders[chat_id] = [r for r in user_list if r.get("id") != job_id]
        if not reminders[chat_id]:
            reminders.pop(chat_id, None)
        save_reminders()
        return

    # for repeating reminders: compute next occurrence strictly after now
    now = datetime.now(KYIV_TZ)
    next_dt = find_next_time(now, rem["time"].timetz(), rem["repeat"])
    if next_dt is None:
        # nothing to schedule
        return

    # update stored rem and save, then schedule next job
    rem["time"] = next_dt
    # clear old id; schedule_reminder will set new id
    rem["id"] = None
    save_reminders()
    schedule_reminder(context.job_queue, chat_id, next_dt, rem["task"], rem["repeat"], rem)
    save_reminders()

# ---------------- restore on start ----------------
def restore_jobs(app):
    now = datetime.now(KYIV_TZ)
    for chat_id, rem_list in list(reminders.items()):
        for rem in rem_list:
            # ensure rem['time'] is > now; if not, compute next for repeats or remove for once
            if rem["time"] <= now:
                if rem["repeat"] == "once":
                    # remove expired once
                    reminders[chat_id] = [r for r in rem_list if r is not rem]
                    continue
                next_dt = find_next_time(now, rem["time"].timetz(), rem["repeat"])
                if next_dt is None:
                    reminders[chat_id] = [r for r in rem_list if r is not rem]
                    continue
                rem["time"] = next_dt
            # schedule
            schedule_reminder(app.job_queue, chat_id, rem["time"], rem["task"], rem["repeat"], rem)
    save_reminders()

# ---------------- run ----------------
def run_app():
    load_reminders()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^(set_reminder|list_reminders|main_menu|delete_\d+)$"))
    app.add_handler(CallbackQueryHandler(repeat_handler, pattern=r"^repeat_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    restore_jobs(app)

    # build webhook_url: RENDER_EXTERNAL_URL should be like 'your-service.onrender.com'
    webhook_url = WEBHOOK_URL if WEBHOOK_URL and WEBHOOK_URL.startswith("http") else (f"https://{WEBHOOK_URL}" if WEBHOOK_URL else None)
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        url_path=TOKEN,  # use token path
        webhook_url=f"{webhook_url}/{TOKEN}" if webhook_url else None
    )

if __name__ == "__main__":
    run_app()
