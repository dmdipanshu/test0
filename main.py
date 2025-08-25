import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict
from bson import ObjectId
from bson.errors import InvalidId

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

# Database Helper Functions
async def upsert_user(user: types.User):
    try:
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
    except Exception as e:
        log.error(f"Error upserting user: {e}")

async def get_user(user_id: int) -> Optional[dict]:
    try:
        return await users_col.find_one({"user_id": user_id})
    except Exception as e:
        log.error(f"Error getting user: {e}")
        return None

async def set_subscription(user_id: int, plan_key: str, days: int):
    try:
        now = datetime.now(timezone.utc)
        end_date = now + timedelta(days=days)
        
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
    except Exception as e:
        log.error(f"Error setting subscription: {e}")
        return None, None

async def add_payment(user_id: int, plan_key: str, file_id: str):
    try:
        result = await payments_col.insert_one({
            "user_id": user_id,
            "plan_key": plan_key,
            "file_id": file_id,
            "created_at": datetime.now(timezone.utc),
            "status": "pending"
        })
        return str(result.inserted_id)
    except Exception as e:
        log.error(f"Error adding payment: {e}")
        raise

async def set_payment_status(payment_id: str, status: str):
    try:
        await payments_col.update_one(
            {"_id": ObjectId(payment_id)},
            {"$set": {"status": status}}
        )
        log.info(f"Payment {payment_id} status updated to {status}")
    except Exception as e:
        log.error(f"Error setting payment status: {e}")

async def get_payment(payment_id: str):
    try:
        return await payments_col.find_one({"_id": ObjectId(payment_id)})
    except Exception as e:
        log.error(f"Error getting payment: {e}")
        return None

async def add_ticket(user_id: int, message: str):
    try:
        result = await tickets_col.insert_one({
            "user_id": user_id,
            "message": message,
            "status": "open",
            "created_at": datetime.now(timezone.utc)
        })
        return str(result.inserted_id)
    except Exception as e:
        log.error(f"Error adding ticket: {e}")
        return "error"

async def get_stats():
    try:
        total = await users_col.count_documents({})
        active = await users_col.count_documents({"status": "active"})
        expired = await users_col.count_documents({"status": "expired"})
        pending = await payments_col.count_documents({"status": "pending"})
        return total, active, expired, pending
    except Exception as e:
        log.error(f"Error getting stats: {e}")
        return 0, 0, 0, 0

# UI Helper Functions
async def safe_send_photo(chat_id: int, photo_url: str, caption: str, reply_markup=None):
    try:
        await bot.send_photo(chat_id, photo_url, caption=caption, reply_markup=reply_markup)
    except Exception as e:
        log.warning(f"Failed to send photo: {e}")
        try:
            await bot.send_message(chat_id, caption, reply_markup=reply_markup)
        except Exception as e2:
            log.error(f"Failed to send message fallback: {e2}")

async def safe_edit_or_send(cq: types.CallbackQuery, text: str = None, photo_url: str = None, reply_markup=None):
    try:
        if photo_url:
            await cq.message.delete()
            await safe_send_photo(cq.from_user.id, photo_url, text, reply_markup)
        elif text:
            await cq.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        try:
            if photo_url:
                await safe_send_photo(cq.from_user.id, photo_url, text, reply_markup)
            else:
                await cq.message.answer(text, reply_markup=reply_markup)
        except Exception as e:
            log.error(f"Failed to send fallback message: {e}")

# Keyboard Functions
def kb_user_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🚀 Upgrade Premium", callback_data="menu_buy")],
        [InlineKeyboardButton(text="📊 My Subscription", callback_data="menu_my"),
         InlineKeyboardButton(text="💬 Support", callback_data="menu_support")],
        [InlineKeyboardButton(text="🎁 Special Offers", callback_data="menu_offers")]
    ]
    
    if is_admin:
        buttons.append([InlineKeyboardButton(text="🛠 Admin Panel", callback_data="admin_menu")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_plans() -> InlineKeyboardMarkup:
    buttons = []
    for plan_key, plan in PLANS.items():
        text = f"{plan['emoji']} {plan['name']} - {plan['price']}"
        if plan.get("popular"):
            text += " ⭐"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"plan_{plan_key}")])
    
    buttons.append([InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_payment_options(plan_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 UPI Payment", callback_data=f"upi_{plan_key}"),
            InlineKeyboardButton(text="📱 QR Code", callback_data=f"qr_{plan_key}")
        ],
        [InlineKeyboardButton(text="📸 Upload Payment Proof", callback_data=f"upload_{plan_key}")],
        [
            InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="menu_buy"),
            InlineKeyboardButton(text="🏠 Main Menu", callback_data="back_menu")
        ]
    ])

def kb_payment_actions(payment_id: str, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve Payment", callback_data=f"approve_{payment_id}_{user_id}"),
        ],
        [
            InlineKeyboardButton(text="❌ Deny Payment", callback_data=f"deny_{payment_id}_{user_id}")
        ]
    ])

def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏳ Pending Payments", callback_data="admin_pending"),
            InlineKeyboardButton(text="📊 Statistics", callback_data="admin_stats")
        ],
        [
            InlineKeyboardButton(text="👥 All Users", callback_data="admin_users"),
            InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast")
        ],
        [InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back_menu")]
    ])

# Bot Handlers
@dp.message(CommandStart())
async def cmd_start(m: types.Message):
    await upsert_user(m.from_user)
    caption = f"👋 **Hello {m.from_user.first_name}!**\n\n🌟 **Upgrade to Premium:**\n• Unlimited downloads\n• Ad-free experience\n• Priority support\n• High-speed access\n\n🚀 **Ready to upgrade?**"
    await safe_send_photo(m.from_user.id, WELCOME_IMAGE, caption, reply_markup=kb_user_menu())

@dp.callback_query(F.data == "back_menu")
async def back_to_menu(cq: types.CallbackQuery):
    caption = f"🏠 **Welcome back {cq.from_user.first_name}!**\n\nChoose an option below:"
    await safe_edit_or_send(cq, text=caption, photo_url=WELCOME_IMAGE, reply_markup=kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data == "menu_buy")
async def on_buy(cq: types.CallbackQuery):
    caption = "💎 **Premium Plans**\n\nChoose your subscription plan:"
    await safe_edit_or_send(cq, text=caption, photo_url=PLANS_IMAGE, reply_markup=kb_plans())
    await cq.answer()

@dp.callback_query(F.data == "menu_offers")
async def show_offers(cq: types.CallbackQuery):
    caption = "🎁 **Special Offers**\n\n🟡 **6 Months:** Save 33%\n🔥 **1 Year:** Best Value\n💎 **Lifetime:** One-time payment\n\n⏰ **Limited time offers!**"
    await safe_edit_or_send(cq, text=caption, photo_url=OFFERS_IMAGE, reply_markup=kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data == "menu_my")
async def on_my_plan(cq: types.CallbackQuery):
    user = await get_user(cq.from_user.id)
    
    if not user or user.get("status") != "active":
        caption = "😔 **No Active Subscription**\n\nYou're using the FREE version.\n\n🌟 **Upgrade benefits:**\n• Unlimited access\n• No advertisements\n• Priority support\n• Premium features\n\n👆 **Ready to upgrade?**"
        await safe_edit_or_send(cq, text=caption, photo_url=UPGRADE_IMAGE, reply_markup=kb_user_menu())
    else:
        plan_info = PLANS.get(user.get('plan_key'), {'name': 'Unknown', 'emoji': '📦'})
        
        # Calculate remaining time
        if user.get('end_at'):
            try:
                end_date = user['end_at']
                now = datetime.now(timezone.utc)
                time_left = end_date - now
                
                if time_left.days > 0:
                    time_display = f"{time_left.days} days, {time_left.seconds // 3600} hours"
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
        
        caption = f"📊 **My Subscription**\n\n{status_emoji} **Status:** {status_text}\n{plan_info['emoji']} **Plan:** {plan_info['name']}\n⏳ **Time Left:** {time_display}\n\n🎉 **Premium Benefits Active!**"
        
        await safe_edit_or_send(cq, text=caption, reply_markup=kb_user_menu())
    
    await cq.answer()

@dp.callback_query(F.data == "menu_support")
async def on_support(cq: types.CallbackQuery):
    text = f"💬 **Customer Support**\n\nHi {cq.from_user.first_name}!\n\n📝 **Need help?**\nJust type your message and our support team will respond quickly!\n\n⚡ **Response time:** 5-30 minutes"
    await safe_edit_or_send(cq, text=text, reply_markup=kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data.startswith("plan_"))
async def on_plan(cq: types.CallbackQuery):
    plan_key = cq.data.replace("plan_", "")
    last_selected_plan[cq.from_user.id] = plan_key
    plan = PLANS[plan_key]
    
    daily_cost = float(plan["price"].replace("₹", "")) / plan["days"]
    
    caption = f"🎯 **{plan['emoji']} {plan['name']} Plan**\n\n💰 **Price:** {plan['price']}\n⏰ **Duration:** {plan['days']} days\n📊 **Daily Cost:** ₹{daily_cost:.2f}/day\n\n💳 **Choose Payment Method:**"
    
    await safe_edit_or_send(cq, text=caption, reply_markup=kb_payment_options(plan_key))
    await cq.answer()

@dp.callback_query(F.data.startswith("upi_"))
async def copy_upi(cq: types.CallbackQuery):
    plan_key = cq.data.replace("upi_", "")
    plan = PLANS[plan_key]
    amount_only = plan['price'].replace('₹', '')
    
    msg = f"💳 **UPI Payment**\n\n🎯 **Plan:** {plan['emoji']} {plan['name']}\n💰 **Amount:** {plan['price']}\n\n📱 **Quick Steps:**\n1. Copy UPI ID from message below\n2. Open any UPI app (GPay/PhonePe/Paytm)\n3. Paste UPI ID and pay exactly {amount_only}\n4. Upload screenshot after payment"
    
    await safe_edit_or_send(cq, text=msg, reply_markup=kb_payment_options(plan_key))
    
    # Send separate copyable UPI ID message
    upi_msg = f"📋 **UPI ID (Long press to copy):**\n\n`{UPI_ID}`\n\n💰 **Amount:** {amount_only}\n\n📸 After payment, upload screenshot here!"
    
    try:
        await bot.send_message(cq.from_user.id, upi_msg, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await bot.send_message(cq.from_user.id, upi_msg.replace('`', '').replace('*', ''))
    
    await cq.answer("💳 UPI ID sent below! Long press to copy and pay in your app.", show_alert=True)

@dp.callback_query(F.data.startswith("qr_"))
async def show_qr(cq: types.CallbackQuery):
    plan_key = cq.data.replace("qr_", "")
    plan = PLANS[plan_key]
    
    caption = f"📱 **QR Code Payment**\n\n🎯 **Plan:** {plan['emoji']} {plan['name']}\n💰 **Amount:** {plan['price']}\n\n📸 **Instructions:**\n1. Scan QR code below\n2. Pay exact amount\n3. Upload screenshot\n\n⚡ **Quick & Secure!**"
    
    await safe_edit_or_send(cq, text=caption, photo_url=QR_CODE_URL, reply_markup=kb_payment_options(plan_key))
    await cq.answer()

@dp.callback_query(F.data.startswith("upload_"))
async def on_pay_ask(cq: types.CallbackQuery):
    plan_key = cq.data.replace("upload_", "")
    last_selected_plan[cq.from_user.id] = plan_key
    plan = PLANS[plan_key]
    
    text = f"📸 **Upload Payment Proof**\n\n🎯 **Plan:** {plan['emoji']} {plan['name']} - {plan['price']}\n\n📋 **Requirements:**\n• Clear screenshot\n• Shows payment success\n• Amount visible\n• Transaction ID visible\n\n📷 **Send screenshot as photo now:**"
    
    await safe_edit_or_send(cq, text=text)
    await cq.answer("📸 Send payment screenshot!")

# Text and Photo handlers
@dp.message(F.text & ~F.command)
async def on_user_text(m: types.Message):
    if is_admin(m.from_user.id):
        return
    
    await upsert_user(m.from_user)
    
    # Get user status for priority
    user_info = await get_user(m.from_user.id)
    is_premium = user_info and user_info.get("status") == "active"
    priority = "HIGH PRIORITY" if is_premium else "STANDARD"
    
    username = safe_text(m.from_user.username)
    first_name = safe_text(m.from_user.first_name)
    
    tid = await add_ticket(m.from_user.id, m.text)
    
    admin_message = f"🎫 **Support Ticket #{tid}**\n🔥 **Priority:** {priority}\n\n👤 **User:** {first_name} (@{username})\n🆔 **ID:** {m.from_user.id}\n💎 **Status:** {'PREMIUM' if is_premium else 'FREE'}\n\n💬 **Message:**\n{m.text}\n\n📞 **Reply:** `/reply {m.from_user.id} Your message`"
    
    try:
        await bot.send_message(ADMIN_ID, admin_message, parse_mode=ParseMode.MARKDOWN)
        
        confirm_text = f"✅ **Support ticket created!**\n\n🎫 **Ticket ID:** #{tid}\n🔥 **Priority:** {priority}\n⏱️ **Response time:** {'2-5 min' if is_premium else '10-30 min'}\n\n🔔 **You'll be notified when we reply!**"
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
        log.info(f"Processing payment photo for user {m.from_user.id}, plan {plan_key}")
        pid = await add_payment(m.from_user.id, plan_key, m.photo[-1].file_id)
        log.info(f"Payment added with ID: {pid}")
        
        plan = PLANS[plan_key]
        
        # Send confirmation to user
        confirmation_text = f"🎉 **Payment proof received!**\n\n📸 **Proof ID:** #{pid}\n📱 **Plan:** {plan['emoji']} {plan['name']}\n💰 **Amount:** {plan['price']}\n\n⏰ **Processing time:** 3-5 minutes\n🔔 **You'll be notified once approved!**"
        
        try:
            await bot.send_photo(m.from_user.id, SUCCESS_IMAGE, caption=confirmation_text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await m.answer(confirmation_text, parse_mode=ParseMode.MARKDOWN)
        
        # Notify admin
        username = safe_text(m.from_user.username)
        first_name = safe_text(m.from_user.first_name)
        
        admin_notification = f"💰 **New Payment #{pid}**\n\n👤 **User:** {first_name} (@{username})\n🆔 **ID:** {m.from_user.id}\n📱 **Plan:** {plan['emoji']} {plan['name']}\n💵 **Amount:** {plan['price']}\n⏰ **Time:** {datetime.now().strftime('%H:%M:%S')}"
        
        await bot.send_message(ADMIN_ID, admin_notification, parse_mode=ParseMode.MARKDOWN)
        await bot.send_photo(
            ADMIN_ID,
            m.photo[-1].file_id,
            caption=f"💳 **Payment Proof #{pid}**\n{plan['emoji']} {plan['name']} - {plan['price']}\n**User:** {first_name} ({m.from_user.id})",
            reply_markup=kb_payment_actions(pid, m.from_user.id),
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        log.error(f"Error processing payment photo: {e}")
        await m.answer("❌ Error processing screenshot. Please try uploading again.")

# Admin handlers
@dp.callback_query(F.data == "admin_menu")
async def admin_menu(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    total, active, expired, pending = await get_stats()
    text = f"🛠️ **Admin Control Panel**\n\n📊 **Live Statistics:**\n👥 Total Users: **{total}**\n✅ Active Subs: **{active}**\n❌ Expired: **{expired}**\n⏳ Pending: **{pending}**\n\n⚡ **System Status:** Online\n🔄 **Last Updated:** {datetime.now().strftime('%H:%M:%S')}"
    
    await cq.message.answer(text, reply_markup=kb_admin_menu(), parse_mode=ParseMode.MARKDOWN)
    await cq.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    total, active, expired, pending = await get_stats()
    active_rate = (active/total*100) if total > 0 else 0
    conversion_rate = ((active + expired)/total*100) if total > 0 else 0
    
    text = f"📊 **Comprehensive Analytics**\n\n👥 **User Statistics:**\n📈 Total Users: **{total}**\n✅ Active Subscriptions: **{active}**\n❌ Expired Subscriptions: **{expired}**\n⏳ Pending Payments: **{pending}**\n\n📈 **Performance Metrics:**\n🎯 Active Rate: **{active_rate:.1f}%**\n💰 Conversion Rate: **{conversion_rate:.1f}%**\n📊 Retention: **{(active/(active+expired)*100) if (active+expired) > 0 else 0:.1f}%**\n\n⏰ **Report Generated:** {datetime.now().strftime('%d %b %Y, %H:%M:%S')}"
    
    await cq.message.answer(text, parse_mode=ParseMode.MARKDOWN)
    await cq.answer()

@dp.callback_query(F.data == "admin_pending")
async def admin_pending(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    try:
        cursor = payments_col.find({"status": "pending"}).sort("created_at", -1).limit(10)
        payments = await cursor.to_list(length=10)
        
        if not payments:
            await cq.message.answer("✅ **No pending payments!**\n\nAll payments have been processed.")
            await cq.answer()
            return
        
        await cq.message.answer(f"⏳ **Processing {len(payments)} pending payment(s)**\n\nLoading payment details...")
        
        for payment in payments:
            plan = PLANS[payment['plan_key']]
            
            payment_details = f"💵 **Payment Review #{str(payment['_id'])}**\n\n👤 **User ID:** {payment['user_id']}\n📱 **Plan:** {plan['emoji']} {plan['name']}\n💰 **Amount:** {plan['price']}\n⏰ **Submitted:** {payment['created_at'].strftime('%d %b, %H:%M')}\n🔍 **Status:** ⏳ PENDING REVIEW\n\n**👆 Choose action below:**"
            
            await cq.message.answer(payment_details, reply_markup=kb_payment_actions(str(payment['_id']), payment['user_id']), parse_mode=ParseMode.MARKDOWN)
        
        await cq.answer(f"📋 {len(payments)} payments ready for review!")
        
    except Exception as e:
        log.error(f"Error getting pending payments: {e}")
        await cq.answer("❌ Error loading payments!", show_alert=True)

@dp.callback_query(F.data.startswith("approve_"))
async def admin_approve(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    try:
        parts = cq.data.split("_")
        if len(parts) != 3:
            await cq.answer("❌ Invalid callback data!", show_alert=True)
            return
            
        payment_id, user_id_str = parts[1], parts[2]
        user_id = int(user_id_str)
        
        # Get payment details to find the plan
        payment = await get_payment(payment_id)
        if not payment:
            await cq.answer("❌ Payment not found!", show_alert=True)
            return
        
        plan_key = payment["plan_key"]
        plan = PLANS[plan_key]
        
        log.info(f"Processing approval: payment_id={payment_id}, user_id={user_id}, plan_key={plan_key}")
        
        await set_payment_status(payment_id, "approved")
        await set_subscription(user_id, plan_key, plan["days"])
        
        # Create invite link
        try:
            link = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
            user_msg = f"🎉 **PAYMENT APPROVED!**\n\n✅ Your **{plan['emoji']} {plan['name']}** subscription is now **ACTIVE**!\n💰 **Amount:** {plan['price']}\n⏰ **Valid for:** {plan['days']} days\n\n🔗 **Join Premium Channel:**\n{link.invite_link}\n\n🌟 **Welcome to Premium Family!**\nEnjoy unlimited access to all premium features! 🚀"
        except Exception as e:
            log.error(f"Error creating invite link: {e}")
            user_msg = f"🎉 **PAYMENT APPROVED!**\n\n✅ Your **{plan['emoji']} {plan['name']}** subscription is now **ACTIVE**!\n💰 **Amount:** {plan['price']}\n⏰ **Valid for:** {plan['days']} days\n\n🌟 **Welcome to Premium!**\nContact admin for channel access."
        
        await bot.send_message(user_id, user_msg, parse_mode=ParseMode.MARKDOWN)
        
        try:
            await cq.message.edit_text(f"✅ **Payment #{payment_id} APPROVED**\n\n{plan['emoji']} **{plan['name']}** activated for user **{user_id}**!", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await cq.message.answer(f"✅ **Payment #{payment_id} APPROVED**\n\n{plan['emoji']} **{plan['name']}** activated for user **{user_id}**!", parse_mode=ParseMode.MARKDOWN)
        
        await cq.answer("✅ Approved and activated!")
        
    except Exception as e:
        log.error(f"Error approving payment: {e}")
        await cq.answer("❌ Error processing approval!", show_alert=True)

@dp.callback_query(F.data.startswith("deny_"))
async def admin_deny(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    try:
        parts = cq.data.split("_")
        if len(parts) != 3:
            await cq.answer("❌ Invalid callback data!", show_alert=True)
            return
            
        payment_id, user_id_str = parts[1], parts[2]
        user_id = int(user_id_str)
        
        log.info(f"Processing denial: payment_id={payment_id}, user_id={user_id}")
        
        await set_payment_status(payment_id, "denied")
        
        user_msg = f"❌ **Payment Proof Not Approved**\n\nYour payment screenshot for proof **#{payment_id}** could not be approved.\n\n🔍 **Common reasons:**\n• Screenshot not clear enough\n• Amount doesn't match plan price\n• Payment status not visible\n• Transaction details missing\n\n🔄 **What to do:**\n1. Take a clearer screenshot\n2. Ensure all details are visible\n3. Upload again\n\n💬 **Need help?** Contact support!"
        
        await bot.send_message(user_id, user_msg, parse_mode=ParseMode.MARKDOWN)
        
        try:
            await cq.message.edit_text(f"❌ **Payment #{payment_id} DENIED**\n\nUser **{user_id}** has been notified with improvement suggestions.", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await cq.message.answer(f"❌ **Payment #{payment_id} DENIED**\n\nUser **{user_id}** has been notified with improvement suggestions.", parse_mode=ParseMode.MARKDOWN)
        
        await cq.answer("❌ Denied with feedback sent!")
        
    except Exception as e:
        log.error(f"Error denying payment: {e}")
        await cq.answer("❌ Error processing denial!", show_alert=True)

@dp.callback_query(F.data == "admin_users")
async def admin_users(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    try:
        cursor = users_col.find({}).sort("created_at", -1).limit(50)
        users = await cursor.to_list(length=50)
        
        if not users:
            await cq.message.answer("👥 **No users found**\n\nThe bot hasn't been used yet.")
            await cq.answer()
            return
        
        lines = [f"👥 **User Management** (Top 50)\n"]
        active_count = 0
        expired_count = 0
        
        for i, user in enumerate(users, 1):
            plan_info = PLANS.get(user.get("plan_key"), {"name": "None", "emoji": "⚪"})
            plan_name = plan_info["name"] if user.get("plan_key") else "None"
            username = safe_text(user.get('username'))
            
            if user.get('status') == "active":
                status_emoji = "✅"
                active_count += 1
            elif user.get('status') == "expired":
                status_emoji = "❌"
                expired_count += 1
            else:
                status_emoji = "⚪"
            
            lines.append(f"{i}. {status_emoji} **{user['user_id']}** (@{username})")
            lines.append(f"   📱 Plan: {plan_name}")
            lines.append(f"   📊 Status: {user.get('status', 'none').upper()}")
            if user.get('end_at'):
                lines.append(f"   ⏰ Expires: {user['end_at'].strftime('%d %b %Y')}\n")
            else:
                lines.append("   ⏰ Expires: Never\n")
        
        lines.insert(1, f"📊 Active: {active_count} | Expired: {expired_count}\n")
        
        user_list = "\n".join(lines)
        
        if len(user_list) > 4000:
            await cq.message.answer(user_list[:4000] + "\n\n... **[List truncated]**", parse_mode=ParseMode.MARKDOWN)
        else:
            await cq.message.answer(user_list, parse_mode=ParseMode.MARKDOWN)
        
        await cq.answer(f"📋 Showing {len(users)} users")
        
    except Exception as e:
        log.error(f"Error getting users: {e}")
        await cq.answer("❌ Error loading users!", show_alert=True)

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    total_users, _, _, _ = await get_stats()
    text = f"📢 **Broadcast Message Center**\n\n👥 **Target Audience:** {total_users} users\n📡 **Delivery Method:** Direct message\n⚡ **Estimated Time:** {total_users * 0.05:.1f} seconds\n\n✍️ **Send your broadcast message now:**"
    
    await cq.message.answer(text, parse_mode=ParseMode.MARKDOWN)
    await state.set_state(BCast.waiting_text)
    await cq.answer("📢 Ready for broadcast message!")

@dp.message(BCast.waiting_text)
async def broadcast_send(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.clear()
        return
    
    cursor = users_col.find({}, {"user_id": 1})
    users = await cursor.to_list(length=None)
    
    if not users:
        await m.answer("❌ **NO USERS TO BROADCAST TO**")
        await state.clear()
        return
    
    await m.answer(f"📤 **BROADCASTING TO {len(users)} USERS...**")
    
    sent = failed = 0
    
    for user in users:
        try:
            broadcast_message = f"📢 **OFFICIAL ANNOUNCEMENT**\n\n{m.text}\n\n───────────────────\n💎 **Premium Bot Team**"
            await bot.send_message(user["user_id"], broadcast_message, parse_mode=ParseMode.MARKDOWN)
            sent += 1
            await asyncio.sleep(0.05)  # Rate limiting
        except Exception:
            failed += 1
    
    final_report = f"📢 **BROADCAST COMPLETED!**\n\n✅ **Successfully Sent:** {sent}\n❌ **Failed:** {failed}\n📈 **Success Rate:** {(sent/(sent+failed)*100) if (sent+failed) > 0 else 0:.1f}%"
    
    await m.answer(final_report, parse_mode=ParseMode.MARKDOWN)
    await state.clear()

@dp.message(Command("reply"))
async def admin_reply(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    
    try:
        parts = m.text.split(maxsplit=2)
        if len(parts) < 3:
            await m.answer("❌ **Usage:** `/reply <user_id> <message>`")
            return
        
        user_id, reply_text = int(parts[1]), parts[2]
        
        user_msg = f"💬 **Support Response**\n\n{reply_text}\n\n─────────────\n🎧 **Premium Support Team**\n💬 **Need more help?** Just reply to this message!"
        await bot.send_message(user_id, user_msg, parse_mode=ParseMode.MARKDOWN)
        await m.answer(f"✅ **REPLY SENT TO USER {user_id}**")
        
    except ValueError:
        await m.answer("❌ **INVALID USER ID**")
    except Exception as e:
        log.error(f"Error sending reply: {e}")
        await m.answer("❌ **ERROR SENDING REPLY**")

# Expiry worker
async def expiry_worker():
    """Enhanced background worker for subscription management"""
    while True:
        try:
            now = datetime.now(timezone.utc)
            
            cursor = users_col.find({"status": {"$in": ["active", "expired"]}})
            users = await cursor.to_list(length=None)
            
            for user in users:
                user_id = user["user_id"]
                status = user.get("status")
                end_at = user.get("end_at")
                reminded = user.get("reminded_3d", False)
                
                if not end_at:
                    continue
                
                try:
                    end_date = end_at
                except Exception:
                    continue
                
                # 3-day expiry reminder
                if (status == "active" and not reminded and 
                    end_date > now and (end_date - now) <= timedelta(days=3)):
                    
                    try:
                        days_left = (end_date - now).days
                        
                        reminder_message = f"⏰ **SUBSCRIPTION EXPIRY REMINDER**\n\nYour premium subscription expires in **{days_left}** day(s)!\n\n📅 **Expiry Date:** {end_date.strftime('%d %b %Y, %H:%M')}\n\n🔄 **Renew now to continue enjoying premium features!**\n🚀 **Use /start to renew now!**"
                        
                        await bot.send_message(user_id, reminder_message, parse_mode=ParseMode.MARKDOWN)
                        
                        # Mark as reminded
                        await users_col.update_one({"user_id": user_id}, {"$set": {"reminded_3d": True}})
                        
                        log.info(f"Sent 3-day reminder to user {user_id}")
                        
                    except Exception as e:
                        log.error(f"Failed to send reminder to user {user_id}: {e}")
                
                # Handle expired subscriptions
                if end_date <= now and status != "expired":
                    try:
                        # Update status
                        await users_col.update_one({"user_id": user_id}, {"$set": {"status": "expired"}})
                        
                        # Remove from channel
                        try:
                            await bot.ban_chat_member(CHANNEL_ID, user_id)
                            await bot.unban_chat_member(CHANNEL_ID, user_id)
                        except Exception as e:
                            log.error(f"Failed to remove user {user_id} from channel: {e}")
                        
                        # Notify user
                        expiry_message = f"❌ **SUBSCRIPTION EXPIRED**\n\nYour premium subscription has expired.\n\n🔄 **To renew:**\n   1️⃣ Use /start to see plans\n   2️⃣ Choose your plan\n   3️⃣ Complete payment\n   4️⃣ Get instant access back!\n\n💎 **We miss you! Come back to premium!**"
                        
                        await bot.send_message(user_id, expiry_message, parse_mode=ParseMode.MARKDOWN)
                        log.info(f"Processed expiry for user {user_id}")
                        
                    except Exception as e:
                        log.error(f"Failed to process expiry for user {user_id}: {e}")
        
        except Exception as e:
            log.exception(f"Error in expiry_worker: {e}")
        
        # Wait 30 minutes before next check
        await asyncio.sleep(1800)

# Main function
async def main():
    """Enhanced main function"""
    try:
        # Test MongoDB connection
        await mongo_client.admin.command('ping')
        log.info("✅ MongoDB connected successfully")
        
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
    if not all([API_TOKEN != "TEST_TOKEN", ADMIN_ID]):
        raise RuntimeError("❌ Missing required environment variables")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("✅ Bot stopped gracefully")
    except Exception as e:
        log.error(f"❌ Bot crashed: {e}")
        raise
