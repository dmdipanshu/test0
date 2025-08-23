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

# MongoDB setup - with error handling
try:
    mongo_client = AsyncIOMotorClient(MONGO_URI)
    db = mongo_client['premium_bot']
    users_col = db['users']
    payments_col = db['payments']
    tickets_col = db['tickets']
except Exception as e:
    log.error(f"MongoDB connection failed: {e}")
    # Fallback to in-memory storage for development
    users_col = payments_col = tickets_col = None

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
memory_tickets = {}
payment_counter = 0

# FSM States
class BCast(StatesGroup):
    waiting_text = State()

# Helper Functions
def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def safe_text(text) -> str:
    return str(text or "No info").replace("None", "No info")

# Database Helper Functions with fallback
async def upsert_user(user: types.User):
    try:
        if users_col:
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
            # Fallback to memory
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
        if users_col:
            return await users_col.find_one({"user_id": user_id})
        else:
            return memory_users.get(user_id)
    except Exception as e:
        log.error(f"Error getting user: {e}")
        return None

async def add_payment(user_id: int, plan_key: str, file_id: str):
    global payment_counter
    try:
        if payments_col:
            result = await payments_col.insert_one({
                "user_id": user_id,
                "plan_key": plan_key,
                "file_id": file_id,
                "created_at": datetime.now(timezone.utc),
                "status": "pending"
            })
            return str(result.inserted_id)
        else:
            # Fallback to memory
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

async def safe_edit_message(cq: types.CallbackQuery, text: str = None, caption: str = None, reply_markup=None):
    try:
        if text:
            await cq.message.edit_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        elif caption:
            await cq.message.edit_caption(caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        try:
            if text:
                await cq.message.answer(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
            elif caption:
                await cq.message.answer(caption, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
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
    if is_admin:
        buttons.append([InlineKeyboardButton(text="🛠 Admin Panel", callback_data="admin:menu")])
    
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
            InlineKeyboardButton(text="✅ 1M", callback_data=f"admin:approve:{payment_id}:{user_id}:plan1"),
            InlineKeyboardButton(text="✅ 6M", callback_data=f"admin:approve:{payment_id}:{user_id}:plan2")
        ],
        [
            InlineKeyboardButton(text="✅ 1Y", callback_data=f"admin:approve:{payment_id}:{user_id}:plan3"),
            InlineKeyboardButton(text="✅ LT", callback_data=f"admin:approve:{payment_id}:{user_id}:plan4")
        ],
        [InlineKeyboardButton(text="❌ Deny", callback_data=f"admin:deny:{payment_id}:{user_id}")]
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

# Fixed Photo handler with better error handling
@dp.message(F.photo)
async def on_payment_photo(m: types.Message):
    if is_admin(m.from_user.id):
        return
        
    plan_key = last_selected_plan.get(m.from_user.id)
    if not plan_key:
        await m.answer("❌ Please select a plan first using /start")
        return
    
    try:
        # Step 1: Add payment to database
        log.info(f"Processing payment photo for user {m.from_user.id}, plan {plan_key}")
        pid = await add_payment(m.from_user.id, plan_key, m.photo[-1].file_id)
        log.info(f"Payment added with ID: {pid}")
        
        plan = PLANS[plan_key]
        
        # Step 2: Send confirmation to user
        try:
            confirmation_text = (
                f"🎉 **Payment proof received!**\n\n"
                f"📸 Proof ID: #{pid}\n"
                f"📱 Plan: {plan['emoji']} {plan['name']}\n"
                f"💰 Amount: {plan['price']}\n\n"
                f"⏰ Processing time: 3-5 minutes\n"
                f"🔔 You'll be notified once approved!"
            )
            
            # Try to send with image first, fallback to text
            try:
                await bot.send_photo(m.from_user.id, SUCCESS_IMAGE, caption=confirmation_text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await m.answer(confirmation_text, parse_mode=ParseMode.MARKDOWN)
                
            log.info(f"Confirmation sent to user {m.from_user.id}")
            
        except Exception as e:
            log.error(f"Error sending user confirmation: {e}")
            await m.answer("✅ Payment proof received! Waiting for admin approval.")
        
        # Step 3: Notify admin
        try:
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
            
            # Send text notification first
            await bot.send_message(ADMIN_ID, admin_notification, parse_mode=ParseMode.MARKDOWN)
            
            # Then send photo with action buttons
            await bot.send_photo(
                ADMIN_ID,
                m.photo[-1].file_id,
                caption=f"💳 Payment Proof #{pid}\n{plan['emoji']} {plan['name']} - {plan['price']}\nUser: {first_name} ({m.from_user.id})",
                reply_markup=kb_payment_actions(pid, m.from_user.id)
            )
            
            log.info(f"Admin notification sent for payment {pid}")
            
        except Exception as e:
            log.error(f"Error sending admin notification: {e}")
            # Still continue - payment was saved
        
    except Exception as e:
        log.error(f"Error processing payment photo: {e}")
        await m.answer("❌ Error processing screenshot. Please try uploading again.")

# Simple admin approval handler
@dp.callback_query(F.data.startswith("admin:approve:"))
async def admin_approve(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    try:
        parts = cq.data.split(":")
        payment_id, user_id, plan_key = parts[2], int(parts[3]), parts[1]
        plan = PLANS[plan_key]
        
        # Simple approval message
        user_msg = (
            f"🎉 **Payment Approved!**\n\n"
            f"✅ Your {plan['emoji']} {plan['name']} subscription is now active!\n"
            f"💰 Amount: {plan['price']}\n\n"
            f"🌟 Welcome to Premium!"
        )
        
        await bot.send_message(user_id, user_msg, parse_mode=ParseMode.MARKDOWN)
        await cq.message.answer(f"✅ Payment #{payment_id} approved!")
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
        
        user_msg = (
            f"❌ **Payment proof not approved**\n\n"
            f"Please upload a clearer screenshot showing:\n"
            f"• Payment success status\n"
            f"• Exact amount\n"
            f"• Transaction details"
        )
        
        await bot.send_message(user_id, user_msg, parse_mode=ParseMode.MARKDOWN)
        await cq.message.answer(f"❌ Payment #{payment_id} denied.")
        await cq.answer("❌ Denied!")
        
    except Exception as e:
        log.error(f"Error denying payment: {e}")
        await cq.answer("❌ Error processing denial!", show_alert=True)

# Main function
async def main():
    log.info("🚀 Starting Premium Subscription Bot")
    
    # Test MongoDB connection if available
    if mongo_client:
        try:
            await mongo_client.admin.command('ping')
            log.info("✅ MongoDB connected")
        except Exception as e:
            log.warning(f"⚠️ MongoDB connection failed, using memory storage: {e}")
    
    # Start bot
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    if not all([API_TOKEN != "TEST_TOKEN", ADMIN_ID]):
        raise RuntimeError("❌ Missing required environment variables")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("✅ Bot stopped gracefully")
