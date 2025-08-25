import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional
from bson import ObjectId

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from motor.motor_asyncio import AsyncIOMotorClient

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger("premium_bot")

# Environment variables
API_TOKEN = os.getenv("API_TOKEN") or "TEST_TOKEN"
ADMIN_ID = int(os.getenv("ADMIN_ID") or "123456789")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or "-10012345678")
UPI_ID = os.getenv("UPI_ID") or "yourupi@upi"
QR_CODE_URL = os.getenv("QR_CODE_URL") or "https://example.com/qr.png"
WELCOME_IMAGE = os.getenv("WELCOME_IMAGE") or "https://i.imgur.com/premium-welcome.jpg"
PLANS_IMAGE = os.getenv("PLANS_IMAGE") or "https://i.imgur.com/premium-plans.jpg"
SUCCESS_IMAGE = os.getenv("SUCCESS_IMAGE") or "https://i.imgur.com/success.jpg"
MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"

if API_TOKEN == "TEST_TOKEN":
    raise RuntimeError("Set API_TOKEN environment variable")

# MongoDB setup
client = AsyncIOMotorClient(MONGO_URI, maxPoolSize=10)
db = client['premium_bot']
users_col = db['users']
payments_col = db['payments']
tickets_col = db['tickets']

# Bot setup
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Plans
PLANS = {
    "plan1": {"name": "1 Month", "price": 99, "days": 30, "emoji": "🟢"},
    "plan2": {"name": "6 Months", "price": 399, "days": 180, "emoji": "🟡"},
    "plan3": {"name": "1 Year", "price": 1999, "days": 365, "emoji": "🔥"},
    "plan4": {"name": "Lifetime", "price": 2999, "days": 36500, "emoji": "💎"},
}

# Global variables
last_plan: Dict[int, str] = {}
support_mode: set = set()

# FSM States
class AdminReply(StatesGroup):
    waiting = State()

class Broadcast(StatesGroup):
    waiting = State()

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

# Database functions
async def save_user(user: types.User):
    try:
        await users_col.update_one(
            {"user_id": user.id},
            {
                "$set": {
                    "username": user.username,
                    "first_name": user.first_name,
                    "updated": datetime.now(timezone.utc)
                },
                "$setOnInsert": {
                    "plan": None,
                    "start": None,
                    "end": None,
                    "status": "free",
                    "created": datetime.now(timezone.utc)
                }
            },
            upsert=True
        )
        log.info(f"User {user.id} saved")
    except Exception as e:
        log.error(f"Error saving user: {e}")

async def get_user_data(user_id: int):
    try:
        return await users_col.find_one({"user_id": user_id})
    except Exception as e:
        log.error(f"Error getting user: {e}")
        return None

async def activate_plan(user_id: int, plan_key: str):
    try:
        plan = PLANS[plan_key]
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=plan["days"])
        
        await users_col.update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "plan": plan_key,
                    "start": now,
                    "end": end,
                    "status": "premium"
                }
            }
        )
        log.info(f"Plan {plan_key} activated for user {user_id}")
        return True
    except Exception as e:
        log.error(f"Error activating plan: {e}")
        return False

async def save_payment(user_id: int, plan_key: str, file_id: str):
    try:
        result = await payments_col.insert_one({
            "user_id": user_id,
            "plan": plan_key,
            "file": file_id,
            "status": "pending",
            "created": datetime.now(timezone.utc)
        })
        return str(result.inserted_id)
    except Exception as e:
        log.error(f"Error saving payment: {e}")
        return None

async def update_payment_status(payment_id: str, status: str):
    try:
        await payments_col.update_one(
            {"_id": ObjectId(payment_id)},
            {"$set": {"status": status}}
        )
        return True
    except Exception as e:
        log.error(f"Error updating payment: {e}")
        return False

async def get_payment_data(payment_id: str):
    try:
        return await payments_col.find_one({"_id": ObjectId(payment_id)})
    except Exception as e:
        log.error(f"Error getting payment: {e}")
        return None

# FIXED: Simple Support System
async def create_ticket(user_id: int, username: str, first_name: str, message: str):
    try:
        result = await tickets_col.insert_one({
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "messages": [{"from": "user", "text": message, "time": datetime.now(timezone.utc)}],
            "status": "open",
            "created": datetime.now(timezone.utc)
        })
        return str(result.inserted_id)
    except Exception as e:
        log.error(f"Error creating ticket: {e}")
        return None

async def add_to_ticket(ticket_id: str, sender: str, message: str):
    try:
        await tickets_col.update_one(
            {"_id": ObjectId(ticket_id)},
            {"$push": {"messages": {"from": sender, "text": message, "time": datetime.now(timezone.utc)}}}
        )
        return True
    except Exception as e:
        log.error(f"Error adding to ticket: {e}")
        return False

async def get_ticket_data(ticket_id: str):
    try:
        return await tickets_col.find_one({"_id": ObjectId(ticket_id)})
    except Exception as e:
        log.error(f"Error getting ticket: {e}")
        return None

async def get_user_ticket(user_id: int):
    try:
        return await tickets_col.find_one({"user_id": user_id, "status": "open"})
    except Exception as e:
        log.error(f"Error getting user ticket: {e}")
        return None

async def close_ticket(ticket_id: str):
    try:
        await tickets_col.update_one(
            {"_id": ObjectId(ticket_id)},
            {"$set": {"status": "closed", "closed": datetime.now(timezone.utc)}}
        )
        return True
    except Exception as e:
        log.error(f"Error closing ticket: {e}")
        return False

# UI Functions - NO MARKDOWN
def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Buy Premium", callback_data="buy")],
        [InlineKeyboardButton(text="📊 My Plan", callback_data="myplan"), 
         InlineKeyboardButton(text="💬 Support", callback_data="support")],
        [InlineKeyboardButton(text="🎁 Offers", callback_data="offers")]
    ])

def plans_keyboard():
    buttons = []
    for key, plan in PLANS.items():
        text = f"{plan['emoji']} {plan['name']} - Rs.{plan['price']}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"plan_{key}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def payment_keyboard(plan_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 UPI", callback_data=f"upi_{plan_key}"),
         InlineKeyboardButton(text="📱 QR", callback_data=f"qr_{plan_key}")],
        [InlineKeyboardButton(text="📸 Upload Proof", callback_data=f"upload_{plan_key}")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="buy")]
    ])

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Payments", callback_data="admin_payments"),
         InlineKeyboardButton(text="📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🎫 Tickets", callback_data="admin_tickets"),
         InlineKeyboardButton(text="👥 Users", callback_data="admin_users")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast")]
    ])

def payment_action_keyboard(payment_id: str, user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{payment_id}_{user_id}"),
         InlineKeyboardButton(text="❌ Deny", callback_data=f"deny_{payment_id}_{user_id}")]
    ])

def support_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 New Message", callback_data="support_new")],
        [InlineKeyboardButton(text="❌ Close Ticket", callback_data="support_close")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="back")]
    ])

def tickets_keyboard(tickets):
    buttons = []
    for ticket in tickets[:10]:
        tid = str(ticket['_id'])[-6:]
        user = ticket['first_name'][:10]
        msg_count = len(ticket['messages'])
        text = f"#{tid} - {user} ({msg_count} msgs)"
        buttons.append([InlineKeyboardButton(text=text, callback_data=f"ticket_{tid}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def ticket_action_keyboard(ticket_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Reply", callback_data=f"reply_{ticket_id}"),
         InlineKeyboardButton(text="❌ Close", callback_data=f"close_{ticket_id}")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="admin_tickets")]
    ])

# Message sending functions - NO MARKDOWN
async def send_message(chat_id: int, text: str, keyboard=None):
    try:
        await bot.send_message(chat_id, text, reply_markup=keyboard)
        return True
    except Exception as e:
        log.error(f"Error sending message: {e}")
        return False

async def send_photo(chat_id: int, photo: str, text: str, keyboard=None):
    try:
        await bot.send_photo(chat_id, photo, caption=text, reply_markup=keyboard)
        return True
    except Exception as e:
        log.warning(f"Photo failed, sending text: {e}")
        return await send_message(chat_id, text, keyboard)

async def edit_message(message, text: str, keyboard=None):
    try:
        await message.edit_text(text, reply_markup=keyboard)
        return True
    except Exception as e:
        log.warning(f"Edit failed, sending new: {e}")
        return await send_message(message.chat.id, text, keyboard)

# Bot Handlers
@dp.message(CommandStart())
async def start_handler(message: types.Message):
    await save_user(message.from_user)
    support_mode.discard(message.from_user.id)  # Remove from support mode
    
    text = f"👋 Hello {message.from_user.first_name}!\n\n🌟 Welcome to Premium Bot!\n• Unlimited downloads\n• Ad-free experience\n• Priority support\n\nClick below to get started!"
    
    # Add admin button if admin
    keyboard = main_keyboard()
    if is_admin(message.from_user.id):
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="🛠 Admin", callback_data="admin")])
    
    await send_photo(message.chat.id, WELCOME_IMAGE, text, keyboard)

@dp.callback_query(F.data == "back")
async def back_handler(callback: types.CallbackQuery):
    support_mode.discard(callback.from_user.id)  # Remove from support mode
    
    text = f"🏠 Welcome back {callback.from_user.first_name}!\n\nWhat would you like to do?"
    
    keyboard = main_keyboard()
    if is_admin(callback.from_user.id):
        keyboard.inline_keyboard.append([InlineKeyboardButton(text="🛠 Admin", callback_data="admin")])
    
    await edit_message(callback.message, text, keyboard)
    await callback.answer()

@dp.callback_query(F.data == "buy")
async def buy_handler(callback: types.CallbackQuery):
    text = "💎 Premium Plans\n\nChoose your subscription plan:"
    await edit_message(callback.message, text, plans_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "offers")
async def offers_handler(callback: types.CallbackQuery):
    text = "🎁 Special Offers\n\n🟡 6 Months: Best Value!\n🔥 1 Year: Most Popular!\n💎 Lifetime: One-time payment!\n\nLimited time offers!"
    await edit_message(callback.message, text, main_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "myplan")
async def myplan_handler(callback: types.CallbackQuery):
    user_data = await get_user_data(callback.from_user.id)
    
    if not user_data or user_data.get("status") != "premium":
        text = "😔 No Active Plan\n\nYou are using the FREE version.\n\nUpgrade to Premium for:\n• Unlimited access\n• No ads\n• Priority support\n\nReady to upgrade?"
        await edit_message(callback.message, text, main_keyboard())
    else:
        plan_key = user_data.get("plan")
        plan = PLANS.get(plan_key, {"name": "Unknown", "emoji": "📦"})
        
        # Calculate remaining time
        if user_data.get("end"):
            end_date = user_data["end"]
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            
            now = datetime.now(timezone.utc)
            time_left = end_date - now
            
            if time_left.days > 0:
                time_text = f"{time_left.days} days remaining"
                status = "ACTIVE ✅"
            else:
                time_text = "Expired"
                status = "EXPIRED ❌"
        else:
            time_text = "Unknown"
            status = "UNKNOWN"
        
        text = f"📊 My Subscription\n\nStatus: {status}\nPlan: {plan['emoji']} {plan['name']}\nTime: {time_text}\n\nEnjoy your premium features!"
        await edit_message(callback.message, text, main_keyboard())
    
    await callback.answer()

# FIXED: Support System Handlers
@dp.callback_query(F.data == "support")
async def support_handler(callback: types.CallbackQuery):
    # Check if user has open ticket
    ticket = await get_user_ticket(callback.from_user.id)
    
    if ticket:
        tid = str(ticket['_id'])[-6:]
        msg_count = len(ticket['messages'])
        created = ticket['created'].strftime('%d %b, %H:%M')
        
        text = f"🎫 Your Support Ticket\n\nTicket: #{tid}\nMessages: {msg_count}\nCreated: {created}\nStatus: OPEN\n\nSend a message to add to this ticket or close it when resolved."
        
        support_mode.add(callback.from_user.id)
        await edit_message(callback.message, text, support_keyboard())
    else:
        text = f"💬 Customer Support\n\nHi {callback.from_user.first_name}!\n\nClick 'New Message' to create a support ticket.\nOur team responds quickly!"
        await edit_message(callback.message, text, support_keyboard())
    
    await callback.answer()

@dp.callback_query(F.data == "support_new")
async def support_new_handler(callback: types.CallbackQuery):
    # Check if already has ticket
    ticket = await get_user_ticket(callback.from_user.id)
    if ticket:
        await callback.answer("You already have an open ticket!", show_alert=True)
        return
    
    text = f"💬 Create Support Ticket\n\nHi {callback.from_user.first_name}!\n\nDescribe your issue or question.\nType your message now:"
    
    support_mode.add(callback.from_user.id)
    await edit_message(callback.message, text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel", callback_data="back")]
    ]))
    await callback.answer("Type your message!")

@dp.callback_query(F.data == "support_close")
async def support_close_handler(callback: types.CallbackQuery):
    ticket = await get_user_ticket(callback.from_user.id)
    
    if not ticket:
        await callback.answer("No open ticket found!", show_alert=True)
        return
    
    ticket_id = str(ticket['_id'])
    success = await close_ticket(ticket_id)
    
    if success:
        support_mode.discard(callback.from_user.id)
        
        # Notify admin
        try:
            await send_message(ADMIN_ID, f"🎫 Ticket #{ticket_id[-6:]} closed by user {callback.from_user.first_name} ({callback.from_user.id})")
        except:
            pass
        
        text = f"✅ Ticket Closed\n\nTicket #{ticket_id[-6:]} has been closed.\n\nThank you for contacting support!"
        await edit_message(callback.message, text, main_keyboard())
        await callback.answer("Ticket closed!")
    else:
        await callback.answer("Error closing ticket!", show_alert=True)

# FIXED: Text message handler for support
@dp.message(F.text & ~F.command)
async def text_handler(message: types.Message):
    if is_admin(message.from_user.id):
        return
    
    await save_user(message.from_user)
    user_id = message.from_user.id
    
    # Check if user is in support mode or has ticket
    ticket = await get_user_ticket(user_id)
    
    if user_id in support_mode or ticket:
        # Handle support message
        if ticket:
            # Add to existing ticket
            ticket_id = str(ticket['_id'])
            success = await add_to_ticket(ticket_id, "user", message.text)
            
            if success:
                await send_message(user_id, f"✅ Message added to ticket #{ticket_id[-6:]}\n\nYour message: {message.text[:100]}{'...' if len(message.text) > 100 else ''}\n\nAdmin will be notified.")
                
                # Notify admin
                user_data = await get_user_data(user_id)
                priority = "HIGH" if user_data and user_data.get("status") == "premium" else "NORMAL"
                
                admin_text = f"🎫 New message on ticket #{ticket_id[-6:]}\nPriority: {priority}\n\nUser: {message.from_user.first_name} (@{message.from_user.username or 'none'})\nID: {user_id}\n\nMessage: {message.text}\n\nReply: /reply {ticket_id} Your response"
                await send_message(ADMIN_ID, admin_text)
            else:
                await send_message(user_id, "❌ Error adding message. Please try again.")
        else:
            # Create new ticket
            ticket_id = await create_ticket(
                user_id,
                message.from_user.username or "none",
                message.from_user.first_name,
                message.text
            )
            
            if ticket_id:
                support_mode.discard(user_id)
                
                user_data = await get_user_data(user_id)
                priority = "HIGH" if user_data and user_data.get("status") == "premium" else "NORMAL"
                response_time = "2-5 min" if priority == "HIGH" else "10-30 min"
                
                await send_message(user_id, f"✅ Support ticket created!\n\nTicket: #{ticket_id[-6:]}\nPriority: {priority}\nResponse time: {response_time}\n\nYou'll be notified when admin replies!")
                
                # Notify admin
                admin_text = f"🎫 NEW TICKET #{ticket_id[-6:]}\nPriority: {priority}\n\nUser: {message.from_user.first_name} (@{message.from_user.username or 'none'})\nID: {user_id}\n\nMessage: {message.text}\n\nReply: /reply {ticket_id} Your response"
                await send_message(ADMIN_ID, admin_text)
            else:
                await send_message(user_id, "❌ Error creating ticket. Please try again.")
    else:
        # Regular message - suggest support
        text = f"Hi {message.from_user.first_name}!\n\nI see you sent a message. If you need help, click Support below to create a ticket."
        await send_message(user_id, text, main_keyboard())

@dp.callback_query(F.data.startswith("plan_"))
async def plan_handler(callback: types.CallbackQuery):
    plan_key = callback.data.replace("plan_", "")
    last_plan[callback.from_user.id] = plan_key
    plan = PLANS[plan_key]
    
    daily = plan["price"] / plan["days"]
    text = f"🎯 {plan['emoji']} {plan['name']} Plan\n\nPrice: Rs.{plan['price']}\nDuration: {plan['days']} days\nDaily cost: Rs.{daily:.1f}\n\nChoose payment method:"
    
    await edit_message(callback.message, text, payment_keyboard(plan_key))
    await callback.answer()

@dp.callback_query(F.data.startswith("upi_"))
async def upi_handler(callback: types.CallbackQuery):
    plan_key = callback.data.replace("upi_", "")
    plan = PLANS[plan_key]
    
    text = f"💳 UPI Payment\n\nPlan: {plan['emoji']} {plan['name']}\nAmount: Rs.{plan['price']}\n\nSteps:\n1. Copy UPI ID from below\n2. Pay in your UPI app\n3. Upload screenshot"
    
    await edit_message(callback.message, text, payment_keyboard(plan_key))
    
    # Send UPI details
    upi_text = f"UPI ID: {UPI_ID}\nAmount: {plan['price']}\n\nLong press the UPI ID above to copy.\nAfter payment, upload screenshot here!"
    await send_message(callback.from_user.id, upi_text)
    await callback.answer("UPI details sent!")

@dp.callback_query(F.data.startswith("qr_"))
async def qr_handler(callback: types.CallbackQuery):
    plan_key = callback.data.replace("qr_", "")
    plan = PLANS[plan_key]
    
    text = f"📱 QR Payment\n\nPlan: {plan['emoji']} {plan['name']}\nAmount: Rs.{plan['price']}\n\nScan QR, pay, and upload screenshot."
    
    await send_photo(callback.from_user.id, QR_CODE_URL, text, payment_keyboard(plan_key))
    await callback.answer()

@dp.callback_query(F.data.startswith("upload_"))
async def upload_handler(callback: types.CallbackQuery):
    plan_key = callback.data.replace("upload_", "")
    last_plan[callback.from_user.id] = plan_key
    plan = PLANS[plan_key]
    
    text = f"📸 Upload Payment Screenshot\n\nPlan: {plan['emoji']} {plan['name']}\nAmount: Rs.{plan['price']}\n\nRequirements:\n• Clear screenshot\n• Shows success\n• Amount visible\n\nSend photo now:"
    
    await edit_message(callback.message, text)
    await callback.answer("Send screenshot!")

@dp.message(F.photo)
async def photo_handler(message: types.Message):
    if is_admin(message.from_user.id):
        return
    
    plan_key = last_plan.get(message.from_user.id)
    if not plan_key:
        await send_message(message.from_user.id, "❌ Please select a plan first using /start")
        return
    
    # Save payment
    payment_id = await save_payment(message.from_user.id, plan_key, message.photo[-1].file_id)
    
    if payment_id:
        plan = PLANS[plan_key]
        
        # Confirm to user
        text = f"🎉 Payment proof received!\n\nProof ID: #{payment_id[-6:]}\nPlan: {plan['emoji']} {plan['name']}\nAmount: Rs.{plan['price']}\n\nProcessing time: 3-5 minutes\nYou'll be notified when approved!"
        
        await send_photo(message.chat.id, SUCCESS_IMAGE, text)
        
        # Notify admin
        admin_text = f"💰 New Payment #{payment_id[-6:]}\n\nUser: {message.from_user.first_name} (@{message.from_user.username})\nID: {message.from_user.id}\nPlan: {plan['emoji']} {plan['name']}\nAmount: Rs.{plan['price']}"
        
        await send_message(ADMIN_ID, admin_text)
        await bot.send_photo(
            ADMIN_ID,
            message.photo[-1].file_id,
            caption=f"Payment proof #{payment_id[-6:]}\n{plan['name']} - Rs.{plan['price']}\nUser: {message.from_user.first_name} ({message.from_user.id})",
            reply_markup=payment_action_keyboard(payment_id, message.from_user.id)
        )
    else:
        await send_message(message.from_user.id, "❌ Error saving payment. Please try again.")

# Admin Handlers
@dp.callback_query(F.data == "admin")
async def admin_handler(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied!", show_alert=True)
        return
    
    # Get stats
    total_users = await users_col.count_documents({})
    premium_users = await users_col.count_documents({"status": "premium"})
    pending_payments = await payments_col.count_documents({"status": "pending"})
    open_tickets = await tickets_col.count_documents({"status": "open"})
    
    text = f"🛠 Admin Panel\n\nStats:\n👥 Users: {total_users}\n💎 Premium: {premium_users}\n💰 Pending: {pending_payments}\n🎫 Tickets: {open_tickets}\n\nSystem: Online\nTime: {datetime.now().strftime('%H:%M:%S')}"
    
    await edit_message(callback.message, text, admin_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "admin_payments")
async def admin_payments_handler(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied!", show_alert=True)
        return
    
    cursor = payments_col.find({"status": "pending"}).sort("created", -1).limit(10)
    payments = await cursor.to_list(10)
    
    if not payments:
        await send_message(callback.message.chat.id, "✅ No pending payments!")
        await callback.answer()
        return
    
    await send_message(callback.message.chat.id, f"⏳ Processing {len(payments)} pending payments...")
    
    for payment in payments:
        plan = PLANS[payment['plan']]
        pid = str(payment['_id'])
        
        text = f"💵 Payment #{pid[-6:]}\n\nUser: {payment['user_id']}\nPlan: {plan['emoji']} {plan['name']}\nAmount: Rs.{plan['price']}\nTime: {payment['created'].strftime('%d %b, %H:%M')}\n\nChoose action:"
        
        await send_message(
            callback.message.chat.id,
            text,
            payment_action_keyboard(pid, payment['user_id'])
        )
    
    await callback.answer(f"📋 {len(payments)} payments loaded")

@dp.callback_query(F.data.startswith("approve_"))
async def approve_handler(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied!", show_alert=True)
        return
    
    try:
        parts = callback.data.split("_")
        payment_id, user_id = parts[1], int(parts[2])
        
        payment = await get_payment_data(payment_id)
        if not payment:
            await callback.answer("Payment not found!", show_alert=True)
            return
        
        plan_key = payment["plan"]
        plan = PLANS[plan_key]
        
        # Update payment status
        await update_payment_status(payment_id, "approved")
        
        # Activate plan
        success = await activate_plan(user_id, plan_key)
        
        if success:
            # Create invite link
            try:
                link = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
                user_text = f"🎉 PAYMENT APPROVED!\n\nYour {plan['emoji']} {plan['name']} subscription is now ACTIVE!\nAmount: Rs.{plan['price']}\nValid for: {plan['days']} days\n\nJoin Premium Channel:\n{link.invite_link}\n\nWelcome to Premium!"
            except Exception as e:
                user_text = f"🎉 PAYMENT APPROVED!\n\nYour {plan['emoji']} {plan['name']} subscription is now ACTIVE!\nAmount: Rs.{plan['price']}\nValid for: {plan['days']} days\n\nWelcome to Premium!"
            
            await send_message(user_id, user_text)
            
            await callback.message.edit_text(f"✅ Payment #{payment_id[-6:]} APPROVED\n\n{plan['emoji']} {plan['name']} activated for user {user_id}!")
            await callback.answer("✅ Approved!")
        else:
            await callback.answer("❌ Error activating plan!", show_alert=True)
            
    except Exception as e:
        log.error(f"Error approving payment: {e}")
        await callback.answer("❌ Error processing!", show_alert=True)

@dp.callback_query(F.data.startswith("deny_"))
async def deny_handler(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied!", show_alert=True)
        return
    
    try:
        parts = callback.data.split("_")
        payment_id, user_id = parts[1], int(parts[2])
        
        await update_payment_status(payment_id, "denied")
        
        user_text = f"❌ Payment proof not approved\n\nProof #{payment_id[-6:]} could not be approved.\n\nReasons:\n• Screenshot not clear\n• Wrong amount\n• Payment not visible\n• Missing details\n\nPlease upload a clearer screenshot.\n\nNeed help? Contact support!"
        
        await send_message(user_id, user_text)
        
        await callback.message.edit_text(f"❌ Payment #{payment_id[-6:]} DENIED\n\nUser {user_id} notified with feedback.")
        await callback.answer("❌ Denied!")
        
    except Exception as e:
        log.error(f"Error denying payment: {e}")
        await callback.answer("❌ Error processing!", show_alert=True)

@dp.callback_query(F.data == "admin_tickets")
async def admin_tickets_handler(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied!", show_alert=True)
        return
    
    cursor = tickets_col.find({"status": "open"}).sort("created", -1).limit(10)
    tickets = await cursor.to_list(10)
    
    if not tickets:
        await send_message(callback.message.chat.id, "✅ No open tickets!")
        await callback.answer()
        return
    
    text = f"🎫 Open Support Tickets ({len(tickets)})\n\nClick on a ticket to view and reply:"
    await send_message(callback.message.chat.id, text, tickets_keyboard(tickets))
    await callback.answer(f"📋 {len(tickets)} tickets")

@dp.callback_query(F.data.startswith("ticket_"))
async def ticket_view_handler(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied!", show_alert=True)
        return
    
    ticket_short_id = callback.data.replace("ticket_", "")
    
    # Find ticket by short ID
    cursor = tickets_col.find({"status": "open"})
    tickets = await cursor.to_list(100)
    
    ticket = None
    for t in tickets:
        if str(t['_id'])[-6:] == ticket_short_id:
            ticket = t
            break
    
    if not ticket:
        await callback.answer("Ticket not found!", show_alert=True)
        return
    
    ticket_id = str(ticket['_id'])
    user_id = ticket['user_id']
    created = ticket['created'].strftime('%d %b, %H:%M')
    messages = ticket['messages']
    
    text = f"🎫 Ticket #{ticket_short_id}\n\nUser: {ticket['first_name']} ({user_id})\nCreated: {created}\nMessages: {len(messages)}\nStatus: OPEN\n\nConversation:\n"
    
    # Show last 5 messages
    recent = messages[-5:] if len(messages) > 5 else messages
    for i, msg in enumerate(recent, 1):
        sender = "Admin" if msg['from'] == 'admin' else "User"
        time = msg['time'].strftime('%H:%M')
        text += f"\n{i}. {sender} ({time}):\n   {msg['text'][:80]}{'...' if len(msg['text']) > 80 else ''}\n"
    
    if len(messages) > 5:
        text += f"\n... and {len(messages) - 5} older messages"
    
    await send_message(callback.message.chat.id, text, ticket_action_keyboard(ticket_id))
    await callback.answer()

@dp.callback_query(F.data.startswith("reply_"))
async def reply_start_handler(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied!", show_alert=True)
        return
    
    ticket_id = callback.data.replace("reply_", "")
    await state.update_data(ticket_id=ticket_id)
    await state.set_state(AdminReply.waiting)
    
    await send_message(callback.message.chat.id, f"✏️ Reply to ticket #{ticket_id[-6:]}\n\nType your response:")
    await callback.answer("Type your reply")

@dp.message(AdminReply.waiting)
async def reply_send_handler(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    
    data = await state.get_data()
    ticket_id = data['ticket_id']
    
    # Add admin reply to ticket
    success = await add_to_ticket(ticket_id, "admin", message.text)
    
    if not success:
        await send_message(message.chat.id, "❌ Error adding reply")
        await state.clear()
        return
    
    # Get ticket to find user
    ticket = await get_ticket_data(ticket_id)
    if not ticket:
        await send_message(message.chat.id, "❌ Ticket not found")
        await state.clear()
        return
    
    # Send reply to user
    user_reply = f"💬 Support Response\nTicket: #{ticket_id[-6:]}\n\n{message.text}\n\n━━━━━━━━━━━━━━━━━\n🎧 Premium Support Team\n💬 Need more help? Just reply!"
    
    try:
        await send_message(ticket['user_id'], user_reply)
        await send_message(message.chat.id, f"✅ Reply sent!\n\nTicket: #{ticket_id[-6:]}\nUser: {ticket['user_id']}\nYour reply: {message.text[:100]}{'...' if len(message.text) > 100 else ''}")
    except Exception as e:
        await send_message(message.chat.id, f"❌ Failed to send reply: {e}")
    
    await state.clear()

@dp.callback_query(F.data.startswith("close_"))
async def close_ticket_handler(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied!", show_alert=True)
        return
    
    ticket_id = callback.data.replace("close_", "")
    
    ticket = await get_ticket_data(ticket_id)
    if not ticket:
        await callback.answer("Ticket not found!", show_alert=True)
        return
    
    success = await close_ticket(ticket_id)
    
    if success:
        # Notify user
        user_msg = f"✅ Ticket Resolved\nTicket: #{ticket_id[-6:]}\n\nYour support ticket has been resolved and closed.\n\nNeed more help? Create a new ticket anytime!"
        
        try:
            await send_message(ticket['user_id'], user_msg)
        except:
            pass
        
        await callback.message.edit_text(f"✅ Ticket #{ticket_id[-6:]} closed\n\nUser notified.")
        await callback.answer("✅ Closed!")
    else:
        await callback.answer("❌ Error closing!", show_alert=True)

@dp.callback_query(F.data == "admin_stats")
async def admin_stats_handler(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied!", show_alert=True)
        return
    
    total = await users_col.count_documents({})
    premium = await users_col.count_documents({"status": "premium"})
    free = total - premium
    pending = await payments_col.count_documents({"status": "pending"})
    tickets = await tickets_col.count_documents({"status": "open"})
    
    premium_rate = (premium/total*100) if total > 0 else 0
    
    text = f"📊 Bot Statistics\n\nUser Stats:\n👥 Total: {total}\n💎 Premium: {premium}\n🆓 Free: {free}\n\nOther Stats:\n💰 Pending Payments: {pending}\n🎫 Open Tickets: {tickets}\n\nMetrics:\n📈 Premium Rate: {premium_rate:.1f}%\n\nGenerated: {datetime.now().strftime('%d %b %Y, %H:%M:%S')}"
    
    await send_message(callback.message.chat.id, text)
    await callback.answer()

@dp.callback_query(F.data == "admin_users")
async def admin_users_handler(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied!", show_alert=True)
        return
    
    cursor = users_col.find({}).sort("created", -1).limit(20)
    users = await cursor.to_list(20)
    
    if not users:
        await send_message(callback.message.chat.id, "👥 No users found")
        await callback.answer()
        return
    
    text = f"👥 User List (Top 20)\n\n"
    premium_count = 0
    
    for i, user in enumerate(users, 1):
        status = user.get("status", "free")
        if status == "premium":
            emoji = "✅"
            premium_count += 1
        else:
            emoji = "⚪"
        
        text += f"{i}. {emoji} {user['user_id']} (@{user.get('username', 'none')})\n"
        text += f"   Status: {status.upper()}\n"
        if user.get("plan"):
            plan = PLANS.get(user["plan"], {"name": "Unknown"})
            text += f"   Plan: {plan['name']}\n"
        text += "\n"
    
    text = f"👥 Users ({len(users)})\nPremium: {premium_count}\n\n" + text
    
    if len(text) > 4000:
        await send_message(callback.message.chat.id, text[:4000] + "\n\n... [List truncated]")
    else:
        await send_message(callback.message.chat.id, text)
    
    await callback.answer(f"📋 {len(users)} users shown")

@dp.callback_query(F.data == "admin_broadcast")
async def broadcast_start_handler(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Access denied!", show_alert=True)
        return
    
    total = await users_col.count_documents({})
    text = f"📢 Broadcast Center\n\nTarget: {total} users\nDelivery: Direct message\nTime: ~{total * 0.05:.1f} seconds\n\nSend your message:"
    
    await send_message(callback.message.chat.id, text)
    await state.set_state(Broadcast.waiting)
    await callback.answer("📢 Type broadcast message")

@dp.message(Broadcast.waiting)
async def broadcast_send_handler(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    
    cursor = users_col.find({}, {"user_id": 1})
    users = await cursor.to_list(None)
    
    if not users:
        await send_message(message.chat.id, "❌ No users to broadcast to")
        await state.clear()
        return
    
    await send_message(message.chat.id, f"📤 Broadcasting to {len(users)} users...")
    
    sent = failed = 0
    
    for user in users:
        try:
            broadcast_text = f"📢 OFFICIAL ANNOUNCEMENT\n\n{message.text}\n\n━━━━━━━━━━━━━━━━━━━\n💎 Premium Bot Team"
            await send_message(user["user_id"], broadcast_text)
            sent += 1
            await asyncio.sleep(0.03)  # Rate limit
        except:
            failed += 1
    
    success_rate = (sent/(sent+failed)*100) if (sent+failed) > 0 else 0
    report = f"📢 Broadcast Complete!\n\n✅ Sent: {sent}\n❌ Failed: {failed}\n📈 Success: {success_rate:.1f}%"
    
    await send_message(message.chat.id, report)
    await state.clear()

# FIXED: Admin reply commands
@dp.message(Command("reply"))
@dp.message(Command("replay"))
async def reply_command_handler(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    
    try:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await send_message(message.chat.id, "❌ Usage:\n/reply <ticket_id> <message>\n\nExample:\n/reply 507f1f77bcf86cd799439011 Hello, thanks for contacting support!")
            return
        
        ticket_id, reply_text = parts[1], parts[2]
        
        # Add reply to ticket
        success = await add_to_ticket(ticket_id, "admin", reply_text)
        
        if not success:
            await send_message(message.chat.id, "❌ Ticket not found\n\nCheck the ticket ID.")
            return
        
        # Get ticket to find user
        ticket = await get_ticket_data(ticket_id)
        if not ticket:
            await send_message(message.chat.id, "❌ Ticket not found")
            return
        
        # Send reply to user
        user_reply = f"💬 Support Response\nTicket: #{ticket_id[-6:]}\n\n{reply_text}\n\n━━━━━━━━━━━━━━━━━\n🎧 Premium Support Team\n💬 Need more help? Just reply!"
        
        try:
            await send_message(ticket['user_id'], user_reply)
            await send_message(message.chat.id, f"✅ Reply sent successfully!\n\nTicket: #{ticket_id[-6:]}\nUser: {ticket['user_id']}\nYour reply: {reply_text[:100]}{'...' if len(reply_text) > 100 else ''}")
        except Exception as e:
            await send_message(message.chat.id, f"❌ Failed to send reply: {e}")
        
    except Exception as e:
        await send_message(message.chat.id, f"❌ Command error: {e}")

# Expiry worker
async def expiry_worker():
    while True:
        try:
            now = datetime.now(timezone.utc)
            
            # Find users with active premium
            cursor = users_col.find({"status": "premium"})
            users = await cursor.to_list(None)
            
            for user in users:
                end_date = user.get("end")
                if not end_date:
                    continue
                
                # Ensure timezone aware
                if end_date.tzinfo is None:
                    end_date = end_date.replace(tzinfo=timezone.utc)
                
                user_id = user["user_id"]
                
                # Check if expired
                if end_date <= now:
                    try:
                        # Update status
                        await users_col.update_one({"user_id": user_id}, {"$set": {"status": "expired"}})
                        
                        # Remove from channel
                        try:
                            await bot.ban_chat_member(CHANNEL_ID, user_id)
                            await bot.unban_chat_member(CHANNEL_ID, user_id)
                        except:
                            pass
                        
                        # Notify user
                        await send_message(user_id, "❌ Subscription Expired\n\nYour premium subscription has expired.\n\nRenew now:\n1. Use /start\n2. Choose plan\n3. Complete payment\n\nWe miss you! Come back to premium!")
                        
                        log.info(f"Processed expiry for user {user_id}")
                    except Exception as e:
                        log.error(f"Error processing expiry for user {user_id}: {e}")
                
                # 3-day reminder
                elif (end_date - now) <= timedelta(days=3) and not user.get("reminded"):
                    try:
                        days_left = (end_date - now).days
                        await send_message(user_id, f"⏰ Subscription expires in {days_left} days!\n\nRenew now to continue premium features.\nUse /start to renew!")
                        await users_col.update_one({"user_id": user_id}, {"$set": {"reminded": True}})
                        log.info(f"Sent reminder to user {user_id}")
                    except Exception as e:
                        log.error(f"Error sending reminder to user {user_id}: {e}")
        
        except Exception as e:
            log.error(f"Expiry worker error: {e}")
        
        await asyncio.sleep(1800)  # 30 minutes

# Main function
async def main():
    try:
        # Test MongoDB
        await client.admin.command('ping')
        log.info("✅ MongoDB connected")
        
        # Start expiry worker
        asyncio.create_task(expiry_worker())
        log.info("✅ Expiry worker started")
        
        # Start bot
        log.info("🚀 Starting Premium Bot - FIXED VERSION")
        await dp.start_polling(bot, skip_updates=True)
        
    except Exception as e:
        log.error(f"❌ Bot failed to start: {e}")
        raise

if __name__ == "__main__":
    if API_TOKEN == "TEST_TOKEN":
        raise RuntimeError("❌ Set API_TOKEN environment variable")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("✅ Bot stopped")
    except Exception as e:
        log.error(f"❌ Bot crashed: {e}")
        raise
