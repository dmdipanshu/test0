import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage

# ───────────────────────── Logging ─────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("subbot")

# ───────────────────────── Config from Environment ─────────────────────────
def load_config() -> dict:
    cfg = {
        "API_TOKEN": os.getenv("API_TOKEN"),
        "ADMIN_ID": os.getenv("ADMIN_ID"),
        "CHANNEL_ID": os.getenv("CHANNEL_ID"),
        "UPI_ID": os.getenv("UPI_ID"),
        "QR_CODE_URL": os.getenv("QR_CODE_URL"),
    }
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise ValueError(f"Missing environment variables: {', '.join(missing)}")
    cfg["ADMIN_ID"] = int(cfg["ADMIN_ID"])
    cfg["CHANNEL_ID"] = int(cfg["CHANNEL_ID"])
    return cfg

cfg = load_config()
API_TOKEN = cfg["API_TOKEN"]
ADMIN_ID = cfg["ADMIN_ID"]
CHANNEL_ID = cfg["CHANNEL_ID"]
UPI_ID = cfg["UPI_ID"]
QR_CODE_URL = cfg["QR_CODE_URL"]

# ───────────────────────── Bot & Dispatcher ─────────────────────────
bot = Bot(API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ───────────────────────── Plans ─────────────────────────
PLANS = {
    "plan1": {"name": "1 Month",  "price": "₹99",   "days": 30},
    "plan2": {"name": "6 Months", "price": "₹199",  "days": 180},
    "plan3": {"name": "1 Year",   "price": "₹1999", "days": 365},
    "plan4": {"name": "Lifetime", "price": "₹2999", "days": 36500},
}

# Keep last user-selected plan in memory
last_selected_plan: dict[int, str] = {}

# ───────────────────────── SQLite ─────────────────────────
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
            created_at TEXT
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

    # Add missing columns if needed
    def ensure_col(table: str, col: str, ddl: str):
        with db() as c:
            cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                c.commit()
    ensure_col("users", "reminded_3d", "reminded_3d INTEGER DEFAULT 0")

# ───────────────────────── DB Helpers ─────────────────────────
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
        start = now
        end = base + timedelta(days=days)
    else:
        start = now
        end = now + timedelta(days=days)

    with db() as c:
        c.execute("""UPDATE users SET plan_key=?, start_at=?, end_at=?, status='active', reminded_3d=0
                     WHERE user_id=?""",
                  (plan_key, start.isoformat(), end.isoformat(), user_id))
        c.commit()
    return start, end

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

def fmt_dt(dtiso: Optional[str]) -> str:
    if not dtiso:
        return "—"
    return datetime.fromisoformat(dtiso).astimezone().strftime("%Y-%m-%d %H:%M")

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

# ───────────────────────── FSM for broadcast ─────────────────────────
class BCast(StatesGroup):
    waiting_text = State()

# ───────────────────────── User Handlers ─────────────────────────
@dp.message(CommandStart())
async def on_start(m: types.Message):
    upsert_user(m.from_user)
    await m.answer("Welcome! Choose an option:", reply_markup=kb_user_menu())

# ───────────────────────── Add your other handlers here (payment, admin, etc.) ─────────────────────────
# Keep all your previous handler code here, just make sure it's below `dp` definition.

# ───────────────────────── Main ─────────────────────────
async def main():
    init_db()
    log.info("Starting bot…")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
