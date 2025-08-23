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

# ───────────────────────── DB Functions ─────────────────────────
# (reuse all existing DB functions from your original code unchanged)

# ───────────────────────── UI, FSM, Handlers ─────────────────────────
# (reuse all existing handlers, menus, admin logic from your original code unchanged)

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

# ───────────────────────── Startup for Koyeb ─────────────────────────
async def start_bot():
    init_db()
    log.info("Starting Telegram bot worker...")
    asyncio.create_task(expiry_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(start_bot())
