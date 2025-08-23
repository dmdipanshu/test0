import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from bson import ObjectId

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from motor.motor_asyncio import AsyncIOMotorClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("subbot")

# Environment variables
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
MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"

if API_TOKEN == "TEST_TOKEN":
    raise RuntimeError("❌ Set API_TOKEN in environment variables")

# MongoDB setup
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client['premium_bot']
users_col = db['users']
payments_col = db['payments']
tickets_col = db['tickets']

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Plans configuration
PLANS = {
    "plan1": {"name": "1 Month", "price": "₹99", "days": 30, "emoji": "🟢"},
    "plan2": {"name": "6 Months", "price": "₹399", "days": 180, "emoji": "🟡", "popular": True},
    "plan3": {"name": "1 Year", "price": "₹1999", "days": 365, "emoji": "🔥"},
    "plan4": {"name": "Lifetime", "price": "₹2999", "days": 36500, "emoji": "💎"},
}
last_selected_plan: Dict[int, str] = {}

# FSM States
class BCast(StatesGroup):
    waiting_text = State()

# Helper Functions
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def safe_text(text) -> str:
    return str(text or "No info").replace("None", "No info")

# MongoDB Helper Functions
async def upsert_user(user: types.User):
    await users_col.update_one(
        {"user_id": user.id},
        {"$set": {
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "updated_at": datetime.now(timezone.utc)
        }, "$setOnInsert": {
            "plan_key": None,
            "start_at": None,
            "end_at": None,
            "status": "none",
            "created_at": datetime.now(timezone.utc),
            "reminded_3d": False
        }},
        upsert=True
    )

async def get_user(user_id: int) -> Optional[dict]:
    return await users_col.find_one({"user_id": user_id})

async def set_subscription(user_id: int, plan_key: str, days: int):
    now = datetime.now(timezone.utc)
    user = await get_user(user_id)
    base = now
    
    if user and user.get("end_at"):
        try:
            end_date = user["end_at"]
            if user.get("status") == "active" and end_date > now:
                base = end_date
        except Exception:
            pass
    
    end_date = base + timedelta(days=days)
    
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {
            "plan_key": plan_key,
            "start_at": now,
            "end_at": end_date,
            "status": "active",
            "reminded_3d": False
        }}
    )
    return now, end_date

async def add_payment(user_id: int, plan_key: str, file_id: str):
    result = await payments_col.insert_one({
        "user_id": user_id,
        "plan_key": plan_key,
        "file_id": file_id,
        "created_at": datetime.now(timezone.utc),
        "status": "pending"
    })
    return str(result.inserted_id)

async def set_payment_status(payment_id: str, status: str):
    await payments_col.update_one(
        {"_id": ObjectId(payment_id)},
        {"$set": {"status": status}}
    )

async def get_pending_payments(limit: int = 10):
    cursor = payments_col.find({"status": "pending"}).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)

async def add_ticket(user_id: int, message: str):
    result = await tickets_col.insert_one({
        "user_id": user_id,
        "message": message,
        "status": "open",
        "created_at": datetime.now(timezone.utc)
    })
    return str(result.inserted_id)

async def get_stats():
    total = await users_col.count_documents({})
    active = await users_col.count_documents({"status": "active"})
    expired = await users_col.count_documents({"status": "expired"})
    pending = await payments_col.count_documents({"status": "pending"})
    return total, active, expired, pending

# UI Helper Functions
async def safe_send_photo(chat_id: int, photo_url: str, caption: str, reply_markup=None):
    try:
        await bot.send_photo(chat_id, photo_url, caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.warning(f"Failed to send photo: {e}")
        await bot.send_message(chat_id, caption, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

async def safe_edit_message(cq: types.CallbackQuery, text: str = None, caption: str = None, reply_markup=None):
    try:
        if text:
            await cq.message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        elif caption:
            await cq.message.edit_caption(caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        if text:
            await cq.message.answer(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        elif caption:
            await cq.message.answer(caption, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

# Keyboard Functions
def kb_user_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Upgrade Premium", callback_data="menu:buy")],
            [InlineKeyboardButton(text="📊 My Subscription", callback_data="menu:my"),
             InlineKeyboardButton(text="💬 Support", callback_data="menu:support")],
            [InlineKeyboardButton(text="🎁 Special Offers", callback_data="menu:offers")],
            [InlineKeyboardButton(text="🛠 Admin Panel", callback_data="admin:menu")] if is_admin else []
        ]
    )

def kb_plans() -> InlineKeyboardMarkup:
    buttons = []
    for plan_key, plan in PLANS.items():
        text = f"{plan['emoji']} {plan['name']} - {plan['price']}"
        if plan.get("popular"):
            text += " ⭐"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"plan:{plan_key}")])
    
    buttons.append([InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back:menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_payment_options(plan_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Copy UPI ID", callback_data=f"copy:upi:{plan_key}"),
            InlineKeyboardButton(text="📱 Show QR Code", callback_data=f"show:qr:{plan_key}")
        ],
        [InlineKeyboardButton(text="📸 Upload Payment Proof", callback_data=f"pay:ask:{plan_key}")],
        [
            InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="menu:buy"),
            InlineKeyboardButton(text="🏠 Main Menu", callback_data="back:menu")
        ]
    ])

def kb_payment_actions(payment_id: str, user_id: int) -> InlineKeyboardMarkup:
    buttons = []
    # Plan approval buttons
    for i, (plan_key, plan) in enumerate(PLANS.items()):
        if i % 2 == 0:
            if i + 1 < len(PLANS):
                next_plan = list(PLANS.items())[i + 1]
                buttons.append([
                    InlineKeyboardButton(text=f"✅ {plan['emoji']} {plan['name']}", 
                                       callback_data=f"admin:approve:{payment_id}:{user_id}:{plan_key}"),
                    InlineKeyboardButton(text=f"✅ {next_plan[1]['emoji']} {next_plan[1]['name']}", 
                                       callback_data=f"admin:approve:{payment_id}:{user_id}:{next_plan[0]}")
                ])
            else:
                buttons.append([InlineKeyboardButton(text=f"✅ {plan['emoji']} {plan['name']}", 
                                                   callback_data=f"admin:approve:{payment_id}:{user_id}:{plan_key}")])
    
    buttons.append([
        InlineKeyboardButton(text="❌ Deny Payment", callback_data=f"admin:deny:{payment_id}:{user_id}"),
        InlineKeyboardButton(text="💬 Contact User", callback_data=f"admin:reply:{user_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏳ Pending Payments", callback_data="admin:pending"),
            InlineKeyboardButton(text="📊 Statistics", callback_data="admin:stats")
        ],
        [
            InlineKeyboardButton(text="👥 Users", callback_data="admin:users"),
            InlineKeyboardButton(text="📢 Broadcast", callback_data="admin:broadcast")
        ]
    ])

# Bot Handlers
@dp.message(CommandStart())
async def cmd_start(m: types.Message):
    await upsert_user(m.from_user)
    caption = (
        f"👋 **Hello {m.from_user.first_name}!**\n\n"
        f"🌟 **Upgrade to Premium:**\n"
        f"• Unlimited downloads\n"
        f"• Ad-free experience\n"
        f"• Priority support\n\n"
        f"🚀 **Ready to upgrade?**"
    )
    await safe_send_photo(m.from_user.id, WELCOME_IMAGE, caption, reply_markup=kb_user_menu())

@dp.callback_query(F.data == "back:menu")
async def back_to_menu(cq: types.CallbackQuery):
    caption = f"🏠 **Welcome back {cq.from_user.first_name}!**\n\nChoose an option below:"
    try:
        await cq.message.delete()
    except Exception:
        pass
    await safe_send_photo(cq.from_user.id, WELCOME_IMAGE, caption, reply_markup=kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data == "menu:buy")
async def on_buy(cq: types.CallbackQuery):
    caption = (
        f"💎 **Premium Plans**\n\n"
        f"Choose your subscription plan:"
    )
    try:
        await cq.message.delete()
    except Exception:
        pass
    await safe_send_photo(cq.from_user.id, PLANS_IMAGE, caption, reply_markup=kb_plans())
    await cq.answer()

@dp.callback_query(F.data == "menu:offers")
async def show_offers(cq: types.CallbackQuery):
    caption = (
        f"🎁 **Special Offers**\n\n"
        f"🟡 **6 Months:** Save 33%\n"
        f"🔥 **1 Year:** Best Value\n"
        f"💎 **Lifetime:** One-time payment\n\n"
        f"⏰ **Limited time offers!**"
    )
    try:
        await cq.message.delete()
    except Exception:
        pass
    await safe_send_photo(cq.from_user.id, OFFERS_IMAGE, caption, reply_markup=kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data == "menu:my")
async def on_my_plan(cq: types.CallbackQuery):
    user = await get_user(cq.from_user.id)
    
    if not user or user.get("status") != "active":
        caption = (
            f"😔 **No Active Subscription**\n\n"
            f"You're using the FREE version.\n\n"
            f"🌟 **Upgrade benefits:**\n"
            f"• Unlimited access\n"
            f"• No advertisements\n"
            f"• Priority support\n\n"
            f"👆 **Ready to upgrade?**"
        )
        try:
            await cq.message.delete()
        except Exception:
            pass
        await safe_send_photo(cq.from_user.id, UPGRADE_IMAGE, caption, reply_markup=kb_user_menu())
    else:
        plan_info = PLANS.get(user['plan_key'], {'name': 'Unknown', 'emoji': '📦'})
        
        # Calculate remaining time
        if user.get('end_at'):
            try:
                end_date = user['end_at']
                now = datetime.now(timezone.utc)
                time_left = end_date - now
                
                if time_left.days > 0:
                    time_display = f"{time_left.days} days"
                    status_emoji = "✅"
                    status_text = "ACTIVE"
                else:
                    time_display = "Expired"
                    status_emoji = "❌"
                    status_text = "EXPIRED"
            except Exception:
                time_display = "Unknown"
                status_emoji = "⚪"
                status_text = "UNKNOWN"
        else:
            time_display = "Unknown"
            status_emoji = "⚪"
            status_text = "UNKNOWN"
        
        caption = (
            f"📊 **My Subscription**\n\n"
            f"{status_emoji} **Status:** {status_text}\n"
            f"{plan_info['emoji']} **Plan:** {plan_info['name']}\n"
            f"⏳ **Time Left:** {time_display}\n\n"
            f"🎉 **Premium Benefits Active!**"
        )
        
        await safe_edit_message(cq, text=caption, reply_markup=kb_user_menu())
    
    await cq.answer()

@dp.callback_query(F.data == "menu:support")
async def on_support(cq: types.CallbackQuery):
    text = (
        f"💬 **Customer Support**\n\n"
        f"Hi {cq.from_user.first_name}!\n\n"
        f"📝 **Need help?**\n"
        f"Just type your message and our support team will respond quickly!\n\n"
        f"⚡ **Response time:** 5-30 minutes"
    )
    await safe_edit_message(cq, text=text, reply_markup=kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data.startswith("plan:"))
async def on_plan(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[1]
    last_selected_plan[cq.from_user.id] = plan_key
    plan = PLANS[plan_key]
    
    daily_cost = float(plan["price"].replace("₹", "")) / plan["days"]
    
    caption = (
        f"🎯 **{plan['emoji']} {plan['name']} Plan**\n\n"
        f"💰 **Price:** {plan['price']}\n"
        f"⏰ **Duration:** {plan['days']} days\n"
        f"📊 **Daily Cost:** ₹{daily_cost:.2f}/day\n\n"
        f"💳 **Choose Payment Method:**"
    )
    
    await safe_edit_message(cq, text=caption, reply_markup=kb_payment_options(plan_key))
    await cq.answer()

@dp.callback_query(F.data.startswith("copy:upi:"))
async def copy_upi(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[2]
    plan = PLANS[plan_key]
    
    text = (
        f"💳 **UPI Payment**\n\n"
        f"🎯 **Plan:** {plan['emoji']} {plan['name']}\n"
        f"💰 **Amount:** {plan['price']}\n\n"
        f"📋 **Payment Details:**\n"
        f"UPI ID: `{UPI_ID}`\n"
        f"Amount: `{plan['price'].replace('₹', '')}`\n\n"
        f"💡 **Steps:**\n"
        f"1. Copy UPI ID above\n"
        f"2. Pay in your UPI app\n"
        f"3. Upload screenshot here\n\n"
        f"⚠️ Pay exact amount: **{plan['price']}**"
    )
    
    await safe_edit_message(cq, text=text, reply_markup=kb_payment_options(plan_key))
    await cq.answer("💳 UPI details ready!")

@dp.callback_query(F.data.startswith("show:qr:"))
async def show_qr(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[2]
    plan = PLANS[plan_key]
    
    caption = (
        f"📱 **QR Code Payment**\n\n"
        f"🎯 **Plan:** {plan['emoji']} {plan['name']}\n"
        f"💰 **Amount:** {plan['price']}\n\n"
        f"📸 **Instructions:**\n"
        f"1. Scan QR code below\n"
        f"2. Pay exact amount\n"
        f"3. Upload screenshot\n\n"
        f"⚡ **Quick & Secure!**"
    )
    
    try:
        await cq.message.delete()
    except Exception:
        pass
    await safe_send_photo(cq.from_user.id, QR_CODE_URL, caption, reply_markup=kb_payment_options(plan_key))
    await cq.answer()

@dp.callback_query(F.data.startswith("pay:ask:"))
async def on_pay_ask(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[2]
    last_selected_plan[cq.from_user.id] = plan_key
    plan = PLANS[plan_key]
    
    text = (
        f"📸 **Upload Payment Proof**\n\n"
        f"🎯 **Plan:** {plan['emoji']} {plan['name']} - {plan['price']}\n\n"
        f"📋 **Requirements:**\n"
        f"• Clear screenshot\n"
        f"• Shows payment success\n"
        f"• Amount visible\n"
        f"• Transaction ID visible\n\n"
        f"📷 **Send screenshot as photo now:**"
    )
    
    await safe_edit_message(cq, text=text)
    await cq.answer("📸 Send payment screenshot!")

# Text and Photo handlers
@dp.message(F.text & ~F.command)
async def on_user_text(m: types.Message):
    if is_admin(m.from_user.id):
        return
    
    await upsert_user(m.from_user)
    tid = await add_ticket(m.from_user.id, m.text)
    
    # Get user status
    user = await get_user(m.from_user.id)
    is_premium = user and user.get("status") == "active"
    priority = "HIGH PRIORITY" if is_premium else "STANDARD"
    
    # Admin notification
    username = safe_text(m.from_user.username)
    first_name = safe_text(m.from_user.first_name)
    
    admin_message = (
        f"🎫 **Support Ticket #{tid}**\n"
        f"🔥 Priority: {priority}\n\n"
        f"👤 User: {first_name} (@{username})\n"
        f"🆔 ID: {m.from_user.id}\n"
        f"💎 Status: {'PREMIUM' if is_premium else 'FREE'}\n\n"
        f"💬 Message:\n{m.text}\n\n"
        f"📞 Reply: `/reply {m.from_user.id} Your message`"
    )
    
    try:
        await bot.send_message(ADMIN_ID, admin_message, parse_mode=ParseMode.MARKDOWN)
        
        # User confirmation
        confirm_text = (
            f"✅ **Support ticket created!**\n\n"
            f"🎫 Ticket ID: #{tid}\n"
            f"⏱️ Response time: {'2-5 min' if is_premium else '10-30 min'}\n\n"
            f"🔔 You'll be notified when we reply!"
        )
        await m.answer(confirm_text, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        log.error(f"Failed to send support ticket: {e}")
        await m.answer("❌ Error creating ticket. Please try again.")

@dp.message(F.photo)
async def on_payment_photo(m: types.Message):
    if is_admin(m.from_user.id):
        return
        
    plan_key = last_selected_plan.get(m.from_user.id)
    if not plan_key:
        await m.answer("❌ Please select a plan first using /start")
        return
    
    try:
        pid = await add_payment(m.from_user.id, plan_key, m.photo[-1].file_id)
        plan = PLANS[plan_key]
        
        # User confirmation with success image
        confirmation_text = (
            f"🎉 **Payment proof received!**\n\n"
            f"📸 Proof ID: #{pid}\n"
            f"📱 Plan: {plan['emoji']} {plan['name']}\n"
            f"💰 Amount: {plan['price']}\n\n"
            f"⏰ Processing time: 3-5 minutes\n"
            f"🔔 You'll be notified once approved!"
        )
        
        await safe_send_photo(m.from_user.id, SUCCESS_IMAGE, confirmation_text)
        
        # Admin notification
        username = safe_text(m.from_user.username)
        first_name = safe_text(m.from_user.first_name)
        
        admin_notification = (
            f"💰 **New Payment #{pid}**\n\n"
            f"👤 User: {first_name} (@{username})\n"
            f"🆔 ID: {m.from_user.id}\n"
            f"📱 Plan: {plan['emoji']} {plan['name']}\n"
            f"💵 Amount: {plan['price']}\n"
            f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}\n\n"
            f"🚀 **Action Required!**"
        )
        
        await bot.send_message(ADMIN_ID, admin_notification, parse_mode=ParseMode.MARKDOWN)
        await bot.send_photo(
            ADMIN_ID,
            m.photo[-1].file_id,
            caption=f"💳 Payment Proof #{pid}\n{plan['emoji']} {plan['name']} - {plan['price']}\nUser: {first_name} ({m.from_user.id})",
            reply_markup=kb_payment_actions(pid, m.from_user.id)
        )
        
    except Exception as e:
        log.error(f"Error processing payment photo: {e}")
        await m.answer("❌ Error processing screenshot. Please try again.")

# Admin handlers
@dp.callback_query(F.data == "admin:menu")
async def admin_menu(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    total, active, expired, pending = await get_stats()
    text = (
        f"🛠️ **Admin Panel**\n\n"
        f"📊 **Stats:**\n"
        f"👥 Total Users: {total}\n"
        f"✅ Active: {active}\n"
        f"❌ Expired: {expired}\n"
        f"⏳ Pending: {pending}\n\n"
        f"⚡ System Status: Online"
    )
    
    await cq.message.answer(text, reply_markup=kb_admin_menu(), parse_mode=ParseMode.MARKDOWN)
    await cq.answer()

@dp.callback_query(F.data == "admin:pending")
async def admin_pending(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    payments = await get_pending_payments(10)
    if not payments:
        await cq.message.answer("✅ No pending payments!")
        await cq.answer()
        return
    
    await cq.message.answer(f"⏳ Processing {len(payments)} pending payments...")
    
    for payment in payments:
        plan = PLANS[payment['plan_key']]
        text = (
            f"💵 **Payment #{str(payment['_id'])}**\n\n"
            f"👤 User ID: {payment['user_id']}\n"
            f"📱 Plan: {plan['emoji']} {plan['name']}\n"
            f"💰 Amount: {plan['price']}\n"
            f"⏰ Submitted: {payment['created_at'].strftime('%d %b, %H:%M')}\n\n"
            f"**Choose action:**"
        )
        
        await cq.message.answer(text, reply_markup=kb_payment_actions(str(payment['_id']), payment['user_id']), parse_mode=ParseMode.MARKDOWN)
    
    await cq.answer()

@dp.callback_query(F.data.startswith("admin:approve:"))
async def admin_approve(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    try:
        parts = cq.data.split(":")
        payment_id, user_id, plan_key = parts[2], int(parts[3]), parts[1]
        
        await set_payment_status(payment_id, "approved")
        _, end_date = await set_subscription(user_id, plan_key, PLANS[plan_key]["days"])
        plan = PLANS[plan_key]
        
        # Create invite link and notify user
        try:
            link = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
            user_notification = (
                f"🎉 **Payment Approved!**\n\n"
                f"✅ Subscription activated!\n"
                f"📱 Plan: {plan['emoji']} {plan['name']}\n"
                f"💰 Amount: {plan['price']}\n"
                f"⏰ Valid until: {end_date.strftime('%d %b %Y')}\n\n"
                f"🔗 **Join Premium Channel:**\n{link.invite_link}\n\n"
                f"🌟 Welcome to Premium!"
            )
            await safe_send_photo(user_id, SUCCESS_IMAGE, user_notification)
        except Exception as e:
            log.error(f"Error creating invite: {e}")
            user_notification = (
                f"🎉 **Payment Approved!**\n\n"
                f"✅ Subscription activated!\n"
                f"📱 Plan: {plan['emoji']} {plan['name']}\n"
                f"⏰ Valid until: {end_date.strftime('%d %b %Y')}\n\n"
                f"Contact admin for channel access."
            )
            await bot.send_message(user_id, user_notification, parse_mode=ParseMode.MARKDOWN)
        
        await cq.message.answer(f"✅ Payment #{payment_id} approved!", parse_mode=ParseMode.MARKDOWN)
        await cq.answer("✅ Approved!")
        
    except Exception as e:
        log.error(f"Error approving payment: {e}")
        await cq.answer("❌ Error processing approval!", show_alert=True)

@dp.callback_query(F.data.startswith("admin:deny:"))
async def admin_deny(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    try:
        parts = cq.data.split(":")
        payment_id, user_id = parts[2], int(parts[2])
        
        await set_payment_status(payment_id, "denied")
        
        user_message = (
            f"❌ **Payment proof not approved**\n\n"
            f"Proof #{payment_id} was denied.\n\n"
            f"**Common reasons:**\n"
            f"• Screenshot not clear\n"
            f"• Amount doesn't match\n"
            f"• Missing details\n\n"
            f"Please upload a clearer screenshot."
        )
        
        await bot.send_message(user_id, user_message, parse_mode=ParseMode.MARKDOWN)
        await cq.message.answer(f"❌ Payment #{payment_id} denied.", parse_mode=ParseMode.MARKDOWN)
        await cq.answer("❌ Denied!")
        
    except Exception as e:
        log.error(f"Error denying payment: {e}")
        await cq.answer("❌ Error processing denial!", show_alert=True)

@dp.callback_query(F.data == "admin:stats")
async def admin_stats(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    total, active, expired, pending = await get_stats()
    active_rate = (active/total*100) if total > 0 else 0
    
    text = (
        f"📊 **Analytics Dashboard**\n\n"
        f"👥 **Users:**\n"
        f"📈 Total: {total}\n"
        f"✅ Active: {active}\n"
        f"❌ Expired: {expired}\n"
        f"⏳ Pending: {pending}\n\n"
        f"📈 **Metrics:**\n"
        f"🎯 Active Rate: {active_rate:.1f}%\n\n"
        f"⏰ Generated: {datetime.now().strftime('%d %b, %H:%M')}"
    )
    
    await cq.message.answer(text, parse_mode=ParseMode.MARKDOWN)
    await cq.answer()

@dp.callback_query(F.data == "admin:users")
async def admin_users(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    cursor = users_col.find({}).sort("created_at", -1).limit(20)
    users = await cursor.to_list(length=20)
    
    if not users:
        await cq.message.answer("👥 No users found.")
        await cq.answer()
        return
    
    lines = ["👥 **Recent Users** (Top 20)\n"]
    
    for i, user in enumerate(users, 1):
        plan_info = PLANS.get(user.get("plan_key"), {"name": "None", "emoji": "⚪"})
        status = user.get("status", "none")
        username = safe_text(user.get("username"))
        
        status_emoji = "✅" if status == "active" else "❌" if status == "expired" else "⚪"
        
        lines.append(f"{i}. {status_emoji} {user['user_id']} (@{username})")
        lines.append(f"   Plan: {plan_info['name']} | Status: {status.upper()}\n")
    
    user_list = "\n".join(lines)
    
    if len(user_list) > 4000:
        await cq.message.answer(user_list[:4000] + "\n\n... **[List truncated]**", parse_mode=ParseMode.MARKDOWN)
    else:
        await cq.message.answer(user_list, parse_mode=ParseMode.MARKDOWN)
    
    await cq.answer()

@dp.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    total_users = await users_col.count_documents({})
    text = (
        f"📢 **Broadcast Center**\n\n"
        f"👥 Target: {total_users} users\n\n"
        f"✍️ **Send your broadcast message now:**"
    )
    
    await cq.message.answer(text, parse_mode=ParseMode.MARKDOWN)
    await state.set_state(BCast.waiting_text)
    await cq.answer()

@dp.message(BCast.waiting_text)
async def broadcast_send(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.clear()
        return
    
    cursor = users_col.find({}, {"user_id": 1})
    users = await cursor.to_list(length=None)
    
    if not users:
        await m.answer("❌ No users to broadcast to.")
        await state.clear()
        return
    
    await m.answer(f"📤 Broadcasting to {len(users)} users...")
    
    sent = failed = 0
    
    for user in users:
        try:
            broadcast_msg = f"📢 **Official Announcement**\n\n{m.text}\n\n─────────────\n💎 Premium Bot Team"
            await bot.send_message(user["user_id"], broadcast_msg, parse_mode=ParseMode.MARKDOWN)
            sent += 1
            await asyncio.sleep(0.05)  # Rate limit
        except Exception:
            failed += 1
    
    report = f"📢 **Broadcast Complete!**\n\n✅ Sent: {sent}\n❌ Failed: {failed}\n📈 Success: {(sent/(sent+failed)*100):.1f}%"
    await m.answer(report, parse_mode=ParseMode.MARKDOWN)
    await state.clear()

@dp.message(Command("reply"))
async def admin_reply(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    
    try:
        parts = m.text.split(maxsplit=2)
        if len(parts) < 3:
            await m.answer("❌ Usage: `/reply <user_id> <message>`")
            return
        
        user_id, reply_text = int(parts[1]), parts[3]
        
        user_msg = f"💬 **Support Response**\n\n{reply_text}\n\n─────────────\n🎧 Premium Support Team"
        await bot.send_message(user_id, user_msg, parse_mode=ParseMode.MARKDOWN)
        await m.answer(f"✅ Reply sent to user {user_id}")
        
    except ValueError:
        await m.answer("❌ Invalid user ID")
    except Exception as e:
        log.error(f"Error sending reply: {e}")
        await m.answer("❌ Error sending reply")

# Expiry worker
async def expiry_worker():
    while True:
        try:
            now = datetime.now(timezone.utc)
            
            # Find users with active subscriptions
            cursor = users_col.find({"status": {"$in": ["active", "expired"]}})
            users = await cursor.to_list(length=None)
            
            for user in users:
                user_id = user["user_id"]
                status = user.get("status")
                end_at = user.get("end_at")
                reminded = user.get("reminded_3d", False)
                
                if not end_at:
                    continue
                
                # 3-day reminder
                if (status == "active" and not reminded and 
                    end_at > now and (end_at - now) <= timedelta(days=3)):
                    
                    try:
                        days_left = (end_at - now).days
                        reminder_msg = (
                            f"⏰ **Subscription Expiry Reminder**\n\n"
                            f"Your subscription expires in {days_left} day(s)!\n"
                            f"📅 Expiry: {end_at.strftime('%d %b %Y')}\n\n"
                            f"🔄 Renew now: /start"
                        )
                        
                        await bot.send_message(user_id, reminder_msg, parse_mode=ParseMode.MARKDOWN)
                        await users_col.update_one({"user_id": user_id}, {"$set": {"reminded_3d": True}})
                        
                    except Exception as e:
                        log.error(f"Failed to send reminder to {user_id}: {e}")
                
                # Handle expired subscriptions
                if end_at <= now and status != "expired":
                    try:
                        await users_col.update_one({"user_id": user_id}, {"$set": {"status": "expired"}})
                        
                        # Remove from channel
                        try:
                            await bot.ban_chat_member(CHANNEL_ID, user_id)
                            await bot.unban_chat_member(CHANNEL_ID, user_id)
                        except Exception:
                            pass
                        
                        expiry_msg = (
                            f"❌ **Subscription Expired**\n\n"
                            f"Your subscription has expired.\n\n"
                            f"🔄 Renew: /start\n"
                            f"💎 We miss you!"
                        )
                        
                        await bot.send_message(user_id, expiry_msg, parse_mode=ParseMode.MARKDOWN)
                        
                    except Exception as e:
                        log.error(f"Failed to process expiry for {user_id}: {e}")
        
        except Exception as e:
            log.exception(f"Error in expiry worker: {e}")
        
        # Wait 30 minutes
        await asyncio.sleep(1800)

# Main function
async def main():
    log.info("🚀 Starting Premium Subscription Bot with MongoDB")
    
    # Test MongoDB connection
    try:
        await mongo_client.admin.command('ping')
        log.info("✅ MongoDB connected successfully")
    except Exception as e:
        log.error(f"❌ MongoDB connection failed: {e}")
        raise
    
    # Start expiry worker
    asyncio.create_task(expiry_worker())
    log.info("✅ Expiry worker started")
    
    # Start bot
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    if not all([API_TOKEN != "TEST_TOKEN", ADMIN_ID, CHANNEL_ID]):
        raise RuntimeError("❌ Missing required environment variables")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("✅ Bot stopped gracefully")
