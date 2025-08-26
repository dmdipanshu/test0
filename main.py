import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from bson import ObjectId
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from motor.motor_asyncio import AsyncIOMotorClient

# Simple logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fastbot")

# Environment variables
API_TOKEN = os.getenv("API_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID") or "123456789")
UPI_ID = os.getenv("UPI_ID") or "yourupi@upi"
QR_CODE_URL = os.getenv("QR_CODE_URL") or "https://example.com/qr.png"
MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"

if not API_TOKEN:
    print("❌ Set API_TOKEN!")
    exit(1)

# MongoDB
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client['premium_bot']
users_col = db['users']
payments_col = db['payments']
support_col = db['support_chats']

# Bot setup
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Plans
PLANS = {
    "1": {"name": "1 Month", "price": 99, "days": 30, "emoji": "🟢"},
    "2": {"name": "6 Months", "price": 399, "days": 180, "emoji": "🟡"},  
    "3": {"name": "1 Year", "price": 1999, "days": 365, "emoji": "🔥"},
    "4": {"name": "Lifetime", "price": 2999, "days": 36500, "emoji": "💎"},
}

# State management
user_plans = {}
admin_replying_to = {}

# States
class SupportState(StatesGroup):
    waiting_message = State()

class AdminReply(StatesGroup):
    waiting_message = State()

def is_admin(user_id):
    return user_id == ADMIN_ID

# SAFE MESSAGE SENDING - No HTML/Markdown errors
async def send_message(chat_id, text, keyboard=None):
    try:
        return await bot.send_message(chat_id, text, reply_markup=keyboard)
    except Exception as e:
        log.error(f"Send error: {e}")
        return None

async def send_photo(chat_id, photo, caption, keyboard=None):
    try:
        return await bot.send_photo(chat_id, photo, caption=caption, reply_markup=keyboard)
    except Exception as e:
        log.error(f"Photo send error: {e}")
        return None

async def edit_message(query, text, keyboard=None):
    try:
        await query.message.edit_text(text, reply_markup=keyboard)
    except:
        await send_message(query.from_user.id, text, keyboard)

# Database functions
async def get_user(user_id):
    return await users_col.find_one({"user_id": user_id})

async def create_user(user: types.User):
    user_data = {
        "user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "status": "free",
        "plan_key": None,
        "created_at": datetime.now(timezone.utc),
        "start_at": None,
        "end_at": None
    }
    await users_col.insert_one(user_data)
    return user_data

async def activate_premium(user_id, plan_key):
    plan = PLANS[plan_key]
    now = datetime.now(timezone.utc)
    end_date = now + timedelta(days=plan["days"])
    
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {
            "status": "premium",
            "plan_key": plan_key,
            "start_at": now,
            "end_at": end_date
        }}
    )
    return end_date

# KEYBOARDS
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Buy Premium", callback_data="buy")],
        [InlineKeyboardButton(text="📊 My Status", callback_data="status"),
         InlineKeyboardButton(text="💬 Support", callback_data="support")]
    ])

def plans_kb():
    buttons = []
    for k, p in PLANS.items():
        buttons.append([InlineKeyboardButton(
            text=f"{p['emoji']} {p['name']} - ₹{p['price']}", 
            callback_data=f"plan_{k}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data="main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# SIMPLE PAYMENT KEYBOARD - One button shows all payment info
def payment_kb(plan_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Show Payment Info", callback_data=f"pay_{plan_id}")],
        [InlineKeyboardButton(text="📸 Upload Screenshot", callback_data=f"upload_{plan_id}")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="buy")]
    ])

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ Payments", callback_data="payments"),
         InlineKeyboardButton(text="📊 Stats", callback_data="stats")],
        [InlineKeyboardButton(text="💬 Support Chats", callback_data="support_chats")]
    ])

def payment_actions_kb(payment_id, user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{payment_id}_{user_id}"),
         InlineKeyboardButton(text="❌ Deny", callback_data=f"deny_{payment_id}_{user_id}")]
    ])

# SIMPLIFIED SUPPORT KEYBOARD - Direct reply buttons
def support_chat_kb(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Reply to User", callback_data=f"reply_{user_id}")],
        [InlineKeyboardButton(text="✅ Close Chat", callback_data=f"close_{user_id}")]
    ])

# HANDLERS

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    user = await get_user(message.from_user.id)
    if not user:
        await create_user(message.from_user)
    
    if is_admin(message.from_user.id):
        await send_message(message.from_user.id, 
            f"🎯 ADMIN PANEL\n\nHello {message.from_user.first_name}!\nManage your bot efficiently.", 
            admin_kb())
    else:
        await send_message(message.from_user.id,
            f"👋 Welcome {message.from_user.first_name}!\n\n🌟 Get Premium Access:\n• Unlimited features\n• Priority support\n• Ad-free experience\n\n🚀 Upgrade now!",
            main_kb())

@dp.callback_query(F.data == "main")
async def main_handler(query: types.CallbackQuery):
    if is_admin(query.from_user.id):
        await edit_message(query, "🎯 ADMIN PANEL\nChoose option:", admin_kb())
    else:
        await edit_message(query, f"🏠 Main Menu\nHello {query.from_user.first_name}!", main_kb())
    await query.answer()

@dp.callback_query(F.data == "buy")
async def buy_handler(query: types.CallbackQuery):
    await edit_message(query, "💎 Premium Plans\n\nChoose your plan:", plans_kb())
    await query.answer()

@dp.callback_query(F.data.startswith("plan_"))
async def plan_handler(query: types.CallbackQuery):
    plan_id = query.data.split("_")[1]
    user_plans[query.from_user.id] = plan_id
    plan = PLANS[plan_id]
    
    text = f"🎯 {plan['emoji']} {plan['name']}\n\n💰 Price: ₹{plan['price']}\n⏰ Duration: {plan['days']} days\n\n📱 Choose payment method:"
    await edit_message(query, text, payment_kb(plan_id))
    await query.answer(f"Selected {plan['name']}")

# SIMPLIFIED PAYMENT INFO - Shows everything at once
@dp.callback_query(F.data.startswith("pay_"))
async def payment_info_handler(query: types.CallbackQuery):
    plan_id = query.data.split("_")[1]
    plan = PLANS[plan_id]
    
    # Send QR code first
    await send_photo(query.from_user.id, QR_CODE_URL, 
        f"📱 QR Code for {plan['name']}")
    
    # SIMPLE TEXT - NO HTML/MARKDOWN
    payment_text = f"""💳 PAYMENT INFORMATION

📋 Plan: {plan['emoji']} {plan['name']}
💰 Amount: ₹{plan['price']}

🏦 UPI ID: {UPI_ID}

📱 EASY STEPS:
1. Copy UPI ID: {UPI_ID}
2. Open GPay/PhonePe/Paytm
3. Pay ₹{plan['price']}
4. Upload screenshot below

⚡ Premium activated instantly!"""
    
    await send_message(query.from_user.id, payment_text, payment_kb(plan_id))
    await query.answer("💳 Payment info sent!")

@dp.callback_query(F.data.startswith("upload_"))
async def upload_handler(query: types.CallbackQuery):
    plan_id = query.data.split("_")[1]
    user_plans[query.from_user.id] = plan_id
    plan = PLANS[plan_id]
    
    text = f"📸 Upload Payment Screenshot\n\nPlan: {plan['name']} - ₹{plan['price']}\n\n📷 Send clear screenshot showing:\n✅ Payment success\n✅ Amount ₹{plan['price']}\n✅ Transaction details\n\n📤 Send photo now:"
    
    await edit_message(query, text)
    await query.answer("📸 Ready for screenshot!")

@dp.callback_query(F.data == "status")
async def status_handler(query: types.CallbackQuery):
    user = await get_user(query.from_user.id)
    
    if user and user.get("status") == "premium":
        plan = PLANS.get(user.get("plan_key", "1"))
        end_date = user.get("end_at")
        
        if end_date and plan['days'] != 36500:
            days_left = (end_date - datetime.now(timezone.utc)).days
            text = f"📊 Premium Status\n\n✅ ACTIVE\n{plan['emoji']} Plan: {plan['name']}\n⏰ Days left: {days_left}\n\n🎉 All benefits active!"
        else:
            text = f"📊 Premium Status\n\n✅ LIFETIME PREMIUM\n💎 All benefits forever!"
    else:
        text = "📊 Account Status\n\n❌ FREE USER\n\n🚀 Upgrade benefits:\n• Unlimited access\n• Priority support\n• Ad-free experience\n\nClick Buy Premium!"
    
    await edit_message(query, text, main_kb())
    await query.answer()

# SIMPLIFIED SUPPORT SYSTEM - No complex states
@dp.callback_query(F.data == "support")
async def support_handler(query: types.CallbackQuery, state: FSMContext):
    await state.set_state(SupportState.waiting_message)
    
    text = f"💬 Support Chat\n\nHello {query.from_user.first_name}!\n\n📝 Describe your issue:\n(Admin will receive your message instantly)"
    
    await edit_message(query, text)
    await query.answer("💬 Support activated!")

# HANDLE SUPPORT MESSAGES
@dp.message(SupportState.waiting_message)
async def support_message_handler(message: types.Message, state: FSMContext):
    if is_admin(message.from_user.id):
        return
    
    user_id = message.from_user.id
    
    # Save support chat to database
    chat_data = {
        "user_id": user_id,
        "username": message.from_user.username,
        "first_name": message.from_user.first_name,
        "user_message": message.text,
        "admin_reply": None,
        "status": "open",
        "time": datetime.now(timezone.utc)
    }
    
    result = await support_col.insert_one(chat_data)
    chat_id = str(result.inserted_id)[:8]
    
    # Get user status for priority
    user = await get_user(user_id)
    priority = "HIGH" if user and user.get("status") == "premium" else "NORMAL"
    
    # Send to admin - SIMPLE FORMAT
    admin_text = f"💬 NEW SUPPORT MESSAGE #{chat_id}\n\n🔥 Priority: {priority}\n👤 User: {message.from_user.first_name}\n📱 @{message.from_user.username or 'None'}\n🆔 ID: {user_id}\n\n💬 Message:\n{message.text}\n\n⏰ {datetime.now().strftime('%H:%M IST')}"
    
    await send_message(ADMIN_ID, admin_text, support_chat_kb(user_id))
    
    # Confirm to user
    response_time = "2-5 min" if priority == "HIGH" else "10-30 min"
    await send_message(user_id,
        f"✅ Message sent to admin!\n\n🎫 Chat ID: #{chat_id}\n🔥 Priority: {priority}\n⏰ Response: {response_time}\n\n🔔 You'll get reply soon!",
        main_kb())
    
    await state.clear()

# ADMIN REPLY SYSTEM - Direct button click
@dp.callback_query(F.data.startswith("reply_"))
async def admin_reply_handler(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
    
    user_id = int(query.data.split("_")[1])
    admin_replying_to[query.from_user.id] = user_id
    
    await state.set_state(AdminReply.waiting_message)
    await query.message.answer(f"💬 Replying to User {user_id}\n\n📝 Type your response:")
    await query.answer("💬 Reply mode activated!")

@dp.message(AdminReply.waiting_message)
async def admin_reply_message_handler(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    
    user_id = admin_replying_to.get(message.from_user.id)
    if not user_id:
        await state.clear()
        return
    
    # Send reply to user
    reply_text = f"💬 Support Reply\n\n{message.text}\n\n────────────────\n🎧 Support Team\n💬 Need more help? Use Support button!"
    
    await send_message(user_id, reply_text, main_kb())
    
    # Update support chat
    await support_col.update_many(
        {"user_id": user_id, "status": "open"},
        {"$set": {"admin_reply": message.text, "status": "closed"}}
    )
    
    await message.answer(f"✅ Reply sent to User {user_id}")
    
    # Clear state
    admin_replying_to.pop(message.from_user.id, None)
    await state.clear()

@dp.callback_query(F.data.startswith("close_"))
async def close_support_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
    
    user_id = int(query.data.split("_")[1])
    
    # Close support chats
    await support_col.update_many(
        {"user_id": user_id, "status": "open"},
        {"$set": {"status": "closed"}}
    )
    
    await query.message.edit_text(f"✅ Support chat closed for User {user_id}")
    await query.answer("✅ Chat closed!")

# HANDLE PHOTO UPLOADS
@dp.message(F.photo)
async def photo_handler(message: types.Message):
    if is_admin(message.from_user.id):
        return
    
    user_id = message.from_user.id
    plan_id = user_plans.get(user_id)
    
    if not plan_id:
        await send_message(user_id, "❌ Select plan first: /start", main_kb())
        return
    
    plan = PLANS[plan_id]
    
    # Save payment
    payment_data = {
        "user_id": user_id,
        "username": message.from_user.username,
        "first_name": message.from_user.first_name,
        "plan_key": plan_id,
        "file_id": message.photo[-1].file_id,
        "status": "pending",
        "time": datetime.now(timezone.utc)
    }
    
    result = await payments_col.insert_one(payment_data)
    payment_id = str(result.inserted_id)[:8]
    
    # User confirmation
    await send_message(user_id,
        f"🎉 Payment received!\n\n📸 ID: #{payment_id}\n📋 Plan: {plan['name']}\n💰 Amount: ₹{plan['price']}\n\n⏳ Processing...\n🔔 You'll be notified!",
        main_kb())
    
    # Admin notification
    await send_message(ADMIN_ID,
        f"💰 Payment #{payment_id}\n👤 {message.from_user.first_name}\n📋 {plan['name']} - ₹{plan['price']}\n⏰ {datetime.now().strftime('%H:%M')}")
    
    await send_photo(ADMIN_ID, message.photo[-1].file_id,
        f"Payment #{payment_id}\n{plan['name']} - ₹{plan['price']}",
        payment_actions_kb(str(result.inserted_id), user_id))

# ADMIN HANDLERS

@dp.callback_query(F.data == "stats")
async def admin_stats_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
    
    total = await users_col.count_documents({})
    premium = await users_col.count_documents({"status": "premium"})
    pending = await payments_col.count_documents({"status": "pending"})
    open_chats = await support_col.count_documents({"status": "open"})
    
    text = f"📊 Bot Statistics\n\n👥 Total Users: {total}\n💎 Premium: {premium}\n⏳ Pending Payments: {pending}\n💬 Open Chats: {open_chats}\n\n⏰ {datetime.now().strftime('%H:%M IST')}"
    
    await edit_message(query, text, admin_kb())
    await query.answer("📊 Stats updated")

@dp.callback_query(F.data == "payments")
async def admin_payments_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
    
    payments = await payments_col.find({"status": "pending"}).limit(10).to_list(10)
    
    if not payments:
        await query.message.answer("✅ No pending payments!", reply_markup=admin_kb())
        await query.answer("✅ All clear")
        return
    
    await query.message.answer(f"⏳ {len(payments)} Pending Payments:")
    
    for payment in payments:
        plan = PLANS[payment['plan_key']]
        text = f"💰 Payment #{str(payment['_id'])[:8]}\n\n👤 {payment['first_name']} ({payment['user_id']})\n📋 {plan['name']} - ₹{plan['price']}\n⏰ {payment['time'].strftime('%H:%M')}"
        
        await query.message.answer(text, reply_markup=payment_actions_kb(str(payment['_id']), payment['user_id']))
    
    await query.answer(f"⏳ {len(payments)} payments")

@dp.callback_query(F.data == "support_chats")
async def admin_support_chats_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
    
    chats = await support_col.find({"status": "open"}).limit(10).to_list(10)
    
    if not chats:
        await query.message.answer("✅ No open chats!", reply_markup=admin_kb())
        await query.answer("✅ All resolved")
        return
    
    await query.message.answer(f"💬 {len(chats)} Open Support Chats:")
    
    for chat in chats:
        text = f"💬 Chat #{str(chat['_id'])[:8]}\n\n👤 {chat['first_name']} ({chat['user_id']})\n⏰ {chat['time'].strftime('%H:%M')}\n\n💬 Message:\n{chat['user_message'][:100]}..."
        
        await query.message.answer(text, reply_markup=support_chat_kb(chat['user_id']))
    
    await query.answer(f"💬 {len(chats)} chats")

# PAYMENT APPROVAL
@dp.callback_query(F.data.startswith("approve_"))
async def approve_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
    
    parts = query.data.split("_")
    payment_id, user_id = parts[1], int(parts[2])
    
    payment = await payments_col.find_one({"_id": ObjectId(payment_id)})
    if not payment:
        await query.answer("❌ Payment not found")
        return
    
    plan = PLANS[payment['plan_key']]
    
    # Activate premium
    end_date = await activate_premium(user_id, payment['plan_key'])
    
    # Update payment
    await payments_col.update_one({"_id": ObjectId(payment_id)}, {"$set": {"status": "approved"}})
    
    # Notify user
    if plan['days'] == 36500:
        user_msg = f"🎉 Payment Approved!\n\n✅ {plan['emoji']} {plan['name']} activated!\n💰 ₹{plan['price']} confirmed\n⏰ Lifetime access\n\n💎 Welcome to Premium!"
    else:
        user_msg = f"🎉 Payment Approved!\n\n✅ {plan['emoji']} {plan['name']} activated!\n💰 ₹{plan['price']} confirmed\n⏰ Until {end_date.strftime('%d %b %Y')}\n\n💎 Welcome to Premium!"
    
    await send_message(user_id, user_msg, main_kb())
    
    # Update admin message
    await query.message.edit_caption(f"✅ APPROVED\n\nPayment #{payment_id}\nUser {user_id} activated\n{plan['name']} - ₹{plan['price']}")
    
    await query.answer("✅ Approved!")

@dp.callback_query(F.data.startswith("deny_"))
async def deny_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
    
    parts = query.data.split("_")
    payment_id, user_id = parts[1], int(parts[2])
    
    # Update payment
    await payments_col.update_one({"_id": ObjectId(payment_id)}, {"$set": {"status": "denied"}})
    
    # Notify user
    await send_message(user_id,
        "❌ Payment Not Approved\n\nIssues found:\n• Screenshot unclear\n• Wrong amount\n• Payment incomplete\n\n📸 Upload clearer screenshot:\n✅ Payment success visible\n✅ Correct amount shown\n✅ Clear image\n\n🚀 Try again: /start",
        main_kb())
    
    # Update admin message
    await query.message.edit_caption(f"❌ DENIED\n\nPayment #{payment_id}\nUser {user_id} notified")
    
    await query.answer("❌ Denied!")

# Expiry worker
async def expiry_worker():
    while True:
        try:
            now = datetime.now(timezone.utc)
            expired = await users_col.find({
                "status": "premium",
                "end_at": {"$lte": now}
            }).to_list(None)
            
            for user in expired:
                await users_col.update_one(
                    {"user_id": user["user_id"]},
                    {"$set": {"status": "expired"}}
                )
                
                await send_message(user["user_id"],
                    "⏰ Premium expired!\n\n🚀 Renew now: /start\n💎 Get premium benefits again!",
                    main_kb())
            
            if expired:
                log.info(f"Expired {len(expired)} users")
                
        except Exception as e:
            log.error(f"Expiry error: {e}")
        
        await asyncio.sleep(3600)  # Check hourly

async def main():
    try:
        # Test connections
        await mongo_client.admin.command('ping')
        log.info("✅ MongoDB connected")
        
        me = await bot.get_me()
        log.info(f"✅ Bot: @{me.username}")
        
        # Start expiry worker
        asyncio.create_task(expiry_worker())
        
        print("🚀 SIMPLE PREMIUM BOT STARTED")
        print("✅ No HTML/Markdown errors")
        print("💬 Easy support system")
        print("💳 Simple payment system")
        print("📋 Tap-to-copy UPI ID")
        
        await dp.start_polling(bot)
        
    except Exception as e:
        log.error(f"Startup error: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped")
    except Exception as e:
        log.error(f"Fatal error: {e}")
