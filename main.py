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
log = logging.getLogger("premiumbot")

# Environment variables
API_TOKEN = os.getenv("API_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID") or "123456789")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or "-10012345678")
UPI_ID = os.getenv("UPI_ID") or "yourupi@upi"
QR_CODE_URL = os.getenv("QR_CODE_URL") or "https://example.com/qr.png"
WELCOME_IMAGE = os.getenv("WELCOME_IMAGE") or "https://i.imgur.com/welcome.jpg"
PLANS_IMAGE = os.getenv("PLANS_IMAGE") or "https://i.imgur.com/plans.jpg"
PAYMENT_IMAGE = os.getenv("PAYMENT_IMAGE") or "https://i.imgur.com/payment.jpg"
STATUS_IMAGE = os.getenv("STATUS_IMAGE") or "https://i.imgur.com/status.jpg"
SUPPORT_IMAGE = os.getenv("SUPPORT_IMAGE") or "https://i.imgur.com/support.jpg"
ADMIN_IMAGE = os.getenv("ADMIN_IMAGE") or "https://i.imgur.com/admin.jpg"
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

# Bot setup with HTML parse mode for monospace
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

# SAFE MESSAGE SENDING WITH IMAGES
async def send_message(chat_id, text, keyboard=None):
    try:
        return await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode='HTML')
    except Exception as e:
        log.error(f"Send error: {e}")
        # Fallback without HTML
        try:
            clean_text = text.replace('<code>', '').replace('</code>', '').replace('<b>', '').replace('</b>', '')
            return await bot.send_message(chat_id, clean_text, reply_markup=keyboard)
        except:
            return None

async def send_photo_with_text(chat_id, photo_url, text, keyboard=None):
    try:
        return await bot.send_photo(chat_id, photo_url, caption=text, reply_markup=keyboard, parse_mode='HTML')
    except Exception as e:
        log.error(f"Photo send error: {e}")
        # Fallback to text message
        return await send_message(chat_id, text, keyboard)

async def edit_message(query, text, keyboard=None):
    try:
        await query.message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
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

# KEYBOARDS WITH IMAGE ICONS
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

def support_chat_kb(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Reply to User", callback_data=f"reply_{user_id}")],
        [InlineKeyboardButton(text="✅ Close Chat", callback_data=f"close_{user_id}")]
    ])

# HANDLERS WITH IMAGES

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    user = await get_user(message.from_user.id)
    if not user:
        await create_user(message.from_user)
    
    if is_admin(message.from_user.id):
        text = f"<b>🎯 ADMIN PANEL</b>\n\nHello <b>{message.from_user.first_name}</b>!\nManage your bot efficiently."
        await send_photo_with_text(message.from_user.id, ADMIN_IMAGE, text, admin_kb())
    else:
        text = f"<b>👋 Welcome {message.from_user.first_name}!</b>\n\n🌟 <b>Get Premium Access:</b>\n• Unlimited features\n• Priority support\n• Ad-free experience\n\n🚀 <b>Upgrade now!</b>"
        await send_photo_with_text(message.from_user.id, WELCOME_IMAGE, text, main_kb())

@dp.callback_query(F.data == "main")
async def main_handler(query: types.CallbackQuery):
    if is_admin(query.from_user.id):
        text = "<b>🎯 ADMIN PANEL</b>\nChoose option:"
        await send_photo_with_text(query.from_user.id, ADMIN_IMAGE, text, admin_kb())
    else:
        text = f"<b>🏠 Main Menu</b>\nHello <b>{query.from_user.first_name}</b>!"
        await send_photo_with_text(query.from_user.id, WELCOME_IMAGE, text, main_kb())
    await query.answer()

@dp.callback_query(F.data == "buy")
async def buy_handler(query: types.CallbackQuery):
    text = "<b>💎 Premium Plans</b>\n\nChoose your plan:"
    await send_photo_with_text(query.from_user.id, PLANS_IMAGE, text, plans_kb())
    await query.answer()

@dp.callback_query(F.data.startswith("plan_"))
async def plan_handler(query: types.CallbackQuery):
    plan_id = query.data.split("_")[1]
    user_plans[query.from_user.id] = plan_id
    plan = PLANS[plan_id]
    
    text = f"<b>🎯 {plan['emoji']} {plan['name']}</b>\n\n💰 <b>Price:</b> ₹{plan['price']}\n⏰ <b>Duration:</b> {plan['days']} days\n\n📱 <b>Choose payment method:</b>"
    await send_photo_with_text(query.from_user.id, PAYMENT_IMAGE, text, payment_kb(plan_id))
    await query.answer(f"Selected {plan['name']}")

# FIXED: Perfect monospace UPI ID that auto-copies on tap
@dp.callback_query(F.data.startswith("pay_"))
async def payment_info_handler(query: types.CallbackQuery):
    plan_id = query.data.split("_")[1]
    plan = PLANS[plan_id]
    
    # Send QR code first
    await bot.send_photo(query.from_user.id, QR_CODE_URL, 
        caption=f"📱 <b>QR Code for {plan['name']}</b>")
    
    # FIXED: Perfect monospace UPI ID using <code> tags for auto-copy
    payment_text = f"""<b>💳 PAYMENT INFORMATION</b>

📋 <b>Plan:</b> {plan['emoji']} {plan['name']}
💰 <b>Amount:</b> ₹{plan['price']}

🏦 <b>UPI ID:</b> <code>{UPI_ID}</code>
<i>(Tap UPI ID above to auto-copy)</i>

📱 <b>PAYMENT STEPS:</b>
1. Tap UPI ID above: <code>{UPI_ID}</code>
2. Open GPay/PhonePe/Paytm
3. Send Money → UPI ID → Paste
4. Enter amount: <b>₹{plan['price']}</b>
5. Complete payment
6. Upload screenshot below

⚡ <b>Premium activated instantly!</b>
🔗 <b>Channel invite link sent after approval</b>"""
    
    await send_message(query.from_user.id, payment_text, payment_kb(plan_id))
    await query.answer("💳 Payment info sent!")

@dp.callback_query(F.data.startswith("upload_"))
async def upload_handler(query: types.CallbackQuery):
    plan_id = query.data.split("_")[1]
    user_plans[query.from_user.id] = plan_id
    plan = PLANS[plan_id]
    
    text = f"<b>📸 Upload Payment Screenshot</b>\n\n<b>Plan:</b> {plan['name']} - ₹{plan['price']}\n\n📷 <b>Send clear screenshot showing:</b>\n✅ Payment success\n✅ Amount ₹{plan['price']}\n✅ Transaction details\n\n📤 <b>Send photo now:</b>"
    
    await send_photo_with_text(query.from_user.id, PAYMENT_IMAGE, text)
    await query.answer("📸 Ready for screenshot!")

@dp.callback_query(F.data == "status")
async def status_handler(query: types.CallbackQuery):
    user = await get_user(query.from_user.id)
    
    if user and user.get("status") == "premium":
        plan = PLANS.get(user.get("plan_key", "1"))
        end_date = user.get("end_at")
        
        if end_date and plan['days'] != 36500:
            # Fix timezone handling
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            
            days_left = (end_date - datetime.now(timezone.utc)).days
            text = f"<b>📊 Premium Status</b>\n\n✅ <b>ACTIVE</b>\n{plan['emoji']} <b>Plan:</b> {plan['name']}\n⏰ <b>Days left:</b> {days_left}\n\n🎉 <b>All benefits active!</b>"
        else:
            text = f"<b>📊 Premium Status</b>\n\n✅ <b>LIFETIME PREMIUM</b>\n💎 <b>All benefits forever!</b>"
    else:
        text = "<b>📊 Account Status</b>\n\n❌ <b>FREE USER</b>\n\n🚀 <b>Upgrade benefits:</b>\n• Unlimited access\n• Priority support\n• Ad-free experience\n\n<b>Click Buy Premium!</b>"
    
    await send_photo_with_text(query.from_user.id, STATUS_IMAGE, text, main_kb())
    await query.answer()

@dp.callback_query(F.data == "support")
async def support_handler(query: types.CallbackQuery, state: FSMContext):
    await state.set_state(SupportState.waiting_message)
    
    text = f"<b>💬 Support Chat</b>\n\nHello <b>{query.from_user.first_name}</b>!\n\n📝 <b>Describe your issue:</b>\n<i>(Admin will receive your message instantly)</i>"
    
    await send_photo_with_text(query.from_user.id, SUPPORT_IMAGE, text)
    await query.answer("💬 Support activated!")

@dp.message(SupportState.waiting_message)
async def support_message_handler(message: types.Message, state: FSMContext):
    if is_admin(message.from_user.id):
        return
    
    user_id = message.from_user.id
    
    # Save support chat
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
    
    # Get user status
    user = await get_user(user_id)
    priority = "HIGH" if user and user.get("status") == "premium" else "NORMAL"
    
    # Send to admin
    admin_text = f"<b>💬 NEW SUPPORT MESSAGE #{chat_id}</b>\n\n🔥 <b>Priority:</b> {priority}\n👤 <b>User:</b> {message.from_user.first_name}\n📱 <b>@{message.from_user.username or 'None'}</b>\n🆔 <b>ID:</b> <code>{user_id}</code>\n\n💬 <b>Message:</b>\n<i>{message.text}</i>\n\n⏰ {datetime.now().strftime('%H:%M IST')}"
    
    await send_message(ADMIN_ID, admin_text, support_chat_kb(user_id))
    
    # Confirm to user
    response_time = "2-5 min" if priority == "HIGH" else "10-30 min"
    await send_message(user_id,
        f"<b>✅ Message sent to admin!</b>\n\n🎫 <b>Chat ID:</b> #{chat_id}\n🔥 <b>Priority:</b> {priority}\n⏰ <b>Response:</b> {response_time}\n\n🔔 <b>You'll get reply soon!</b>",
        main_kb())
    
    await state.clear()

@dp.callback_query(F.data.startswith("reply_"))
async def admin_reply_handler(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
    
    user_id = int(query.data.split("_")[1])
    admin_replying_to[query.from_user.id] = user_id
    
    await state.set_state(AdminReply.waiting_message)
    await query.message.answer(f"<b>💬 Replying to User {user_id}</b>\n\n📝 <b>Type your response:</b>", parse_mode='HTML')
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
    reply_text = f"<b>💬 Support Reply</b>\n\n{message.text}\n\n────────────────\n🎧 <b>Support Team</b>\n💬 <b>Need more help? Use Support button!</b>"
    
    await send_message(user_id, reply_text, main_kb())
    
    # Update support chat
    await support_col.update_many(
        {"user_id": user_id, "status": "open"},
        {"$set": {"admin_reply": message.text, "status": "closed"}}
    )
    
    await message.answer(f"<b>✅ Reply sent to User {user_id}</b>", parse_mode='HTML')
    
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
    
    await query.message.edit_text(f"<b>✅ Support chat closed for User {user_id}</b>", parse_mode='HTML')
    await query.answer("✅ Chat closed!")

@dp.message(F.photo)
async def photo_handler(message: types.Message):
    if is_admin(message.from_user.id):
        return
    
    user_id = message.from_user.id
    plan_id = user_plans.get(user_id)
    
    if not plan_id:
        await send_message(user_id, "<b>❌ Select plan first: /start</b>", main_kb())
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
        f"<b>🎉 Payment received!</b>\n\n📸 <b>ID:</b> #{payment_id}\n📋 <b>Plan:</b> {plan['name']}\n💰 <b>Amount:</b> ₹{plan['price']}\n\n⏳ <b>Processing...</b>\n🔔 <b>You'll be notified!</b>",
        main_kb())
    
    # Admin notification
    await send_message(ADMIN_ID,
        f"<b>💰 Payment #{payment_id}</b>\n👤 {message.from_user.first_name}\n📋 {plan['name']} - ₹{plan['price']}\n⏰ {datetime.now().strftime('%H:%M')}")
    
    await bot.send_photo(ADMIN_ID, message.photo[-1].file_id,
        caption=f"<b>Payment #{payment_id}</b>\n{plan['name']} - ₹{plan['price']}",
        reply_markup=payment_actions_kb(str(result.inserted_id), user_id), 
        parse_mode='HTML')

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
    
    text = f"<b>📊 Bot Statistics</b>\n\n👥 <b>Total Users:</b> {total}\n💎 <b>Premium:</b> {premium}\n⏳ <b>Pending Payments:</b> {pending}\n💬 <b>Open Chats:</b> {open_chats}\n\n⏰ {datetime.now().strftime('%H:%M IST')}"
    
    await send_photo_with_text(query.from_user.id, ADMIN_IMAGE, text, admin_kb())
    await query.answer("📊 Stats updated")

@dp.callback_query(F.data == "payments")
async def admin_payments_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
    
    payments = await payments_col.find({"status": "pending"}).limit(10).to_list(10)
    
    if not payments:
        await query.message.answer("<b>✅ No pending payments!</b>", reply_markup=admin_kb(), parse_mode='HTML')
        await query.answer("✅ All clear")
        return
    
    await query.message.answer(f"<b>⏳ {len(payments)} Pending Payments:</b>", parse_mode='HTML')
    
    for payment in payments:
        plan = PLANS[payment['plan_key']]
        text = f"<b>💰 Payment #{str(payment['_id'])[:8]}</b>\n\n👤 {payment['first_name']} (<code>{payment['user_id']}</code>)\n📋 {plan['name']} - ₹{plan['price']}\n⏰ {payment['time'].strftime('%H:%M')}"
        
        await query.message.answer(text, reply_markup=payment_actions_kb(str(payment['_id']), payment['user_id']), parse_mode='HTML')
    
    await query.answer(f"⏳ {len(payments)} payments")

@dp.callback_query(F.data == "support_chats")
async def admin_support_chats_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
    
    chats = await support_col.find({"status": "open"}).limit(10).to_list(10)
    
    if not chats:
        await query.message.answer("<b>✅ No open chats!</b>", reply_markup=admin_kb(), parse_mode='HTML')
        await query.answer("✅ All resolved")
        return
    
    await query.message.answer(f"<b>💬 {len(chats)} Open Support Chats:</b>", parse_mode='HTML')
    
    for chat in chats:
        text = f"<b>💬 Chat #{str(chat['_id'])[:8]}</b>\n\n👤 {chat['first_name']} (<code>{chat['user_id']}</code>)\n⏰ {chat['time'].strftime('%H:%M')}\n\n💬 <b>Message:</b>\n<i>{chat['user_message'][:100]}...</i>"
        
        await query.message.answer(text, reply_markup=support_chat_kb(chat['user_id']), parse_mode='HTML')
    
    await query.answer(f"💬 {len(chats)} chats")

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
    
    # Generate channel invite link
    invite_link_text = ""
    try:
        invite_link = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
        invite_link_text = f"\n\n🔗 <b>JOIN PREMIUM CHANNEL:</b>{invite_link.invite_link}\n\n⚡ <b>Click link above to join!</b>"
    except Exception as e:
        log.error(f"Failed to create invite link: {e}")
        invite_link_text = "\n\n🔗 <b>Channel access will be provided shortly.</b>"
    
    # Notify user with invite link
    if plan['days'] == 36500:
        user_msg = f"<b>🎉 Payment Approved!</b>\n\n✅ {plan['emoji']} <b>{plan['name']} activated!</b>\n💰 ₹{plan['price']} confirmed\n⏰ <b>Lifetime access</b>{invite_link_text}\n\n💎 <b>Welcome to Premium!</b>"
    else:
        user_msg = f"<b>🎉 Payment Approved!</b>\n\n✅ {plan['emoji']} <b>{plan['name']} activated!</b>\n💰 ₹{plan['price']} confirmed\n⏰ <b>Until {end_date.strftime('%d %b %Y')}</b>{invite_link_text}\n\n💎 <b>Welcome to Premium!</b>"
    
    await send_message(user_id, user_msg, main_kb())
    
    # Update admin message
    await query.message.edit_caption(f"<b>✅ APPROVED</b>\n\nPayment #{payment_id}\nUser {user_id} activated\n{plan['name']} - ₹{plan['price']}", parse_mode='HTML')
    
    await query.answer("✅ Approved & invite sent!")

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
        "<b>❌ Payment Not Approved</b>\n\n<b>Issues found:</b>\n• Screenshot unclear\n• Wrong amount\n• Payment incomplete\n\n📸 <b>Upload clearer screenshot:</b>\n✅ Payment success visible\n✅ Correct amount shown\n✅ Clear image\n\n🚀 <b>Try again: /start</b>",
        main_kb())
    
    # Update admin message
    await query.message.edit_caption(f"<b>❌ DENIED</b>\n\nPayment #{payment_id}\nUser {user_id} notified", parse_mode='HTML')
    
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
                
                # Remove from channel
                try:
                    await bot.ban_chat_member(CHANNEL_ID, user["user_id"])
                    await bot.unban_chat_member(CHANNEL_ID, user["user_id"])
                except:
                    pass
                
                await send_message(user["user_id"],
                    "<b>⏰ Premium expired!</b>\n\n🚀 <b>Renew now: /start</b>\n💎 <b>Get premium benefits again!</b>",
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
        
        print("🚀 PREMIUM BOT WITH IMAGES STARTED")
        print("✅ Perfect monospace UPI copy")
        print("🖼️ All buttons start with images")
        print("💬 Beautiful UI with images")
        print("🔗 Channel invite links working")
        print("📋 HTML formatted messages")
        
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
