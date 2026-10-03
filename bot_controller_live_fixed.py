#!/usr/bin/env python3
import os
import re
import sys
import json
import time
import signal
import threading
import subprocess
from pathlib import Path
from collections import defaultdict

import pexpect
import telebot
from telebot import types
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
GEN = BASE / "gen.py"
load_dotenv(BASE / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "8808818824:AAHv8flnCrxc-2pEt3sV_cBR4kJiBnmp-0M").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "6625618149").split(",") if x.strip().isdigit()}

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is missing. Put it in .env")
if not ADMIN_IDS:
    raise SystemExit("ADMIN_IDS is missing. Put it in .env")

GROUPS = {
    "NARUTO_BUNDLE": -1004358509203,
    "SASUKE_BUNDLE": -1004319048684,
    "NINJA_RUN_EMOTE": -1003938081706,
    "WRATH_NINE_TAILS": -1003822038058,
    "DARK_DESIRE_BUNDLE": -1003807114730,
    "ACCOUNT_GENERATED": -1004332193121,
    "ACCOUNT_ACTIVATED": -1003753147637,
    "LIVE_LOGS": -1003979718864,
    "ACTIVATED_JSON": -1004455475932,
}
EVENTS = [k for k in GROUPS if k not in {"ACCOUNT_GENERATED", "ACCOUNT_ACTIVATED", "LIVE_LOGS"}]

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)

ansi_re = re.compile(r"\x1b(?:[@-_][0-?]*[ -/]*[@-~])")
secret_re = re.compile(r"(?i)(password|passwd|token|authorization)\s*[:=]\s*([^\s]+)")
ip_re = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d+\b")

state = {
    "child": None,
    "thread": None,
    "lock": threading.RLock(),
    "running": False,
    "awaiting": False,
    "last_prompt": "",
    "buffer": "",
    "started_at": None,
    "target": None,
    "answers": [],
    "last_error": "-",
    "log_queue": [],
    "last_sent": {},
    "batch_sent": defaultdict(int),
    "counts": {},
    "watch_offsets": {},
    "batch_bases": {},
    "activation_seen_slots": set(),
    "pending_activation_slots": set(),
    "activation_live_records": [],
}


def clean(text: str) -> str:
    text = ansi_re.sub("", text or "")
    text = ip_re.sub("[IP masked]", text)
    return text.replace("\x00", "")


def html(text: str) -> str:
    from html import escape
    return escape(clean(text))


def authorized(message) -> bool:
    return message.from_user and message.from_user.id in ADMIN_IDS


def control_markup():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("▶️ START", callback_data="start"),
        types.InlineKeyboardButton("📊 LIVE STATS", callback_data="stats"),
        types.InlineKeyboardButton("🛑 STOP", callback_data="stop"),
        types.InlineKeyboardButton("📝 LOG STATUS", callback_data="logs"),
        types.InlineKeyboardButton("📁 FILES", callback_data="files"),
        types.InlineKeyboardButton("🔄 RESTART", callback_data="restart"),
    )
    return kb


def log_group(msg: str):
    msg = clean(msg).strip()
    if not msg:
        return
    try:
        bot.send_message(GROUPS["LIVE_LOGS"], f"<pre>{html(msg[-3900:])}</pre>")
    except Exception:
        pass


def queue_log(msg: str):
    with state["lock"]:
        state["log_queue"].append(clean(msg))
        if len(state["log_queue"]) > 80:
            state["log_queue"] = state["log_queue"][-80:]


def stats_snapshot():
    counts = {}
    for name, path in file_paths().items():
        counts[name] = json_count(path)
    generated = counts.get("ACCOUNT_GENERATED", 0)
    activated = counts.get("ACCOUNT_ACTIVATED", 0)
    event_counts = {e: counts.get(e, 0) for e in EVENTS}
    target = state.get("target")
    failed = max(0, target - generated) if isinstance(target, int) else 0
    pending = max(0, generated - activated)
    return {
        "generated": generated,
        "activated": activated,
        "failed": failed,
        "pending": pending,
        "events": event_counts,
        "target": target,
        "running": state["running"],
        "uptime": int(time.time() - state["started_at"]) if state["started_at"] else 0,
        "last_error": state.get("last_error", "-"),
    }


def json_count(path: Path) -> int:
    try:
        if not path.exists():
            return 0
        data = json.loads(path.read_text(encoding="utf-8"))
        return len(data) if isinstance(data, list) else 0
    except Exception:
        return 0



def read_json_list(path: Path):
    """Read a JSON array safely. Returns [] on a partial/invalid write."""
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def mask_ip_text(value) -> str:
    """Hide IPv4:port values from activation messages."""
    return ip_re.sub("[IP masked]", str(value or ""))


def code(value) -> str:
    return f"<code>{html(str(value if value is not None else '-'))}</code>"


def send_generated_record(rec: dict):
    msg = (
        "👤 <b>ACCOUNT GENERATED</b>\n\n"
        f"🆔 UID: {code(rec.get('uid'))}\n"
        f"🔑 Password: {code(rec.get('password'))}\n\n"
        f"🆔 Account ID: {code(rec.get('account_id'))}\n"
        f"👤 Name: {code(rec.get('name'))}\n"
        f"🌍 Region: {code(rec.get('region'))}\n"
        f"📅 Created: {code(rec.get('date_created'))}"
    )
    try:
        bot.send_message(GROUPS["ACCOUNT_GENERATED"], msg)
    except Exception as e:
        state["last_error"] = f"Generated send: {e}"


def send_activated_record(rec: dict):
    msg = (
        "🔐 <b>ACCOUNT ACTIVATED</b>\n\n"
        f"🆔 UID: {code(rec.get('uid'))}\n"
        f"🔑 Password: {code(rec.get('password'))}\n\n"
        f"🆔 Account ID: {code(rec.get('account_id'))}\n"
        f"👤 Name: {code(rec.get('name'))}\n"
        f"🌍 Region: {code(rec.get('region'))}\n"
        f"📌 Status: {code(rec.get('activation_status') or 'SUCCESS')}\n"
        f"💬 Message: {code(mask_ip_text(rec.get('activation_message')))}\n"
        f"⏰ Activated: {code(rec.get('activated_at') or rec.get('date_created'))}"
    )
    try:
        bot.send_message(GROUPS["ACCOUNT_ACTIVATED"], msg)
        # Full-auto does not always write activated_accounts.json, so keep
        # a run-local copy for the 50-record activation batch.
        with state["lock"]:
            state["activation_live_records"].append(dict(rec))
            n = len(state["activation_live_records"])
        # Send every 5 activated accounts as a JSON file
        # to the dedicated ACTIVATED_JSON group.
        if n % 5 == 0:
            batch_no = n // 5
            send_activated_json_batch(
                state["activation_live_records"][n - 5:n],
                batch_no,
            )
    except Exception as e:
        state["last_error"] = f"Activated send: {e}"


def send_activated_json_batch(batch: list, batch_no: int):
    """Send every 5 activated account records as a JSON document."""
    if not batch:
        return

    out = BASE / f"activated_accounts_batch_{batch_no:04d}.json"
    try:
        out.write_text(
            json.dumps(batch, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        bot.send_document(
            GROUPS["ACTIVATED_JSON"],
            types.InputFile(str(out)),
            caption=(
                "📦 <b>ACTIVATED ACCOUNTS JSON</b>\n"
                f"Batch: <code>{batch_no}</code>\n"
                f"Records: <code>{len(batch)}</code>"
            ),
        )
        state["batch_sent"]["ACTIVATED_JSON"] = batch_no
    except Exception as e:
        state["last_error"] = f"Activated JSON batch: {e}"


def send_spin_record(rec: dict):
    event = str(rec.get("event") or "").upper()
    if event not in EVENTS:
        return
    msg = (
        "🎰 <b>SPIN RESULT</b>\n\n"
        f"⏰ Time: {code(rec.get('timestamp'))}\n\n"
        f"👤 UID: {code(rec.get('guestUid'))}\n"
        f"🔑 Password: {code(rec.get('guestPass'))}\n\n"
        f"🌍 Region: {code(rec.get('region'))}\n"
        f"🎯 Event: {code(event)}\n\n"
        f"🆔 Item ID: {code(rec.get('item_id'))}\n"
        f"💎 Item: {code(rec.get('item_name'))}\n\n"
        f"⚙️ Method: {code(rec.get('method'))}"
    )
    try:
        bot.send_message(GROUPS[event], msg)
    except Exception as e:
        state["last_error"] = f"Spin send {event}: {e}"


def initialize_output_watch():
    """Set the current file lengths as the baseline for this run."""
    with state["lock"]:
        paths = file_paths()
        state["watch_offsets"] = {name: json_count(path) for name, path in paths.items()}
        state["batch_bases"] = dict(state["watch_offsets"])
        state["activation_seen_slots"] = set()
        state["pending_activation_slots"] = set()
        state["activation_live_records"] = []
        state["batch_sent"] = defaultdict(int)


def send_batch_file(category: str, batch: list, batch_no: int):
    if not batch:
        return
    out = BASE / f"{category.lower()}_batch_{batch_no:04d}.json"
    try:
        out.write_text(
            json.dumps(batch, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        bot.send_document(
            GROUPS[category],
            types.InputFile(str(out)),
            caption=(
                f"📦 <b>{category}</b>\n"
                f"Batch: <code>{batch_no}</code>\n"
                f"Records: <code>{len(batch)}</code>"
            ),
        )
        state["batch_sent"][category] = batch_no
    except Exception as e:
        state["last_error"] = f"Batch {category}: {e}"


def try_send_activation_slot(slot: int):
    """Resolve a terminal activation slot to the generated account record."""
    if slot in state["activation_seen_slots"]:
        return True

    accounts = read_json_list(file_paths()["ACCOUNT_GENERATED"])
    if slot < 1 or slot > len(accounts):
        state["pending_activation_slots"].add(slot)
        return False

    rec = dict(accounts[slot - 1])
    if not rec:
        return False

    # The terminal reports activation separately; enrich the saved account.
    rec["activated"] = True
    rec.setdefault("activation_status", "SUCCESS")
    rec.setdefault("activation_message", "Activated")
    rec.setdefault("activated_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    send_activated_record(rec)
    state["activation_seen_slots"].add(slot)
    state["pending_activation_slots"].discard(slot)
    return True


def detect_activation_slots(text: str):
    """Detect [slot/total] activation lines from gen.py output."""
    cleaned = clean(text)
    patterns = [
        r"\[(\d+)\s*/\s*\d+\]\s*.*?Activated(?:\s*→|$)",
        r"\[(\d+)\s*/\s*\d+\]\s*.*?Already activated",
        r"\[(\d+)\s*/\s*\d+\]\s*.*?ACCOUNT\s+.*?ACTIVATED",
        # ACTIVATE ONLY logs: [slot/total] ✅ <uid>
        r"\[(\d+)\s*/\s*\d+\]\s*.*?✅\s*\d+",
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, cleaned, re.I):
            try_send_activation_slot(int(m.group(1)))


def monitor_outputs():
    """Route new JSON records live and send every 50 new records as a batch."""
    while True:
        time.sleep(0.7)
        paths = file_paths()
        try:
            for category, path in paths.items():
                data = read_json_list(path)
                old = state["watch_offsets"].get(category, len(data))
                if len(data) < old:
                    old = 0
                    state["batch_bases"][category] = 0

                new_records = data[old:]
                if new_records:
                    if category == "ACCOUNT_GENERATED":
                        for rec in new_records:
                            send_generated_record(rec)
                    elif category == "ACCOUNT_ACTIVATED":
                        # Individual activation delivery is driven by the live
                        # activation log above; do not duplicate it from this file.
                        pass
                    elif category in EVENTS:
                        for rec in new_records:
                            send_spin_record(rec)

                    state["watch_offsets"][category] = len(data)

                # 50-record batches are measured from the start of this run.
                # Activation batches are handled by send_activated_record(),
                # because FULL AUTO may not append activated_accounts.json.
                if category == "ACCOUNT_ACTIVATED":
                    continue
                base = state["batch_bases"].get(category, len(data))
                sent_before = state["batch_sent"].get(category, 0)
                available = len(data) - base
                next_batch = sent_before + 1
                while available >= next_batch * 50:
                    start = base + (next_batch - 1) * 50
                    batch = data[start:start + 50]
                    send_batch_file(category, batch, next_batch)
                    next_batch += 1

            # Retry activation records whose account file had not flushed yet.
            for slot in list(state["pending_activation_slots"]):
                try_send_activation_slot(slot)

        except Exception as e:
            state["last_error"] = f"Output monitor: {e}"


def file_paths():
    result = {
        "ACCOUNT_GENERATED": BASE / "accounts.json",
        "ACCOUNT_ACTIVATED": BASE / "activated_accounts.json",
    }
    for e in EVENTS:
        safe_event_name = re.sub(r"[^a-zA-Z0-9_-]", "_", e)
        result[e] = BASE / "events" / f"{safe_event_name}.json"
    return result


def send_stats(chat_id: int):
    s = stats_snapshot()
    def dur(sec):
        h, r = divmod(sec, 3600); m, sec = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{sec:02d}"
    lines = [
        "📊 <b>ANSH LIVE STATISTICS</b>",
        "",
        f"🟢 Status: <code>{'RUNNING' if s['running'] else 'STOPPED'}</code>",
        f"⏱️ Uptime: <code>{dur(s['uptime'])}</code>",
        "",
        "👤 <b>ACCOUNTS</b>",
        f"├ Generated: <code>{s['generated']}</code>",
        f"├ Failed: <code>{s['failed']}</code>",
        f"└ Pending activation: <code>{s['pending']}</code>",
        "",
        "🔐 <b>ACTIVATION</b>",
        f"└ Activated: <code>{s['activated']}</code>",
        "",
        "🎰 <b>EVENT RESULTS</b>",
    ]
    for e in EVENTS:
        lines.append(f"├ {e}: <code>{s['events'][e]}</code>")
    if s["target"] is not None:
        lines += ["", f"🎯 Target: <code>{s['target']}</code>"]
    lines += ["", f"📝 Last error: <code>{html(s['last_error'][:300])}</code>"]
    bot.send_message(chat_id, "\n".join(lines), reply_markup=control_markup())


def detect_target(prompt: str, answer: str):
    p = clean(prompt).upper()
    if "HOW MANY ACCOUNTS" not in p:
        return
    presets = {"1": 10, "2": 100, "3": 1000, "4": 10000, "5": 100000, "6": 1000000}
    if answer in presets:
        state["target"] = presets[answer]
    elif answer.isdigit():
        state["target"] = int(answer)


def is_prompt(text: str) -> bool:
    t = clean(text)
    return ("└──╼❯" in t or "▶ Server" in t or "Press Enter" in t or
            re.search(r"Choice\s*\[", t, re.I) is not None)


def start_process(admin_chat_id: int):
    with state["lock"]:
        if state["running"]:
            bot.send_message(admin_chat_id, "⚠️ Process already running.", reply_markup=control_markup())
            return
        if not GEN.exists():
            bot.send_message(admin_chat_id, "❌ gen.py not found.")
            return
        state["running"] = True
        state["awaiting"] = False
        state["last_prompt"] = ""
        state["buffer"] = ""
        state["answers"] = []
        state["target"] = None
        state["last_error"] = "-"
        state["started_at"] = time.time()
        initialize_output_watch()
        try:
            child = pexpect.spawn(sys.executable, [str(GEN)], cwd=str(BASE), encoding="utf-8", timeout=None)
            child.delaybeforesend = 0.05
            state["child"] = child
            th = threading.Thread(target=reader_loop, args=(admin_chat_id,), daemon=True)
            state["thread"] = th
            th.start()
        except Exception as e:
            state["running"] = False
            state["last_error"] = str(e)
            bot.send_message(admin_chat_id, f"❌ Start failed: <code>{html(str(e))}</code>")
            return
    bot.send_message(admin_chat_id, "🚀 <b>Process started.</b> Telegram input flow is now active.", reply_markup=control_markup())
    log_group("[BOT] Process started")


def reader_loop(admin_chat_id: int):
    child = state["child"]
    buf = ""
    last_log = time.time()
    log_buf = []
    try:
        while True:
            try:
                data = child.read_nonblocking(size=4096, timeout=0.2)
            except pexpect.TIMEOUT:
                data = ""
            except pexpect.EOF:
                break
            except Exception as e:
                state["last_error"] = str(e)
                break
            if data:
                buf += data
                log_buf.append(data)
                if len("".join(log_buf)) >= 2500 or time.time() - last_log >= 1.5:
                    chunk = clean("".join(log_buf)).strip()
                    if chunk:
                        queue_log(chunk)
                        log_group(chunk)
                        detect_activation_slots(chunk)
                    log_buf.clear(); last_log = time.time()
                if is_prompt(buf):
                    prompt = clean(buf).strip()
                    state["last_prompt"] = prompt[-6000:]
                    state["awaiting"] = True
                    state["buffer"] = ""
                    try:
                        bot.send_message(admin_chat_id, f"🤖 <b>INPUT REQUIRED</b>\n\n<pre>{html(prompt[-6500:])}</pre>\n\n✏️ Reply with your answer.", reply_markup=control_markup())
                    except Exception:
                        pass
                    buf = ""
            if state["child"] is None:
                break
        if log_buf:
            log_group("".join(log_buf))
    finally:
        with state["lock"]:
            state["running"] = False
            state["awaiting"] = False
            state["child"] = None
        try:
            bot.send_message(admin_chat_id, "🏁 <b>Process finished.</b>", reply_markup=control_markup())
        except Exception:
            pass
        log_group("[BOT] Process finished")


def send_answer(text: str, chat_id: int):
    with state["lock"]:
        child = state["child"]
        if not state["running"] or child is None or not state["awaiting"]:
            bot.send_message(chat_id, "⚠️ Bot is not waiting for input. Use /start or check LIVE STATS.", reply_markup=control_markup())
            return
        prompt = state["last_prompt"]
        detect_target(prompt, text.strip())
        state["answers"].append(text.strip())
        state["awaiting"] = False
    try:
        child.sendline(text.strip())
        log_group(f"[INPUT] {text.strip()}")
    except Exception as e:
        state["last_error"] = str(e)
        bot.send_message(chat_id, f"❌ Input failed: <code>{html(str(e))}</code>")


def stop_process(reason="manual stop"):
    with state["lock"]:
        child = state.get("child")
        state["awaiting"] = False
    if child:
        try:
            child.sendcontrol("c")
            time.sleep(0.5)
            if child.isalive():
                child.terminate(force=True)
        except Exception:
            pass
    log_group(f"[BOT] Process stopped: {reason}")


def batch_monitor():
    # Compatibility placeholder. monitor_outputs() handles live routing
    # and 50-record batch delivery.
    while True:
        time.sleep(60)


def stats_monitor():
    while True:
        time.sleep(5)
        # No spam to admin; statistics are pulled on demand.


@bot.message_handler(commands=["start", "help"])
def start_cmd(message):
    if not authorized(message):
        return
    bot.send_message(message.chat.id,
        "🤖 <b>ANSH CONTROL PANEL</b>\n\n"
        "Telegram se gen.py ka same terminal flow control hoga.\n"
        "Jo numbered option code mein aayega, wahi number reply karna hai.\n\n"
        "📊 LIVE STATS se current counters dekho.\n"
        "📝 LIVE_LOGS group mein sirf process logs jayenge.",
        reply_markup=control_markup())


@bot.message_handler(commands=["stats"])
def stats_cmd(message):
    if authorized(message):
        send_stats(message.chat.id)


@bot.message_handler(commands=["stop"])
def stop_cmd(message):
    if authorized(message):
        stop_process("/stop")
        bot.send_message(message.chat.id, "🛑 Stop signal sent.", reply_markup=control_markup())


@bot.message_handler(func=lambda m: authorized(m) and not (m.text or "").startswith("/"), content_types=["text"])
def text_handler(message):
    send_answer(message.text, message.chat.id)


@bot.callback_query_handler(func=lambda c: c.from_user and c.from_user.id in ADMIN_IDS)
def callback(call):
    bot.answer_callback_query(call.id)
    if call.data == "start":
        start_process(call.message.chat.id)
    elif call.data == "stats":
        send_stats(call.message.chat.id)
    elif call.data == "stop":
        stop_process("button")
        bot.send_message(call.message.chat.id, "🛑 Stop signal sent.", reply_markup=control_markup())
    elif call.data == "restart":
        stop_process("restart")
        time.sleep(1)
        start_process(call.message.chat.id)
    elif call.data == "logs":
        with state["lock"]:
            logs = "\n".join(state["log_queue"][-12:]) or "No live logs yet."
        bot.send_message(call.message.chat.id, f"📝 <b>RECENT LOGS</b>\n<pre>{html(logs[-6500:])}</pre>", reply_markup=control_markup())
    elif call.data == "files":
        rows = []
        for name, path in file_paths().items():
            rows.append(f"{name}: {path.name} ({json_count(path)})")
        bot.send_message(call.message.chat.id, "📁 <b>OUTPUT FILES</b>\n<pre>" + html("\n".join(rows)) + "</pre>", reply_markup=control_markup())


if __name__ == "__main__":
    threading.Thread(target=monitor_outputs, daemon=True).start()
    threading.Thread(target=batch_monitor, daemon=True).start()
    print("ANSH Telegram controller started", flush=True)
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
