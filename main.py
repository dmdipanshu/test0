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

# ───────────────────────── Logging ─────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("subbot")

# ───────────────────────── Config (ENV based for Koyeb) ─────────────────────────
API_TOKEN = os.getenv("API_TOKEN") or "TEST_TOKEN"
ADMIN_ID = int(os.getenv("ADMIN_ID") or "123456789")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or "-10012345678")
UPI_ID = os.getenv("UPI_ID") or "yourupi@upi"
QR_CODE_URL = os.getenv("QR_CODE_URL") or "https://example.com/qr.png"

if API_TOKEN == "TEST_TOKEN":
    raise RuntimeError("❌ API_TOKEN not set! Please configure environment variables.")

bot = Bot(API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ───────────────────────── Enhanced Plans with Visual Elements ─────────────────────────
PLANS = {
    "plan1": {
        "name": "1 Month", 
        "price": "₹99", 
        "days": 30, 
        "emoji": "🟢", 
        "popular": False, 
        "discount": "",
        "features": ["✅ Full Premium Access", "✅ Priority Support", "✅ No Ads", "✅ HD Content"]
    },
    "plan2": {
        "name": "6 Months", 
        "price": "₹399", 
        "days": 180, 
        "emoji": "🟡", 
        "popular": True, 
        "discount": "💰 67% OFF",
        "features": ["✅ Everything in 1 Month", "✅ Extended Support", "✅ Bonus Content", "✅ Priority Downloads"]
    },
    "plan3": {
        "name": "1 Year", 
        "price": "₹1999", 
        "days": 365, 
        "emoji": "🔥", 
        "popular": False, 
        "discount": "🎯 BEST VALUE",
        "features": ["✅ Everything in 6 Months", "✅ VIP Support", "✅ Exclusive Content", "✅ Early Access"]
    },
    "plan4": {
        "name": "Lifetime", 
        "price": "₹2999", 
        "days": 36500, 
        "emoji": "💎", 
        "popular": False, 
        "discount": "⭐ PREMIUM",
        "features": ["✅ Everything Forever", "✅ Lifetime Updates", "✅ VIP Treatment", "✅ All Future Features"]
    },
}
last_selected_plan: Dict[int, str] = {}

# ───────────────────────── SQLite Database Setup ─────────────────────────
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
        c.execute("""CREATE TABLE IF NOT EXISTS tickets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message TEXT,
            status TEXT,
            created_at TEXT
        )""")
        c.commit()

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

def stats():
    with db() as c:
        total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        active = c.execute("SELECT COUNT(*) n FROM users WHERE status='active'").fetchone()["n"]
        expired = c.execute("SELECT COUNT(*) n FROM users WHERE status='expired'").fetchone()["n"]
        pend = c.execute("SELECT COUNT(*) n FROM payments WHERE status='pending'").fetchone()["n"]
        return total, active, expired, pend

# ───────────────────────── Helper Functions ─────────────────────────
def fmt_dt(dtiso: Optional[str]) -> str:
    if not dtiso:
        return "—"
    return datetime.fromisoformat(dtiso).astimezone().strftime("%d %b %Y, %H:%M")

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def safe_text(text: str) -> str:
    """Clean text for safe display - removes None and handles special chars"""
    if not text:
        return "No info"
    return str(text).replace("None", "No info")

def calculate_savings(plan_key: str) -> str:
    """Calculate savings compared to monthly plan"""
    if plan_key == "plan1":
        return ""
    
    monthly_price = float(PLANS["plan1"]["price"].replace("₹", ""))
    current_price = float(PLANS[plan_key]["price"].replace("₹", ""))
    months = PLANS[plan_key]["days"] / 30
    
    regular_cost = monthly_price * months
    savings = regular_cost - current_price
    savings_percent = (savings / regular_cost) * 100
    
    return f"💰 Save ₹{savings:.0f} ({savings_percent:.0f}% OFF)"

# ───────────────────────── Enhanced UI Keyboards ─────────────────────────
def kb_user_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Upgrade to Premium", callback_data="menu:buy")],
        [InlineKeyboardButton(text="📊 My Subscription", callback_data="menu:my"), 
         InlineKeyboardButton(text="💬 Support", callback_data="menu:support")],
        [InlineKeyboardButton(text="🎁 Special Offers", callback_data="menu:offers")],
        [InlineKeyboardButton(text="🛠 Admin Panel", callback_data="admin:menu")],
    ])

def kb_plans() -> InlineKeyboardMarkup:
    buttons = []
    for plan_key, plan in PLANS.items():
        emoji = plan["emoji"]
        name = plan["name"]
        price = plan["price"]
        discount = plan["discount"]
        popular = " ⭐ POPULAR" if plan["popular"] else ""
        
        button_text = f"{emoji} {name} - {price}{popular}"
        if discount:
            button_text = f"{emoji} {name} - {price} {discount}"
        
        buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"plan:{plan_key}")])
    
    buttons.extend([
        [InlineKeyboardButton(text="🔄 Compare Plans", callback_data="compare:plans")],
        [InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back:menu")]
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_payment_options(plan_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Copy UPI ID", callback_data=f"copy:upi:{plan_key}"),
         InlineKeyboardButton(text="📱 Show QR Code", callback_data=f"show:qr:{plan_key}")],
        [InlineKeyboardButton(text="📸 Upload Payment Proof", callback_data=f"pay:ask:{plan_key}")],
        [InlineKeyboardButton(text="❓ Payment Help", callback_data=f"help:payment:{plan_key}")],
        [InlineKeyboardButton(text="⬅️ Choose Other Plan", callback_data="menu:buy"),
         InlineKeyboardButton(text="🏠 Main Menu", callback_data="back:menu")]
    ])

def kb_screenshot_guide(plan_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 Camera Tips", callback_data="help:camera"),
         InlineKeyboardButton(text="🖼️ Gallery Tips", callback_data="help:gallery")],
        [InlineKeyboardButton(text="✅ Screenshot Examples", callback_data="help:examples")],
        [InlineKeyboardButton(text="⬅️ Back to Payment", callback_data=f"plan:{plan_key}")],
    ])

def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ Pending Payments", callback_data="admin:pending"),
         InlineKeyboardButton(text="📊 Analytics", callback_data="admin:stats")],
        [InlineKeyboardButton(text="👥 User Management", callback_data="admin:users"),
         InlineKeyboardButton(text="📢 Broadcast", callback_data="admin:broadcast")],
    ])

def kb_payment_actions(payment_id: int, user_id: int) -> InlineKeyboardMarkup:
    r1 = [
        InlineKeyboardButton(text=f"✅ {PLANS['plan1']['emoji']} {PLANS['plan1']['name']}", 
                           callback_data=f"admin:approve:{payment_id}:{user_id}:plan1"),
        InlineKeyboardButton(text=f"✅ {PLANS['plan2']['emoji']} {PLANS['plan2']['name']}", 
                           callback_data=f"admin:approve:{payment_id}:{user_id}:plan2")
    ]
    r2 = [
        InlineKeyboardButton(text=f"✅ {PLANS['plan3']['emoji']} {PLANS['plan3']['name']}", 
                           callback_data=f"admin:approve:{payment_id}:{user_id}:plan3"),
        InlineKeyboardButton(text=f"✅ {PLANS['plan4']['emoji']} {PLANS['plan4']['name']}", 
                           callback_data=f"admin:approve:{payment_id}:{user_id}:plan4")
    ]
    r3 = [
        InlineKeyboardButton(text="❌ Deny Payment", callback_data=f"admin:deny:{payment_id}:{user_id}"),
        InlineKeyboardButton(text="💬 Contact User", callback_data=f"admin:reply:{user_id}")
    ]
    return InlineKeyboardMarkup(inline_keyboard=[r1, r2, r3])

# ───────────────────────── FSM States ─────────────────────────
class BCast(StatesGroup):
    waiting_text = State()

# ───────────────────────── Enhanced User Flow ─────────────────────────
@dp.message(CommandStart())
async def on_start(m: types.Message):
    upsert_user(m.from_user)
    
    welcome_animation = "🎊✨🎉"
    welcome_text = (
        f"{welcome_animation} **WELCOME TO PREMIUM WORLD** {welcome_animation}\n\n"
        f"👋 Hello **{m.from_user.first_name}**!\n\n"
        f"🌟 **Unlock Premium Features:**\n"
        f"   💎 Exclusive premium content library\n"
        f"   🚀 Lightning-fast downloads\n"
        f"   🛡️ Ad-free browsing experience\n"
        f"   💬 24/7 priority support\n"
        f"   🎯 Early access to new features\n\n"
        f"⚡ **Join 10,000+ Premium Users!**\n\n"
        f"🎯 **Ready to upgrade your experience?**"
    )
    
    await m.answer(welcome_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_user_menu())

@dp.callback_query(F.data == "back:menu")
async def back_to_menu(cq: types.CallbackQuery):
    welcome_text = (
        f"🏠 **MAIN DASHBOARD**\n\n"
        f"Welcome back **{cq.from_user.first_name}**! 👋\n\n"
        f"📊 **Your Account Status:**\n"
        f"🎯 Ready to explore premium features\n"
        f"💫 Best deals available now\n\n"
        f"**What would you like to do?**"
    )
    await cq.message.edit_text(welcome_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data == "menu:buy")
async def on_buy(cq: types.CallbackQuery):
    plans_header = (
        f"💎 **PREMIUM SUBSCRIPTION PLANS** 💎\n\n"
        f"🔥 **LIMITED TIME OFFERS AVAILABLE!**\n\n"
        f"🌟 **All Plans Include:**\n"
        f"   ✅ Unlimited premium content access\n"
        f"   ✅ Zero advertisements\n"
        f"   ✅ Priority customer support\n"
        f"   ✅ Multi-device synchronization\n"
        f"   ✅ Offline download capability\n"
        f"   ✅ Exclusive member-only content\n\n"
        f"💫 **Choose Your Perfect Plan:**"
    )
    await cq.message.edit_text(plans_header, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_plans())
    await cq.answer("💎 Choose your premium plan!")

@dp.callback_query(F.data.startswith("plan:"))
async def on_plan(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[1]
    last_selected_plan[cq.from_user.id] = plan_key
    plan = PLANS[plan_key]
    
    # Calculate value metrics
    daily_cost = float(plan["price"].replace("₹", "")) / plan["days"]
    monthly_cost = daily_cost * 30
    
    # Build plan details
    plan_details = (
        f"🎯 **{plan['emoji']} {plan['name']} Plan Selected**\n\n"
        f"💰 **Price:** {plan['price']}\n"
        f"⏰ **Duration:** {plan['days']} days\n"
        f"📊 **Daily Cost:** ₹{daily_cost:.2f}/day\n"
        f"📈 **Monthly Equivalent:** ₹{monthly_cost:.0f}/month\n"
    )
    
    if plan["discount"]:
        plan_details += f"🏷️ **Special Offer:** {plan['discount']}\n"
    
    savings = calculate_savings(plan_key)
    if savings:
        plan_details += f"🎁 **Your Savings:** {savings}\n"
    
    plan_details += f"\n🎁 **Premium Features Included:**\n"
    for feature in plan["features"]:
        plan_details += f"   {feature}\n"
    
    plan_details += (
        f"\n⚡ **Instant Activation Process:**\n"
        f"   1️⃣ Choose payment method below\n"
        f"   2️⃣ Complete secure payment\n"
        f"   3️⃣ Upload payment proof\n"
        f"   4️⃣ Get instant premium access!\n\n"
        f"💳 **Select Your Payment Method:**"
    )
    
    await cq.message.edit_text(plan_details, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_payment_options(plan_key))
    await cq.answer(f"{plan['emoji']} {plan['name']} plan selected!")

@dp.callback_query(F.data.startswith("copy:upi:"))
async def copy_upi(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[2]
    plan = PLANS[plan_key]
    
    upi_details = (
        f"💳 **UPI PAYMENT GATEWAY**\n\n"
        f"🎯 **Selected Plan:** {plan['emoji']} {plan['name']}\n"
        f"💰 **Amount to Pay:** {plan['price']}\n\n"
        f"📋 **Payment Details:**\n"
        f"```
        f"UPI ID: {UPI_ID}\n"
        f"Amount: {plan['price'].replace('₹', '')}\n"
        f"```\n\n"
        f"📱 **Step-by-Step Payment Guide:**\n"
        f"   1️⃣ **Copy UPI ID** (tap the box above)\n"
        f"   2️⃣ **Open UPI App** (GPay/PhonePe/Paytm)\n"
        f"   3️⃣ **Send Money** → Paste UPI ID\n"
        f"   4️⃣ **Enter Amount:** {plan['price'].replace('₹', '')}\n"
        f"   5️⃣ **Add Note:** {plan['name']} Subscription\n"
        f"   6️⃣ **Complete Payment** → Take screenshot\n"
        f"   7️⃣ **Upload Screenshot** here for activation\n\n"
        f"⚠️ **Important:** Amount must be exactly **{plan['price']}**\n"
        f"📸 **Screenshot must show:** Payment success + Amount + Date"
    )
    
    await cq.message.edit_text(upi_details, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_payment_options(plan_key))
    await cq.answer("💳 UPI details ready! Copy and use in your payment app", show_alert=False)

@dp.callback_query(F.data.startswith("show:qr:"))
async def show_qr(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[2]
    plan = PLANS[plan_key]
    
    qr_caption = (
        f"📱 **QR CODE PAYMENT** 📱\n\n"
        f"🎯 **Plan:** {plan['emoji']} {plan['name']}\n"
        f"💰 **Amount:** {plan['price']}\n\n"
        f"📸 **QR Payment Instructions:**\n"
        f"   1️⃣ **Open UPI App** camera/scanner\n"
        f"   2️⃣ **Scan QR Code** below\n"
        f"   3️⃣ **Verify Amount:** {plan['price'].replace('₹', '')}\n"
        f"   4️⃣ **Add Description:** {plan['name']} Plan\n"
        f"   5️⃣ **Complete Payment** securely\n"
        f"   6️⃣ **Screenshot Success** page\n"
        f"   7️⃣ **Return here** to upload proof\n\n"
        f"⚡ **Instant & Secure Payment!**\n"
        f"🔒 **256-bit SSL Encrypted**"
    )
    
    await cq.message.delete()
    await bot.send_photo(
        cq.from_user.id,
        QR_CODE_URL,
        caption=qr_caption,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_payment_options(plan_key)
    )
    await cq.answer("📱 QR Code ready for scanning!")

@dp.callback_query(F.data.startswith("pay:ask:"))
async def on_pay_ask(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[2]
    last_selected_plan[cq.from_user.id] = plan_key
    plan = PLANS[plan_key]
    
    screenshot_guide = (
        f"📸 **PAYMENT PROOF UPLOAD CENTER** 📸\n\n"
        f"🎯 **Your Selection:** {plan['emoji']} {plan['name']} - {plan['price']}\n\n"
        f"📋 **Screenshot Requirements:**\n"
        f"   ✅ **Payment Status:** Must show 'SUCCESS' or 'COMPLETED'\n"
        f"   ✅ **Amount Visible:** Exactly {plan['price']} should be clear\n"
        f"   ✅ **Date & Time:** Payment timestamp must be visible\n"
        f"   ✅ **Transaction ID:** Reference number must be clear\n"
        f"   ✅ **Image Quality:** Clear, bright, readable text\n"
        f"   ✅ **Full Screen:** Don't crop important details\n\n"
        f"🚀 **Upload Process:**\n"
        f"   1️⃣ **Complete Payment** using UPI/QR method\n"
        f"   2️⃣ **Take Clear Screenshot** of success page\n"
        f"   3️⃣ **Send as Photo** (not document) in this chat\n"
        f"   4️⃣ **Wait for Approval** (usually 2-5 minutes)\n"
        f"   5️⃣ **Get Premium Access** instantly after approval!\n\n"
        f"📱 **Pro Tip:** Use good lighting for clear screenshots\n\n"
        f"📷 **Ready? Send your payment screenshot now! ⬇️**"
    )
    
    await cq.message.edit_text(
        screenshot_guide, 
        parse_mode=ParseMode.MARKDOWN, 
        reply_markup=kb_screenshot_guide(plan_key)
    )
    await cq.answer("📸 Upload your payment proof now!")

# Enhanced help callbacks
@dp.callback_query(F.data == "help:camera")
async def help_camera(cq: types.CallbackQuery):
    await cq.answer(
        "📷 CAMERA TIPS:\n"
        "• Use good lighting\n"
        "• Hold phone steady\n"
        "• Capture full screen\n"
        "• Ensure text is readable\n"
        "• Take multiple shots if needed", 
        show_alert=True
    )

@dp.callback_query(F.data == "help:gallery")
async def help_gallery(cq: types.CallbackQuery):
    await cq.answer(
        "🖼️ GALLERY UPLOAD:\n"
        "• Tap attachment button (📎)\n"
        "• Select 'Photo' option\n"
        "• Choose from gallery\n"
        "• Pick the clearest screenshot\n"
        "• Send as photo (not document)", 
        show_alert=True
    )

@dp.callback_query(F.data == "help:examples")
async def help_examples(cq: types.CallbackQuery):
    await cq.answer(
        "✅ GOOD SCREENSHOTS:\n"
        "• Shows 'Payment Successful'\n"
        "• Amount clearly visible\n"
        "• Date/time stamp present\n"
        "• Transaction ID visible\n"
        "\n❌ AVOID:\n"
        "• Blurry images\n"
        "• Cropped screenshots\n"
        "• Dark/unclear text", 
        show_alert=True
    )

@dp.callback_query(F.data == "compare:plans")
async def compare_plans(cq: types.CallbackQuery):
    comparison = (
        f"📊 **PLAN COMPARISON TABLE** 📊\n\n"
        f"🟢 **1 Month:** ₹99 (₹3.30/day)\n"
        f"   • Basic premium access\n"
        f"   • Standard support\n\n"
        f"🟡 **6 Months:** ₹399 (₹2.22/day) ⭐\n"
        f"   • Everything in 1 Month +\n"
        f"   • Extended features\n"
        f"   • Save ₹195!\n\n"
        f"🔥 **1 Year:** ₹1999 (₹5.47/day)\n"
        f"   • Everything in 6 Months +\n"
        f"   • VIP support\n"
        f"   • Exclusive content\n\n"
        f"💎 **Lifetime:** ₹2999 (One-time)\n"
        f"   • Everything forever\n"
        f"   • Lifetime updates\n"
        f"   • Best value overall\n\n"
        f"💡 **Most Popular:** 6 Months plan\n"
        f"🏆 **Best Value:** Lifetime plan"
    )
    
    await cq.message.edit_text(comparison, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_plans())
    await cq.answer()

@dp.callback_query(F.data == "menu:offers")
async def show_offers(cq: types.CallbackQuery):
    offers_text = (
        f"🎁 **SPECIAL OFFERS & DEALS** 🎁\n\n"
        f"🔥 **LIMITED TIME OFFERS:**\n\n"
        f"🟡 **6 Months Plan:** 67% OFF\n"
        f"   Regular: ₹594 → Now: ₹399\n"
        f"   Save ₹195! ⭐ MOST POPULAR\n\n"
        f"🔥 **1 Year Plan:** Best Value\n"
        f"   Only ₹5.47/day\n"
        f"   Includes VIP support\n\n"
        f"💎 **Lifetime Plan:** One-time payment\n"
        f"   Never pay again!\n"
        f"   All future updates included\n\n"
        f"🎯 **New User Bonus:**\n"
        f"   First-time subscribers get:\n"
        f"   • Instant activation\n"
        f"   • Priority support\n"
        f"   • Welcome bonus content\n\n"
        f"⏰ **Offers expire soon!**"
    )
    
    await cq.message.edit_text(offers_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_user_menu())
    await cq.answer("🎁 Check out these amazing offers!")

@dp.callback_query(F.data == "menu:my")
async def on_my_plan(cq: types.CallbackQuery):
    r = get_user(cq.from_user.id)
    
    if not r or r["status"] != "active":
        no_subscription_text = (
            f"😔 **NO ACTIVE SUBSCRIPTION**\n\n"
            f"You're currently using the **FREE** version\n\n"
            f"🌟 **Upgrade to Premium and Get:**\n"
            f"   💎 Unlimited premium content\n"
            f"   🚀 10x faster downloads\n"
            f"   🛡️ Zero advertisements\n"
            f"   💬 Priority support (24/7)\n"
            f"   📱 Multi-device access\n"
            f"   🎯 Early access to new features\n\n"
            f"💫 **Join 10,000+ Happy Premium Users!**\n\n"
            f"🎁 **Special Launch Offers Available!**\n\n"
            f"👆 **Ready to upgrade? Tap 'Upgrade to Premium'**"
        )
        await cq.message.edit_text(no_subscription_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_user_menu())
    else:
        plan_info = PLANS.get(r['plan_key'], {'name': 'Unknown', 'emoji': '📦'})
        
        # Calculate remaining time
        if r['end_at']:
            try:
                end_date = datetime.fromisoformat(r['end_at'])
                now = datetime.now(timezone.utc)
                time_left = end_date - now
                
                if time_left.days > 0:
                    days_left = time_left.days
                    hours_left = time_left.seconds // 3600
                    time_display = f"{days_left} days, {hours_left} hours"
                    status_emoji = "✅"
                    status_text = "ACTIVE"
                else:
                    time_display = "Expired"
                    status_emoji = "❌"
                    status_text = "EXPIRED"
            except:
                time_display = "Unknown"
                status_emoji = "⚪"
                status_text = "UNKNOWN"
        else:
            time_display = "Unknown"
            status_emoji = "⚪"
            status_text = "UNKNOWN"
        
        subscription_details = (
            f"📊 **MY PREMIUM SUBSCRIPTION** 📊\n\n"
            f"{status_emoji} **Status:** {status_text}\n"
            f"{plan_info['emoji']} **Plan:** {plan_info['name']}\n"
            f"📅 **Started:** {fmt_dt(r['start_at'])}\n"
            f"⏰ **Expires:** {fmt_dt(r['end_at'])}\n"
            f"⏳ **Time Remaining:** {time_display}\n\n"
            f"🎉 **Premium Benefits Active:**\n"
            f"   ✅ Unlimited content access\n"
            f"   ✅ Ad-free experience\n"
            f"   ✅ Priority support access\n"
            f"   ✅ Multi-device sync\n"
            f"   ✅ Offline downloads\n\n"
            f"💎 **You're a Premium Member!**\n"
            f"Enjoy exclusive content and features!\n\n"
            f"💬 **Need help?** Our support team is ready to assist!"
        )
        
        await cq.message.edit_text(subscription_details, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_user_menu())
    
    await cq.answer()

@dp.callback_query(F.data == "menu:support")
async def on_support(cq: types.CallbackQuery):
    support_text = (
        f"💬 **CUSTOMER SUPPORT CENTER** 💬\n\n"
        f"👋 Hi **{cq.from_user.first_name}**!\n\n"
        f"🎯 **How can we help you today?**\n\n"
        f"🔧 **Common Issues:**\n"
        f"   • Payment problems\n"
        f"   • Account activation\n"
        f"   • Technical difficulties\n"
        f"   • Subscription questions\n"
        f"   • Feature requests\n"
        f"   • Billing inquiries\n\n"
        f"📝 **Get Help:**\n"
        f"Just type your message below and our support team will respond quickly!\n\n"
        f"⚡ **Response Time:**\n"
        f"   🟢 Premium Users: 2-5 minutes\n"
        f"   🟡 Free Users: 10-30 minutes\n\n"
        f"📞 **24/7 Premium Support Available!**"
    )
    await cq.message.edit_text(support_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_user_menu())
    await cq.answer("💬 Support is ready to help!")

# Enhanced user text handler (support tickets)
@dp.message(F.text & (F.from_user.id != ADMIN_ID))
async def on_user_text(m: types.Message):
    if m.text.startswith("/"):
        return
    
    upsert_user(m.from_user)
    tid = add_ticket(m.from_user.id, m.text)
    
    # Get user subscription status for priority
    user_info = get_user(m.from_user.id)
    is_premium = user_info and user_info["status"] == "active"
    priority = "HIGH PRIORITY" if is_premium else "STANDARD"
    
    # Enhanced admin notification
    username = safe_text(m.from_user.username)
    first_name = safe_text(m.from_user.first_name)
    
    admin_message = (
        f"🎫 NEW SUPPORT TICKET #{tid}\n"
        f"🔥 PRIORITY: {priority}\n\n"
        f"👤 User: {first_name} (@{username})\n"
        f"🆔 ID: {m.from_user.id}\n"
        f"💎 Status: {'PREMIUM' if is_premium else 'FREE'}\n"
        f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}\n\n"
        f"💬 Message:\n{m.text}\n\n"
        f"📞 Quick Reply: /reply {m.from_user.id} <message>"
    )
    
    try:
        await bot.send_message(ADMIN_ID, admin_message)
        
        # Enhanced user confirmation
        confirm_text = (
            f"✅ **SUPPORT TICKET CREATED!**\n\n"
            f"🎫 **Ticket ID:** #{tid}\n"
            f"🔥 **Priority:** {priority}\n"
            f"👨‍💼 **Assigned to:** Premium Support Team\n"
            f"⏱️ **Expected Response:** {'2-5 minutes' if is_premium else '10-30 minutes'}\n\n"
            f"📋 **Your Message:**\n{m.text[:100]}{'...' if len(m.text) > 100 else ''}\n\n"
            f"🔔 **We'll notify you here when we reply!**\n\n"
            f"{'💎 Thank you for being a premium member!' if is_premium else '🌟 Consider upgrading for faster support!'}"
        )
        await m.answer(confirm_text, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        log.error(f"Failed to send support ticket to admin: {e}")
        await m.answer(
            "❌ **TECHNICAL ERROR**\n\n"
            "Sorry, something went wrong while creating your support ticket.\n\n"
            "🔄 **Please try again in a moment**\n"
            "💬 **Or contact us directly**\n\n"
            "We apologize for the inconvenience! 🙏", 
            parse_mode=ParseMode.MARKDOWN
        )

# Enhanced payment photo handler
@dp.message(F.photo & (F.from_user.id != ADMIN_ID))
async def on_payment_photo(m: types.Message):
    try:
        plan_key = last_selected_plan.get(m.from_user.id, "plan1")
        pid = add_payment(m.from_user.id, plan_key, m.photo[-1].file_id)
        plan = PLANS[plan_key]
        
        # Safe message formatting
        username = safe_text(m.from_user.username)
        first_name = safe_text(m.from_user.first_name)
        
        # Enhanced admin notification
        admin_notification = (
            f"💰 NEW PAYMENT SUBMISSION #{pid}\n\n"
            f"👤 User: {first_name} (@{username})\n"
            f"🆔 User ID: {m.from_user.id}\n"
            f"📱 Plan: {plan['emoji']} {plan['name']}\n"
            f"💵 Amount: {plan['price']}\n"
            f"⏰ Submitted: {datetime.now().strftime('%d %b %Y, %H:%M:%S')}\n"
            f"🔍 Status: PENDING REVIEW\n\n"
            f"🚀 ADMIN ACTION REQUIRED!\n"
            f"👆 Review screenshot below and approve/deny"
        )
        
        # Send notification and photo to admin
        await bot.send_message(ADMIN_ID, admin_notification)
        await bot.send_photo(
            ADMIN_ID, 
            m.photo[-1].file_id, 
            caption=(
                f"💳 PAYMENT PROOF #{pid}\n"
                f"{plan['emoji']} {plan['name']} - {plan['price']}\n"
                f"User: {first_name} ({m.from_user.id})\n"
                f"Submitted: {datetime.now().strftime('%H:%M')}"
            ),
            reply_markup=kb_payment_actions(pid, m.from_user.id)
        )
        
        # Enhanced user confirmation
        confirmation_text = (
            f"🎉 **PAYMENT PROOF UPLOADED SUCCESSFULLY!**\n\n"
            f"📸 **Proof ID:** #{pid}\n"
            f"📱 **Plan:** {plan['emoji']} {plan['name']}\n"
            f"💰 **Amount:** {plan['price']}\n"
            f"⏰ **Submitted:** {datetime.now().strftime('%d %b %Y, %H:%M')}\n\n"
            f"🔄 **Processing Timeline:**\n"
            f"   1️⃣ **Screenshot Review** (2-3 minutes)\n"
            f"   2️⃣ **Payment Verification** (1-2 minutes)\n"
            f"   3️⃣ **Account Activation** (Instant)\n"
            f"   4️⃣ **Premium Access** (Immediate)\n\n"
            f"⏰ **Total Processing Time: 3-5 minutes**\n\n"
            f"🔔 **You'll receive instant notification once approved!**\n\n"
            f"🌟 **Thank you for choosing Premium!**\n"
            f"Get ready for an amazing experience! 🚀"
        )
        
        await m.answer(confirmation_text, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        log.error(f"Error processing payment photo: {e}")
        error_text = (
            f"❌ **UPLOAD ERROR**\n\n"
            f"Something went wrong while processing your payment screenshot.\n\n"
            f"🔄 **Troubleshooting Steps:**\n"
            f"   1️⃣ **Check image quality** (clear & bright)\n"
            f"   2️⃣ **Send as photo** (not document)\n"
            f"   3️⃣ **Verify internet** connection\n"
            f"   4️⃣ **Try again** in a moment\n\n"
            f"💬 **Still having issues?**\n"
            f"Contact our support team for immediate help!"
        )
        await m.answer(error_text, parse_mode=ParseMode.MARKDOWN)

# ───────────────────────── Enhanced Admin Panel ─────────────────────────
@dp.callback_query(F.data == "admin:menu")
async def admin_menu(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied! Admin privileges required.", show_alert=True)
        return
    
    total, active, expired, pending = stats()
    admin_dashboard = (
        f"🛠️ **ADMIN CONTROL PANEL** 🛠️\n\n"
        f"📊 **Live Statistics:**\n"
        f"   👥 Total Users: **{total}**\n"
        f"   ✅ Active Subs: **{active}**\n"
        f"   ❌ Expired: **{expired}**\n"
        f"   ⏳ Pending: **{pending}**\n\n"
        f"⚡ **System Status:** Online\n"
        f"🔄 **Last Updated:** {datetime.now().strftime('%H:%M:%S')}\n\n"
        f"**Choose an action:**"
    )
    
    await cq.message.answer(admin_dashboard, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_menu())
    await cq.answer("🛠️ Welcome to Admin Panel!")

@dp.callback_query(F.data == "admin:pending")
async def admin_pending(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied!", show_alert=True)
        return
        
    rows = pending_payments(10)
    if not rows:
        await cq.message.answer(
            "✅ **NO PENDING PAYMENTS**\n\n"
            "All payment proofs have been processed!\n"
            "Great job staying on top of approvals! 🎉"
        )
        await cq.answer()
        return
        
    await cq.message.answer(f"⏳ **PROCESSING {len(rows)} PENDING PAYMENT(S)**\n\nLoading payment details...")
    
    for r in rows:
        plan = PLANS[r['plan_key']]
        
        payment_details = (
            f"💵 **PAYMENT REVIEW #{r['id']}**\n\n"
            f"👤 **User ID:** {r['user_id']}\n"
            f"📱 **Plan:** {plan['emoji']} {plan['name']}\n"
            f"💰 **Amount:** {plan['price']}\n"
            f"⏰ **Submitted:** {datetime.fromisoformat(r['created_at']).strftime('%d %b, %H:%M')}\n"
            f"🔍 **Status:** ⏳ PENDING REVIEW\n\n"
            f"**👆 Choose action below:**"
        )
        
        await cq.message.answer(payment_details, parse_mode=ParseMode.MARKDOWN, 
                              reply_markup=kb_payment_actions(r["id"], r["user_id"]))
    
    await cq.answer(f"📋 {len(rows)} payments ready for review!")

@dp.callback_query(F.data.startswith("admin:approve:"))
async def admin_approve(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied!", show_alert=True)
        return
        
    try:
        _, _, pid, uid, plan_key = cq.data.split(":")
        pid = int(pid)
        uid = int(uid)
        
        if plan_key not in PLANS:
            await cq.answer("❌ Invalid plan selected!", show_alert=True)
            return
            
        # Process approval
        set_payment_status(pid, "approved")
        _, end_date = set_subscription(uid, plan_key, PLANS[plan_key]["days"])
        plan = PLANS[plan_key]
        
        # Create premium invitation and notify user
        try:
            link = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
            user_notification = (
                f"🎉 **PAYMENT APPROVED! WELCOME TO PREMIUM!** 🎉\n\n"
                f"✅ **Your subscription is now ACTIVE!**\n\n"
                f"📱 **Plan Details:**\n"
                f"   {plan['emoji']} **Plan:** {plan['name']}\n"
                f"   💰 **Amount Paid:** {plan['price']}\n"
                f"   📅 **Activation:** {datetime.now().strftime('%d %b %Y, %H:%M')}\n"
                f"   ⏰ **Valid Until:** {end_date.astimezone().strftime('%d %b %Y, %H:%M')}\n\n"
                f"🔗 **JOIN PREMIUM CHANNEL:**\n{link.invite_link}\n\n"
                f"🎁 **Your Premium Benefits:**\n"
                f"   🔓 Unlimited content access\n"
                f"   💬 24/7 priority support\n"
                f"   📱 Multi-device synchronization\n"
                f"   🚀 Lightning-fast downloads\n"
                f"   🛡️ Ad-free experience\n"
                f"   🎯 Early access to new features\n\n"
                f"🌟 **Welcome to the Premium Family!**\n"
                f"Enjoy exclusive content and features! 🚀\n\n"
                f"💬 **Questions?** Our premium support team is here 24/7!"
            )
            await bot.send_message(uid, user_notification, parse_mode=ParseMode.MARKDOWN)
            
        except Exception as e:
            log.error(f"Error creating invite link: {e}")
            # Fallback without invite link
            user_notification = (
                f"🎉 **PAYMENT APPROVED! WELCOME TO PREMIUM!** 🎉\n\n"
                f"✅ **Subscription activated successfully!**\n\n"
                f"📱 **Plan:** {plan['emoji']} {plan['name']}\n"
                f"💰 **Amount:** {plan['price']}\n"
                f"⏰ **Valid Until:** {end_date.astimezone().strftime('%d %b %Y, %H:%M')}\n\n"
                f"📞 **Contact admin for premium channel access**\n\n"
                f"🌟 **Welcome to Premium!** 🚀"
            )
            await bot.send_message(uid, user_notification, parse_mode=ParseMode.MARKDOWN)
        
        # Confirm to admin
        admin_confirmation = (
            f"✅ **PAYMENT APPROVED SUCCESSFULLY!**\n\n"
            f"💵 **Payment ID:** #{pid}\n"
            f"👤 **User:** {uid}\n"
            f"📱 **Plan:** {plan['emoji']} {plan['name']}\n"
            f"💰 **Amount:** {plan['price']}\n"
            f"🎯 **Status:** SUBSCRIPTION ACTIVATED\n\n"
            f"🔔 **User has been notified and given access!**"
        )
        await cq.message.answer(admin_confirmation, parse_mode=ParseMode.MARKDOWN)
        await cq.answer("✅ Payment approved! User activated!")
        
    except Exception as e:
        log.error(f"Error approving payment: {e}")
        await cq.answer("❌ Error processing approval! Please try again.", show_alert=True)

@dp.callback_query(F.data.startswith("admin:deny:"))
async def admin_deny(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied!", show_alert=True)
        return
        
    try:
        _, _, pid, uid = cq.data.split(":")
        pid = int(pid)
        uid = int(uid)
        
        # Process denial
        set_payment_status(pid, "denied")
        
        # Enhanced user notification
        user_message = (
            f"❌ **PAYMENT PROOF NOT APPROVED**\n\n"
            f"Unfortunately, payment proof **#{pid}** could not be approved.\n\n"
            f"🔍 **Common reasons:**\n"
            f"   📸 Screenshot not clear enough\n"
            f"   💰 Payment amount doesn't match\n"
            f"   📋 Missing transaction details\n"
            f"   ⏰ Payment method not recognized\n"
            f"   🔄 Duplicate/processed payment\n\n"
            f"🛠️ **What to do next:**\n"
            f"   1️⃣ **Verify** your payment was successful\n"
            f"   2️⃣ **Retake screenshot** with better lighting\n"
            f"   3️⃣ **Ensure amount** matches exactly\n"
            f"   4️⃣ **Include full screen** (don't crop)\n"
            f"   5️⃣ **Try uploading again** or contact support\n\n"
            f"💬 **Need help?**\n"
            f"Contact our support team - we're here to help you get premium access!\n\n"
            f"😊 **Don't worry - we'll get this sorted!**"
        )
        
        try:
            await bot.send_message(uid, user_message, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            log.warning(f"Could not notify user {uid} about denied payment")
        
        # Confirm to admin
        admin_confirmation = (
            f"❌ **PAYMENT DENIED**\n\n"
            f"💵 **Payment ID:** #{pid}\n"
            f"👤 **User:** {uid}\n"
            f"🔍 **Status:** REJECTED\n\n"
            f"🔔 **User has been notified with helpful guidance**"
        )
        await cq.message.answer(admin_confirmation, parse_mode=ParseMode.MARKDOWN)
        await cq.answer("❌ Payment denied! User notified.")
        
    except Exception as e:
        log.error(f"Error denying payment: {e}")
        await cq.answer("❌ Error processing denial! Please try again.", show_alert=True)

# Additional admin functions (users, stats, broadcast, reply)
@dp.callback_query(F.data == "admin:users")
async def admin_users(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied!", show_alert=True)
        return
        
    rows = list_users(50)
    if not rows:
        await cq.message.answer("👥 **NO USERS FOUND**\n\nThe bot hasn't been used yet.")
        await cq.answer()
        return
    
    # Create enhanced user list
    lines = [f"👥 **USER MANAGEMENT** (Top 50)\n"]
    active_count = 0
    expired_count = 0
    
    for i, r in enumerate(rows, 1):
        plan_info = PLANS.get(r["plan_key"], {"name": "None", "emoji": "⚪"})
        plan_name = plan_info["name"] if r["plan_key"] else "None"
        username = safe_text(r['username'])
        
        if r['status'] == "active":
            status_emoji = "✅"
            active_count += 1
        elif r['status'] == "expired":
            status_emoji = "❌"
            expired_count += 1
        else:
            status_emoji = "⚪"
        
        lines.append(f"{i}. {status_emoji} **{r['user_id']}** (@{username})")
        lines.append(f"   📱 Plan: {plan_name}")
        lines.append(f"   📊 Status: {r['status'].upper()}")
        lines.append(f"   ⏰ Expires: {fmt_dt(r['end_at'])}\n")
    
    lines.insert(1, f"📊 Active: {active_count} | Expired: {expired_count}\n")
    
    user_list = "\n".join(lines)
    
    # Split if too long
    if len(user_list) > 4000:
        await cq.message.answer(user_list[:4000] + "\n\n... **[List truncated]**", parse_mode=ParseMode.MARKDOWN)
    else:
        await cq.message.answer(user_list, parse_mode=ParseMode.MARKDOWN)
    
    await cq.answer(f"📋 Showing {len(rows)} users")

@dp.callback_query(F.data == "admin:stats")
async def admin_stats(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied!", show_alert=True)
        return
        
    total, active, expired, pending = stats()
    
    # Calculate metrics
    active_rate = (active/total*100) if total > 0 else 0
    conversion_rate = ((active + expired)/total*100) if total > 0 else 0
    
    stats_report = (
        f"📊 **COMPREHENSIVE ANALYTICS** 📊\n\n"
        f"👥 **User Statistics:**\n"
        f"   📈 Total Users: **{total}**\n"
        f"   ✅ Active Subscriptions: **{active}**\n"
        f"   ❌ Expired Subscriptions: **{expired}**\n"
        f"   ⏳ Pending Payments: **{pending}**\n\n"
        f"📈 **Performance Metrics:**\n"
        f"   🎯 Active Rate: **{active_rate:.1f}%**\n"
        f"   💰 Conversion Rate: **{conversion_rate:.1f}%**\n"
        f"   📊 Retention: **{(active/(active+expired)*100) if (active+expired) > 0 else 0:.1f}%**\n\n"
        f"⏰ **Report Generated:** {datetime.now().strftime('%d %b %Y, %H:%M:%S')}\n"
        f"🟢 **System Status:** Operational"
    )
    
    await cq.message.answer(stats_report, parse_mode=ParseMode.MARKDOWN)
    await cq.answer("📊 Analytics updated!")

# Broadcast and reply functions
@dp.callback_query(F.data == "admin:broadcast")
async def bc_start(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied!", show_alert=True)
        return
    
    total_users = stats()[0]
    broadcast_info = (
        f"📢 **BROADCAST MESSAGE CENTER** 📢\n\n"
        f"👥 **Target Audience:** {total_users} users\n"
        f"📡 **Delivery Method:** Direct message\n"
        f"⚡ **Estimated Time:** {total_users * 0.05:.1f} seconds\n\n"
        f"✍️ **Send your broadcast message now:**"
    )
        
    await cq.message.answer(broadcast_info, parse_mode=ParseMode.MARKDOWN)
    await state.set_state(BCast.waiting_text)
    await cq.answer("📢 Ready for broadcast message!")

@dp.message(BCast.waiting_text)
async def bc_send(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.clear()
        return
    
    with db() as c:
        rows = c.execute("SELECT user_id FROM users").fetchall()
    
    if not rows:
        await m.answer("❌ **NO USERS TO BROADCAST TO**")
        await state.clear()
        return
    
    await m.answer(f"📤 **BROADCASTING TO {len(rows)} USERS...**")
    
    sent = 0
    failed = 0
    
    for r in rows:
        try:
            broadcast_message = (
                f"📢 **OFFICIAL ANNOUNCEMENT** 📢\n\n"
                f"{m.text}\n\n"
                f"───────────────────\n"
                f"💎 **Premium Bot Team**"
            )
            await bot.send_message(r["user_id"], broadcast_message, parse_mode=ParseMode.MARKDOWN)
            sent += 1
            await asyncio.sleep(0.05)  # Rate limiting
        except Exception:
            failed += 1
    
    final_report = (
        f"📢 **BROADCAST COMPLETED!**\n\n"
        f"✅ **Successfully Sent:** {sent}\n"
        f"❌ **Failed:** {failed}\n"
        f"📈 **Success Rate:** {(sent/(sent+failed)*100):.1f}%"
    )
    
    await m.answer(final_report, parse_mode=ParseMode.MARKDOWN)
    await state.clear()

@dp.callback_query(F.data.startswith("admin:reply:"))
async def admin_reply_hint(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access Denied!", show_alert=True)
        return
        
    uid = int(cq.data.split(":")[2])
    
    reply_guide = (
        f"💬 **QUICK REPLY SYSTEM**\n\n"
        f"👤 **Target User:** {uid}\n\n"
        f"📝 **Usage:** `/reply {uid} Your message here`\n\n"
        f"⚡ **Messages are delivered instantly!**"
    )
    
    await cq.message.answer(reply_guide, parse_mode=ParseMode.MARKDOWN)
    await cq.answer(f"💬 Ready to reply to user {uid}")

@dp.message(Command("reply"))
async def admin_reply_cmd(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    
    try:
        parts = m.text.split(maxsplit=2)
        if len(parts) < 3:
            await m.answer("❌ **Usage:** `/reply <user_id> <message>`")
            return
        
        _, uid_str, reply_text = parts
        uid = int(uid_str)
        
        user_message = (
            f"💬 **PREMIUM SUPPORT RESPONSE**\n\n"
            f"{reply_text}\n\n"
            f"───────────────────\n"
            f"🎧 **Premium Support Team**\n"
            f"💬 **Need more help?** Just reply to this message!"
        )
        
        await bot.send_message(uid, user_message, parse_mode=ParseMode.MARKDOWN)
        await m.answer(f"✅ **REPLY SENT TO USER {uid}**")
        
    except ValueError:
        await m.answer("❌ **INVALID USER ID**")
    except Exception as e:
        log.error(f"Error sending reply: {e}")
        await m.answer("❌ **ERROR SENDING REPLY**")

# ───────────────────────── Auto-Expiry Worker ─────────────────────────
async def expiry_worker():
    """Enhanced background worker for subscription management"""
    while True:
        try:
            now = datetime.now(timezone.utc)
            
            with db() as c:
                rows = c.execute("SELECT * FROM users WHERE status IN ('active', 'expired')").fetchall()
            
            for r in rows:
                uid = r["user_id"]
                status = r["status"]
                end_at = r["end_at"]
                reminded = r["reminded_3d"]
                
                if not end_at:
                    continue
                
                try:
                    end_date = datetime.fromisoformat(end_at)
                except Exception:
                    continue
                
                # 3-day expiry reminder
                if (status == "active" and not reminded and 
                    end_date > now and (end_date - now) <= timedelta(days=3)):
                    
                    try:
                        days_left = (end_date - now).days
                        
                        reminder_message = (
                            f"⏰ **SUBSCRIPTION EXPIRY REMINDER**\n\n"
                            f"Your premium subscription expires in **{days_left}** day(s)!\n\n"
                            f"📅 **Expiry Date:** {end_date.astimezone().strftime('%d %b %Y, %H:%M')}\n\n"
                            f"🔄 **Renew now to continue enjoying premium features!**\n"
                            f"🚀 **Use /start to renew now!**"
                        )
                        
                        await bot.send_message(uid, reminder_message, parse_mode=ParseMode.MARKDOWN)
                        
                        # Mark as reminded
                        with db() as c:
                            c.execute("UPDATE users SET reminded_3d=1 WHERE user_id=?", (uid,))
                            c.commit()
                        
                        log.info(f"Sent 3-day reminder to user {uid}")
                        
                    except Exception as e:
                        log.error(f"Failed to send reminder to user {uid}: {e}")
                
                # Handle expired subscriptions
                if end_date <= now and status != "expired":
                    try:
                        # Update status
                        with db() as c:
                            c.execute("UPDATE users SET status='expired' WHERE user_id=?", (uid,))
                            c.commit()
                        
                        # Remove from channel
                        try:
                            await bot.ban_chat_member(CHANNEL_ID, uid)
                            await bot.unban_chat_member(CHANNEL_ID, uid)
                        except Exception as e:
                            log.error(f"Failed to remove user {uid} from channel: {e}")
                        
                        # Notify user
                        expiry_message = (
                            f"❌ **SUBSCRIPTION EXPIRED**\n\n"
                            f"Your premium subscription has expired.\n\n"
                            f"🔄 **To renew:**\n"
                            f"   1️⃣ Use /start to see plans\n"
                            f"   2️⃣ Choose your plan\n"
                            f"   3️⃣ Complete payment\n"
                            f"   4️⃣ Get instant access back!\n\n"
                            f"💎 **We miss you! Come back to premium!**"
                        )
                        
                        await bot.send_message(uid, expiry_message, parse_mode=ParseMode.MARKDOWN)
                        log.info(f"Processed expiry for user {uid}")
                        
                    except Exception as e:
                        log.error(f"Failed to process expiry for user {uid}: {e}")
        
        except Exception as e:
            log.exception(f"Error in expiry_worker: {e}")
        
        # Wait 30 minutes before next check
        await asyncio.sleep(1800)

# ───────────────────────── Main Function ─────────────────────────
async def main():
    """Enhanced main function"""
    try:
        # Initialize database
        init_db()
        log.info("✅ Database initialized successfully")
        
        # Start expiry worker
        asyncio.create_task(expiry_worker())
        log.info("✅ Enhanced expiry worker started")
        
        # Start bot
        log.info("🚀 Starting Enhanced Premium Subscription Bot")
        await dp.start_polling(bot, skip_updates=True)
        
    except Exception as e:
        log.error(f"❌ Failed to start bot: {e}")
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("✅ Bot stopped gracefully")
    except Exception as e:
        log.error(f"❌ Bot crashed: {e}")
        raise
