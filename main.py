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

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger("premium_bot")

# Environment variables
API_TOKEN = os.getenv("API_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID") or "123456789")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or "-10012345678")
UPI_ID = os.getenv("UPI_ID") or "yourupi@upi"
QR_CODE_URL = os.getenv("QR_CODE_URL") or "https://example.com/qr.png"
MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"

if not API_TOKEN:
    print("❌ API_TOKEN not set!")
    exit(1)

# MongoDB setup
try:
    mongo_client = AsyncIOMotorClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo_client['premium_bot']
    users_col = db['users']
    payments_col = db['payments']
    tickets_col = db['tickets']
except Exception as e:
    log.error(f"MongoDB error: {e}")
    exit(1)

# Bot initialization
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# FIXED: Plans with consistent naming
PLANS = {
    "1": {"name": "1 Month", "price": "99", "days": 30, "emoji": "🟢"},
    "2": {"name": "6 Months", "price": "399", "days": 180, "emoji": "🟡"},
    "3": {"name": "1 Year", "price": "1999", "days": 365, "emoji": "🔥"},
    "4": {"name": "Lifetime", "price": "2999", "days": 36500, "emoji": "💎"},
}

# Global state management
user_plans = {}  # Store selected plans
support_users = set()  # Users in support mode

# FSM States
class SupportChat(StatesGroup):
    waiting_message = State()

class AdminBroadcast(StatesGroup):
    waiting_message = State()

def is_admin(user_id):
    return user_id == ADMIN_ID

# Database functions
async def get_or_create_user(user: types.User):
    """Get or create user in database"""
    try:
        user_data = await users_col.find_one({"user_id": user.id})
        if not user_data:
            user_data = {
                "user_id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "plan_key": None,
                "status": "free",
                "created_at": datetime.now(timezone.utc),
                "start_at": None,
                "end_at": None
            }
            await users_col.insert_one(user_data)
        return user_data
    except Exception as e:
        log.error(f"Database error: {e}")
        return None

async def activate_premium(user_id, plan_key):
    """Activate premium subscription"""
    try:
        plan = PLANS[plan_key]
        now = datetime.now(timezone.utc)
        end_date = now + timedelta(days=plan["days"])
        
        await users_col.update_one(
            {"user_id": user_id},
            {"$set": {
                "plan_key": plan_key,
                "status": "premium",
                "start_at": now,
                "end_at": end_date
            }}
        )
        return now, end_date
    except Exception as e:
        log.error(f"Activation error: {e}")
        return None, None

# FIXED: Safe message sending with HTML escaping
async def safe_send_message(chat_id, text, reply_markup=None):
    """Send message with error handling"""
    try:
        # Clean text of problematic HTML
        clean_text = text.replace("<code>", "`").replace("</code>", "`")
        return await bot.send_message(chat_id, clean_text, reply_markup=reply_markup, parse_mode=None)
    except Exception as e:
        log.error(f"Send message error: {e}")
        try:
            # Fallback without formatting
            simple_text = text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").replace("<code>", "").replace("</code>", "")
            return await bot.send_message(chat_id, simple_text, reply_markup=reply_markup, parse_mode=None)
        except:
            return None

async def safe_edit_message(query, text, reply_markup=None):
    """Edit message with error handling"""
    try:
        clean_text = text.replace("<code>", "`").replace("</code>", "`")
        await query.message.edit_text(clean_text, reply_markup=reply_markup, parse_mode=None)
    except Exception as e:
        log.warning(f"Edit failed: {e}")
        await safe_send_message(query.from_user.id, text, reply_markup)

# Keyboard layouts
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Buy Premium", callback_data="buy_premium")],
        [InlineKeyboardButton(text="📊 My Status", callback_data="my_status"),
         InlineKeyboardButton(text="💬 Support", callback_data="support")]
    ])

def plans_kb():
    buttons = []
    for plan_id, plan_info in PLANS.items():
        buttons.append([InlineKeyboardButton(
            text=f"{plan_info['emoji']} {plan_info['name']} - ₹{plan_info['price']}", 
            callback_data=f"select_plan_{plan_id}"
        )])
    buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# FIXED: New payment system with UPI and QR in same view
def payment_kb(plan_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 View Payment Details", callback_data=f"payment_details_{plan_id}")],
        [InlineKeyboardButton(text="📸 Upload Screenshot", callback_data=f"upload_screenshot_{plan_id}")],
        [InlineKeyboardButton(text="⬅️ Back to Plans", callback_data="buy_premium")]
    ])

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ Pending Payments", callback_data="admin_pending"),
         InlineKeyboardButton(text="📊 Statistics", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🎫 Support Tickets", callback_data="admin_tickets"),
         InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast")]
    ])

def payment_action_kb(payment_id, user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{payment_id}_{user_id}"),
         InlineKeyboardButton(text="❌ Deny", callback_data=f"deny_{payment_id}_{user_id}")]
    ])

# HANDLERS

@dp.message(CommandStart())
async def start_command(message: types.Message):
    """Start command handler"""
    await get_or_create_user(message.from_user)
    
    if is_admin(message.from_user.id):
        text = f"🎯 ADMIN PANEL\n\nHello {message.from_user.first_name}!\nManage your premium bot efficiently."
        await safe_send_message(message.from_user.id, text, admin_kb())
    else:
        text = f"👋 Welcome {message.from_user.first_name}!\n\n🌟 Premium Benefits:\n• Unlimited access\n• Priority support\n• Ad-free experience\n\nUpgrade now!"
        await safe_send_message(message.from_user.id, text, main_menu_kb())

@dp.callback_query(F.data == "main_menu")
async def main_menu_handler(query: types.CallbackQuery):
    if is_admin(query.from_user.id):
        text = "🎯 ADMIN PANEL\n\nChoose an option:"
        await safe_edit_message(query, text, admin_kb())
    else:
        text = f"🏠 Main Menu\n\nHello {query.from_user.first_name}!"
        await safe_edit_message(query, text, main_menu_kb())
    await query.answer()

@dp.callback_query(F.data == "buy_premium")
async def buy_premium_handler(query: types.CallbackQuery):
    text = "💎 Premium Plans\n\nChoose your subscription plan:"
    await safe_edit_message(query, text, plans_kb())
    await query.answer("💎 Select your plan")

@dp.callback_query(F.data.startswith("select_plan_"))
async def select_plan_handler(query: types.CallbackQuery):
    plan_id = query.data.split("_")[2]
    user_plans[query.from_user.id] = plan_id
    
    plan = PLANS[plan_id]
    text = f"🎯 {plan['emoji']} {plan['name']} Plan\n\n💰 Price: ₹{plan['price']}\n⏰ Duration: {plan['days']} days\n\nProceed with payment:"
    
    await safe_edit_message(query, text, payment_kb(plan_id))
    await query.answer(f"Selected {plan['name']}")

# FIXED: New payment details system with UPI ID and QR code
@dp.callback_query(F.data.startswith("payment_details_"))
async def payment_details_handler(query: types.CallbackQuery):
    plan_id = query.data.split("_")[2]
    plan = PLANS[plan_id]
    
    # Send QR code image first
    try:
        await bot.send_photo(
            query.from_user.id,
            QR_CODE_URL,
            caption=f"📱 QR Code for {plan['emoji']} {plan['name']}\nAmount: ₹{plan['price']}"
        )
    except:
        pass
    
    # FIXED: Send UPI details with proper formatting
    upi_text = f"""💳 PAYMENT DETAILS

📋 Plan: {plan['emoji']} {plan['name']}
💰 Amount: ₹{plan['price']}

🏦 UPI ID: {UPI_ID}
(Copy the UPI ID above)

💡 PAYMENT STEPS:
1. Copy UPI ID: {UPI_ID}
2. Open any UPI app (GPay/PhonePe/Paytm)
3. Send Money → UPI ID
4. Enter amount: ₹{plan['price']}
5. Complete payment
6. Take screenshot of success page
7. Upload screenshot using button below

⚠️ Pay exactly ₹{plan['price']} rupees
🔥 Premium activated after verification!"""
    
    await safe_send_message(query.from_user.id, upi_text, payment_kb(plan_id))
    await query.answer("💳 Payment details sent!")

@dp.callback_query(F.data.startswith("upload_screenshot_"))
async def upload_screenshot_handler(query: types.CallbackQuery):
    plan_id = query.data.split("_")[2]
    user_plans[query.from_user.id] = plan_id
    
    plan = PLANS[plan_id]
    text = f"""📸 Upload Payment Screenshot

Plan: {plan['emoji']} {plan['name']} - ₹{plan['price']}

📷 Requirements:
✅ Payment success visible
✅ Amount ₹{plan['price']} shown
✅ Clear and readable

📤 Send your screenshot now:"""
    
    await safe_edit_message(query, text)
    await query.answer("📸 Ready for screenshot!")

@dp.callback_query(F.data == "my_status")
async def my_status_handler(query: types.CallbackQuery):
    user_data = await users_col.find_one({"user_id": query.from_user.id})
    
    if user_data and user_data.get("status") == "premium":
        plan = PLANS.get(user_data.get("plan_key", "1"))
        end_date = user_data.get("end_at")
        if end_date:
            days_left = (end_date - datetime.now(timezone.utc)).days
            text = f"📊 Premium Status\n\n✅ ACTIVE\n{plan['emoji']} Plan: {plan['name']}\n⏰ Days left: {days_left}\n\n🎉 Enjoying premium benefits!"
        else:
            text = f"📊 Premium Status\n\n✅ LIFETIME\n💎 All benefits forever active!"
    else:
        text = "📊 Account Status\n\n❌ FREE USER\n\n🚀 Upgrade to premium for:\n• Unlimited access\n• Priority support\n• Ad-free experience"
    
    await safe_edit_message(query, text, main_menu_kb())
    await query.answer("📊 Status updated")

# FIXED: Support System
@dp.callback_query(F.data == "support")
async def support_handler(query: types.CallbackQuery):
    support_users.add(query.from_user.id)
    
    text = f"""💬 Customer Support

Hello {query.from_user.first_name}!

Our support team is ready to help you.

📝 Please describe your issue:
(Send your next message and it will reach admin directly)"""
    
    await safe_edit_message(query, text)
    await query.answer("💬 Support activated! Send your message.")

# FIXED: Handle text messages for support
@dp.message(F.text & ~F.command)
async def handle_text_message(message: types.Message):
    if is_admin(message.from_user.id):
        return
    
    user_id = message.from_user.id
    
    # Check if user is in support mode
    if user_id in support_users:
        support_users.remove(user_id)
        
        # Create support ticket
        try:
            ticket_data = {
                "user_id": user_id,
                "username": message.from_user.username,
                "first_name": message.from_user.first_name,
                "message": message.text,
                "status": "open",
                "created_at": datetime.now(timezone.utc)
            }
            
            result = await tickets_col.insert_one(ticket_data)
            ticket_id = str(result.inserted_id)[:8]
            
            # Get user status
            user_data = await users_col.find_one({"user_id": user_id})
            priority = "HIGH" if user_data and user_data.get("status") == "premium" else "NORMAL"
            
            # Send to admin
            admin_text = f"""🎫 SUPPORT TICKET #{ticket_id}

🔥 Priority: {priority}
👤 User: {message.from_user.first_name}
📱 Username: @{message.from_user.username or 'None'}
🆔 ID: {user_id}

💬 Message:
{message.text}

📞 Reply: /reply {user_id} Your response"""
            
            await safe_send_message(ADMIN_ID, admin_text)
            
            # Confirm to user
            response_time = "2-5 minutes" if priority == "HIGH" else "10-30 minutes"
            await safe_send_message(user_id, 
                f"✅ Support ticket #{ticket_id} created!\n\n🔥 Priority: {priority}\n⏰ Response time: {response_time}\n\nYou'll be notified when admin replies!", 
                main_menu_kb()
            )
            
        except Exception as e:
            log.error(f"Support ticket error: {e}")
            await safe_send_message(user_id, "❌ Error creating ticket. Please try again.", main_menu_kb())
    else:
        # Guide user to support
        await safe_send_message(user_id, "💬 Use Support button for help!", main_menu_kb())

# FIXED: Handle photo uploads for payments
@dp.message(F.photo)
async def handle_photo(message: types.Message):
    if is_admin(message.from_user.id):
        return
    
    user_id = message.from_user.id
    plan_id = user_plans.get(user_id)
    
    if not plan_id:
        await safe_send_message(user_id, "❌ Please select a plan first using /start", main_menu_kb())
        return
    
    try:
        plan = PLANS[plan_id]
        
        # Save payment record
        payment_data = {
            "user_id": user_id,
            "username": message.from_user.username,
            "first_name": message.from_user.first_name,
            "plan_key": plan_id,
            "file_id": message.photo[-1].file_id,
            "status": "pending",
            "created_at": datetime.now(timezone.utc)
        }
        
        result = await payments_col.insert_one(payment_data)
        payment_id = str(result.inserted_id)[:8]
        
        # Notify user
        await safe_send_message(
            user_id,
            f"🎉 Payment screenshot received!\n\n📸 ID: #{payment_id}\n📋 Plan: {plan['emoji']} {plan['name']}\n💰 Amount: ₹{plan['price']}\n\n⏳ Processing...\nYou'll be notified once approved!",
            main_menu_kb()
        )
        
        # Notify admin
        await safe_send_message(ADMIN_ID, 
            f"💰 New payment #{payment_id}\n👤 User: {message.from_user.first_name} ({user_id})\n📋 Plan: {plan['name']} - ₹{plan['price']}"
        )
        
        # Send photo to admin with approval buttons
        await bot.send_photo(
            ADMIN_ID,
            message.photo[-1].file_id,
            caption=f"Payment #{payment_id}\n{plan['name']} - ₹{plan['price']}\nUser: {user_id}",
            reply_markup=payment_action_kb(str(result.inserted_id), user_id)
        )
        
        log.info(f"Payment {payment_id} submitted by user {user_id}")
        
    except Exception as e:
        log.error(f"Photo handler error: {e}")
        await safe_send_message(user_id, "❌ Error processing screenshot. Please try again.", main_menu_kb())

# ADMIN HANDLERS

@dp.callback_query(F.data == "admin_stats")
async def admin_stats_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not authorized")
        return
    
    try:
        total_users = await users_col.count_documents({})
        premium_users = await users_col.count_documents({"status": "premium"})
        pending_payments = await payments_col.count_documents({"status": "pending"})
        open_tickets = await tickets_col.count_documents({"status": "open"})
        
        text = f"""📊 Bot Statistics

👥 Total Users: {total_users}
💎 Premium Users: {premium_users}
⏳ Pending Payments: {pending_payments}
🎫 Open Tickets: {open_tickets}

Updated: {datetime.now().strftime('%H:%M IST')}"""
        
        await safe_edit_message(query, text, admin_kb())
        await query.answer("📊 Stats updated")
        
    except Exception as e:
        log.error(f"Stats error: {e}")
        await query.answer("❌ Error loading stats")

@dp.callback_query(F.data == "admin_pending")
async def admin_pending_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not authorized")
        return
    
    try:
        payments = await payments_col.find({"status": "pending"}).limit(10).to_list(10)
        
        if not payments:
            await safe_send_message(query.from_user.id, "✅ No pending payments!", admin_kb())
            await query.answer("✅ All clear")
            return
        
        await safe_send_message(query.from_user.id, f"⏳ {len(payments)} Pending Payments:")
        
        for payment in payments:
            plan = PLANS.get(payment['plan_key'], PLANS['1'])
            text = f"""💰 Payment #{str(payment['_id'])[:8]}

👤 User: {payment.get('first_name', 'Unknown')} ({payment['user_id']})
📋 Plan: {plan['emoji']} {plan['name']} - ₹{plan['price']}
⏰ Time: {payment['created_at'].strftime('%d %b, %H:%M')}"""
            
            await safe_send_message(
                query.from_user.id, 
                text, 
                payment_action_kb(str(payment['_id']), payment['user_id'])
            )
        
        await query.answer(f"⏳ {len(payments)} payments loaded")
        
    except Exception as e:
        log.error(f"Pending payments error: {e}")
        await query.answer("❌ Error loading payments")

@dp.callback_query(F.data == "admin_tickets")
async def admin_tickets_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not authorized")
        return
    
    try:
        tickets = await tickets_col.find({"status": "open"}).limit(10).to_list(10)
        
        if not tickets:
            await safe_send_message(query.from_user.id, "✅ No open tickets!", admin_kb())
            await query.answer("✅ All resolved")
            return
        
        await safe_send_message(query.from_user.id, f"🎫 {len(tickets)} Open Tickets:")
        
        for ticket in tickets:
            text = f"""🎫 Ticket #{str(ticket['_id'])[:8]}

👤 User: {ticket.get('first_name', 'Unknown')} ({ticket['user_id']})
⏰ Time: {ticket['created_at'].strftime('%d %b, %H:%M')}

💬 Message: {ticket['message'][:100]}...

📞 Reply: /reply {ticket['user_id']} Your response"""
            
            await safe_send_message(query.from_user.id, text)
        
        await query.answer(f"🎫 {len(tickets)} tickets loaded")
        
    except Exception as e:
        log.error(f"Tickets error: {e}")
        await query.answer("❌ Error loading tickets")

# FIXED: Payment approval handler
@dp.callback_query(F.data.startswith("approve_"))
async def approve_payment_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not authorized")
        return
    
    try:
        parts = query.data.split("_")
        payment_id, user_id = parts[1], int(parts[2])
        
        # Get payment details
        payment = await payments_col.find_one({"_id": ObjectId(payment_id)})
        if not payment:
            await query.answer("❌ Payment not found")
            return
        
        plan = PLANS[payment['plan_key']]
        
        # Activate premium
        start_date, end_date = await activate_premium(user_id, payment['plan_key'])
        if not start_date:
            await query.answer("❌ Activation failed")
            return
        
        # Update payment status
        await payments_col.update_one({"_id": ObjectId(payment_id)}, {"$set": {"status": "approved"}})
        
        # Notify user
        if plan['days'] == 36500:  # Lifetime
            user_msg = f"""🎉 Payment Approved!

✅ {plan['emoji']} {plan['name']} activated!
💰 Amount: ₹{plan['price']}
⏰ Duration: Lifetime

💎 Welcome to Premium!
All benefits are now active!"""
        else:
            user_msg = f"""🎉 Payment Approved!

✅ {plan['emoji']} {plan['name']} activated!
💰 Amount: ₹{plan['price']}
⏰ Valid until: {end_date.strftime('%d %b %Y')}

💎 Welcome to Premium!
All benefits are now active!"""
        
        await safe_send_message(user_id, user_msg)
        
        # Update admin message
        await query.message.edit_text(
            f"✅ APPROVED\n\nPayment #{payment_id}\nUser {user_id} activated\n{plan['emoji']} {plan['name']} - ₹{plan['price']}"
        )
        
        await query.answer("✅ Payment approved!")
        log.info(f"Payment {payment_id} approved for user {user_id}")
        
    except Exception as e:
        log.error(f"Approval error: {e}")
        await query.answer("❌ Approval error")

@dp.callback_query(F.data.startswith("deny_"))
async def deny_payment_handler(query: types.CallbackQuery):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not authorized")
        return
    
    try:
        parts = query.data.split("_")
        payment_id, user_id = parts[1], int(parts[2])
        
        # Update payment status
        await payments_col.update_one({"_id": ObjectId(payment_id)}, {"$set": {"status": "denied"}})
        
        # Notify user
        user_msg = """❌ Payment Not Approved

Your screenshot needs improvement:

🔍 Issues might be:
• Screenshot unclear
• Wrong amount shown
• Payment incomplete

📸 Please upload a clearer screenshot showing:
✅ Payment success
✅ Correct amount
✅ Transaction details

Try again: /start"""
        
        await safe_send_message(user_id, user_msg)
        
        # Update admin message
        await query.message.edit_text(f"❌ DENIED\n\nPayment #{payment_id}\nUser {user_id} notified")
        
        await query.answer("❌ Payment denied!")
        log.info(f"Payment {payment_id} denied for user {user_id}")
        
    except Exception as e:
        log.error(f"Denial error: {e}")
        await query.answer("❌ Denial error")

# FIXED: Admin reply system
@dp.message(Command("reply"))
async def admin_reply_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.answer("❌ Usage: /reply <user_id> <message>")
            return
        
        user_id, reply_text = int(parts[1]), parts[2]
        
        # Send reply to user
        user_msg = f"""💬 Support Response

{reply_text}

────────────────
🎧 Support Team
💬 Need more help? Use Support button!"""
        
        await safe_send_message(user_id, user_msg)
        
        # Close tickets for this user
        await tickets_col.update_many(
            {"user_id": user_id, "status": "open"}, 
            {"$set": {"status": "closed"}}
        )
        
        await message.answer(f"✅ Reply sent to user {user_id}")
        log.info(f"Admin replied to user {user_id}")
        
    except Exception as e:
        log.error(f"Reply error: {e}")
        await message.answer("❌ Reply error")

# Broadcast handler
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_handler(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        await query.answer("❌ Not authorized")
        return
    
    total = await users_col.count_documents({})
    await query.message.answer(f"📢 Broadcast to {total} users\n\nSend your message:")
    await state.set_state(AdminBroadcast.waiting_message)
    await query.answer("📢 Ready for broadcast")

@dp.message(AdminBroadcast.waiting_message)
async def broadcast_message_handler(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    
    try:
        users = await users_col.find({}, {"user_id": 1}).to_list(None)
        await message.answer(f"📤 Broadcasting to {len(users)} users...")
        
        sent = failed = 0
        broadcast_text = f"📢 Announcement\n\n{message.text}\n\n────────────────\n💎 Premium Bot"
        
        for user in users:
            try:
                await safe_send_message(user["user_id"], broadcast_text)
                sent += 1
                await asyncio.sleep(0.05)  # Rate limiting
            except:
                failed += 1
        
        await message.answer(f"📢 Broadcast Complete!\n✅ Sent: {sent}\n❌ Failed: {failed}")
        await state.clear()
        
    except Exception as e:
        log.error(f"Broadcast error: {e}")
        await message.answer("❌ Broadcast error")
        await state.clear()

# Expiry worker
async def expiry_worker():
    while True:
        try:
            now = datetime.now(timezone.utc)
            
            # Find expired users
            expired_users = await users_col.find({
                "status": "premium",
                "end_at": {"$lte": now}
            }).to_list(None)
            
            for user in expired_users:
                # Update status
                await users_col.update_one(
                    {"user_id": user["user_id"]}, 
                    {"$set": {"status": "expired"}}
                )
                
                # Notify user
                await safe_send_message(
                    user["user_id"], 
                    "⏰ Premium subscription expired!\n\n🚀 Renew now: /start"
                )
            
            if expired_users:
                log.info(f"Processed {len(expired_users)} expired subscriptions")
                
        except Exception as e:
            log.error(f"Expiry worker error: {e}")
        
        await asyncio.sleep(3600)  # Check every hour

async def main():
    try:
        # Test connections
        await mongo_client.admin.command('ping')
        log.info("✅ MongoDB connected")
        
        me = await bot.get_me()
        log.info(f"✅ Bot connected: @{me.username}")
        
        # Start expiry worker
        asyncio.create_task(expiry_worker())
        
        print("🚀 PREMIUM BOT STARTED")
        print("✅ All systems operational")
        print("💎 Ready to serve users!")
        
        await dp.start_polling(bot, skip_updates=True)
        
    except Exception as e:
        log.error(f"Startup error: {e}")
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("✅ Bot stopped")
    except Exception as e:
        log.error(f"Fatal error: {e}")
