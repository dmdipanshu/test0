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
mongo_client = None
db = None
users_col = None
payments_col = None
tickets_col = None

try:
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    db = mongo_client['premium_bot']
    users_col = db['users']
    payments_col = db['payments']
    tickets_col = db['tickets']
    log.info("MongoDB collections initialized")
except Exception as e:
    log.error(f"MongoDB setup failed: {e}")

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

# In-memory fallback storage
memory_users = {}
memory_payments = {}
payment_counter = 0

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
        if users_col is not None:
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
        else:
            if user.id not in memory_users:
                memory_users[user.id] = {
                    "user_id": user.id,
                    "username": user.username,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "status": "none",
                    "created_at": datetime.now(timezone.utc)
                }
    except Exception as e:
        log.error(f"Error upserting user: {e}")

async def get_user(user_id: int) -> Optional[dict]:
    try:
        if users_col is not None:
            return await users_col.find_one({"user_id": user_id})
        else:
            return memory_users.get(user_id)
    except Exception as e:
        log.error(f"Error getting user: {e}")
        return None

async def set_subscription(user_id: int, plan_key: str, days: int):
    try:
        now = datetime.now(timezone.utc)
        end_date = now + timedelta(days=days)
        
        if users_col is not None:
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
        else:
            if user_id in memory_users:
                memory_users[user_id].update({
                    "plan_key": plan_key,
                    "start_at": now,
                    "end_at": end_date,
                    "status": "active"
                })
        
        return now, end_date
    except Exception as e:
        log.error(f"Error setting subscription: {e}")
        return None, None

async def add_payment(user_id: int, plan_key: str, file_id: str):
    global payment_counter
    try:
        if payments_col is not None:
            result = await payments_col.insert_one({
                "user_id": user_id,
                "plan_key": plan_key,
                "file_id": file_id,
                "created_at": datetime.now(timezone.utc),
                "status": "pending"
            })
            return str(result.inserted_id)
        else:
            payment_counter += 1
            payment_id = str(payment_counter)
            memory_payments[payment_id] = {
                "_id": payment_id,
                "user_id": user_id,
                "plan_key": plan_key,
                "file_id": file_id,
                "created_at": datetime.now(timezone.utc),
                "status": "pending"
            }
            return payment_id
    except Exception as e:
        log.error(f"Error adding payment: {e}")
        raise

# Fixed payment status function with better error handling
async def set_payment_status(payment_id: str, status: str):
    try:
        if payments_col is not None:
            # Try to convert to ObjectId, handle both MongoDB ObjectId strings and simple strings
            try:
                # Check if it's a valid ObjectId
                if len(payment_id) == 24:
                    object_id = ObjectId(payment_id)
                    result = await payments_col.update_one(
                        {"_id": object_id},
                        {"$set": {"status": status}}
                    )
                else:
                    # Fallback for non-ObjectId strings
                    result = await payments_col.update_one(
                        {"_id": payment_id},
                        {"$set": {"status": status}}
                    )
                
                if result.modified_count == 0:
                    log.warning(f"No payment found with ID: {payment_id}")
                else:
                    log.info(f"Payment {payment_id} status updated to {status}")
                    
            except InvalidId:
                # Handle invalid ObjectId, try as string
                result = await payments_col.update_one(
                    {"_id": payment_id},
                    {"$set": {"status": status}}
                )
        else:
            if payment_id in memory_payments:
                memory_payments[payment_id]["status"] = status
    except Exception as e:
        log.error(f"Error setting payment status: {e}")
        raise

async def get_stats():
    try:
        if users_col is not None:
            total = await users_col.count_documents({})
            active = await users_col.count_documents({"status": "active"})
            expired = await users_col.count_documents({"status": "expired"})
            pending = await payments_col.count_documents({"status": "pending"}) if payments_col is not None else 0
        else:
            total = len(memory_users)
            active = len([u for u in memory_users.values() if u.get("status") == "active"])
            expired = len([u for u in memory_users.values() if u.get("status") == "expired"])
            pending = len([p for p in memory_payments.values() if p.get("status") == "pending"])
        
        return total, active, expired, pending
    except Exception as e:
        log.error(f"Error getting stats: {e}")
        return 0, 0, 0, 0

# UI Helper Functions
async def safe_send_photo(chat_id: int, photo_url: str, caption: str, reply_markup=None):
    try:
        await bot.send_photo(chat_id, photo_url, caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.warning(f"Failed to send photo: {e}")
        try:
            await bot.send_message(chat_id, caption, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        except Exception as e2:
            log.error(f"Failed to send message fallback: {e2}")

async def safe_edit_message(cq: types.CallbackQuery, text: str = None, reply_markup=None):
    try:
        if text:
            await cq.message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        try:
            await cq.message.answer(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            log.error(f"Failed to send fallback message: {e}")

# Keyboard Functions
def kb_user_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🚀 Upgrade Premium", callback_data="menu:buy")],
        [InlineKeyboardButton(text="📊 My Subscription", callback_data="menu:my"),
         InlineKeyboardButton(text="💬 Support", callback_data="menu:support")],
        [InlineKeyboardButton(text="🎁 Special Offers", callback_data="menu:offers")]
    ]
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

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
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ 1M", callback_data=f"approve_{payment_id}_{user_id}_plan1"),
            InlineKeyboardButton(text="✅ 6M", callback_data=f"approve_{payment_id}_{user_id}_plan2")
        ],
        [
            InlineKeyboardButton(text="✅ 1Y", callback_data=f"approve_{payment_id}_{user_id}_plan3"),
            InlineKeyboardButton(text="✅ LT", callback_data=f"approve_{payment_id}_{user_id}_plan4")
        ],
        [InlineKeyboardButton(text="❌ Deny", callback_data=f"deny_{payment_id}_{user_id}")]
    ])

def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistics", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin:broadcast")]
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
    caption = f"💎 **Premium Plans**\n\nChoose your subscription plan:"
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
        plan_info = PLANS.get(user.get('plan_key'), {'name': 'Unknown', 'emoji': '📦'})
        
        caption = (
            f"📊 **My Subscription**\n\n"
            f"✅ **Status:** ACTIVE\n"
            f"{plan_info['emoji']} **Plan:** {plan_info['name']}\n\n"
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

# Enhanced UPI display with better highlighting
@dp.callback_query(F.data.startswith("copy:upi:"))
async def copy_upi(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[2]
    plan = PLANS[plan_key]
    
    text = (
        f"💳 **UPI PAYMENT GATEWAY**\n\n"
        f"🎯 **Plan:** {plan['emoji']} {plan['name']}\n"
        f"💰 **Amount:** {plan['price']}\n\n"
        f"🔥 **HIGHLIGHTED PAYMENT DETAILS:**\n"
        f"┏━━━━━━━━━━━━━━━━━━━━━━━━━┓\n"
        f"┃ **UPI ID:** `{UPI_ID}` ┃\n"
        f"┃ **Amount:** `{plan['price'].replace('₹', '')}` ┃\n"
        f"┗━━━━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
        f"📱 **Quick Steps:**\n"
        f"1️⃣ **Copy UPI ID** (tap above)\n"
        f"2️⃣ **Open UPI App** (GPay/PhonePe)\n"
        f"3️⃣ **Send Money** → Paste UPI ID\n"
        f"4️⃣ **Enter Amount:** {plan['price'].replace('₹', '')}\n"
        f"5️⃣ **Complete Payment**\n"
        f"6️⃣ **Upload Screenshot** here\n\n"
        f"⚠️ **Important:** Pay exactly **{plan['price']}**\n"
        f"📸 **Screenshot must show:** Success + Amount + Date"
    )
    
    await safe_edit_message(cq, text=text, reply_markup=kb_payment_options(plan_key))
    await cq.answer("💳 UPI details highlighted! Copy and paste in your app", show_alert=True)

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
    
    username = safe_text(m.from_user.username)
    first_name = safe_text(m.from_user.first_name)
    
    admin_message = (
        f"💬 **Support Message**\n\n"
        f"👤 User: {first_name} (@{username})\n"
        f"🆔 ID: {m.from_user.id}\n\n"
        f"Message: {m.text}\n\n"
        f"Reply: `/reply {m.from_user.id} Your message`"
    )
    
    try:
        await bot.send_message(ADMIN_ID, admin_message, parse_mode=ParseMode.MARKDOWN)
        await m.answer("✅ **Message sent to support!**\n\n🔔 You'll get a reply soon.", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.error(f"Failed to send support message: {e}")
        await m.answer("❌ Error sending message. Please try again.")

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
        confirmation_text = (
            f"🎉 **Payment proof received!**\n\n"
            f"📸 Proof ID: #{pid}\n"
            f"📱 Plan: {plan['emoji']} {plan['name']}\n"
            f"💰 Amount: {plan['price']}\n\n"
            f"⏰ Processing time: 3-5 minutes\n"
            f"🔔 You'll be notified once approved!"
        )
        
        try:
            await bot.send_photo(m.from_user.id, SUCCESS_IMAGE, caption=confirmation_text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await m.answer(confirmation_text, parse_mode=ParseMode.MARKDOWN)
        
        # Notify admin
        username = safe_text(m.from_user.username)
        first_name = safe_text(m.from_user.first_name)
        
        admin_notification = (
            f"💰 **New Payment #{pid}**\n\n"
            f"👤 User: {first_name} (@{username})\n"
            f"🆔 ID: {m.from_user.id}\n"
            f"📱 Plan: {plan['emoji']} {plan['name']}\n"
            f"💵 Amount: {plan['price']}\n"
            f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
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
        await m.answer("❌ Error processing screenshot. Please try uploading again.")

# Fixed admin handlers
@dp.callback_query(F.data.startswith("approve_"))
async def admin_approve(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    try:
        parts = cq.data.split("_")
        log.info(f"Parsing approve callback: {cq.data}, parts: {parts}")
        
        if len(parts) != 4:
            await cq.answer("❌ Invalid callback data!", show_alert=True)
            return
            
        payment_id, user_id, plan_key = parts[1], int(parts[2]), parts[1]
        log.info(f"Processing approval for payment_id: {payment_id}, user_id: {user_id}, plan_key: {plan_key}")
        
        await set_payment_status(payment_id, "approved")
        await set_subscription(user_id, plan_key, PLANS[plan_key]["days"])
        plan = PLANS[plan_key]
        
        # Create invite link
        try:
            link = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
            user_msg = (
                f"🎉 **PAYMENT APPROVED!**\n\n"
                f"✅ Your {plan['emoji']} {plan['name']} subscription is now **ACTIVE**!\n"
                f"💰 Amount: {plan['price']}\n"
                f"⏰ Valid for: {plan['days']} days\n\n"
                f"🔗 **Join Premium Channel:**\n{link.invite_link}\n\n"
                f"🌟 **Welcome to Premium Family!**\n"
                f"Enjoy unlimited access to all premium features! 🚀"
            )
        except Exception as e:
            log.error(f"Error creating invite link: {e}")
            user_msg = (
                f"🎉 **PAYMENT APPROVED!**\n\n"
                f"✅ Your {plan['emoji']} {plan['name']} subscription is now **ACTIVE**!\n"
                f"💰 Amount: {plan['price']}\n"
                f"⏰ Valid for: {plan['days']} days\n\n"
                f"🌟 **Welcome to Premium!**\n"
                f"Contact admin for channel access."
            )
        
        await bot.send_message(user_id, user_msg, parse_mode=ParseMode.MARKDOWN)
        await cq.message.edit_text(f"✅ **Payment #{payment_id} APPROVED**\n\n{plan['emoji']} {plan['name']} activated for user {user_id}!", parse_mode=ParseMode.MARKDOWN)
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
        log.info(f"Parsing deny callback: {cq.data}, parts: {parts}")
        
        if len(parts) != 3:
            await cq.answer("❌ Invalid callback data!", show_alert=True)
            return
            
        payment_id, user_id = parts[1], int(parts[2])
        log.info(f"Processing denial for payment_id: {payment_id}, user_id: {user_id}")
        
        await set_payment_status(payment_id, "denied")
        
        user_msg = (
            f"❌ **Payment Proof Not Approved**\n\n"
            f"Your payment screenshot for proof #{payment_id} could not be approved.\n\n"
            f"🔍 **Common reasons:**\n"
            f"• Screenshot not clear enough\n"
            f"• Amount doesn't match plan price\n"
            f"• Payment status not visible\n"
            f"• Transaction details missing\n\n"
            f"🔄 **What to do:**\n"
            f"1. Take a clearer screenshot\n"
            f"2. Ensure all details are visible\n"
            f"3. Upload again\n\n"
            f"💬 **Need help?** Contact support!"
        )
        
        await bot.send_message(user_id, user_msg, parse_mode=ParseMode.MARKDOWN)
        await cq.message.edit_text(f"❌ **Payment #{payment_id} DENIED**\n\nUser {user_id} has been notified with improvement suggestions.", parse_mode=ParseMode.MARKDOWN)
        await cq.answer("❌ Denied with feedback sent!")
        
    except Exception as e:
        log.error(f"Error denying payment: {e}")
        await cq.answer("❌ Error processing denial!", show_alert=True)

@dp.callback_query(F.data == "admin:stats")
async def admin_stats(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    total, active, expired, pending = await get_stats()
    
    text = (
        f"📊 **Bot Statistics**\n\n"
        f"👥 Total Users: {total}\n"
        f"✅ Active: {active}\n"
        f"❌ Expired: {expired}\n"
        f"⏳ Pending: {pending}\n\n"
        f"⏰ {datetime.now().strftime('%d %b, %H:%M')}"
    )
    
    await cq.message.answer(text, parse_mode=ParseMode.MARKDOWN)
    await cq.answer()

@dp.message(Command("reply"))
async def admin_reply(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    
    try:
        parts = m.text.split(maxsplit=2)
        if len(parts) < 3:
            await m.answer("❌ Usage: `/reply <user_id> <message>`")
            return
        
        user_id, reply_text = int(parts[1]), parts[2]
        
        user_msg = f"💬 **Support Response**\n\n{reply_text}\n\n─────────────\n🎧 Premium Support"
        await bot.send_message(user_id, user_msg, parse_mode=ParseMode.MARKDOWN)
        await m.answer(f"✅ Reply sent to user {user_id}")
        
    except Exception as e:
        log.error(f"Error sending reply: {e}")
        await m.answer("❌ Error sending reply")

# Main function
async def main():
    log.info("🚀 Starting Premium Subscription Bot")
    
    if mongo_client is not None:
        try:
            await mongo_client.admin.command('ping')
            log.info("✅ MongoDB connected")
        except Exception as e:
            log.warning(f"⚠️ MongoDB connection failed: {e}")
    
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    if not all([API_TOKEN != "TEST_TOKEN", ADMIN_ID]):
        raise RuntimeError("❌ Missing required environment variables")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("✅ Bot stopped gracefully")
