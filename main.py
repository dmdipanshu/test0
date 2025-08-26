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
import time

# Ultra-fast logging setup
logging.basicConfig(level=logging.WARNING)  # Reduced logging for speed
log = logging.getLogger("fastbot")

# Environment variables - cached for speed
API_TOKEN = os.getenv("API_TOKEN") or "TEST_TOKEN"
ADMIN_ID = int(os.getenv("ADMIN_ID") or "123456789")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or "-10012345678")
UPI_ID = os.getenv("UPI_ID") or "yourupi@upi"
QR_CODE_URL = os.getenv("QR_CODE_URL") or "https://example.com/qr.png"

# Optimized MongoDB with connection pooling
mongo_client = AsyncIOMotorClient(
    os.getenv("MONGO_URI") or "mongodb://localhost:27017",
    maxPoolSize=50,  # Increased pool for speed
    minPoolSize=10,
    maxIdleTimeMS=30000,
    serverSelectionTimeoutMS=5000
)
db = mongo_client['premium_bot']
users_col = db['users']
payments_col = db['payments']
tickets_col = db['tickets']

# Ultra-fast bot setup
bot = Bot(token=API_TOKEN, parse_mode='HTML')
dp = Dispatcher(storage=MemoryStorage())

# Optimized plans - cached
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

async def get_user_fast(user_id):
    """Ultra-fast user retrieval with caching"""
    if user_id in user_cache:
        return user_cache[user_id]
    
    user = await users_col.find_one({"user_id": user_id})
    if user:
        user_cache[user_id] = user
    return user

# FIXED: Ultra-fast keyboard functions
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Premium", callback_data="buy")],
        [InlineKeyboardButton(text="📊 Status", callback_data="status"),
         InlineKeyboardButton(text="💬 Support", callback_data="support")]
    ])

def kb_plans():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{p['emoji']} {p['name']} - ₹{p['price']}", callback_data=f"plan_{k}")] 
        for k, p in PLANS.items()] + 
        [[InlineKeyboardButton(text="⬅️ Back", callback_data="menu")]]
    )

def kb_payment(plan_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 UPI Pay", callback_data=f"upi_{plan_id}")],
        [InlineKeyboardButton(text="📸 Upload Screenshot", callback_data=f"upload_{plan_id}")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="buy")]
    ])

def kb_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ Pending", callback_data="pending"),
         InlineKeyboardButton(text="📊 Stats", callback_data="stats")],
        [InlineKeyboardButton(text="🎫 Tickets", callback_data="tickets"),
         InlineKeyboardButton(text="📢 Broadcast", callback_data="broadcast")]
    ])

def kb_approve(payment_id, user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{payment_id}_{user_id}"),
         InlineKeyboardButton(text="❌ Deny", callback_data=f"deny_{payment_id}_{user_id}")]
    ])

# ULTRA-FAST message sending
async def send_fast(chat_id, text, keyboard=None):
    """Ultra-optimized message sending"""
    try:
        return await bot.send_message(chat_id, text, reply_markup=keyboard)
    except Exception as e:
        log.error(f"Send error {chat_id}: {e}")
        return None

async def edit_fast(query, text, keyboard=None):
    """Ultra-fast message editing"""
    try:
        await query.message.edit_text(text, reply_markup=keyboard)
    except:
        await send_fast(query.from_user.id, text, keyboard)

# MAIN HANDLERS - ULTRA OPTIMIZED

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    """Ultra-fast start handler"""
    start_time = time.time()
    
    await fast_upsert_user(message.from_user)
    
    if is_admin(message.from_user.id):
        text = f"⚡ ADMIN PANEL\n\n👋 {message.from_user.first_name}\n🎯 Lightning Fast Control"
        await send_fast(message.from_user.id, text, kb_admin())
    else:
        text = f"⚡ PREMIUM BOT\n\n👋 {message.from_user.first_name}\n🚀 Ultra Fast Experience\n💎 Premium Benefits Await"
        await send_fast(message.from_user.id, text, kb_main())
    
    # Speed metrics
    response_time = (time.time() - start_time) * 1000
    log.info(f"Start response: {response_time:.0f}ms")

@dp.callback_query(F.data == "menu")
async def menu_handler(query: types.CallbackQuery):
    if is_admin(query.from_user.id):
        await edit_fast(query, "⚡ ADMIN PANEL", kb_admin())
    else:
        await edit_fast(query, f"⚡ MAIN MENU\n\n👋 {query.from_user.first_name}", kb_main())
    await query.answer("⚡")

@dp.callback_query(F.data == "buy")
async def plans_handler(query: types.CallbackQuery):
    text = "💎 PREMIUM PLANS\n\n⚡ Choose Your Plan:"
    await edit_fast(query, text, kb_plans())
    await query.answer("💎")

@dp.callback_query(F.data.startswith("plan_"))
async def plan_handler(query: types.CallbackQuery):
    plan_id = query.data.split("_")[1]
    plan = PLANS[plan_id]
    plan_cache[query.from_user.id] = plan_id
    
    text = f"🎯 {plan['emoji']} {plan['name']}\n💰 ₹{plan['price']}\n⏰ {plan['days']} days\n\n⚡ Choose Payment:"
    await edit_fast(query, text, kb_payment(plan_id))
    await query.answer(f"Selected {plan['name']}")

# FIXED: ULTRA-FAST UPI COPY SYSTEM
@dp.callback_query(F.data.startswith("upi_"))
async def upi_handler(query: types.CallbackQuery):
    """FIXED: Ultra-fast UPI with tap-to-copy functionality"""
    plan_id = query.data.split("_")[1]
    plan = PLANS[plan_id]
    
    # Main UPI message
    text = f"""💳 UPI PAYMENT

📋 Plan: {plan['emoji']} {plan['name']}
💰 Amount: ₹{plan['price']}

⚡ INSTANT PAYMENT STEPS:
1. Tap UPI ID below to copy
2. Open any UPI app
3. Pay ₹{plan['price']}
4. Upload screenshot here

🔥 Premium activated in 2-5 minutes!"""
    
    await edit_fast(query, text, kb_payment(plan_id))
    
    # FIXED: Tap-to-copy UPI ID with proper formatting
    upi_copy_text = f"""📋 TAP TO COPY UPI ID:

<code>{UPI_ID}</code>

💰 AMOUNT: <code>₹{plan['price']}</code>

🚀 QUICK STEPS:
1. TAP the UPI ID above (it will copy automatically)
2. Open GPay/PhonePe/Paytm
3. Send Money → UPI ID
4. Paste: {UPI_ID}
5. Enter: ₹{plan['price']}
6. Pay & screenshot
7. Upload screenshot here

⚡ FASTEST PAYMENT METHOD!
🔥 Premium activated instantly after verification!"""
    
    # Send copyable UPI message
    await send_fast(query.from_user.id, upi_copy_text)
    await query.answer("💳 UPI ID sent! Tap to copy", show_alert=True)

@dp.callback_query(F.data.startswith("upload_"))
async def upload_handler(query: types.CallbackQuery):
    plan_id = query.data.split("_")[1]
    plan = PLANS[plan_id]
    plan_cache[query.from_user.id] = plan_id
    
    text = f"""📸 UPLOAD SCREENSHOT

📋 Plan: {plan['emoji']} {plan['name']} - ₹{plan['price']}

📷 Requirements:
✅ Payment success visible
✅ Amount ₹{plan['price']} shown
✅ Clear & readable

📤 Send screenshot now:"""
    
    await edit_fast(query, text)
    await query.answer("📸 Send screenshot!")

@dp.callback_query(F.data == "status")
async def status_handler(query: types.CallbackQuery):
    user = await get_user_fast(query.from_user.id)
    
    if user and user.get("status") == "active":
        plan = PLANS.get(user.get("plan_key"), {"name": "Premium", "emoji": "💎"})
        end_date = user.get("end_at")
        days_left = (end_date - datetime.now(timezone.utc)).days if end_date else 0
        
        text = f"""📊 PREMIUM STATUS

✅ ACTIVE
{plan['emoji']} {plan['name']}
⏰ {days_left} days left

🎉 All benefits active!"""
    else:
        text = """📊 ACCOUNT STATUS

❌ FREE USER
🚀 Upgrade to Premium

💎 Benefits:
• Unlimited access
• Priority support
• Ad-free experience"""
    
    await edit_fast(query, text, kb_main())
    await query.answer("📊")

# FIXED: ULTRA-FAST SUPPORT SYSTEM
@dp.callback_query(F.data == "support")
async def support_handler(query: types.CallbackQuery):
    """FIXED: Ultra-fast support system"""
    support_mode.add(query.from_user.id)
    
    text = f"""💬 ULTRA-FAST SUPPORT

Hi {query.from_user.first_name}! ⚡

🔥 Features:
• Instant admin notification
• Priority response system
• Real-time ticket tracking

📝 Send your message now:
(Next message goes directly to admin)"""
    
    await edit_fast(query, text)
    await query.answer("💬 Support mode ON! Send message.")

# FIXED: Support message handling
@dp.message(F.text & ~F.command)
async def message_handler(message: types.Message):
    """FIXED: Ultra-fast support message processing"""
    if is_admin(message.from_user.id):
        return
        
    user_id = message.from_user.id
    
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
        priority = "⚡ HIGH PRIORITY" if is_premium else "🔵 NORMAL"
        
        # FIXED: Instant admin notification
        admin_text = f"""🎫 SUPPORT TICKET #{ticket_id}

{priority}
👤 {message.from_user.first_name}
🆔 {user_id}
💎 {'PREMIUM' if is_premium else 'FREE'}

💬 "{message.text}"

📞 /reply {user_id} Your response"""
        
        # Send to admin instantly
        await send_fast(ADMIN_ID, admin_text)
        
        # Confirm to user
        response_time = "2-5 min" if is_premium else "10-30 min"
        await send_fast(user_id, f"""✅ TICKET #{ticket_id} CREATED!

{priority}
⏰ Response: {response_time}

🔔 You'll be notified when admin replies!
⚡ Ultra-fast support system active!""")
        
        return
    
    # Guide user to support if not in support mode
    await send_fast(user_id, "💬 Use Support button for help!", kb_main())

@dp.message(F.photo)
async def photo_handler(message: types.Message):
    """Ultra-fast payment processing"""
    if is_admin(message.from_user.id):
        return
        
    user_id = message.from_user.id
    plan_id = plan_cache.get(user_id)
    
    if not plan_id:
        await send_fast(user_id, "❌ Select plan first: /start")
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
    await send_fast(user_id, f"""🎉 PAYMENT RECEIVED!

📸 ID: #{payment_id}
📋 {plan['emoji']} {plan['name']} - ₹{plan['price']}

⚡ Processing...
🔔 You'll be notified!""")
    
    # Admin notification
    await send_fast(ADMIN_ID, f"💰 Payment #{payment_id}\n👤 User {user_id}\n📋 {plan['name']} - ₹{plan['price']}")
    await bot.send_photo(ADMIN_ID, message.photo[-1].file_id, 
                        caption=f"Payment #{payment_id}", 
                        reply_markup=kb_approve(str(result.inserted_id), user_id))

# ADMIN HANDLERS - ULTRA OPTIMIZED

@dp.callback_query(F.data == "stats")
async def admin_stats(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
        
    # Ultra-fast stats
    total = await users_col.count_documents({})
    active = await users_col.count_documents({"status": "active"})
    pending = await payments_col.count_documents({"status": "pending"})
    
    text = f"""📊 ULTRA-FAST STATS

👥 Total: {total}
✅ Premium: {active}
⏳ Pending: {pending}

⚡ Updated: {datetime.now().strftime('%H:%M')}`"""
    
    await edit_fast(query, text, kb_admin())
    await query.answer(f"📊 {total} users")

@dp.callback_query(F.data == "pending")
async def admin_pending(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
        
    payments = await payments_col.find({"status": "pending"}).limit(5).to_list(5)
    
    if not payments:
        await query.message.answer("✅ No pending payments!")
        await query.answer("✅ All clear")
        return
    
    await query.message.answer(f"⏳ {len(payments)} PENDING:")
    
    for payment in payments:
        plan = PLANS[payment['plan_key']]
        text = f"""💰 #{str(payment['_id'])[:8]}

👤 User: {payment['user_id']}
📋 {plan['emoji']} {plan['name']} - ₹{plan['price']}
⏰ {payment['created_at'].strftime('%H:%M')}"""
        
        await query.message.answer(text, reply_markup=kb_approve(str(payment['_id']), payment['user_id']))
    
    await query.answer(f"⏳ {len(payments)} pending")

@dp.callback_query(F.data == "tickets")
async def admin_tickets(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
        
    tickets = await tickets_col.find({"status": "open"}).limit(5).to_list(5)
    
    if not tickets:
        await query.message.answer("✅ No open tickets!")
        await query.answer("✅ All resolved")
        return
    
    await query.message.answer(f"🎫 {len(tickets)} OPEN TICKETS:")
    
    for ticket in tickets:
        user = await get_user_fast(ticket['user_id'])
        priority = "⚡ HIGH" if user and user.get("status") == "active" else "🔵 NORMAL"
        
        text = f"""🎫 #{str(ticket['_id'])[:8]}

{priority}
👤 {ticket['user_id']}
💬 "{ticket['message'][:50]}..."
⏰ {ticket['created_at'].strftime('%H:%M')}

📞 /reply {ticket['user_id']} Your response"""
        
        await query.message.answer(text)
    
    await query.answer(f"🎫 {len(tickets)} tickets")

@dp.callback_query(F.data.startswith("approve_"))
async def approve_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
        
    parts = query.data.split("_")
    payment_id, user_id = parts[1], int(parts[2])
    
    # Get payment details
    payment = await payments_col.find_one({"_id": ObjectId(payment_id)})
    if not payment:
        await query.answer("❌ Payment not found")
        return
    
    plan = PLANS[payment['plan_key']]
    
    # Ultra-fast approval
    now = datetime.now(timezone.utc)
    end_date = now + timedelta(days=plan['days'])
    
    # Update payment status
    await payments_col.update_one({"_id": ObjectId(payment_id)}, {"$set": {"status": "approved"}})
    
    # Activate subscription
    await users_col.update_one(
        {"user_id": user_id},
        {"$set": {
            "plan_key": payment['plan_key'],
            "start_at": now,
            "end_at": end_date,
            "status": "active"
        }}
    )
    
    # Update cache
    if user_id in user_cache:
        user_cache[user_id]["status"] = "active"
        user_cache[user_id]["plan_key"] = payment['plan_key']
    
    # Notify user
    await send_fast(user_id, f"""🎉 PAYMENT APPROVED!

✅ {plan['emoji']} {plan['name']} ACTIVATED!
💰 ₹{plan['price']} confirmed
⏰ Valid for {plan['days']} days

🔥 Premium benefits active now!
💎 Welcome to Premium!""")
    
    # Update admin message
    await query.message.edit_text(f"""✅ APPROVED

Payment #{payment_id[:8]}
User {user_id} activated
{plan['emoji']} {plan['name']} - ₹{plan['price']}""")
    
    await query.answer("✅ Approved & Activated!")

@dp.callback_query(F.data.startswith("deny_"))
async def deny_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
        
    parts = query.data.split("_")
    payment_id, user_id = parts[1], int(parts[2])
    
    # Update payment status
    await payments_col.update_one({"_id": ObjectId(payment_id)}, {"$set": {"status": "denied"}})
    
    # Notify user
    await send_fast(user_id, """❌ PAYMENT NOT APPROVED

Please upload clearer screenshot:
✅ Payment success visible
✅ Correct amount shown
✅ Transaction details clear

🚀 Try again: /start""")
    
    # Update admin message
    await query.message.edit_text(f"❌ DENIED\nPayment #{payment_id[:8]}\nUser {user_id} notified")
    await query.answer("❌ Denied!")

@dp.callback_query(F.data == "broadcast")
async def broadcast_start(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not admin")
        return
        
    total = await users_col.count_documents({})
    await query.message.answer(f"📢 Broadcast to {total} users\nSend message:")
    await state.set_state(Broadcast.message)
    await query.answer("📢 Ready to broadcast")

@dp.message(Broadcast.message)
async def broadcast_send(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
        
    users = await users_col.find({}, {"user_id": 1}).to_list(None)
    await message.answer(f"📤 Broadcasting to {len(users)} users...")
    
    sent = failed = 0
    broadcast_text = f"📢 ANNOUNCEMENT\n\n{message.text}\n\n⚡ Ultra-Fast Premium Bot"
    
    for user in users:
        try:
            await send_fast(user["user_id"], broadcast_text)
            sent += 1
            await asyncio.sleep(0.03)
        except:
            failed += 1
    
    await message.answer(f"""📢 BROADCAST COMPLETE!

✅ Sent: {sent}
❌ Failed: {failed}
⚡ Speed: Ultra-Fast""")
    
    await state.clear()

# FIXED: Ultra-fast admin reply system
@dp.message(Command("reply"))
async def admin_reply(message: types.Message):
    if not is_admin(message.from_user.id):
        return
        
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.answer("❌ Usage: /reply <user_id> <message>")
            return
            
        user_id, reply_text = int(parts[1]), parts[2]
        
        # FIXED: Ultra-fast support response
        response = f"""💬 SUPPORT RESPONSE

{reply_text}

─────────────────
⚡ Ultra-Fast Support Team
📞 Need more help? Use Support button!"""
        
        await send_fast(user_id, response)
        
        # Close tickets
        await tickets_col.update_many(
            {"user_id": user_id, "status": "open"}, 
            {"$set": {"status": "closed"}}
        )
        
        await message.answer(f"✅ REPLY SENT to user {user_id}")
        
    except Exception as e:
        await message.answer(f"❌ Error: {e}")

# Ultra-fast expiry worker
async def expiry_worker():
    while True:
        try:
            now = datetime.now(timezone.utc)
            
            # Process expired users
            expired = await users_col.find({"status": "active", "end_at": {"$lte": now}}).to_list(None)
            
            for user in expired:
                await users_col.update_one({"user_id": user["user_id"]}, {"$set": {"status": "expired"}})
                
                # Update cache
                if user["user_id"] in user_cache:
                    user_cache[user["user_id"]]["status"] = "expired"
                
                # Notify user
                await send_fast(user["user_id"], "⏰ Premium expired!\n🚀 Renew: /start")
                
        except Exception as e:
            log.error(f"Expiry error: {e}")
            
        await asyncio.sleep(1800)  # 30 minutes

async def main():
    try:
        # Test MongoDB connection
        await mongo_client.admin.command('ping')
        print("✅ MongoDB connected")
        
        # Start expiry worker
        asyncio.create_task(expiry_worker())
        print("✅ Expiry worker started")
        
        print("⚡ ULTRA-FAST PREMIUM BOT STARTING...")
        print("🔥 Features: Tap-to-copy UPI, Instant support, Lightning speed")
        
        await dp.start_polling(bot, skip_updates=True)
        
    except Exception as e:
        print(f"❌ Error: {e}")
        raise

if __name__ == "__main__":
    if API_TOKEN == "TEST_TOKEN":
        raise RuntimeError("❌ Set API_TOKEN environment variable")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("✅ Bot stopped")
