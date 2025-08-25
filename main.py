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
log = logging.getLogger("premium_bot")

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
mongo_client = AsyncIOMotorClient(MONGO_URI, maxPoolSize=10, minPoolSize=2)
db = mongo_client['premium_bot']
users_col = db['users']
payments_col = db['payments']
tickets_col = db['support_tickets']

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Plans
PLANS = {
    "plan1": {"name": "1 Month", "price": "₹99", "days": 30, "emoji": "🟢"},
    "plan2": {"name": "6 Months", "price": "₹399", "days": 180, "emoji": "🟡", "popular": True},
    "plan3": {"name": "1 Year", "price": "₹1999", "days": 365, "emoji": "🔥"},
    "plan4": {"name": "Lifetime", "price": "₹2999", "days": 36500, "emoji": "💎"},
}
last_selected_plan: Dict[int, str] = {}

# Track users in support mode
users_in_support = set()

# FSM States
class BCast(StatesGroup):
    waiting_text = State()

class SupportReply(StatesGroup):
    waiting_reply = State()

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def safe_text(text) -> str:
    return str(text or "No info").replace("None", "No info")

def ensure_timezone_aware(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

# Database Operations
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

# FIXED: Support System Functions
async def create_support_ticket(user_id: int, username: str, first_name: str, message: str):
    """Create a new support ticket"""
    try:
        now = datetime.now(timezone.utc)
        ticket_data = {
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "subject": message[:50] + "..." if len(message) > 50 else message,
            "messages": [
                {
                    "from": "user",
                    "message": message,
                    "timestamp": now
                }
            ],
            "status": "open",
            "priority": "normal",
            "created_at": now,
            "updated_at": now,
            "admin_id": None
        }
        
        result = await tickets_col.insert_one(ticket_data)
        ticket_id = str(result.inserted_id)
        
        log.info(f"Created support ticket {ticket_id} for user {user_id}")
        return ticket_id
        
    except Exception as e:
        log.error(f"Error creating support ticket: {e}")
        return None

async def add_message_to_ticket(ticket_id: str, sender: str, message: str):
    """Add a message to existing ticket"""
    try:
        now = datetime.now(timezone.utc)
        await tickets_col.update_one(
            {"_id": ObjectId(ticket_id)},
            {
                "$push": {
                    "messages": {
                        "from": sender,
                        "message": message,
                        "timestamp": now
                    }
                },
                "$set": {"updated_at": now}
            }
        )
        log.info(f"Added message to ticket {ticket_id} from {sender}")
        return True
    except Exception as e:
        log.error(f"Error adding message to ticket: {e}")
        return False

async def get_ticket(ticket_id: str):
    """Get ticket by ID"""
    try:
        return await tickets_col.find_one({"_id": ObjectId(ticket_id)})
    except Exception as e:
        log.error(f"Error getting ticket {ticket_id}: {e}")
        return None

async def get_user_active_ticket(user_id: int):
    """Get user's active ticket if exists"""
    try:
        return await tickets_col.find_one({"user_id": user_id, "status": "open"})
    except Exception as e:
        log.error(f"Error getting user active ticket: {e}")
        return None

async def close_support_ticket(ticket_id: str):
    """Close a support ticket"""
    try:
        await tickets_col.update_one(
            {"_id": ObjectId(ticket_id)},
            {
                "$set": {
                    "status": "closed",
                    "closed_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc)
                }
            }
        )
        log.info(f"Closed support ticket {ticket_id}")
        return True
    except Exception as e:
        log.error(f"Error closing ticket: {e}")
        return False

async def get_open_tickets(limit: int = 20):
    """Get all open tickets"""
    try:
        cursor = tickets_col.find({"status": "open"}).sort("updated_at", -1).limit(limit)
        return await cursor.to_list(length=limit)
    except Exception as e:
        log.error(f"Error getting open tickets: {e}")
        return []

async def get_stats():
    try:
        total = await users_col.count_documents({})
        active = await users_col.count_documents({"status": "active"})
        expired = await users_col.count_documents({"status": "expired"})
        pending = await payments_col.count_documents({"status": "pending"})
        open_tickets = await tickets_col.count_documents({"status": "open"})
        return total, active, expired, pending, open_tickets
    except Exception as e:
        log.error(f"Error getting stats: {e}")
        return 0, 0, 0, 0, 0

# UI Functions
async def send_photo_fast(chat_id: int, photo_url: str, caption: str, reply_markup=None):
    try:
        await bot.send_photo(chat_id, photo_url, caption=caption, reply_markup=reply_markup)
    except Exception as e:
        log.warning(f"Failed to send photo: {e}")
        try:
            await bot.send_message(chat_id, caption, reply_markup=reply_markup)
        except Exception as e2:
            log.error(f"Failed to send message fallback: {e2}")

async def edit_or_send(cq: types.CallbackQuery, text: str = None, photo_url: str = None, reply_markup=None):
    try:
        if photo_url:
            await cq.message.delete()
            await send_photo_fast(cq.from_user.id, photo_url, text, reply_markup)
        elif text:
            await cq.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        try:
            if photo_url:
                await send_photo_fast(cq.from_user.id, photo_url, text, reply_markup)
            else:
                await cq.message.answer(text, reply_markup=reply_markup)
        except Exception as e:
            log.error(f"Failed to send fallback message: {e}")

# Keyboards
def kb_user_menu() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🚀 Upgrade Premium", callback_data="menu_buy")],
        [InlineKeyboardButton(text="📊 My Subscription", callback_data="menu_my"),
         InlineKeyboardButton(text="💬 Support", callback_data="menu_support")],
        [InlineKeyboardButton(text="🎁 Special Offers", callback_data="menu_offers")]
    ]
    
    if is_admin(ADMIN_ID):
        buttons.append([InlineKeyboardButton(text="🛠 Admin Panel", callback_data="admin_menu")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_support_options() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 New Support Ticket", callback_data="support_new")],
        [InlineKeyboardButton(text="📋 My Tickets", callback_data="support_my")],
        [InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back_menu")]
    ])

def kb_support_active() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Close Ticket", callback_data="support_close")],
        [InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back_menu")]
    ])

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
        [InlineKeyboardButton(text="✅ Approve Payment", callback_data=f"approve_{payment_id}_{user_id}")],
        [InlineKeyboardButton(text="❌ Deny Payment", callback_data=f"deny_{payment_id}_{user_id}")]
    ])

def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏳ Pending Payments", callback_data="admin_pending"),
            InlineKeyboardButton(text="📊 Statistics", callback_data="admin_stats")
        ],
        [
            InlineKeyboardButton(text="🎫 Support Tickets", callback_data="admin_tickets"),
            InlineKeyboardButton(text="👥 All Users", callback_data="admin_users")
        ],
        [
            InlineKeyboardButton(text="📢 Broadcast", callback_data="admin_broadcast"),
            InlineKeyboardButton(text="⚙️ Settings", callback_data="admin_settings")
        ],
        [InlineKeyboardButton(text="⬅️ Back to Menu", callback_data="back_menu")]
    ])

def kb_admin_tickets(tickets) -> InlineKeyboardMarkup:
    buttons = []
    for ticket in tickets[:10]:  # Limit to 10 tickets
        ticket_id = str(ticket['_id'])
        user_id = ticket['user_id']
        subject = ticket.get('subject', 'No subject')[:30]
        
        # Count messages
        msg_count = len(ticket.get('messages', []))
        
        button_text = f"#{ticket_id[-6:]} - {user_id} ({msg_count} msgs)"
        buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"admin_ticket_{ticket_id}")])
    
    buttons.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="admin_tickets")])
    buttons.append([InlineKeyboardButton(text="⬅️ Back to Admin", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def kb_admin_ticket_actions(ticket_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Reply", callback_data=f"admin_reply_{ticket_id}"),
            InlineKeyboardButton(text="❌ Close", callback_data=f"admin_close_{ticket_id}")
        ],
        [InlineKeyboardButton(text="⬅️ Back to Tickets", callback_data="admin_tickets")]
    ])

# Bot Handlers
@dp.message(CommandStart())
async def cmd_start(m: types.Message):
    await upsert_user(m.from_user)
    # Remove user from support mode when they restart
    if m.from_user.id in users_in_support:
        users_in_support.remove(m.from_user.id)
    
    caption = f"👋 **Hello {m.from_user.first_name}!**\n\n🌟 **Upgrade to Premium:**\n• Unlimited downloads\n• Ad-free experience\n• Priority support\n• High-speed access\n\n🚀 **Ready to upgrade?**"
    await send_photo_fast(m.from_user.id, WELCOME_IMAGE, caption, kb_user_menu())

@dp.callback_query(F.data == "back_menu")
async def back_to_menu(cq: types.CallbackQuery):
    # Remove user from support mode
    if cq.from_user.id in users_in_support:
        users_in_support.remove(cq.from_user.id)
    
    caption = f"🏠 **Welcome back {cq.from_user.first_name}!**\n\nChoose an option below:"
    await edit_or_send(cq, text=caption, photo_url=WELCOME_IMAGE, reply_markup=kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data == "menu_buy")
async def on_buy(cq: types.CallbackQuery):
    caption = "💎 **Premium Plans**\n\nChoose your subscription plan:"
    await edit_or_send(cq, text=caption, photo_url=PLANS_IMAGE, reply_markup=kb_plans())
    await cq.answer()

@dp.callback_query(F.data == "menu_offers")
async def show_offers(cq: types.CallbackQuery):
    caption = "🎁 **Special Offers**\n\n🟡 **6 Months:** Save 33%\n🔥 **1 Year:** Best Value\n💎 **Lifetime:** One-time payment\n\n⏰ **Limited time offers!**"
    await edit_or_send(cq, text=caption, photo_url=OFFERS_IMAGE, reply_markup=kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data == "menu_my")
async def on_my_plan(cq: types.CallbackQuery):
    user = await get_user(cq.from_user.id)
    
    if not user or user.get("status") != "active":
        caption = "😔 **No Active Subscription**\n\nYou're using the FREE version.\n\n🌟 **Upgrade benefits:**\n• Unlimited access\n• No advertisements\n• Priority support\n• Premium features\n\n👆 **Ready to upgrade?**"
        await edit_or_send(cq, text=caption, photo_url=UPGRADE_IMAGE, reply_markup=kb_user_menu())
    else:
        plan_info = PLANS.get(user.get('plan_key'), {'name': 'Unknown', 'emoji': '📦'})
        
        if user.get('end_at'):
            try:
                end_date = ensure_timezone_aware(user['end_at'])
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
        
        await edit_or_send(cq, text=caption, reply_markup=kb_user_menu())
    
    await cq.answer()

# FIXED: Complete Support System
@dp.callback_query(F.data == "menu_support")
async def on_support_menu(cq: types.CallbackQuery):
    # Check if user has active ticket
    active_ticket = await get_user_active_ticket(cq.from_user.id)
    
    if active_ticket:
        ticket_id = str(active_ticket['_id'])
        msg_count = len(active_ticket.get('messages', []))
        created = active_ticket['created_at'].strftime('%d %b, %H:%M')
        subject = active_ticket.get('subject', 'No subject')
        
        text = f"🎫 **Your Active Support Ticket**\n\n**Ticket ID:** #{ticket_id[-6:]}\n**Subject:** {subject}\n**Created:** {created}\n**Messages:** {msg_count}\n**Status:** 🟢 Open\n\n💬 **Send a message to add to this ticket**\n❌ **Or close the ticket if resolved**"
        
        # Add user to support mode
        users_in_support.add(cq.from_user.id)
        await edit_or_send(cq, text=text, reply_markup=kb_support_active())
    else:
        text = f"💬 **Customer Support**\n\nHi {cq.from_user.first_name}!\n\n🎫 **Create a new support ticket to get help**\n📋 **Or view your previous tickets**"
        await edit_or_send(cq, text=text, reply_markup=kb_support_options())
    
    await cq.answer()

@dp.callback_query(F.data == "support_new")
async def support_new_ticket(cq: types.CallbackQuery):
    # Check if user already has active ticket
    active_ticket = await get_user_active_ticket(cq.from_user.id)
    
    if active_ticket:
        await cq.answer("❌ You already have an active support ticket! Close it first to create a new one.", show_alert=True)
        return
    
    text = f"💬 **Create New Support Ticket**\n\nHi {cq.from_user.first_name}!\n\n📝 **Describe your issue or question:**\n\nType your message and I'll create a support ticket for you. Our team will respond quickly!"
    
    # Add user to support mode
    users_in_support.add(cq.from_user.id)
    await edit_or_send(cq, text=text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel", callback_data="back_menu")]
    ]))
    await cq.answer("💬 Type your support message")

@dp.callback_query(F.data == "support_my")
async def support_my_tickets(cq: types.CallbackQuery):
    try:
        # Get user's recent tickets
        cursor = tickets_col.find({"user_id": cq.from_user.id}).sort("created_at", -1).limit(5)
        tickets = await cursor.to_list(length=5)
        
        if not tickets:
            text = "📋 **Your Support Tickets**\n\n❌ **No tickets found**\n\nYou haven't created any support tickets yet."
        else:
            text = f"📋 **Your Support Tickets** ({len(tickets)})\n\n"
            
            for i, ticket in enumerate(tickets, 1):
                ticket_id = str(ticket['_id'])[-6:]
                status = "🟢 Open" if ticket['status'] == 'open' else "🔴 Closed"
                subject = ticket.get('subject', 'No subject')[:30]
                created = ticket['created_at'].strftime('%d %b')
                msg_count = len(ticket.get('messages', []))
                
                text += f"{i}. **#{ticket_id}** - {status}\n"
                text += f"   📝 {subject}\n"
                text += f"   📅 {created} • {msg_count} messages\n\n"
        
        await edit_or_send(cq, text=text, reply_markup=kb_support_options())
        await cq.answer()
        
    except Exception as e:
        log.error(f"Error getting user tickets: {e}")
        await cq.answer("❌ Error loading tickets", show_alert=True)

@dp.callback_query(F.data == "support_close")
async def support_close_ticket(cq: types.CallbackQuery):
    try:
        # Get user's active ticket
        active_ticket = await get_user_active_ticket(cq.from_user.id)
        
        if not active_ticket:
            await cq.answer("❌ No active support ticket found", show_alert=True)
            return
        
        ticket_id = str(active_ticket['_id'])
        
        # Close the ticket
        success = await close_support_ticket(ticket_id)
        
        if success:
            # Remove user from support mode
            if cq.from_user.id in users_in_support:
                users_in_support.remove(cq.from_user.id)
            
            # Notify admin
            try:
                admin_msg = f"🎫 **Ticket Closed by User**\n\n**Ticket:** #{ticket_id[-6:]}\n**User:** {cq.from_user.first_name} ({cq.from_user.id})\n**Action:** User closed the ticket"
                await bot.send_message(ADMIN_ID, admin_msg)
            except Exception as e:
                log.error(f"Failed to notify admin about ticket closure: {e}")
            
            text = f"✅ **Support Ticket Closed**\n\n**Ticket #{ticket_id[-6:]}** has been closed successfully.\n\n💬 **Need more help?** Create a new support ticket anytime!"
            await edit_or_send(cq, text=text, reply_markup=kb_user_menu())
            await cq.answer("✅ Ticket closed successfully!")
        else:
            await cq.answer("❌ Error closing ticket", show_alert=True)
    
    except Exception as e:
        log.error(f"Error closing support ticket: {e}")
        await cq.answer("❌ Error closing ticket", show_alert=True)

# FIXED: User Text Messages (Support System)
@dp.message(F.text & ~F.command)
async def on_user_text(message: types.Message):
    if is_admin(message.from_user.id):
        return
    
    await upsert_user(message.from_user)
    
    # Check if user is in support mode or has active ticket
    user_id = message.from_user.id
    active_ticket = await get_user_active_ticket(user_id)
    
    if user_id in users_in_support or active_ticket:
        # Handle support message
        try:
            if active_ticket:
                # Add message to existing ticket
                ticket_id = str(active_ticket['_id'])
                success = await add_message_to_ticket(ticket_id, "user", message.text)
                
                if success:
                    # Confirm to user
                    await message.answer(f"✅ **Message added to ticket #{ticket_id[-6:]}**\n\n📩 **Your message:** {message.text[:100]}{'...' if len(message.text) > 100 else ''}\n\n🔔 **Admin will be notified**")
                    
                    # Notify admin
                    user_info = await get_user(user_id)
                    is_premium = user_info and user_info.get("status") == "active"
                    priority = "🔥 HIGH PRIORITY" if is_premium else "📋 NORMAL"
                    
                    admin_msg = f"🎫 **New Message on Ticket #{ticket_id[-6:]}**\n{priority}\n\n👤 **User:** {message.from_user.first_name} (@{message.from_user.username or 'none'})\n🆔 **ID:** {user_id}\n💎 **Status:** {'PREMIUM' if is_premium else 'FREE'}\n\n💬 **Message:**\n{message.text}\n\n📞 **Reply:** `/reply {ticket_id} Your response`"
                    
                    await bot.send_message(ADMIN_ID, admin_msg)
                    log.info(f"Added message to existing ticket {ticket_id} from user {user_id}")
                else:
                    await message.answer("❌ **Error adding message to ticket**\n\nPlease try again or contact admin directly.")
            else:
                # Create new ticket
                ticket_id = await create_support_ticket(
                    user_id,
                    message.from_user.username or "none",
                    message.from_user.first_name,
                    message.text
                )
                
                if ticket_id:
                    # Remove from support mode since ticket is created
                    if user_id in users_in_support:
                        users_in_support.remove(user_id)
                    
                    # Get user info for priority
                    user_info = await get_user(user_id)
                    is_premium = user_info and user_info.get("status") == "active"
                    priority = "🔥 HIGH PRIORITY" if is_premium else "📋 NORMAL"
                    
                    # Confirm to user
                    response_time = "2-5 minutes" if is_premium else "10-30 minutes"
                    await message.answer(f"✅ **Support Ticket Created!**\n\n🎫 **Ticket ID:** #{ticket_id[-6:]}\n{priority}\n⏱️ **Response Time:** {response_time}\n\n📩 **Your message:** {message.text[:100]}{'...' if len(message.text) > 100 else ''}\n\n🔔 **You'll be notified when admin replies!**")
                    
                    # Notify admin
                    admin_msg = f"🎫 **NEW SUPPORT TICKET #{ticket_id[-6:]}**\n{priority}\n\n👤 **User:** {message.from_user.first_name} (@{message.from_user.username or 'none'})\n🆔 **ID:** {user_id}\n💎 **Status:** {'PREMIUM' if is_premium else 'FREE'}\n\n💬 **Message:**\n{message.text}\n\n📞 **Reply:** `/reply {ticket_id} Your response`"
                    
                    await bot.send_message(ADMIN_ID, admin_msg)
                    log.info(f"Created new support ticket {ticket_id} for user {user_id}")
                else:
                    await message.answer("❌ **Error creating support ticket**\n\nPlease try again or contact admin directly.")
                    
        except Exception as e:
            log.error(f"Error handling support message from user {user_id}: {e}")
            await message.answer("❌ **Error processing your message**\n\nPlease try again or contact admin directly.")
    else:
        # Regular message - suggest support
        await message.answer(f"💬 **Hi {message.from_user.first_name}!**\n\nI see you sent a message. If you need help:\n\n🎫 **Tap Support below to create a ticket**\n🚀 **Or tap Upgrade to go Premium**", reply_markup=kb_user_menu())

@dp.callback_query(F.data.startswith("plan_"))
async def on_plan(cq: types.CallbackQuery):
    plan_key = cq.data.replace("plan_", "")
    last_selected_plan[cq.from_user.id] = plan_key
    plan = PLANS[plan_key]
    
    daily_cost = float(plan["price"].replace("₹", "")) / plan["days"]
    
    caption = f"🎯 **{plan['emoji']} {plan['name']} Plan**\n\n💰 **Price:** {plan['price']}\n⏰ **Duration:** {plan['days']} days\n📊 **Daily Cost:** ₹{daily_cost:.2f}/day\n\n💳 **Choose Payment Method:**"
    
    await edit_or_send(cq, text=caption, reply_markup=kb_payment_options(plan_key))
    await cq.answer()

@dp.callback_query(F.data.startswith("upi_"))
async def copy_upi(cq: types.CallbackQuery):
    plan_key = cq.data.replace("upi_", "")
    plan = PLANS[plan_key]
    amount_only = plan['price'].replace('₹', '')
    
    msg = f"💳 **UPI Payment**\n\n🎯 **Plan:** {plan['emoji']} {plan['name']}\n💰 **Amount:** {plan['price']}\n\n📱 **Quick Steps:**\n1. Copy UPI ID from message below\n2. Open any UPI app (GPay/PhonePe/Paytm)\n3. Paste UPI ID and pay exactly {amount_only}\n4. Upload screenshot after payment"
    
    await edit_or_send(cq, text=msg, reply_markup=kb_payment_options(plan_key))
    
    upi_message = f"""📋 <b>UPI PAYMENT DETAILS</b>

<b>UPI ID:</b> <code>{UPI_ID}</code>
<b>Amount:</b> <code>{amount_only}</code>

<b>📱 HOW TO PAY:</b>
1. Tap and hold the UPI ID above to copy
2. Open GPay, PhonePe, or Paytm
3. Go to "Send Money" or "Pay"
4. Paste the UPI ID
5. Enter amount: {amount_only}
6. Complete payment
7. Take screenshot and send here

<b>⚠️ IMPORTANT:</b> Pay exactly {amount_only} rupees"""
    
    try:
        await bot.send_message(cq.from_user.id, upi_message, parse_mode=ParseMode.HTML)
    except:
        await bot.send_message(cq.from_user.id, f"📋 UPI ID: {UPI_ID}\nAmount: {amount_only}\n\nTap and hold UPI ID to copy")
    
    await cq.answer("💳 UPI details sent! Tap and hold UPI ID to copy", show_alert=True)

@dp.callback_query(F.data.startswith("qr_"))
async def show_qr(cq: types.CallbackQuery):
    plan_key = cq.data.replace("qr_", "")
    plan = PLANS[plan_key]
    
    caption = f"📱 **QR Code Payment**\n\n🎯 **Plan:** {plan['emoji']} {plan['name']}\n💰 **Amount:** {plan['price']}\n\n📸 **Instructions:**\n1. Scan QR code below\n2. Pay exact amount\n3. Upload screenshot\n\n⚡ **Quick & Secure!**"
    
    await edit_or_send(cq, text=caption, photo_url=QR_CODE_URL, reply_markup=kb_payment_options(plan_key))
    await cq.answer()

@dp.callback_query(F.data.startswith("upload_"))
async def on_pay_ask(cq: types.CallbackQuery):
    plan_key = cq.data.replace("upload_", "")
    last_selected_plan[cq.from_user.id] = plan_key
    plan = PLANS[plan_key]
    
    text = f"📸 **Upload Payment Proof**\n\n🎯 **Plan:** {plan['emoji']} {plan['name']} - {plan['price']}\n\n📋 **Requirements:**\n• Clear screenshot\n• Shows payment success\n• Amount visible\n• Transaction ID visible\n\n📷 **Send screenshot as photo now:**"
    
    await edit_or_send(cq, text=text)
    await cq.answer("📸 Send payment screenshot!")

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
        
        confirmation_text = f"🎉 **Payment proof received!**\n\n📸 **Proof ID:** #{pid}\n📱 **Plan:** {plan['emoji']} {plan['name']}\n💰 **Amount:** {plan['price']}\n\n⏰ **Processing time:** 3-5 minutes\n🔔 **You'll be notified once approved!**"
        
        try:
            await bot.send_photo(m.from_user.id, SUCCESS_IMAGE, caption=confirmation_text)
        except Exception:
            await m.answer(confirmation_text)
        
        username = safe_text(m.from_user.username)
        first_name = safe_text(m.from_user.first_name)
        
        admin_notification = f"💰 **New Payment #{pid}**\n\n👤 **User:** {first_name} (@{username})\n🆔 **ID:** {m.from_user.id}\n📱 **Plan:** {plan['emoji']} {plan['name']}\n💵 **Amount:** {plan['price']}\n⏰ **Time:** {datetime.now().strftime('%H:%M:%S')}"
        
        await bot.send_message(ADMIN_ID, admin_notification)
        await bot.send_photo(
            ADMIN_ID,
            m.photo[-1].file_id,
            caption=f"💳 **Payment Proof #{pid}**\n{plan['emoji']} {plan['name']} - {plan['price']}\n**User:** {first_name} ({m.from_user.id})",
            reply_markup=kb_payment_actions(pid, m.from_user.id)
        )
        
    except Exception as e:
        log.error(f"Error processing payment photo: {e}")
        await m.answer("❌ Error processing screenshot. Please try uploading again.")

# Admin Handlers
@dp.callback_query(F.data == "admin_menu")
async def admin_menu(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    total, active, expired, pending, tickets = await get_stats()
    text = f"🛠️ **Admin Control Panel**\n\n📊 **Live Statistics:**\n👥 Total Users: **{total}**\n✅ Active Subs: **{active}**\n❌ Expired: **{expired}**\n⏳ Pending Payments: **{pending}**\n🎫 Open Tickets: **{tickets}**\n\n⚡ **System Status:** Online\n🔄 **Last Updated:** {datetime.now().strftime('%H:%M:%S')}"
    
    await cq.message.answer(text, reply_markup=kb_admin_menu())
    await cq.answer()

@dp.callback_query(F.data == "admin_tickets")
async def admin_view_tickets(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    try:
        tickets = await get_open_tickets(10)
        
        if not tickets:
            await cq.message.answer("✅ **No Open Support Tickets**\n\nAll tickets have been resolved!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back to Admin", callback_data="admin_menu")]
            ]))
            await cq.answer()
            return
        
        text = f"🎫 **Open Support Tickets ({len(tickets)})**\n\nClick on a ticket to view and reply:"
        await cq.message.answer(text, reply_markup=kb_admin_tickets(tickets))
        await cq.answer(f"📋 {len(tickets)} open tickets")
        
    except Exception as e:
        log.error(f"Error getting admin tickets: {e}")
        await cq.answer("❌ Error loading tickets", show_alert=True)

@dp.callback_query(F.data.startswith("admin_ticket_"))
async def admin_view_ticket(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    try:
        ticket_id = cq.data.replace("admin_ticket_", "")
        ticket = await get_ticket(ticket_id)
        
        if not ticket:
            await cq.answer("❌ Ticket not found!", show_alert=True)
            return
        
        # Build ticket details
        user_id = ticket['user_id']
        created = ticket['created_at'].strftime('%d %b, %H:%M')
        subject = ticket.get('subject', 'No subject')
        messages = ticket.get('messages', [])
        
        text = f"🎫 **Ticket #{ticket_id[-6:]}**\n\n👤 **User:** {ticket.get('first_name', 'Unknown')} ({user_id})\n📅 **Created:** {created}\n📝 **Subject:** {subject}\n💬 **Messages:** {len(messages)}\n🔍 **Status:** {ticket['status'].upper()}\n\n**📜 Conversation:**\n"
        
        # Add messages (limit to last 5 to avoid message length issues)
        recent_messages = messages[-5:] if len(messages) > 5 else messages
        
        for i, msg in enumerate(recent_messages, 1):
            sender = "👨‍💼 **Admin**" if msg['from'] == 'admin' else "👤 **User**"
            timestamp = msg['timestamp'].strftime('%H:%M')
            message_text = msg['message'][:100] + "..." if len(msg['message']) > 100 else msg['message']
            text += f"\n{i}. {sender} ({timestamp}):\n   {message_text}\n"
        
        if len(messages) > 5:
            text += f"\n... and {len(messages) - 5} older messages"
        
        await cq.message.answer(text, reply_markup=kb_admin_ticket_actions(ticket_id))
        await cq.answer()
        
    except Exception as e:
        log.error(f"Error viewing ticket: {e}")
        await cq.answer("❌ Error loading ticket!", show_alert=True)

@dp.callback_query(F.data.startswith("admin_reply_"))
async def admin_start_reply(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    ticket_id = cq.data.replace("admin_reply_", "")
    await state.update_data(ticket_id=ticket_id)
    await state.set_state(SupportReply.waiting_reply)
    
    await cq.message.answer(f"✏️ **Reply to Ticket #{ticket_id[-6:]}**\n\nType your response message:")
    await cq.answer("✏️ Type your reply")

@dp.message(SupportReply.waiting_reply)
async def admin_send_reply(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.clear()
        return
    
    try:
        data = await state.get_data()
        ticket_id = data['ticket_id']
        
        # Add admin reply to ticket
        success = await add_message_to_ticket(ticket_id, "admin", m.text)
        
        if not success:
            await m.answer("❌ **Error adding reply to ticket**")
            await state.clear()
            return
        
        # Get ticket to find user
        ticket = await get_ticket(ticket_id)
        if not ticket:
            await m.answer("❌ **Ticket not found**")
            await state.clear()
            return
        
        # Send reply to user
        user_reply = f"💬 **Support Response**\n🎫 **Ticket:** #{ticket_id[-6:]}\n\n{m.text}\n\n─────────────────\n🎧 **Premium Support Team**\n💬 **Need more help?** Just reply to add to this ticket!"
        
        try:
            await bot.send_message(ticket['user_id'], user_reply)
            await m.answer(f"✅ **Reply sent successfully!**\n\n🎫 **Ticket:** #{ticket_id[-6:]}\n👤 **User:** {ticket['user_id']}\n📩 **Your reply:** {m.text[:100]}{'...' if len(m.text) > 100 else ''}")
            log.info(f"Admin replied to ticket {ticket_id}")
        except Exception as e:
            log.error(f"Failed to send reply to user {ticket['user_id']}: {e}")
            await m.answer(f"❌ **Failed to send reply to user**\n\nError: {str(e)}")
        
        await state.clear()
        
    except Exception as e:
        log.error(f"Error sending admin reply: {e}")
        await m.answer("❌ **Error sending reply**")
        await state.clear()

@dp.callback_query(F.data.startswith("admin_close_"))
async def admin_close_ticket(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    try:
        ticket_id = cq.data.replace("admin_close_", "")
        
        # Get ticket before closing
        ticket = await get_ticket(ticket_id)
        if not ticket:
            await cq.answer("❌ Ticket not found!", show_alert=True)
            return
        
        # Close ticket
        success = await close_support_ticket(ticket_id)
        
        if success:
            # Notify user
            user_msg = f"✅ **Ticket Resolved**\n🎫 **Ticket:** #{ticket_id[-6:]}\n\nYour support ticket has been resolved and closed by our admin team.\n\n💬 **Need more help?** Create a new support ticket anytime!"
            
            try:
                await bot.send_message(ticket['user_id'], user_msg)
            except Exception as e:
                log.error(f"Failed to notify user about ticket closure: {e}")
            
            await cq.message.edit_text(f"✅ **Ticket #{ticket_id[-6:]} Closed**\n\nUser has been notified that the ticket is resolved.")
            await cq.answer("✅ Ticket closed successfully!")
            log.info(f"Admin closed ticket {ticket_id}")
        else:
            await cq.answer("❌ Error closing ticket", show_alert=True)
    
    except Exception as e:
        log.error(f"Error closing ticket: {e}")
        await cq.answer("❌ Error closing ticket!", show_alert=True)

# FIXED: Command-based Admin Reply
@dp.message(Command("reply"))
@dp.message(Command("replay"))
async def admin_reply_command(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    
    try:
        parts = m.text.split(maxsplit=2)
        if len(parts) < 3:
            await m.answer("❌ **USAGE:**\n`/reply <ticket_id> <your_message>`\n`/replay <ticket_id> <your_message>`\n\n**Example:**\n`/reply 507f1f77bcf86cd799439011 Hello, thanks for contacting support!`")
            return
        
        command, ticket_id, reply_text = parts[0], parts[1], parts[2]
        
        # Add admin reply to ticket
        success = await add_message_to_ticket(ticket_id, "admin", reply_text)
        
        if not success:
            await m.answer("❌ **TICKET NOT FOUND**\n\nPlease check the ticket ID.")
            return
        
        # Get ticket to find user
        ticket = await get_ticket(ticket_id)
        if not ticket:
            await m.answer("❌ **TICKET NOT FOUND**\n\nPlease check the ticket ID.")
            return
        
        # Send reply to user
        user_reply = f"💬 **Support Response**\n🎫 **Ticket:** #{ticket_id[-6:]}\n\n{reply_text}\n\n─────────────────\n🎧 **Premium Support Team**\n💬 **Need more help?** Just reply to add to this ticket!"
        
        try:
            await bot.send_message(ticket['user_id'], user_reply)
            await m.answer(f"✅ **REPLY SENT SUCCESSFULLY**\n\n🎫 **Ticket:** #{ticket_id[-6:]}\n👤 **User:** {ticket['user_id']}\n📩 **Your message:** {reply_text[:100]}{'...' if len(reply_text) > 100 else ''}")
            log.info(f"Admin replied to ticket {ticket_id} via command")
        except Exception as e:
            await m.answer(f"❌ **FAILED TO SEND REPLY**\n\nError: {str(e)}")
            log.error(f"Failed to send admin reply via command: {e}")
        
    except Exception as e:
        await m.answer(f"❌ **COMMAND ERROR:** {e}")
        log.error(f"Admin reply command error: {e}")

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
        
        payment = await get_payment(payment_id)
        if not payment:
            await cq.answer("❌ Payment not found!", show_alert=True)
            return
        
        plan_key = payment["plan_key"]
        plan = PLANS[plan_key]
        
        log.info(f"Processing approval: payment_id={payment_id}, user_id={user_id}, plan_key={plan_key}")
        
        await set_payment_status(payment_id, "approved")
        await set_subscription(user_id, plan_key, plan["days"])
        
        try:
            link = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
            user_msg = f"🎉 **PAYMENT APPROVED!**\n\n✅ Your **{plan['emoji']} {plan['name']}** subscription is now **ACTIVE**!\n💰 **Amount:** {plan['price']}\n⏰ **Valid for:** {plan['days']} days\n\n🔗 **Join Premium Channel:**\n{link.invite_link}\n\n🌟 **Welcome to Premium Family!**\nEnjoy unlimited access to all premium features! 🚀"
        except Exception as e:
            log.error(f"Error creating invite link: {e}")
            user_msg = f"🎉 **PAYMENT APPROVED!**\n\n✅ Your **{plan['emoji']} {plan['name']}** subscription is now **ACTIVE**!\n💰 **Amount:** {plan['price']}\n⏰ **Valid for:** {plan['days']} days\n\n🌟 **Welcome to Premium!**\nContact admin for channel access."
        
        await bot.send_message(user_id, user_msg)
        
        try:
            await cq.message.edit_text(f"✅ **Payment #{payment_id} APPROVED**\n\n{plan['emoji']} **{plan['name']}** activated for user **{user_id}**!")
        except Exception:
            await cq.message.answer(f"✅ **Payment #{payment_id} APPROVED**\n\n{plan['emoji']} **{plan['name']}** activated for user **{user_id}**!")
        
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
        
        await bot.send_message(user_id, user_msg)
        
        try:
            await cq.message.edit_text(f"❌ **Payment #{payment_id} DENIED**\n\nUser **{user_id}** has been notified with improvement suggestions.")
        except Exception:
            await cq.message.answer(f"❌ **Payment #{payment_id} DENIED**\n\nUser **{user_id}** has been notified with improvement suggestions.")
        
        await cq.answer("❌ Denied with feedback sent!")
        
    except Exception as e:
        log.error(f"Error denying payment: {e}")
        await cq.answer("❌ Error processing denial!", show_alert=True)

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
            
            await cq.message.answer(payment_details, reply_markup=kb_payment_actions(str(payment['_id']), payment['user_id']))
        
        await cq.answer(f"📋 {len(payments)} payments ready for review!")
        
    except Exception as e:
        log.error(f"Error getting pending payments: {e}")
        await cq.answer("❌ Error loading payments!", show_alert=True)

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    total, active, expired, pending, tickets = await get_stats()
    active_rate = (active/total*100) if total > 0 else 0
    conversion_rate = ((active + expired)/total*100) if total > 0 else 0
    
    text = f"📊 **Comprehensive Analytics**\n\n👥 **User Statistics:**\n📈 Total Users: **{total}**\n✅ Active Subscriptions: **{active}**\n❌ Expired Subscriptions: **{expired}**\n⏳ Pending Payments: **{pending}**\n🎫 Open Support Tickets: **{tickets}**\n\n📈 **Performance Metrics:**\n🎯 Active Rate: **{active_rate:.1f}%**\n💰 Conversion Rate: **{conversion_rate:.1f}%**\n📊 Retention: **{(active/(active+expired)*100) if (active+expired) > 0 else 0:.1f}%**\n\n⏰ **Report Generated:** {datetime.now().strftime('%d %b %Y, %H:%M:%S')}"
    
    await cq.message.answer(text)
    await cq.answer()

@dp.callback_query(F.data == "admin_users")
async def admin_users(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    try:
        cursor = users_col.find({}).sort("created_at", -1).limit(20)
        users = await cursor.to_list(length=20)
        
        if not users:
            await cq.message.answer("👥 **No users found**\n\nThe bot hasn't been used yet.")
            await cq.answer()
            return
        
        lines = [f"👥 **User Management** (Top 20)\n"]
        active_count = expired_count = 0
        
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
            lines.append(f"   📊 Status: {user.get('status', 'none').upper()}\n")
        
        lines.insert(1, f"📊 Active: {active_count} | Expired: {expired_count}\n")
        user_list = "\n".join(lines)
        
        if len(user_list) > 4000:
            await cq.message.answer(user_list[:4000] + "\n\n... **[List truncated]**")
        else:
            await cq.message.answer(user_list)
        
        await cq.answer(f"📋 Showing {len(users)} users")
        
    except Exception as e:
        log.error(f"Error getting users: {e}")
        await cq.answer("❌ Error loading users!", show_alert=True)

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("❌ Access denied!", show_alert=True)
        return
    
    total_users, _, _, _, _ = await get_stats()
    text = f"📢 **Broadcast Message Center**\n\n👥 **Target Audience:** {total_users} users\n📡 **Delivery Method:** Direct message\n⚡ **Estimated Time:** {total_users * 0.05:.1f} seconds\n\n✍️ **Send your broadcast message now:**"
    
    await cq.message.answer(text)
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
            await bot.send_message(user["user_id"], broadcast_message)
            sent += 1
            await asyncio.sleep(0.03)
        except Exception:
            failed += 1
    
    final_report = f"📢 **BROADCAST COMPLETED!**\n\n✅ **Successfully Sent:** {sent}\n❌ **Failed:** {failed}\n📈 **Success Rate:** {(sent/(sent+failed)*100) if (sent+failed) > 0 else 0:.1f}%"
    
    await m.answer(final_report)
    await state.clear()

# FIXED: Expiry worker
async def expiry_worker():
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
                    end_date = ensure_timezone_aware(end_at)
                    if not end_date:
                        continue
                except Exception as e:
                    log.error(f"Error processing end_date for user {user_id}: {e}")
                    continue
                
                # 3-day expiry reminder
                if (status == "active" and not reminded and 
                    end_date > now and (end_date - now) <= timedelta(days=3)):
                    
                    try:
                        days_left = (end_date - now).days
                        
                        reminder_message = f"⏰ **SUBSCRIPTION EXPIRY REMINDER**\n\nYour premium subscription expires in **{days_left}** day(s)!\n\n📅 **Expiry Date:** {end_date.strftime('%d %b %Y, %H:%M')}\n\n🔄 **Renew now to continue enjoying premium features!**\n🚀 **Use /start to renew now!**"
                        
                        await bot.send_message(user_id, reminder_message)
                        await users_col.update_one({"user_id": user_id}, {"$set": {"reminded_3d": True}})
                        log.info(f"Sent 3-day reminder to user {user_id}")
                        
                    except Exception as e:
                        log.error(f"Failed to send reminder to user {user_id}: {e}")
                
                # Handle expired subscriptions
                if end_date <= now and status != "expired":
                    try:
                        await users_col.update_one({"user_id": user_id}, {"$set": {"status": "expired"}})
                        
                        try:
                            await bot.ban_chat_member(CHANNEL_ID, user_id)
                            await bot.unban_chat_member(CHANNEL_ID, user_id)
                        except Exception as e:
                            log.error(f"Failed to remove user {user_id} from channel: {e}")
                        
                        expiry_message = f"❌ **SUBSCRIPTION EXPIRED**\n\nYour premium subscription has expired.\n\n🔄 **To renew:**\n   1️⃣ Use /start to see plans\n   2️⃣ Choose your plan\n   3️⃣ Complete payment\n   4️⃣ Get instant access back!\n\n💎 **We miss you! Come back to premium!**"
                        
                        await bot.send_message(user_id, expiry_message)
                        log.info(f"Processed expiry for user {user_id}")
                        
                    except Exception as e:
                        log.error(f"Failed to process expiry for user {user_id}: {e}")
        
        except Exception as e:
            log.exception(f"Error in expiry_worker: {e}")
        
        await asyncio.sleep(1800)  # 30 minutes

# Main function
async def main():
    try:
        await mongo_client.admin.command('ping')
        log.info("✅ MongoDB connected successfully")
        
        asyncio.create_task(expiry_worker())
        log.info("✅ Expiry worker started")
        
        log.info("🚀 Starting Complete Premium Bot with Fixed Support System")
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
