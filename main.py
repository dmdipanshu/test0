import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage

# ───────────────────────── Logging ─────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("subbot")

# ───────────────────────── Config from ENV ─────────────────────────
API_TOKEN = os.getenv("API_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-100123456789"))
UPI_ID = os.getenv("UPI_ID", "yourupi@upi")
QR_CODE_URL = os.getenv("QR_CODE_URL", "https://example.com/qr.png")

if not API_TOKEN:
    raise RuntimeError("API_TOKEN is required in environment variables.")

bot = Bot(API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ───────────────────────── Plans ─────────────────────────
PLANS = {
    "plan1": {"name": "1 Month",  "price": "₹99",   "days": 30},
    "plan2": {"name": "6 Months", "price": "₹199",  "days": 180},
    "plan3": {"name": "1 Year",   "price": "₹1999", "days": 365},
    "plan4": {"name": "Lifetime", "price": "₹2999", "days": 36500},
}
last_selected_plan: Dict[int, str] = {}

# ───────────────────────── SQLite Setup ─────────────────────────
DB = "subs.db"

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            plan_key TEXT,
            start_at TEXT,
            end_at TEXT,
            status TEXT,
            created_at TEXT,
            reminded_3d INTEGER DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan_key TEXT,
            file_id TEXT,
            created_at TEXT,
            status TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS tickets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message TEXT,
            status TEXT,
            created_at TEXT
        )""")
        c.commit()

# ───────────────────────── DB functions ─────────────────────────
def upsert_user(usr: types.User):
    with db() as c:
        now = datetime.now(timezone.utc).isoformat()
        c.execute(
            """INSERT INTO users(user_id,username,first_name,last_name,plan_key,start_at,end_at,status,created_at)
               VALUES(?,?,?,?,NULL,NULL,NULL,'none',?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username,
                 first_name=excluded.first_name,
                 last_name=excluded.last_name
            """,
            (usr.id, usr.username, usr.first_name, usr.last_name, now),
        )
        c.commit()

def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def list_users(limit: int = 1000):
    with db() as c:
        return c.execute("SELECT * FROM users ORDER BY COALESCE(end_at,'') DESC LIMIT ?", (limit,)).fetchall()

def set_status(user_id: int, status: str):
    with db() as c:
        c.execute("UPDATE users SET status=? WHERE user_id=?", (status, user_id))
        c.commit()

def set_subscription(user_id: int, plan_key: str, days: int):
    now = datetime.now(timezone.utc)
    row = get_user(user_id)
    if row and row["end_at"]:
        try:
            current_end = datetime.fromisoformat(row["end_at"])
        except Exception:
            current_end = now
        base = current_end if (row["status"] == "active" and current_end > now) else now
        end = base + timedelta(days=days)
    else:
        end = now + timedelta(days=days)

    with db() as c:
        c.execute("""UPDATE users SET plan_key=?, start_at=?, end_at=?, status='active', reminded_3d=0
                     WHERE user_id=?""",
                  (plan_key, now.isoformat(), end.isoformat(), user_id))
        c.commit()
    return now, end

def add_payment(user_id: int, plan_key: str, file_id: str) -> int:
    with db() as c:
        c.execute("""INSERT INTO payments(user_id, plan_key, file_id, created_at, status)
                     VALUES(?,?,?,?, 'pending')""",
                  (user_id, plan_key, file_id, datetime.now(timezone.utc).isoformat()))
        pid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        c.commit()
        return pid

def set_payment_status(payment_id: int, status: str):
    with db() as c:
        c.execute("UPDATE payments SET status=? WHERE id=?", (status, payment_id))
        c.commit()

def pending_payments(limit: int = 10):
    with db() as c:
        return c.execute("SELECT * FROM payments WHERE status='pending' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

def add_ticket(user_id: int, message: str) -> int:
    with db() as c:
        c.execute("""INSERT INTO tickets(user_id,message,status,created_at)
                     VALUES(?,?,'open',?)""",
                  (user_id, message, datetime.now(timezone.utc).isoformat()))
        tid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        c.commit()
        return tid

def mark_reminded(user_id: int):
    with db() as c:
        c.execute("UPDATE users SET reminded_3d=1 WHERE user_id=?", (user_id,))
        c.commit()

def stats():
    with db() as c:
        total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        active = c.execute("SELECT COUNT(*) n FROM users WHERE status='active'").fetchone()["n"]
        expired = c.execute("SELECT COUNT(*) n FROM users WHERE status='expired'").fetchone()["n"]
        pend = c.execute("SELECT COUNT(*) n FROM payments WHERE status='pending'").fetchone()["n"]
        return total, active, expired, pend

# ───────────────────────── UI helpers ─────────────────────────
def kb_user_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Buy Subscription", callback_data="menu:buy")],
        [InlineKeyboardButton(text="📦 My Plan", callback_data="menu:my")],
        [InlineKeyboardButton(text="📞 Contact Support", callback_data="menu:support")],
        [InlineKeyboardButton(text="🛠 Admin Panel", callback_data="admin:menu")],
    ])

def kb_plans() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{PLANS['plan1']['name']} - {PLANS['plan1']['price']}", callback_data="plan:plan1")],
        [InlineKeyboardButton(text=f"{PLANS['plan2']['name']} - {PLANS['plan2']['price']}", callback_data="plan:plan2")],
        [InlineKeyboardButton(text=f"{PLANS['plan3']['name']} - {PLANS['plan3']['price']}", callback_data="plan:plan3")],
        [InlineKeyboardButton(text=f"{PLANS['plan4']['name']} - {PLANS['plan4']['price']}", callback_data="plan:plan4")],
    ])

def kb_after_plan(plan_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 I Paid — Send Screenshot", callback_data=f"pay:ask:{plan_key}")],
        [InlineKeyboardButton(text="⬅️ Choose Other Plan", callback_data="menu:buy")],
    ])

def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⌛ Pending Payments", callback_data="admin:pending")],
        [InlineKeyboardButton(text="👥 Users", callback_data="admin:users")],
        [InlineKeyboardButton(text="📊 Stats", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin:broadcast")],
    ])

def kb_payment_actions(payment_id: int, user_id: int) -> InlineKeyboardMarkup:
    r1 = [
        InlineKeyboardButton(text=f"✅ {PLANS['plan1']['name']}", callback_data=f"admin:approve:{payment_id}:{user_id}:plan1"),
        InlineKeyboardButton(text=f"✅ {PLANS['plan2']['name']}", callback_data=f"admin:approve:{payment_id}:{user_id}:plan2"),
    ]
    r2 = [
        InlineKeyboardButton(text=f"✅ {PLANS['plan3']['name']}", callback_data=f"admin:approve:{payment_id}:{user_id}:plan3"),
        InlineKeyboardButton(text=f"✅ {PLANS['plan4']['name']}", callback_data=f"admin:approve:{payment_id}:{user_id}:plan4"),
    ]
    r3 = [InlineKeyboardButton(text="❌ Deny", callback_data=f"admin:deny:{payment_id}:{user_id}")]
    r4 = [InlineKeyboardButton(text="💬 Quick Reply", callback_data=f"admin:reply:{user_id}")]
    return InlineKeyboardMarkup(inline_keyboard=[r1, r2, r3, r4])

def fmt_dt(dtiso: Optional[str]) -> str:
    if not dtiso:
        return "—"
    return datetime.fromisoformat(dtiso).astimezone().strftime("%Y-%m-%d %H:%M")

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

# ───────────────────────── FSM for broadcast ─────────────────────────
class BCast(StatesGroup):
    waiting_text = State()

# ───────────────────────── Handlers (User + Admin) ─────────────────────────
# COPY ALL your existing handlers here exactly as in your original code
# (Start, Buy, Plan selection, Payment, Admin approval, Broadcast, etc.)

# ───────────────────────── Auto-Expiry Worker ─────────────────────────
async def expiry_worker():
    while True:
        try:
            now = datetime.now(timezone.utc)
            rows = list_users(10000)
            for r in rows:
                uid = r["user_id"]
                end_at = r["end_at"]
                status = r["status"]
                reminded = r["reminded_3d"]

                if end_at:
                    try:
                        end = datetime.fromisoformat(end_at)
                    except Exception:
                        continue

                    if status == "active" and not reminded and end > now and (end - now) <= timedelta(days=3):
                        try:
                            await bot.send_message(uid, "⏳ Your subscription expires in ~3 days. Renew via /start.")
                            mark_reminded(uid)
                        except Exception:
                            pass

                    if end <= now and status != "expired":
                        set_status(uid, "expired")
                        try:
                            await bot.ban_chat_member(CHANNEL_ID, uid)
                            await bot.unban_chat_member(CHANNEL_ID, uid)
                        except Exception:
                            pass
                        try:
                            await bot.send_message(uid, "❌ Your subscription expired. Use /start to renew.")
                        except Exception:
                            pass
        except Exception as e:
            log.exception(f"expiry_worker error: {e}")
        await asyncio.sleep(1800)

# ───────────────────────── Startup ─────────────────────────
async def start_bot():
    init_db()
    log.info("Starting Telegram bot worker...")
    asyncio.create_task(expiry_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(start_bot())
