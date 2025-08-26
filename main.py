import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from bson import ObjectId
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from motor.motor_asyncio import AsyncIOMotorClient
import time

# Ultra-fast logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger("ultrafast_bot")

# FIXED: Environment variables validation
def get_env_or_exit(key, default=None):
    """Get environment variable or exit if critical"""
    value = os.getenv(key) or default
    if value == "TEST_TOKEN" or not value:
        log.error(f"❌ {key} not set! Please set environment variable.")
        if key == "API_TOKEN":
            print("❌ CRITICAL: Bot token not found!")
            print("🔧 Set environment variable: API_TOKEN=your_bot_token")
            exit(1)
    return value

API_TOKEN = get_env_or_exit("API_TOKEN", "TEST_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID") or "123456789")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or "-10012345678")
UPI_ID = os.getenv("UPI_ID") or "yourupi@upi"
QR_CODE_URL = os.getenv("QR_CODE_URL") or "https://example.com/qr.png"
MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"

# FIXED: MongoDB setup with proper error handling
try:
    mongo_client = AsyncIOMotorClient(
        MONGO_URI,
        maxPoolSize=50,
        minPoolSize=10,
        maxIdleTimeMS=30000,
        serverSelectionTimeoutMS=5000
    )
    db = mongo_client['premium_bot']
    users_col = db['users']
    payments_col = db['payments']
    tickets_col = db['tickets']
    log.info("✅ MongoDB client initialized")
except Exception as e:
    log.error(f"❌ MongoDB initialization error: {e}")
    exit(1)

# FIXED: Bot initialization for aiogram 3.7.0+
try:
    bot = Bot(
        token=API_TOKEN, 
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher(storage=MemoryStorage())
    log.info("✅ Bot initialized with latest aiogram version")
except Exception as e:
    log.error(f"❌ Bot initialization failed: {e}")
    print("🔧 Check if your bot token is valid!")
    exit(1)

# Optimized plans
PLANS = {
    "1": {"name": "1 Month", "price": "99", "days": 30, "emoji": "🟢"},
    "2": {"name": "6 Months", "price": "399", "days": 180, "emoji": "🟡"},
    "3": {"name": "1 Year", "price": "1999", "days": 365, "emoji": "🔥"},
    "4": {"name": "Lifetime", "price": "2999", "days": 36500, "emoji": "💎"},
}

# Speed optimized cache
user_cache = {}
plan_cache = {}
support_mode = set()

# Ultra-fast FSM
class Support(StatesGroup):
    waiting = State()

class Broadcast(StatesGroup):
    message = State()

# Speed optimized functions
def is_admin(uid): 
    return uid == ADMIN_ID

async def validate_connection():
    """Validate MongoDB and Bot connections"""
    try:
        # Test MongoDB
        await mongo_client.admin.command('ping')
        log.info("✅ MongoDB connection verified")
        
        # Test Bot token
        me = await bot.get_me()
        log.info(f"✅ Bot verified: @{me.username} (ID: {me.id})")
        return True
    except Exception as e:
        log.error(f"❌ Connection validation failed: {e}")
        return False

async def fast_upsert_user(user: types.User):
    """Ultra-fast user upsert with caching"""
    user_id = user.id
    if user_id in user_cache:
        return user_cache[user_id]
    
    user_data = {
        "user_id": user_id,
        "username": user.username,
        "first_name": user.first_name,
        "updated_at": datetime.now(timezone.utc)
    }
    
    try:
        result = await users_col.update_one(
            {"user_id": user_id},
            {"$set": user_data, "$setOnInsert": {
                "plan_key": None, "status": "none", "created_at": datetime.now(timezone.utc),
                "start_at": None, "end_at": None
            }}, 
            upsert=True
        )
        user_cache[user_id] = user_data
        return user_data
    except Exception as e:
        log.error(f"Database error for user {user_id}: {e}")
        return None

async def get_user_fast(user_id):
    """Ultra-fast user retrieval with caching"""
    if user_id in user_cache:
        return user_cache[user_id]
    
    try:
        user = await users_col.find_one({"user_id": user_id})
        if user:
            user_cache[user_id] = user
        return user
    except Exception as e:
        log.error(f"Get user error {user_id}: {e}")
        return None

# FIXED: Keyboard functions
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Premium Plans", callback_data="buy")],
        [InlineKeyboardButton(text="📊 My Status", callback_data="status"),
         InlineKeyboardButton(text="💬 Support", callback_data="support")]
    ])

def kb_plans():
    buttons = []
    for k, p in PLANS.items():
        buttons.append([InlineKeyboardButton(text=f"{p['emoji']} {p['name']} - ₹{p['price']}", callback_data=f"plan_{k}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_payment(plan_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 UPI Payment", callback_data=f"upi_{plan_id}")],
        [InlineKeyboardButton(text="📱 QR Code", callback_data=f"qr_{plan_id}")],
        [InlineKeyboardButton(text="📸 Upload Screenshot", callback_data=f"upload_{plan_id}")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="buy")]
    ])

def kb_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ Pending Payments", callback_data="pending"),
         InlineKeyboardButton(text="📊 Statistics", callback_data="stats")],
        [InlineKeyboardButton(text="🎫 Support Tickets", callback_data="tickets"),
         InlineKeyboardButton(text="📢 Broadcast", callback_data="broadcast")]
    ])

def kb_approve(payment_id, user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{payment_id}_{user_id}"),
         InlineKeyboardButton(text="❌ Deny", callback_data=f"deny_{payment_id}_{user_id}")]
    ])

# ULTRA-FAST message sending with error handling
async def send_fast(chat_id, text, keyboard=None, photo=None):
    """Ultra-optimized message sending with error handling"""
    try:
        if photo:
            return await bot.send_photo(chat_id, photo, caption=text, reply_markup=keyboard)
        else:
            return await bot.send_message(chat_id, text, reply_markup=keyboard)
    except Exception as e:
        log.error(f"Send error to {chat_id}: {e}")
        return None

async def edit_fast(query, text, keyboard=None):
    """Ultra-fast message editing with fallback"""
    try:
        await query.message.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        log.warning(f"Edit failed, sending new message: {e}")
        await send_fast(query.from_user.id, text, keyboard)

# MAIN HANDLERS - ULTRA OPTIMIZED

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    """Ultra-fast start handler with performance tracking"""
    start_time = time.time()
    
    try:
        await fast_upsert_user(message.from_user)
        
        if is_admin(message.from_user.id):
            text = f"""⚡ <b>ADMIN PANEL</b>

👋 Hello <b>{message.from_user.first_name}</b>!

🎯 Ultra-Fast Bot Control
⚡ Response Time Optimized
🔧 Advanced Management Tools

Choose an option:"""
            await send_fast(message.from_user.id, text, kb_admin())
        else:
            text = f"""⚡ <b>ULTRA-FAST PREMIUM BOT</b>

👋 Welcome <b>{message.from_user.first_name}</b>!

🚀 Lightning Fast Experience
💎 Premium Benefits Available
⚡ Instant Activation System
🔥 Tap-to-Copy UPI Feature

Ready to upgrade?"""
            await send_fast(message.from_user.id, text, kb_main())
        
        # Performance tracking
        response_time = (time.time() - start_time) * 1000
        log.info(f"⚡ Start response time: {response_time:.0f}ms for user {message.from_user.id}")
        
    except Exception as e:
        log.error(f"Start handler error: {e}")
        await send_fast(message.from_user.id, "❌ Error occurred. Please try again.")

@dp.callback_query(F.data == "menu")
async def menu_handler(query: types.CallbackQuery):
    try:
        if is_admin(query.from_user.id):
            await edit_fast(query, "⚡ <b>ADMIN PANEL</b>\n\nChoose management option:", kb_admin())
        else:
            await edit_fast(query, f"⚡ <b>MAIN MENU</b>\n\n👋 Welcome back <b>{query.from_user.first_name}</b>!", kb_main())
        await query.answer("⚡ Menu loaded")
    except Exception as e:
        log.error(f"Menu error: {e}")
        await query.answer("❌ Error loading menu")

@dp.callback_query(F.data == "buy")
async def plans_handler(query: types.CallbackQuery):
    try:
        text = """💎 <b>PREMIUM PLANS</b>

⚡ Ultra-Fast Activation
🔥 Instant Benefits
💳 Easy Payment Methods

Choose your plan:"""
        await edit_fast(query, text, kb_plans())
        await query.answer("💎 Plans loaded")
    except Exception as e:
        log.error(f"Plans error: {e}")
        await query.answer("❌ Error loading plans")

@dp.callback_query(F.data.startswith("plan_"))
async def plan_handler(query: types.CallbackQuery):
    try:
        plan_id = query.data.split("_")[1]
        plan = PLANS[plan_id]
        plan_cache[query.from_user.id] = plan_id
        
        daily_cost = round(int(plan['price']) / plan['days'], 1)
        
        text = f"""🎯 <b>{plan['emoji']} {plan['name']} Plan</b>

💰 <b>Price:</b> ₹{plan['price']}
📊 <b>Daily Cost:</b> ₹{daily_cost}
⏰ <b>Duration:</b> {plan['days']} days

✨ <b>Included Benefits:</b>
• Unlimited downloads
• Ad-free experience  
• Priority support
• Exclusive content

⚡ Choose payment method:"""
        
        await edit_fast(query, text, kb_payment(plan_id))
        await query.answer(f"Selected {plan['name']} plan!")
    except Exception as e:
        log.error(f"Plan selection error: {e}")
        await query.answer("❌ Error selecting plan")

# FIXED: ULTRA-FAST UPI COPY SYSTEM
@dp.callback_query(F.data.startswith("upi_"))
async def upi_handler(query: types.CallbackQuery):
    """FIXED: Ultra-fast UPI with perfect tap-to-copy functionality"""
    try:
        plan_id = query.data.split("_")[1]
        plan = PLANS[plan_id]
        
        # Main payment message
        text = f"""💳 <b>UPI PAYMENT METHOD</b>

📋 <b>Plan:</b> {plan['emoji']} {plan['name']}
💰 <b>Amount:</b> ₹{plan['price']}

⚡ <b>FASTEST PAYMENT STEPS:</b>
1️⃣ Tap UPI ID below to copy
2️⃣ Open any UPI app (GPay/PhonePe/Paytm)
3️⃣ Send Money → UPI ID → Paste
4️⃣ Enter amount: ₹{plan['price']}
5️⃣ Complete payment
6️⃣ Upload screenshot here

🔥 Premium activated in 2-5 minutes!"""
        
        await edit_fast(query, text, kb_payment(plan_id))
        
        # FIXED: Perfect tap-to-copy UPI message
        upi_copy_message = f"""📋 <b>TAP TO COPY UPI ID:</b>

<code>{UPI_ID}</code>

💰 <b>EXACT AMOUNT:</b> <code>₹{plan['price']}</code>

📱 <b>PAYMENT NOTE:</b> <code>Premium {plan['name']}</code>

🎯 <b>ONE-TAP COPY INSTRUCTIONS:</b>
• <b>Mobile:</b> Long press the UPI ID above
• <b>Desktop:</b> Click to select and copy
• <b>Works on:</b> All Telegram apps

⚡ <b>QUICK PAYMENT APPS:</b>
• GPay: Search UPI ID and pay
• PhonePe: Send Money → UPI ID
• Paytm: Send → UPI ID
• Any bank UPI app works!

🔥 <b>IMPORTANT:</b> Pay exactly ₹{plan['price']} rupees
✅ Upload payment screenshot after paying
🚀 Premium activated instantly!</code>"""
        
        # Send copyable UPI details
        await send_fast(query.from_user.id, upi_copy_message)
        await query.answer("💳 UPI ID sent! Tap to copy and pay in your app", show_alert=True)
        
    except Exception as e:
        log.error(f"UPI handler error: {e}")
        await query.answer("❌ Error loading UPI details")

@dp.callback_query(F.data.startswith("qr_"))
async def qr_handler(query: types.CallbackQuery):
    """QR Code payment method"""
    try:
        plan_id = query.data.split("_")[1]
        plan = PLANS[plan_id]
        
        text = f"""📱 <b>QR CODE PAYMENT</b>

📋 <b>Plan:</b> {plan['emoji']} {plan['name']}
💰 <b>Amount:</b> ₹{plan['price']}

📱 <b>STEPS:</b>
1️⃣ Open any UPI app
2️⃣ Scan QR code below
3️⃣ Enter amount: ₹{plan['price']}
4️⃣ Complete payment
5️⃣ Upload screenshot

⚡ Fast & secure payment!"""
        
        await send_fast(query.from_user.id, text, kb_payment(plan_id), QR_CODE_URL)
        await query.answer("📱 QR code sent! Scan to pay")
        
    except Exception as e:
        log.error(f"QR handler error: {e}")
        await query.answer("❌ Error loading QR code")

@dp.callback_query(F.data.startswith("upload_"))
async def upload_handler(query: types.CallbackQuery):
    """Upload screenshot handler"""
    try:
        plan_id = query.data.split("_")[1]
        plan = PLANS[plan_id]
        plan_cache[query.from_user.id] = plan_id
        
        text = f"""📸 <b>UPLOAD PAYMENT SCREENSHOT</b>

📋 <b>Plan:</b> {plan['emoji']} {plan['name']} - ₹{plan['price']}

📷 <b>Screenshot Requirements:</b>
✅ Payment success message visible
✅ Amount ₹{plan['price']} clearly shown
✅ Transaction ID visible
✅ Clear and readable image

📤 <b>Send your payment screenshot now:</b>
(Photo will be reviewed by admin instantly)"""
        
        await edit_fast(query, text)
        await query.answer("📸 Ready to receive screenshot!")
        
    except Exception as e:
        log.error(f"Upload handler error: {e}")
        await query.answer("❌ Error preparing upload")

@dp.callback_query(F.data == "status")
async def status_handler(query: types.CallbackQuery):
    """User subscription status"""
    try:
        user = await get_user_fast(query.from_user.id)
        
        if user and user.get("status") == "active":
            plan = PLANS.get(user.get("plan_key"), {"name": "Premium", "emoji": "💎"})
            end_date = user.get("end_at")
            if end_date:
                days_left = (end_date - datetime.now(timezone.utc)).days
                text = f"""📊 <b>PREMIUM STATUS</b>

✅ <b>Status:</b> ACTIVE PREMIUM
{plan['emoji']} <b>Plan:</b> {plan['name']}
⏰ <b>Days Left:</b> {days_left} days
📅 <b>Expires:</b> {end_date.strftime('%d %b %Y')}

🎉 <b>Active Benefits:</b>
• Unlimited downloads
• Ad-free experience
• Priority support
• Exclusive content

💎 Thank you for being premium!"""
            else:
                text = f"""📊 <b>PREMIUM STATUS</b>

✅ <b>Status:</b> LIFETIME PREMIUM
💎 <b>Plan:</b> {plan['name']}
⚡ <b>Expires:</b> Never

🔥 All premium benefits forever active!"""
        else:
            text = """📊 <b>ACCOUNT STATUS</b>

❌ <b>Status:</b> FREE USER
🚀 <b>Action:</b> Upgrade to Premium

💎 <b>Premium Benefits:</b>
• Unlimited downloads
• Ad-free experience
• Priority support
• Exclusive premium content
• Lightning fast service

🔥 Upgrade now for instant access!"""
        
        await edit_fast(query, text, kb_main())
        await query.answer("📊 Status updated")
        
    except Exception as e:
        log.error(f"Status handler error: {e}")
        await query.answer("❌ Error loading status")

# FIXED: ULTRA-FAST SUPPORT SYSTEM
@dp.callback_query(F.data == "support")
async def support_handler(query: types.CallbackQuery):
    """FIXED: Ultra-fast support system activation"""
    try:
        support_mode.add(query.from_user.id)
        
        user = await get_user_fast(query.from_user.id)
        is_premium = user and user.get("status") == "active"
        response_time = "2-5 minutes" if is_premium else "10-30 minutes"
        priority = "HIGH PRIORITY" if is_premium else "NORMAL PRIORITY"
        
        text = f"""💬 <b>ULTRA-FAST SUPPORT</b>

Hi <b>{query.from_user.first_name}</b>! ⚡

🔥 <b>Support Features:</b>
• Instant admin notification
• Real-time ticket system
• {priority}
• Response time: {response_time}

📝 <b>Send your message now:</b>
(Your next message will go directly to admin)

⚡ Support mode is now ACTIVE!</code>"""
        
        await edit_fast(query, text)
        await query.answer("💬 Support activated! Send your message now.", show_alert=True)
        
    except Exception as e:
        log.error(f"Support handler error: {e}")
        await query.answer("❌ Error activating support")

# FIXED: Support message handling
@dp.message(F.text & ~F.command)
async def message_handler(message: types.Message):
    """FIXED: Ultra-fast support message processing"""
    if is_admin(message.from_user.id):
        return
        
    user_id = message.from_user.id
    
    try:
        # Check if user is in support mode
        if user_id in support_mode:
            support_mode.discard(user_id)  # Remove from support mode
            
            # Ultra-fast ticket creation
            ticket_data = {
                "user_id": user_id,
                "message": message.text,
                "status": "open",
                "created_at": datetime.now(timezone.utc)
            }
            
            result = await tickets_col.insert_one(ticket_data)
            ticket_id = str(result.inserted_id)[:8]
            
            # Get user status for priority
            user = await get_user_fast(user_id)
            is_premium = user and user.get("status") == "active"
            priority = "🔥 HIGH PRIORITY" if is_premium else "⚡ NORMAL PRIORITY"
            
            # FIXED: Instant admin notification with enhanced formatting
            admin_text = f"""🎫 <b>SUPPORT TICKET #{ticket_id}</b>

{priority}
👤 <b>User:</b> {message.from_user.first_name} {message.from_user.last_name or ''}
📱 <b>Username:</b> @{message.from_user.username or 'No username'}
🆔 <b>User ID:</b> <code>{user_id}</code>
💎 <b>Status:</b> {'PREMIUM USER' if is_premium else 'FREE USER'}

💬 <b>MESSAGE:</b>
<i>"{message.text}"</i>

📞 <b>QUICK REPLY:</b>
<code>/reply {user_id} Your response here</code>

⏰ <b>Created:</b> {datetime.now().strftime('%d %b %Y, %H:%M IST')}"""
            
            # Send to admin instantly
            await send_fast(ADMIN_ID, admin_text)
            
            # Enhanced user confirmation
            response_time = "2-5 minutes" if is_premium else "10-30 minutes"
            user_confirmation = f"""✅ <b>SUPPORT TICKET CREATED!</b>

🎫 <b>Ticket ID:</b> #{ticket_id}
{priority}
⏰ <b>Response Time:</b> {response_time}

🔔 <b>What's Next:</b>
• Admin has been notified instantly
• You'll get a reply soon
• No need to send more messages

💡 <b>Need to add details?</b> Click Support again!

Thank you for contacting us! ⚡"""
            
            await send_fast(user_id, user_confirmation, kb_main())
            
            log.info(f"⚡ Support ticket {ticket_id} created for user {user_id}")
            return
    
        # Guide user to support if not in support mode
        await send_fast(user_id, """💬 <b>Need Support?</b>

Click the <b>Support</b> button below to start a support chat!

⚡ Our ultra-fast support system will connect you directly with admin.""", kb_main())
        
    except Exception as e:
        log.error(f"Message handler error: {e}")
        await send_fast(user_id, "❌ Error processing message. Please try again.", kb_main())

@dp.message(F.photo)
async def photo_handler(message: types.Message):
    """Ultra-fast payment screenshot processing"""
    if is_admin(message.from_user.id):
        return
        
    user_id = message.from_user.id
    plan_id = plan_cache.get(user_id)
    
    try:
        if not plan_id:
            await send_fast(user_id, "❌ Please select a plan first using /start", kb_main())
            return
        
        plan = PLANS[plan_id]
        
        # Ultra-fast payment record
        payment_data = {
            "user_id": user_id,
            "plan_key": plan_id,
            "file_id": message.photo[-1].file_id,
            "created_at": datetime.now(timezone.utc),
            "status": "pending"
        }
        
        result = await payments_col.insert_one(payment_data)
        payment_id = str(result.inserted_id)[:8]
        
        # User confirmation
        user_msg = f"""🎉 <b>PAYMENT SCREENSHOT RECEIVED!</b>

📸 <b>Payment ID:</b> #{payment_id}
📋 <b>Plan:</b> {plan['emoji']} {plan['name']} - ₹{plan['price']}

⚡ <b>Processing Status:</b>
• Screenshot uploaded successfully
• Admin reviewing now
• You'll be notified when approved

🔥 <b>Activation Time:</b>
• Premium users: 2-5 minutes
• Free users: 10-30 minutes

Thank you for your payment! 💎"""
        
        await send_fast(user_id, user_msg, kb_main())
        
        # Enhanced admin notification
        admin_notification = f"""💰 <b>NEW PAYMENT SUBMISSION #{payment_id}</b>

👤 <b>User:</b> {message.from_user.first_name} (@{message.from_user.username or 'No username'})
🆔 <b>User ID:</b> <code>{user_id}</code>
📋 <b>Plan:</b> {plan['emoji']} {plan['name']} - ₹{plan['price']}
⏰ <b>Time:</b> {datetime.now().strftime('%d %b %Y, %H:%M IST')}

📸 Payment screenshot attached below ⬇️"""
        
        await send_fast(ADMIN_ID, admin_notification)
        await bot.send_photo(
            ADMIN_ID, 
            message.photo[-1].file_id, 
            caption=f"""💰 <b>Payment Screenshot #{payment_id}</b>

{plan['emoji']} {plan['name']} - ₹{plan['price']}
👤 User: <code>{user_id}</code>""",
            reply_markup=kb_approve(str(result.inserted_id), user_id)
        )
        
        log.info(f"⚡ Payment {payment_id} processed for user {user_id}")
        
    except Exception as e:
        log.error(f"Payment photo error: {e}")
        await send_fast(user_id, "❌ Error processing payment screenshot. Please try again.", kb_main())

# ADMIN HANDLERS - ULTRA OPTIMIZED

@dp.callback_query(F.data == "stats")
async def admin_stats(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not authorized", show_alert=True)
        return
        
    try:
        # Ultra-fast parallel stats gathering
        total_task = users_col.count_documents({})
        active_task = users_col.count_documents({"status": "active"})
        pending_task = payments_col.count_documents({"status": "pending"})
        tickets_task = tickets_col.count_documents({"status": "open"})
        
        total, active, pending, open_tickets = await asyncio.gather(
            total_task, active_task, pending_task, tickets_task
        )
        
        # Additional stats
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        new_today = await users_col.count_documents({"created_at": {"$gte": today}})
        
        text = f"""📊 <b>ULTRA-FAST BOT STATISTICS</b>

👥 <b>USER STATS:</b>
• Total Users: <code>{total}</code>
• Premium Active: <code>{active}</code>
• New Today: <code>{new_today}</code>

💰 <b>PAYMENT STATS:</b>
• Pending Payments: <code>{pending}</code>

🎫 <b>SUPPORT STATS:</b>
• Open Tickets: <code>{open_tickets}</code>

⚡ <b>Performance:</b> Ultra-Fast
🕐 <b>Updated:</b> {datetime.now().strftime('%H:%M IST')}"""
        
        await edit_fast(query, text, kb_admin())
        await query.answer(f"📊 Stats: {total} users, {active} premium")
        
    except Exception as e:
        log.error(f"Admin stats error: {e}")
        await query.answer("❌ Error loading statistics")

@dp.callback_query(F.data == "pending")
async def admin_pending(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not authorized", show_alert=True)
        return
        
    try:
        payments = await payments_col.find({"status": "pending"}).sort("created_at", -1).limit(10).to_list(10)
        
        if not payments:
            await query.message.answer("✅ <b>No pending payments!</b>\n\nAll payments processed! 🎉", reply_markup=kb_admin())
            await query.answer("✅ No pending payments")
            return
        
        await query.message.answer(f"⏳ <b>{len(payments)} PENDING PAYMENTS</b>\n\n(Latest first, max 10 shown)")
        
        for payment in payments:
            user = await get_user_fast(payment['user_id'])
            plan = PLANS[payment['plan_key']]
            user_name = user['first_name'] if user else 'Unknown User'
            
            text = f"""💰 <b>Payment #{str(payment['_id'])[:8]}...</b>

👤 <b>User:</b> {user_name} (ID: <code>{payment['user_id']}</code>)
📋 <b>Plan:</b> {plan['emoji']} {plan['name']} - ₹{plan['price']}
⏰ <b>Submitted:</b> {payment['created_at'].strftime('%d %b, %H:%M IST')}

📸 Review screenshot and approve/deny:"""
            
            await query.message.answer(text, reply_markup=kb_approve(str(payment['_id']), payment['user_id']))
        
        await query.answer(f"⏳ {len(payments)} payments loaded")
        
    except Exception as e:
        log.error(f"Admin pending error: {e}")
        await query.answer("❌ Error loading pending payments")

@dp.callback_query(F.data == "tickets")
async def admin_tickets(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not authorized", show_alert=True)
        return
        
    try:
        tickets = await tickets_col.find({"status": "open"}).sort("created_at", -1).limit(10).to_list(10)
        
        if not tickets:
            await query.message.answer("✅ <b>No open support tickets!</b>\n\nGreat job managing support! 🎉", reply_markup=kb_admin())
            await query.answer("✅ No open tickets")
            return
        
        await query.message.answer(f"🎫 <b>{len(tickets)} OPEN SUPPORT TICKETS</b>\n\n(Latest first, max 10 shown)")
        
        for ticket in tickets:
            user = await get_user_fast(ticket['user_id'])
            user_name = user['first_name'] if user else 'Unknown User'
            priority = "🔥 HIGH" if user and user.get("status") == "active" else "⚡ NORMAL"
            
            text = f"""🎫 <b>Ticket #{str(ticket['_id'])[:8]}...</b>

{priority} PRIORITY
👤 <b>User:</b> {user_name} (ID: <code>{ticket['user_id']}</code>)
⏰ <b>Created:</b> {ticket['created_at'].strftime('%d %b, %H:%M IST')}

💬 <b>Message:</b>
<i>"{ticket['message'][:150]}{'...' if len(ticket['message']) > 150 else ''}"</i>

📞 <b>Quick Reply:</b>
<code>/reply {ticket['user_id']} Your response message</code>"""
            
            await query.message.answer(text)
        
        await query.answer(f"🎫 {len(tickets)} tickets loaded")
        
    except Exception as e:
        log.error(f"Admin tickets error: {e}")
        await query.answer("❌ Error loading tickets")

# Enhanced approval/denial system
@dp.callback_query(F.data.startswith("approve_"))
async def approve_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not authorized", show_alert=True)
        return
    
    try:
        parts = query.data.split("_")
        payment_id, user_id = parts[1], int(parts[2])
        
        payment = await payments_col.find_one({"_id": ObjectId(payment_id)})
        if not payment:
            await query.answer("❌ Payment not found", show_alert=True)
            return
        
        plan_key = payment["plan_key"]
        plan = PLANS[plan_key]
        
        # Ultra-fast approval process
        now = datetime.now(timezone.utc)
        end_date = now + timedelta(days=plan['days'])
        
        # Parallel database updates
        payment_update = payments_col.update_one({"_id": ObjectId(payment_id)}, {"$set": {"status": "approved"}})
        user_update = users_col.update_one(
            {"user_id": user_id},
            {"$set": {
                "plan_key": plan_key,
                "start_at": now,
                "end_at": end_date,
                "status": "active"
            }}
        )
        
        await asyncio.gather(payment_update, user_update)
        
        # Update cache
        if user_id in user_cache:
            user_cache[user_id].update({
                "status": "active",
                "plan_key": plan_key,
                "start_at": now,
                "end_at": end_date
            })
        
        # Create channel invite link
        try:
            link = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
            invite_link = f"\n\n🔗 <b>Join Premium Channel:</b>\n{link.invite_link}"
        except:
            invite_link = ""
        
        # Enhanced user notification
        user_msg = f"""🎉 <b>PAYMENT APPROVED - WELCOME TO PREMIUM!</b>

✅ Your <b>{plan['emoji']} {plan['name']}</b> subscription is now <b>ACTIVE!</b>

📋 <b>Subscription Details:</b>
• Plan: {plan['name']}
• Amount Paid: ₹{plan['price']}
• Duration: {plan['days']} days
• Started: {start_date.strftime('%d %b %Y')}
• Expires: {end_date.strftime('%d %b %Y')}

🎊 <b>Premium Benefits Now Active:</b>
• ✅ Unlimited downloads
• ✅ Ad-free experience
• ✅ Priority support
• ✅ Exclusive content access
• ✅ Lightning fast service{invite_link}

💎 <b>Welcome to the Premium family!</b>
🚀 Thank you for your support!

Use /start to access all premium features!"""
        
        await send_fast(user_id, user_msg)
        
        # Update admin message
        try:
            await query.message.edit_text(f"""✅ <b>PAYMENT APPROVED & PREMIUM ACTIVATED</b>

💰 <b>Payment ID:</b> #{payment_id[:8]}...
👤 <b>User ID:</b> <code>{user_id}</code>
📋 <b>Plan:</b> {plan['emoji']} {plan['name']} - ₹{plan['price']}
✅ <b>Status:</b> APPROVED & ACTIVATED
⏰ <b>Processed:</b> {datetime.now().strftime('%d %b, %H:%M IST')}

User has been notified and premium activated instantly! ⚡""")
        except:
            await query.message.answer(f"✅ <b>APPROVED</b> - Payment #{payment_id[:8]}... for user {user_id}")
        
        await query.answer("✅ Payment approved & premium activated!", show_alert=True)
        log.info(f"⚡ Payment {payment_id} approved for user {user_id}")
        
    except Exception as e:
        log.error(f"Approval error: {e}")
        await query.answer("❌ Error approving payment", show_alert=True)

@dp.callback_query(F.data.startswith("deny_"))
async def deny_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not authorized", show_alert=True)
        return
    
    try:
        parts = query.data.split("_")
        payment_id, user_id = parts[1], int(parts[2])
        
        await payments_col.update_one({"_id": ObjectId(payment_id)}, {"$set": {"status": "denied"}})
        
        user_msg = """❌ <b>Payment Verification Failed</b>

Your payment screenshot could not be verified.

🔍 <b>Common Issues:</b>
• Screenshot not clear enough
• Amount doesn't match exactly
• Payment not completed successfully
• Transaction details missing or unclear

📸 <b>To resolve this:</b>
1. Take a clearer screenshot of payment success
2. Ensure amount and transaction ID are visible
3. Make sure the image is clear and readable
4. Upload again using /start

💬 <b>Need help?</b> Contact support for assistance.

🚀 We're here to help you get premium access!"""
        
        await send_fast(user_id, user_msg)
        
        # Update admin message
        try:
            await query.message.edit_text(f"""❌ <b>PAYMENT DENIED</b>

💰 <b>Payment ID:</b> #{payment_id[:8]}...
👤 <b>User ID:</b> <code>{user_id}</code>
❌ <b>Status:</b> DENIED
⏰ <b>Processed:</b> {datetime.now().strftime('%d %b, %H:%M IST')}

User has been notified with clear instructions to resubmit.""")
        except:
            await query.message.answer(f"❌ <b>DENIED</b> - Payment #{payment_id[:8]}... for user {user_id}")
        
        await query.answer("❌ Payment denied & user notified!", show_alert=True)
        log.info(f"⚡ Payment {payment_id} denied for user {user_id}")
        
    except Exception as e:
        log.error(f"Denial error: {e}")
        await query.answer("❌ Error denying payment", show_alert=True)

@dp.callback_query(F.data == "broadcast")
async def broadcast_start(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not authorized", show_alert=True)
        return
        
    try:
        total = await users_col.count_documents({})
        active = await users_col.count_documents({"status": "active"})
        
        text = f"""📢 <b>BROADCAST MESSAGE TO ALL USERS</b>

📊 <b>Target Audience:</b>
• Total Users: <code>{total}</code>
• Premium Users: <code>{active}</code>
• Free Users: <code>{total - active}</code>

📝 <b>Send your broadcast message:</b>
(Your next message will be sent to all {total} users)

⚡ Ultra-fast delivery system ready!"""
        
        await query.message.answer(text)
        await state.set_state(Broadcast.message)
        await query.answer("📢 Broadcast system ready")
        
    except Exception as e:
        log.error(f"Broadcast start error: {e}")
        await query.answer("❌ Error preparing broadcast")

@dp.message(Broadcast.message)
async def broadcast_send(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
        
    try:
        users = await users_col.find({}, {"user_id": 1}).to_list(None)
        total_users = len(users)
        
        await message.answer(f"""📤 <b>BROADCASTING MESSAGE...</b>

📊 Sending to <code>{total_users}</code> users
⚡ Ultra-fast delivery in progress
🕐 Estimated time: {total_users * 0.05:.1f} seconds

Please wait...""")
        
        sent = failed = 0
        start_time = time.time()
        
        broadcast_message = f"""📢 <b>PREMIUM BOT ANNOUNCEMENT</b>

{message.text}

──────────────────────
⚡ Ultra-Fast Premium Bot Team
🚀 Upgrade to premium for exclusive benefits!
💎 Use /start to access all features"""
        
        # Ultra-fast broadcasting with batching
        batch_size = 20
        for i in range(0, len(users), batch_size):
            batch = users[i:i + batch_size]
            tasks = []
            
            for user in batch:
                tasks.append(send_fast(user["user_id"], broadcast_message))
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for result in results:
                if isinstance(result, Exception):
                    failed += 1
                else:
                    sent += 1
            
            # Small delay between batches
            await asyncio.sleep(0.1)
        
        end_time = time.time()
        duration = end_time - start_time
        
        await message.answer(f"""📢 <b>BROADCAST COMPLETED!</b>

✅ <b>Successfully Sent:</b> <code>{sent}</code>
❌ <b>Failed to Send:</b> <code>{failed}</code>
📊 <b>Success Rate:</b> <code>{(sent/(sent+failed)*100):.1f}%</code>
⚡ <b>Delivery Time:</b> <code>{duration:.1f}</code> seconds
🚀 <b>Speed:</b> <code>{sent/duration:.1f}</code> msg/sec

⏰ <b>Completed:</b> {datetime.now().strftime('%d %b %Y, %H:%M IST')}

Ultra-fast broadcast system! ⚡""", reply_markup=kb_admin())
        
        await state.clear()
        log.info(f"⚡ Broadcast completed: {sent} sent, {failed} failed in {duration:.1f}s")
        
    except Exception as e:
        log.error(f"Broadcast send error: {e}")
        await message.answer("❌ Error during broadcast. Please try again.")
        await state.clear()

# FIXED: Ultra-fast admin reply system
@dp.message(Command("reply"))
async def admin_reply(message: types.Message):
    if not is_admin(message.from_user.id):
        return
        
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.answer("""❌ <b>Invalid Format</b>

📝 <b>Correct Usage:</b>
<code>/reply &lt;user_id&gt; &lt;your_response_message&gt;</code>

📝 <b>Example:</b>
<code>/reply 123456789 Hello! Thanks for contacting support. Your issue has been resolved.</code>

💡 <b>Tips:</b>
• Use user ID from support tickets
• Keep responses clear and helpful
• Professional tone recommended""")
            return
            
        user_id, reply_text = int(parts[1]), parts[2]
        
        # Get user info for personalized response
        user = await get_user_fast(user_id)
        user_name = user['first_name'] if user else 'User'
        
        # FIXED: Enhanced ultra-fast support response
        response = f"""💬 <b>SUPPORT TEAM RESPONSE</b>

Hi <b>{user_name}</b>! 👋

{reply_text}

──────────────────────
⚡ <b>Ultra-Fast Support Team</b>
📞 <b>Need more help?</b> Use Support button anytime!
💎 <b>Thank you for choosing our service!</b>

Use /start to access all features!"""
        
        await send_fast(user_id, response)
        
        # Close related tickets automatically
        await tickets_col.update_many(
            {"user_id": user_id, "status": "open"}, 
            {"$set": {"status": "closed"}}
        )
        
        await message.answer(f"""✅ <b>REPLY SENT SUCCESSFULLY!</b>

👤 <b>To User:</b> {user_name} (ID: <code>{user_id}</code>)
💬 <b>Message:</b> "{reply_text[:100]}{'...' if len(reply_text) > 100 else ''}"
🎫 <b>Related Tickets:</b> Closed automatically
⚡ <b>Delivery:</b> Instant
⏰ <b>Sent:</b> {datetime.now().strftime('%H:%M IST')}

Ultra-fast support response delivered! 🚀""")
        
        log.info(f"⚡ Admin replied to user {user_id}")
        
    except ValueError:
        await message.answer("❌ <b>INVALID USER ID</b>\n\nUser ID must be a number.\n\n<b>Usage:</b> <code>/reply &lt;user_id&gt; &lt;message&gt;</code>")
    except Exception as e:
        log.error(f"Admin reply error: {e}")
        await message.answer(f"❌ <b>ERROR SENDING REPLY</b>\n\n<code>{str(e)}</code>")

# Ultra-fast expiry worker with enhanced notifications
async def expiry_worker():
    """Ultra-optimized expiry management system"""
    while True:
        try:
            now = datetime.now(timezone.utc)
            
            # Process expired subscriptions with batch operations
            expired_users = await users_col.find({"status": "active", "end_at": {"$lte": now}}).to_list(None)
            
            if expired_users:
                # Batch update all expired users
                expired_ids = [user["user_id"] for user in expired_users]
                await users_col.update_many(
                    {"user_id": {"$in": expired_ids}}, 
                    {"$set": {"status": "expired"}}
                )
                
                # Update cache
                for user in expired_users:
                    if user["user_id"] in user_cache:
                        user_cache[user["user_id"]]["status"] = "expired"
                
                # Send expiry notifications
                for user in expired_users:
                    try:
                        # Remove from channel
                        try:
                            await bot.ban_chat_member(CHANNEL_ID, user["user_id"])
                            await bot.unban_chat_member(CHANNEL_ID, user["user_id"])
                        except: 
                            pass
                        
                        # Enhanced expiry notification
                        expiry_msg = f"""⏰ <b>Premium Subscription Expired</b>

Hi <b>{user.get('first_name', 'User')}</b>!

Your premium subscription has expired, but don't worry!

🔄 <b>Renew now to continue enjoying:</b>
• ✅ Unlimited downloads
• ✅ Ad-free experience
• ✅ Priority support
• ✅ Exclusive premium content
• ✅ Lightning fast service

🎁 <b>Special Renewal Offers Available!</b>

🚀 <b>Renew instantly:</b> /start

⚡ Thank you for being part of our premium community!"""
                        
                        await send_fast(user["user_id"], expiry_msg)
                        await asyncio.sleep(0.1)  # Rate limiting
                        
                    except Exception as e:
                        log.error(f"Expiry notification error for {user['user_id']}: {e}")
                
                # Notify admin of batch expiry
                await send_fast(ADMIN_ID, f"⏰ <b>{len(expired_users)} users' subscriptions expired</b>\n\nBatch processed automatically.")
                log.info(f"⚡ Processed {len(expired_users)} expired subscriptions")
            
            # Send 3-day expiry reminders
            reminder_date = now + timedelta(days=3)
            reminder_users = await users_col.find({
                "status": "active", 
                "end_at": {"$lte": reminder_date, "$gt": now},
                "reminded_3d": {"$ne": True}
            }).to_list(None)
            
            for user in reminder_users:
                try:
                    days_left = (user["end_at"] - now).days
                    plan = PLANS.get(user["plan_key"], {"name": "Premium", "emoji": "💎"})
                    
                    reminder_msg = f"""⏰ <b>Subscription Expiring Soon!</b>

Hi <b>{user.get('first_name', 'User')}</b>!

Your <b>{plan['emoji']} {plan['name']}</b> subscription expires in <b>{days_left} day{'s' if days_left != 1 else ''}</b>!

🚀 <b>Renew now to continue enjoying:</b>
• Unlimited downloads
• Ad-free experience
• Priority support
• Exclusive premium content
• Lightning fast service

💡 <b>Renew early for special discounts!</b>

Use /start to renew your subscription instantly! ⚡"""
                    
                    await send_fast(user["user_id"], reminder_msg)
                    await users_col.update_one(
                        {"user_id": user["user_id"]}, 
                        {"$set": {"reminded_3d": True}}
                    )
                    
                    await asyncio.sleep(0.1)  # Rate limiting
                    
                except Exception as e:
                    log.error(f"Reminder error for {user['user_id']}: {e}")
            
            if reminder_users:
                log.info(f"⚡ Sent {len(reminder_users)} expiry reminders")
                    
        except Exception as e:
            log.error(f"Expiry worker error: {e}")
        
        # Check every 30 minutes for optimal performance
        await asyncio.sleep(1800)

async def main():
    """Ultra-fast bot startup with comprehensive validation"""
    try:
        print("⚡ ULTRA-FAST PREMIUM BOT")
        print("🔧 Initializing connections...")
        
        # Validate all connections
        if not await validate_connection():
            print("❌ Connection validation failed!")
            return
        
        # Start background tasks
        asyncio.create_task(expiry_worker())
        log.info("✅ Ultra-fast expiry worker started")
        
        print("🚀 Bot Features:")
        print("   • ⚡ Sub-100ms response times")
        print("   • 📋 Perfect UPI tap-to-copy")
        print("   • 💬 Instant support system")
        print("   • 🎯 Advanced admin panel")
        print("   • 🔥 Real-time notifications")
        print("   • 💎 Premium user management")
        
        print("\n⚡ STARTING ULTRA-FAST PREMIUM BOT...")
        print("🔥 All systems optimized for maximum speed!")
        
        await dp.start_polling(bot, skip_updates=True)
        
    except Exception as e:
        log.error(f"❌ Fatal startup error: {e}")
        print(f"❌ Bot failed to start: {e}")
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("✅ Ultra-fast bot stopped gracefully")
        print("✅ Bot stopped by user")
    except Exception as e:
        log.error(f"❌ Fatal error: {e}")
        print(f"❌ Fatal error: {e}")
