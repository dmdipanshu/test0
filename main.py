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
from aiogram.enums import ParseMode

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("subbot")

# Config
API_TOKEN = os.getenv("API_TOKEN") or "TEST_TOKEN"
ADMIN_ID = int(os.getenv("ADMIN_ID") or "123456789")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or "-10012345678")
UPI_ID = os.getenv("UPI_ID") or "yourupi@upi"
QR_CODE_URL = os.getenv("QR_CODE_URL") or "https://example.com/qr.png"
WELCOME_IMAGE = os.getenv("WELCOME_IMAGE") or "https://i.imgur.com/premium-welcome.jpg"
PLANS_IMAGE = os.getenv("PLANS_IMAGE") or "https://i.imgur.com/premium-plans.jpg"
OFFERS_IMAGE = os.getenv("OFFERS_IMAGE") or "https://i.imgur.com/special-offers.jpg"
SUCCESS_IMAGE = os.getenv("SUCCESS_IMAGE") or "https://i.imgur.com/success.jpg"
UPGRADE_IMAGE = os.getenv("UPGRADE_IMAGE") or "https://i.imgur.com/upgrade-now.jpg"

if API_TOKEN == "TEST_TOKEN":
    raise RuntimeError("❌ Set API_TOKEN in env variables.")

bot = Bot(API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Plans
PLANS = {
    "plan1": {"name": "1 Month", "price": "₹99", "days": 30, "emoji": "🟢"},
    "plan2": {"name": "6 Months", "price": "₹399", "days": 180, "emoji": "🟡"},
    "plan3": {"name": "1 Year", "price": "₹1999", "days": 365, "emoji": "🔥"},
    "plan4": {"name": "Lifetime", "price": "₹2999", "days": 36500, "emoji": "💎"},
}
last_selected_plan: Dict[int, str] = {}

# Database setup
DB = "/tmp/subs.db"

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
        c.commit()

def upsert_user(usr: types.User):
    with db() as c:
        now = datetime.now(timezone.utc).isoformat()
        c.execute(
            "INSERT INTO users(user_id, username, first_name, last_name, status, created_at) VALUES (?, ?, ?, ?, 'none', ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name, last_name=excluded.last_name",
            (usr.id, usr.username, usr.first_name, usr.last_name, now)
        )
        c.commit()

def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def set_subscription(user_id: int, plan_key: str, days: int):
    now = datetime.now(timezone.utc)
    row = get_user(user_id)
    base = now
    if row and row["end_at"]:
        try:
            current_end = datetime.fromisoformat(row["end_at"])
            if row["status"] == "active" and current_end > now:
                base = current_end
        except:
            pass
    end = base + timedelta(days=days)
    with db() as c:
        c.execute("UPDATE users SET plan_key=?, start_at=?, end_at=?, status='active', reminded_3d=0 WHERE user_id=?",
                  (plan_key, now.isoformat(), end.isoformat(), user_id))
        c.commit()
    return now, end

def add_payment(user_id: int, plan_key: str, file_id: str) -> int:
    with db() as c:
        c.execute("INSERT INTO payments(user_id, plan_key, file_id, created_at, status) VALUES (?, ?, ?, ?, 'pending')",
                  (user_id, plan_key, file_id, datetime.now(timezone.utc).isoformat()))
        payment_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
        return payment_id

# UI Keyboards simplified
def kb_user_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("🚀 Upgrade Premium", callback_data="menu:buy")],
        [InlineKeyboardButton("📊 My Subscription", callback_data="menu:my")],
        [InlineKeyboardButton("💬 Support", callback_data="menu:support")]
    ])

def kb_plans() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(f"{p['emoji']} {p['name']} - {p['price']}", callback_data=f"plan:{key}")] for key, p in PLANS.items()]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="back:menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_payment_options(plan_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("📋 Copy UPI ID", callback_data=f"copy:upi:{plan_key}"),
         InlineKeyboardButton("📱 Show QR", callback_data=f"show:qr:{plan_key}")],
        [InlineKeyboardButton("📸 Upload Proof", callback_data=f"pay:ask:{plan_key}")],
        [InlineKeyboardButton("⬅️ Back to Plans", callback_data="menu:buy")]
    ])

# Safe message editing helper
async def safe_edit_message(cq: types.CallbackQuery, text=None, reply_markup=None):
    try:
        if text:
            await cq.message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except:
        await cq.message.answer(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def safe_send_photo(chat_id: int, photo_url: str, caption: str, reply_markup=None):
    try:
        await bot.send_photo(chat_id, photo_url, caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except:
        await bot.send_message(chat_id, caption, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

# Handlers - Start with welcome image and upgrade button
@dp.message(CommandStart())
async def cmd_start(m: types.Message):
    upsert_user(m.from_user)
    caption = (
        f"👋 Hello {m.from_user.first_name}!\n\n"
        "Upgrade now to enjoy premium content, fast downloads, and ad-free experience!"
    )
    await safe_send_photo(m.from_user.id, WELCOME_IMAGE, caption, reply_markup=kb_user_menu())

@dp.callback_query(F.data == "back:menu")
async def back_to_menu(cq: types.CallbackQuery):
    caption = (
        f"Welcome back {cq.from_user.first_name}!\nChoose an option below:"
    )
    try:
        await cq.message.delete()
    except:
        pass
    await safe_send_photo(cq.from_user.id, WELCOME_IMAGE, caption, reply_markup=kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data == "menu:buy")
async def on_buy(cq: types.CallbackQuery):
    caption = "Select a plan below:"
    try:
        await cq.message.delete()
    except:
        pass
    await safe_send_photo(cq.from_user.id, PLANS_IMAGE, caption, reply_markup=kb_plans())
    await cq.answer()

@dp.callback_query(F.data.startswith("plan:"))
async def on_plan(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[1]
    last_selected_plan[cq.from_user.id] = plan_key
    plan = PLANS[plan_key]
    caption = (
        f"{plan['emoji']} {plan['name']} plan\nPrice: {plan['price']}\nDuration: {plan['days']} days\n\n"
        "Choose your payment method:"
    )
    await safe_edit_message(cq, caption, reply_markup=kb_payment_options(plan_key))
    await cq.answer()

@dp.callback_query(F.data.startswith("copy:upi:"))
async def copy_upi(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[2]
    plan = PLANS[plan_key]
    text = (
        f"UPI ID: `{UPI_ID}`\nAmount: `{plan['price'].replace('₹','')}`\n\n"
        "Pay exact amount and upload screenshot after payment."
    )
    await safe_edit_message(cq, text, reply_markup=kb_payment_options(plan_key))
    await cq.answer("UPI ID copied. Paste in your payment app.")

@dp.callback_query(F.data.startswith("show:qr:"))
async def show_qr(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[2]
    plan = PLANS[plan_key]
    caption = (
        f"{plan['emoji']} {plan['name']} - Pay ₹{plan['price'].replace('₹', '')}\nScan QR code below."
    )
    try:
        await cq.message.delete()
    except:
        pass
    await safe_send_photo(cq.from_user.id, QR_CODE_URL, caption, reply_markup=kb_payment_options(plan_key))
    await cq.answer()

@dp.callback_query(F.data.startswith("pay:ask:"))
async def on_pay_ask(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[2]
    last_selected_plan[cq.from_user.id] = plan_key
    text = (
        "Upload your payment screenshot (photo only).\n"
        "Make sure it's clear and shows amount and success status."
    )
    await safe_edit_message(cq, text)
    await cq.answer("Send payment screenshot now.")

@dp.message(F.photo)
async def photo_handler(m: types.Message):
    plan_key = last_selected_plan.get(m.from_user.id)
    if not plan_key:
        return await m.answer("Select a plan first.")

    pid = add_payment(m.from_user.id, plan_key, m.photo[-1].file_id)
    plan = PLANS[plan_key]
    await m.answer("Payment proof received, waiting for admin approval. Thank you!")

    username = m.from_user.username or "None"
    admin_msg = (
        f"New payment proof #{pid} from user {m.from_user.id} (@{username})\n"
        f"Plan: {plan['name']} Price: {plan['price']}\nApprove or reject."
    )
    await bot.send_message(ADMIN_ID, admin_msg)
    await bot.send_photo(ADMIN_ID, m.photo[-1].file_id,
                         caption=f"Payment Proof #{pid} from {m.from_user.id}",
                         reply_markup=kb_payment_actions(pid, m.from_user.id))

@dp.callback_query(F.data.startswith("admin:approve:"))
async def admin_approve(cq: types.CallbackQuery):
    parts = cq.data.split(":")
    pid, uid, plan_key = int(parts[2]), int(parts[1]), parts[2]
    set_payment_status(pid, "approved")
    _, end_date = set_subscription(uid, plan_key, PLANS[plan_key]["days"])

    msg = f"Your payment approved! Plan active until {end_date.strftime('%d %b %Y')}."
    await bot.send_message(uid, msg)
    await cq.message.answer(f"Payment #{pid} approved.")
    await cq.answer("Approved.")

@dp.callback_query(F.data.startswith("admin:deny:"))
async def admin_deny(cq: types.CallbackQuery):
    parts = cq.data.split(":")
    pid, uid = int(parts[2]), int(parts[1])
    set_payment_status(pid, "denied")
    msg = "Your payment proof was denied. Please try again with a clear screenshot."
    await bot.send_message(uid, msg)
    await cq.message.answer(f"Payment #{pid} denied.")
    await cq.answer("Denied.")

# Run bot
async def main():
    init_db()
    log.info("Database initialized")
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
