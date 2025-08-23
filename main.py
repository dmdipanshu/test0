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

# ───────── Logging & Config ─────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("subbot")

API_TOKEN = os.getenv("API_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-100123456789"))
UPI_ID = os.getenv("UPI_ID", "yourupi@upi")
QR_CODE_URL = os.getenv("QR_CODE_URL", "https://example.com/qr.png")

if not API_TOKEN:
    raise RuntimeError("API_TOKEN is required in env.")

bot = Bot(API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ───────── Plans ─────────
PLANS = {
    "plan1": {"name": "1 Month",  "price": "₹99",   "days": 30},
    "plan2": {"name": "6 Months", "price": "₹199",  "days": 180},
    "plan3": {"name": "1 Year",   "price": "₹1999", "days": 365},
    "plan4": {"name": "Lifetime", "price": "₹2999", "days": 36500},
}
last_selected_plan: Dict[int, str] = {}

# ───────── DB Setup ─────────
DB = "subs.db"

def db():
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
        c.commit()

# ───────── DB Functions ─────────
def upsert_user(u: types.User):
    with db() as c:
        now = datetime.now(timezone.utc).isoformat()
        c.execute("""INSERT INTO users(user_id,username,first_name,last_name,status,created_at)
                     VALUES(?,?,?,?, 'none', ?)
                     ON CONFLICT(user_id) DO UPDATE SET
                       username=excluded.username,
                       first_name=excluded.first_name,
                       last_name=excluded.last_name""",
                  (u.id, u.username, u.first_name, u.last_name, now))
        c.commit()

def get_user(uid: int):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def list_users(limit=1000):
    with db() as c:
        return c.execute("SELECT * FROM users ORDER BY COALESCE(end_at,'') DESC LIMIT ?", (limit,)).fetchall()

def set_subscription(uid: int, plan_key: str, days: int):
    now = datetime.now(timezone.utc)
    row = get_user(uid)
    if row and row["end_at"]:
        try:
            end_old = datetime.fromisoformat(row["end_at"])
        except:
            end_old = now
        base = end_old if (row["status"] == "active" and end_old > now) else now
        end = base + timedelta(days=days)
    else:
        end = now + timedelta(days=days)

    with db() as c:
        c.execute("""UPDATE users SET plan_key=?, start_at=?, end_at=?, status='active', reminded_3d=0 WHERE user_id=?""",
                  (plan_key, now.isoformat(), end.isoformat(), uid))
        c.commit()
    return now, end

def add_payment(uid: int, plan_key: str, file_id: str):
    with db() as c:
        c.execute("""INSERT INTO payments(user_id,plan_key,file_id,created_at,status)
                     VALUES(?,?,?,?,'pending')""",
                  (uid, plan_key, file_id, datetime.now(timezone.utc).isoformat()))
        pid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        c.commit()
        return pid

def pending_payments(limit=10):
    with db() as c:
        return c.execute("SELECT * FROM payments WHERE status='pending' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

def set_payment_status(pid: int, status: str):
    with db() as c:
        c.execute("UPDATE payments SET status=? WHERE id=?", (status, pid))
        c.commit()

def mark_reminded(uid: int):
    with db() as c:
        c.execute("UPDATE users SET reminded_3d=1 WHERE user_id=?", (uid,))
        c.commit()

def stats():
    with db() as c:
        t = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        a = c.execute("SELECT COUNT(*) n FROM users WHERE status='active'").fetchone()["n"]
        e = c.execute("SELECT COUNT(*) n FROM users WHERE status='expired'").fetchone()["n"]
        p = c.execute("SELECT COUNT(*) n FROM payments WHERE status='pending'").fetchone()["n"]
        return t, a, e, p

# ───────── UI ─────────
def kb_user():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Buy Subscription", callback_data="buy")],
        [InlineKeyboardButton(text="📦 My Plan", callback_data="my")],
        [InlineKeyboardButton(text="📞 Support", callback_data="support")],
        [InlineKeyboardButton(text="🛠 Admin", callback_data="admin")] if ADMIN_ID else []
    ])

def kb_plans():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{v['name']} - {v['price']}", callback_data=f"plan:{k}")]
        for k, v in PLANS.items()
    ])

def kb_payment(pid: int, uid: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ {PLANS[k]['name']}", callback_data=f"approve:{pid}:{uid}:{k}")]
        for k in PLANS
    ] + [[InlineKeyboardButton(text="❌ Deny", callback_data=f"deny:{pid}:{uid}")]])

# ───────── Handlers ─────────
@dp.message(CommandStart())
async def start(m: types.Message):
    upsert_user(m.from_user)
    await m.answer("Welcome! Choose an option:", reply_markup=kb_user())

@dp.callback_query(F.data == "buy")
async def buy(c: types.CallbackQuery):
    await c.message.edit_text("Choose a plan:", reply_markup=kb_plans())

@dp.callback_query(F.data.startswith("plan:"))
async def plan_sel(c: types.CallbackQuery):
    plan = c.data.split(":")[1]
    last_selected_plan[c.from_user.id] = plan
    await c.message.answer(f"Send payment to {UPI_ID}\nPrice: {PLANS[plan]['price']}\nThen upload screenshot here.")

@dp.message(F.photo)
async def handle_payment(m: types.Message):
    uid = m.from_user.id
    if uid not in last_selected_plan:
        return await m.reply("First choose a plan via /start.")
    plan = last_selected_plan[uid]
    pid = add_payment(uid, plan, m.photo[-1].file_id)
    await m.reply("Payment submitted, waiting for admin approval.")
    await bot.send_photo(ADMIN_ID, m.photo[-1].file_id,
                         caption=f"Payment from {m.from_user.username or uid} for {PLANS[plan]['name']}",
                         reply_markup=kb_payment(pid, uid))

@dp.callback_query(F.data.startswith("approve:"))
async def approve(c: types.CallbackQuery):
    _, pid, uid, plan = c.data.split(":")
    uid, pid = int(uid), int(pid)
    set_payment_status(pid, "approved")
    set_subscription(uid, plan, PLANS[plan]["days"])
    await bot.send_message(uid, f"✅ Payment approved. {PLANS[plan]['name']} activated.")
    await c.answer("Approved")

@dp.callback_query(F.data.startswith("deny:"))
async def deny(c: types.CallbackQuery):
    _, pid, uid = c.data.split(":")
    set_payment_status(int(pid), "denied")
    await bot.send_message(int(uid), "❌ Payment denied. Contact support.")
    await c.answer("Denied")

@dp.callback_query(F.data == "my")
async def my_plan(c: types.CallbackQuery):
    row = get_user(c.from_user.id)
    if not row or not row["plan_key"]:
        return await c.message.answer("No active plan.")
    await c.message.answer(f"Plan: {PLANS[row['plan_key']]['name']}\nEnds: {row['end_at']}")

@dp.callback_query(F.data == "admin")
async def admin_menu(c: types.CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Not allowed")
    t,a,e,p = stats()
    await c.message.answer(f"Users: {t}\nActive: {a}\nExpired: {e}\nPending: {p}")

# ───────── Auto-expiry ─────────
async def expiry_worker():
    while True:
        now = datetime.now(timezone.utc)
        for u in list_users(10000):
            if u["end_at"]:
                try:
                    end = datetime.fromisoformat(u["end_at"])
                except:
                    continue
                if end < now and u["status"] == "active":
                    with db() as c:
                        c.execute("UPDATE users SET status='expired' WHERE user_id=?", (u["user_id"],))
                        c.commit()
                    try:
                        await bot.send_message(u["user_id"], "Your subscription expired.")
                    except: pass
        await asyncio.sleep(1800)

# ───────── Start Bot ─────────
async def main():
    init_db()
    asyncio.create_task(expiry_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
