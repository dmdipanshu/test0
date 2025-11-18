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
API_TOKEN = os.getenv("API_TOKEN") or "6838473237:AAFRh0ZTHfz5r-H7Gi3OgPZIQkGZXwd_z2M"
ADMIN_ID = int(os.getenv("ADMIN_ID") or "123456789")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or "-10012345678")
UPI_ID = os.getenv("UPI_ID") or "yourupi@upi"
QR_CODE_URL = os.getenv("QR_CODE_URL") or "https://example.com/qr.png"

if API_TOKEN == "TEST_TOKEN":
    raise RuntimeError("❌ API_TOKEN not set! Please configure environment variables.")

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

# ───────────────────────── SQLite (ephemeral in Koyeb) ─────────────────────────
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
    return datetime.fromisoformat(dtiso).astimezone().strftime("%Y-%m-%d %H:%M")

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def safe_text(text: str) -> str:
    """Clean text for safe display - removes None and handles special chars"""
    if not text:
        return "No info"
    return str(text).replace("None", "No info")

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

# ───────────────────────── FSM for broadcast ─────────────────────────
class BCast(StatesGroup):
    waiting_text = State()

# ───────────────────────── User Flow ─────────────────────────
@dp.message(CommandStart())
async def on_start(m: types.Message):
    upsert_user(m.from_user)
    await m.answer("🎉 Welcome to Premium Subscription Bot!\n\nChoose an option below:", reply_markup=kb_user_menu())

@dp.callback_query(F.data == "menu:buy")
async def on_buy(cq: types.CallbackQuery):
    await cq.message.answer("📋 Choose your subscription plan:", reply_markup=kb_plans())
    await cq.answer()

@dp.callback_query(F.data.startswith("plan:"))
async def on_plan(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[1]
    last_selected_plan[cq.from_user.id] = plan_key
    plan = PLANS[plan_key]
    
    caption = (
        f"✅ Selected Plan: {plan['name']}\n"
        f"💰 Price: {plan['price']}\n"
        f"⏰ Duration: {plan['days']} days\n\n"
        f"📲 Pay to UPI ID: {UPI_ID}\n"
        f"Or scan the QR code below.\n\n"
        f"After payment, tap 'I Paid' button and send your screenshot."
    )
    await cq.message.answer_photo(QR_CODE_URL, caption=caption, reply_markup=kb_after_plan(plan_key))
    await cq.answer()

@dp.callback_query(F.data.startswith("pay:ask:"))
async def on_pay_ask(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[2]
    last_selected_plan[cq.from_user.id] = plan_key
    plan_name = PLANS[plan_key]['name']
    await bot.send_message(
        cq.from_user.id, 
        f"📤 Please send your payment screenshot now.\n\n"
        f"Selected Plan: {plan_name}\n"
        f"Just send the image and I'll forward it to admin for approval."
    )
    await cq.answer()

@dp.callback_query(F.data == "menu:my")
async def on_my_plan(cq: types.CallbackQuery):
    r = get_user(cq.from_user.id)
    if not r or r["status"] != "active":
        await cq.message.answer(
            "❌ You don't have an active subscription.\n\n"
            "Use 'Buy Subscription' to get access to our premium content!"
        )
    else:
        plan_name = PLANS.get(r['plan_key'], {'name': 'Unknown'})['name']
        await cq.message.answer(
            f"📦 Your Current Plan\n\n"
            f"Plan: {plan_name}\n"
            f"Started: {fmt_dt(r['start_at'])}\n"
            f"Expires: {fmt_dt(r['end_at'])}\n"
            f"Status: {r['status'].upper()}\n\n"
            f"Enjoy your premium access! 🎉"
        )
    await cq.answer()

@dp.callback_query(F.data == "menu:support")
async def on_support(cq: types.CallbackQuery):
    await bot.send_message(
        cq.from_user.id, 
        "📞 Contact Support\n\n"
        "Type your question or issue below and I'll forward it to our support team.\n"
        "We'll get back to you as soon as possible!"
    )
    await cq.answer()

# Handle user text messages (support tickets)
@dp.message(F.text & (F.from_user.id != ADMIN_ID))
async def on_user_text(m: types.Message):
    if m.text.startswith("/"):
        return
    
    upsert_user(m.from_user)
    tid = add_ticket(m.from_user.id, m.text)
    
    # Safe message to admin - no markdown to avoid parsing errors
    username = safe_text(m.from_user.username)
    first_name = safe_text(m.from_user.first_name)
    
    admin_message = (
        f"📩 NEW SUPPORT TICKET #{tid}\n"
        f"From: {first_name} (@{username})\n"
        f"User ID: {m.from_user.id}\n"
        f"Message:\n\n{m.text}"
    )
    
    try:
        await bot.send_message(ADMIN_ID, admin_message)
        await m.answer(f"✅ Your message has been sent to support!\n\nTicket ID: #{tid}\nWe'll respond soon.")
    except Exception as e:
        log.error(f"Failed to send support ticket to admin: {e}")
        await m.answer("❌ Sorry, there was an error sending your message. Please try again later.")

# FIXED: Payment proof handler - main source of parsing errors
@dp.message(F.photo & (F.from_user.id != ADMIN_ID))
async def on_payment_photo(m: types.Message):
    try:
        plan_key = last_selected_plan.get(m.from_user.id, "plan1")
        pid = add_payment(m.from_user.id, plan_key, m.photo[-1].file_id)
        
        # Safe message formatting - no markdown parsing issues
        username = safe_text(m.from_user.username)
        first_name = safe_text(m.from_user.first_name)
        plan_name = PLANS[plan_key]['name']
        
        admin_notification = (
            f"💵 NEW PAYMENT PROOF #{pid}\n"
            f"From: {first_name} (@{username})\n"
            f"User ID: {m.from_user.id}\n"
            f"Selected Plan: {plan_name}\n"
            f"Price: {PLANS[plan_key]['price']}\n\n"
            f"Review the screenshot and approve/deny below:"
        )
        
        # Send text notification to admin
        await bot.send_message(ADMIN_ID, admin_notification)
        
        # Send photo with action buttons
        await bot.send_photo(
            ADMIN_ID, 
            m.photo[-1].file_id, 
            caption=f"Payment proof #{pid} - {plan_name}",
            reply_markup=kb_payment_actions(pid, m.from_user.id)
        )
        
        # Confirm to user
        await m.answer(
            f"✅ Payment screenshot received!\n\n"
            f"Plan: {plan_name}\n"
            f"Proof ID: #{pid}\n\n"
            f"Our admin will review and approve it shortly. "
            f"You'll get a notification once it's processed."
        )
        
    except Exception as e:
        log.error(f"Error processing payment photo: {e}")
        await m.answer("❌ Sorry, there was an error processing your screenshot. Please try again.")

# ───────────────────────── Admin Panel ─────────────────────────
@dp.callback_query(F.data == "admin:menu")
async def admin_menu(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Admin access only!", show_alert=True)
        return
    await cq.message.answer("🛠 Admin Panel\n\nChoose an option below:", reply_markup=kb_admin_menu())
    await cq.answer()

@dp.callback_query(F.data == "admin:pending")
async def admin_pending(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Admin access only!", show_alert=True)
        return
        
    rows = pending_payments(10)
    if not rows:
        await cq.message.answer("✅ No pending payments to review.")
        await cq.answer()
        return
        
    await cq.message.answer(f"⌛ Found {len(rows)} pending payment(s). Loading...")
    
    for r in rows:
        plan_name = PLANS[r['plan_key']]['name']
        price = PLANS[r['plan_key']]['price']
        
        payment_info = (
            f"💵 Payment Proof #{r['id']}\n"
            f"User ID: {r['user_id']}\n"
            f"Plan: {plan_name}\n"
            f"Price: {price}\n"
            f"Status: PENDING REVIEW\n\n"
            f"Choose action below:"
        )
        
        await cq.message.answer(payment_info, reply_markup=kb_payment_actions(r["id"], r["user_id"]))
    
    await cq.answer()

@dp.callback_query(F.data.startswith("admin:approve:"))
async def admin_approve(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Admin access only!", show_alert=True)
        return
        
    try:
        _, _, pid, uid, plan_key = cq.data.split(":")
        pid = int(pid)
        uid = int(uid)
        
        if plan_key not in PLANS:
            await cq.answer("❌ Invalid plan selected!", show_alert=True)
            return
            
        # Update payment status
        set_payment_status(pid, "approved")
        
        # Activate subscription
        _, end_date = set_subscription(uid, plan_key, PLANS[plan_key]["days"])
        
        plan_name = PLANS[plan_key]['name']
        
        # Create invite link and notify user
        try:
            link = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
            user_message = (
                f"🎉 Payment Approved!\n\n"
                f"Plan: {plan_name}\n"
                f"Valid until: {end_date.astimezone().strftime('%Y-%m-%d %H:%M')}\n\n"
                f"🔗 Join our premium channel:\n{link.invite_link}\n\n"
                f"Welcome to premium! Enjoy exclusive content! 🚀"
            )
            await bot.send_message(uid, user_message)
        except Exception as e:
            log.error(f"Error creating invite link: {e}")
            # Fallback message without invite link
            user_message = (
                f"🎉 Payment Approved!\n\n"
                f"Plan: {plan_name}\n"
                f"Valid until: {end_date.astimezone().strftime('%Y-%m-%d %H:%M')}\n\n"
                f"Contact admin for channel access.\n"
                f"Welcome to premium! 🚀"
            )
            await bot.send_message(uid, user_message)
        
        # Confirm to admin
        admin_confirm = f"✅ APPROVED Payment #{pid}\nUser: {uid}\nPlan: {plan_name}\nSubscription activated!"
        await cq.message.answer(admin_confirm)
        await cq.answer("✅ Payment approved successfully!")
        
    except Exception as e:
        log.error(f"Error approving payment: {e}")
        await cq.answer("❌ Error processing approval!", show_alert=True)

@dp.callback_query(F.data.startswith("admin:deny:"))
async def admin_deny(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Admin access only!", show_alert=True)
        return
        
    try:
        _, _, pid, uid = cq.data.split(":")
        pid = int(pid)
        uid = int(uid)
        
        # Update payment status
        set_payment_status(pid, "denied")
        
        # Notify user
        user_message = (
            f"❌ Payment Not Approved\n\n"
            f"Your payment proof #{pid} was not approved.\n"
            f"This could be due to:\n"
            f"• Unclear screenshot\n"
            f"• Wrong amount\n"
            f"• Invalid payment method\n\n"
            f"Please contact support or try again with a clear screenshot."
        )
        
        try:
            await bot.send_message(uid, user_message)
        except Exception:
            log.warning(f"Could not notify user {uid} about denied payment")
        
        # Confirm to admin
        await cq.message.answer(f"❌ DENIED Payment #{pid} for user {uid}")
        await cq.answer("❌ Payment denied!")
        
    except Exception as e:
        log.error(f"Error denying payment: {e}")
        await cq.answer("❌ Error processing denial!", show_alert=True)

@dp.callback_query(F.data == "admin:users")
async def admin_users(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Admin access only!", show_alert=True)
        return
        
    rows = list_users(50)
    if not rows:
        await cq.message.answer("👥 No users found.")
        await cq.answer()
        return
    
    # Create user list
    lines = ["👥 USER LIST (Top 50)\n"]
    for i, r in enumerate(rows, 1):
        plan = PLANS.get(r["plan_key"], {"name": "None"})["name"] if r["plan_key"] else "None"
        username = safe_text(r['username'])
        status_emoji = "✅" if r['status'] == "active" else "❌" if r['status'] == "expired" else "⚪"
        
        lines.append(f"{i}. {status_emoji} {r['user_id']} (@{username})")
        lines.append(f"   Plan: {plan} | Status: {r['status']}")
        lines.append(f"   Expires: {fmt_dt(r['end_at'])}")
        lines.append("")
    
    user_list = "\n".join(lines)
    
    # Split message if too long
    if len(user_list) > 4000:
        await cq.message.answer(user_list[:4000] + "\n\n... (truncated)")
    else:
        await cq.message.answer(user_list)
    
    await cq.answer()

@dp.callback_query(F.data == "admin:stats")
async def admin_stats(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Admin access only!", show_alert=True)
        return
        
    total, active, expired, pending = stats()
    
    stats_message = (
        f"📊 BOT STATISTICS\n\n"
        f"👥 Total Users: {total}\n"
        f"✅ Active Subscriptions: {active}\n"
        f"❌ Expired Subscriptions: {expired}\n"
        f"⌛ Pending Payments: {pending}\n\n"
        f"📈 Active Rate: {(active/total*100 if total > 0 else 0):.1f}%"
    )
    
    await cq.message.answer(stats_message)
    await cq.answer()

# Broadcast system
@dp.callback_query(F.data == "admin:broadcast")
async def bc_start(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Admin access only!", show_alert=True)
        return
        
    await cq.message.answer(
        "📢 Broadcast Message\n\n"
        "Send the message you want to broadcast to all users.\n"
        "This will be sent to everyone who has used the bot."
    )
    await state.set_state(BCast.waiting_text)
    await cq.answer()

@dp.message(BCast.waiting_text)
async def bc_send(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.clear()
        return
    
    # Get all users
    with db() as c:
        rows = c.execute("SELECT user_id FROM users").fetchall()
    
    if not rows:
        await m.answer("❌ No users to broadcast to.")
        await state.clear()
        return
    
    await m.answer(f"📤 Broadcasting to {len(rows)} users... Please wait.")
    
    sent = 0
    failed = 0
    
    for r in rows:
        try:
            await bot.send_message(r["user_id"], f"📢 Broadcast Message:\n\n{m.text}")
            sent += 1
            await asyncio.sleep(0.05)  # Rate limiting
        except Exception:
            failed += 1
    
    result_message = (
        f"📢 Broadcast Complete!\n\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}\n"
        f"📊 Success Rate: {(sent/(sent+failed)*100 if (sent+failed) > 0 else 0):.1f}%"
    )
    
    await m.answer(result_message)
    await state.clear()

# Quick reply system
@dp.callback_query(F.data.startswith("admin:reply:"))
async def admin_reply_hint(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Admin access only!", show_alert=True)
        return
        
    uid = int(cq.data.split(":")[2])
    await cq.message.answer(
        f"💬 Quick Reply\n\n"
        f"To reply to user {uid}, use:\n"
        f"`/reply {uid} Your message here`\n\n"
        f"Example:\n"
        f"`/reply {uid} Thanks for contacting us!`"
    )
    await cq.answer()

@dp.message(Command("reply"))
async def admin_reply_cmd(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    
    try:
        parts = m.text.split(maxsplit=2)
        if len(parts) < 3:
            await m.answer(
                "❌ Usage: /reply <user_id> <message>\n\n"
                "Example: /reply 123456789 Thank you for your message!"
            )
            return
        
        _, uid_str, reply_text = parts
        uid = int(uid_str)
        
        # Send reply to user
        user_message = f"📞 Support Reply:\n\n{reply_text}"
        await bot.send_message(uid, user_message)
        
        # Confirm to admin
        await m.answer(f"✅ Reply sent to user {uid}")
        
    except ValueError:
        await m.answer("❌ Invalid user ID. Please use a valid number.")
    except Exception as e:
        log.error(f"Error sending reply: {e}")
        await m.answer("❌ Error sending reply. Please check the user ID.")

# ───────────────────────── Auto-Expiry Worker ─────────────────────────
async def expiry_worker():
    """Background worker for handling subscription expiry and reminders"""
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
                
                # Send 3-day expiry reminder
                if (status == "active" and not reminded and 
                    end_date > now and (end_date - now) <= timedelta(days=3)):
                    
                    try:
                        days_left = (end_date - now).days
                        reminder_message = (
                            f"⏳ Subscription Expiry Reminder\n\n"
                            f"Your subscription expires in {days_left} day(s)!\n"
                            f"Expires on: {end_date.astimezone().strftime('%Y-%m-%d %H:%M')}\n\n"
                            f"Renew now to continue enjoying premium access!\n"
                            f"Use /start to see available plans."
                        )
                        await bot.send_message(uid, reminder_message)
                        
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
                        # Update status to expired
                        with db() as c:
                            c.execute("UPDATE users SET status='expired' WHERE user_id=?", (uid,))
                            c.commit()
                        
                        # Remove user from channel
                        try:
                            await bot.ban_chat_member(CHANNEL_ID, uid)
                            await bot.unban_chat_member(CHANNEL_ID, uid)  # Unban so they can rejoin later
                        except Exception as e:
                            log.error(f"Failed to remove user {uid} from channel: {e}")
                        
                        # Notify user about expiry
                        expiry_message = (
                            f"❌ Subscription Expired\n\n"
                            f"Your premium subscription has expired.\n"
                            f"You've been removed from the premium channel.\n\n"
                            f"To renew your subscription and regain access:\n"
                            f"👉 Use /start to see available plans\n\n"
                            f"Thank you for being a valued customer!"
                        )
                        await bot.send_message(uid, expiry_message)
                        
                        log.info(f"Processed expiry for user {uid}")
                        
                    except Exception as e:
                        log.error(f"Failed to process expiry for user {uid}: {e}")
        
        except Exception as e:
            log.exception(f"Error in expiry_worker: {e}")
        
        # Wait 30 minutes before next check
        await asyncio.sleep(1800)

# ───────────────────────── Main ─────────────────────────
async def main():
    """Main function to start the bot"""
    try:
        # Initialize database
        init_db()
        log.info("Database initialized ✅")
        
        # Start expiry worker in background
        asyncio.create_task(expiry_worker())
        log.info("Expiry worker started ✅")
        
        # Start bot polling
        log.info("Starting bot on Koyeb ✅")
        await dp.start_polling(bot, skip_updates=True)
        
    except Exception as e:
        log.error(f"Failed to start bot: {e}")
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped gracefully ✅")
    except Exception as e:
        log.error(f"Bot crashed: {e}")
        raise
