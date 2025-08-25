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

# OPTIMIZED: MongoDB setup with connection pooling
mongo_client = AsyncIOMotorClient(MONGO_URI, maxPoolSize=10, minPoolSize=2)
db = mongo_client['premium_bot']
users_col = db['users']
payments_col = db['payments']
tickets_col = db['tickets']

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Plans
PLANS = {
    "plan1": {"name": "1 Month", "price": "₹99", "days": 30, "emoji": "🟢"},
    "plan2": {"name": "6 Months", "price": "₹399", "days": 180, "emoji": "🟡"},
    "plan3": {"name": "1 Year", "price": "₹1999", "days": 365, "emoji": "🔥"},
    "plan4": {"name": "Lifetime", "price": "₹2999", "days": 36500, "emoji": "💎"},
}
last_selected_plan = {}

# FSM States
class BCast(StatesGroup):
    waiting_text = State()

def is_admin(uid): return uid == ADMIN_ID

# OPTIMIZED: Fast database operations
async def upsert_user(user: types.User):
    await users_col.update_one(
        {"user_id": user.id},
        {"$set": {"username": user.username, "first_name": user.first_name, "last_name": user.last_name, "updated_at": datetime.now(timezone.utc)}, 
         "$setOnInsert": {"plan_key": None, "start_at": None, "end_at": None, "status": "none", "created_at": datetime.now(timezone.utc), "reminded_3d": False}}, 
        upsert=True)

async def get_user(user_id):
    return await users_col.find_one({"user_id": user_id}, {"_id": 0})

async def set_subscription(user_id, plan_key, days):
    now = datetime.now(timezone.utc)
    end_date = now + timedelta(days=days)
    await users_col.update_one({"user_id": user_id}, {"$set": {"plan_key": plan_key, "start_at": now, "end_at": end_date, "status": "active", "reminded_3d": False}})
    return now, end_date

async def add_payment(user_id, plan_key, file_id):
    result = await payments_col.insert_one({"user_id": user_id, "plan_key": plan_key, "file_id": file_id, "created_at": datetime.now(timezone.utc), "status": "pending"})
    return str(result.inserted_id)

async def set_payment_status(payment_id, status):
    await payments_col.update_one({"_id": ObjectId(payment_id)}, {"$set": {"status": status}})

async def get_payment(payment_id):
    return await payments_col.find_one({"_id": ObjectId(payment_id)}, {"_id": 0})

async def add_ticket(user_id, message):
    result = await tickets_col.insert_one({"user_id": user_id, "message": message, "status": "open", "created_at": datetime.now(timezone.utc)})
    return str(result.inserted_id)

async def get_stats():
    pipeline = [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
    stats = {doc["_id"]: doc["count"] for doc in await users_col.aggregate(pipeline).to_list(None)}
    pending = await payments_col.count_documents({"status": "pending"})
    return stats.get("none", 0) + stats.get("active", 0) + stats.get("expired", 0), stats.get("active", 0), stats.get("expired", 0), pending

# OPTIMIZED: Fast UI functions
def kb_user_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Upgrade Premium", callback_data="buy")],
        [InlineKeyboardButton(text="📊 My Subscription", callback_data="my"), InlineKeyboardButton(text="💬 Support", callback_data="support")],
        [InlineKeyboardButton(text="🎁 Special Offers", callback_data="offers")]
    ])

def kb_plans():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{p['emoji']} {p['name']} - {p['price']}", callback_data=f"plan_{k}")] for k, p in PLANS.items()
    ] + [[InlineKeyboardButton(text="⬅️ Back", callback_data="menu")]])

def kb_payment_options(plan_key):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 UPI", callback_data=f"upi_{plan_key}"), InlineKeyboardButton(text="📱 QR", callback_data=f"qr_{plan_key}")],
        [InlineKeyboardButton(text="📸 Upload Proof", callback_data=f"upload_{plan_key}")],
        [InlineKeyboardButton(text="⬅️ Back", callback_data="buy")]
    ])

def kb_payment_actions(payment_id, user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{payment_id}_{user_id}"), InlineKeyboardButton(text="❌ Deny", callback_data=f"deny_{payment_id}_{user_id}")]
    ])

def kb_admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏳ Pending", callback_data="pending"), InlineKeyboardButton(text="📊 Stats", callback_data="stats")],
        [InlineKeyboardButton(text="👥 Users", callback_data="users"), InlineKeyboardButton(text="📢 Broadcast", callback_data="broadcast")]
    ])

# OPTIMIZED: Fast message functions
async def send_photo_fast(chat_id, photo_url, text, markup=None):
    try:
        await bot.send_photo(chat_id, photo_url, caption=text, reply_markup=markup)
    except:
        await bot.send_message(chat_id, text, reply_markup=markup)

async def edit_or_send(cq, text, photo=None, markup=None):
    try:
        if photo:
            await cq.message.delete()
            await send_photo_fast(cq.from_user.id, photo, text, markup)
        else:
            await cq.message.edit_text(text, reply_markup=markup)
    except:
        await (send_photo_fast(cq.from_user.id, photo, text, markup) if photo else cq.message.answer(text, reply_markup=markup))

# SUPER FAST: Bot handlers
@dp.message(CommandStart())
async def start(m):
    asyncio.create_task(upsert_user(m.from_user))  # Non-blocking
    text = f"👋 Hello {m.from_user.first_name}!\n\n🌟 Premium Benefits:\n• Unlimited downloads\n• Ad-free experience\n• Priority support\n\n🚀 Upgrade now!"
    await send_photo_fast(m.from_user.id, WELCOME_IMAGE, text, kb_user_menu())

@dp.callback_query(F.data == "menu")
async def menu(cq):
    text = f"🏠 Welcome {cq.from_user.first_name}!\nChoose option:"
    await edit_or_send(cq, text, WELCOME_IMAGE, kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data == "buy")
async def buy(cq):
    await edit_or_send(cq, "💎 Premium Plans\nChoose subscription:", PLANS_IMAGE, kb_plans())
    await cq.answer()

@dp.callback_query(F.data == "offers")
async def offers(cq):
    await edit_or_send(cq, "🎁 Special Offers\n\n🟡 6 Months: Save 33%\n🔥 1 Year: Best Value\n💎 Lifetime: One payment", OFFERS_IMAGE, kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data == "my")
async def my_sub(cq):
    user = await get_user(cq.from_user.id)
    if not user or user.get("status") != "active":
        text = "😔 No Active Subscription\n\nUpgrade to Premium for unlimited access!"
        await edit_or_send(cq, text, UPGRADE_IMAGE, kb_user_menu())
    else:
        plan = PLANS.get(user["plan_key"], {"name": "Unknown", "emoji": "📦"})
        text = f"📊 My Subscription\n\n✅ Status: ACTIVE\n{plan['emoji']} Plan: {plan['name']}\n\n🎉 Premium Benefits Active!"
        await edit_or_send(cq, text, None, kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data == "support")
async def support(cq):
    await edit_or_send(cq, f"💬 Customer Support\n\nHi {cq.from_user.first_name}!\nType your message for quick support response.", None, kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data.startswith("plan_"))
async def plan_select(cq):
    plan_key = cq.data[5:]  # Faster than replace
    last_selected_plan[cq.from_user.id] = plan_key
    plan = PLANS[plan_key]
    daily = float(plan["price"][1:]) / plan["days"]  # Remove ₹ symbol
    text = f"🎯 {plan['emoji']} {plan['name']}\n💰 {plan['price']} ({daily:.1f}/day)\n⏰ {plan['days']} days\n\nChoose payment:"
    await edit_or_send(cq, text, None, kb_payment_options(plan_key))
    await cq.answer()

# UPI with proper tap-to-copy functionality
@dp.callback_query(F.data.startswith("upi_"))
async def upi_pay(cq):
    plan_key = cq.data[4:]  # Faster than replace
    plan = PLANS[plan_key]
    amount = plan["price"][1:]  # Remove ₹
    text = f"💳 UPI Payment\n\nPlan: {plan['emoji']} {plan['name']}\nAmount: {plan['price']}\n\n1. Copy UPI ID below\n2. Pay in UPI app\n3. Upload screenshot"
    await edit_or_send(cq, text, None, kb_payment_options(plan_key))
    
    # Send UPI ID with HTML formatting for better copy functionality
    upi_message = f"""📋 <b>UPI PAYMENT DETAILS</b>

<b>UPI ID:</b> <code>{UPI_ID}</code>
<b>Amount:</b> <code>{amount}</code>

<b>📱 HOW TO PAY:</b>
1. Tap and hold the UPI ID above to copy
2. Open GPay, PhonePe, or Paytm
3. Go to "Send Money" or "Pay"
4. Paste the UPI ID
5. Enter amount: {amount}
6. Complete payment
7. Take screenshot and send here

<b>⚠️ IMPORTANT:</b> Pay exactly {amount} rupees"""
    
    try:
        await bot.send_message(cq.from_user.id, upi_message, parse_mode=ParseMode.HTML)
    except:
        # Fallback without HTML
        await bot.send_message(cq.from_user.id, f"📋 UPI ID: {UPI_ID}\nAmount: {amount}\n\nTap and hold UPI ID to copy")
    
    await cq.answer("💳 UPI details sent! Tap and hold UPI ID to copy", show_alert=True)

@dp.callback_query(F.data.startswith("qr_"))
async def qr_pay(cq):
    plan_key = cq.data[3:]
    plan = PLANS[plan_key]
    text = f"📱 QR Payment\n\nPlan: {plan['emoji']} {plan['name']}\nAmount: {plan['price']}\n\nScan QR, pay, upload screenshot."
    await edit_or_send(cq, text, QR_CODE_URL, kb_payment_options(plan_key))
    await cq.answer()

@dp.callback_query(F.data.startswith("upload_"))
async def upload(cq):
    plan_key = cq.data[7:]
    last_selected_plan[cq.from_user.id] = plan_key
    plan = PLANS[plan_key]
    text = f"📸 Upload Payment Screenshot\n\nPlan: {plan['emoji']} {plan['name']} - {plan['price']}\n\n• Clear screenshot\n• Shows success\n• Amount visible\n\nSend photo now:"
    await edit_or_send(cq, text)
    await cq.answer("📸 Send screenshot!")

# Support system
@dp.message(F.text & ~F.command)
async def support_msg(m):
    if is_admin(m.from_user.id): 
        return
    
    # Non-blocking user upsert
    asyncio.create_task(upsert_user(m.from_user))
    
    # Fast user check
    user = await get_user(m.from_user.id)
    priority = "HIGH" if user and user.get("status") == "active" else "NORMAL"
    tid = await add_ticket(m.from_user.id, m.text)
    
    # Fast admin notification
    admin_msg = f"🎫 SUPPORT #{tid} ({priority})\n👤 {m.from_user.first_name} (@{m.from_user.username or 'none'})\n🆔 {m.from_user.id}\n💎 {'PREMIUM' if priority == 'HIGH' else 'FREE'}\n\n💬 {m.text}\n\n📞 /reply {m.from_user.id} Your response"
    
    try:
        await bot.send_message(ADMIN_ID, admin_msg)
        log.info(f"Support ticket {tid} sent to admin from user {m.from_user.id}")
    except Exception as e:
        log.error(f"Failed to send support message to admin: {e}")
    
    response_time = "2-5 min" if priority == "HIGH" else "10-30 min"
    await m.answer(f"✅ Support ticket #{tid} created!\n🔥 Priority: {priority}\n⏰ Response: {response_time}")

@dp.message(F.photo)
async def payment_photo(m):
    if is_admin(m.from_user.id): return
    
    plan_key = last_selected_plan.get(m.from_user.id)
    if not plan_key:
        await m.answer("❌ Select plan first: /start")
        return
    
    try:
        pid = await add_payment(m.from_user.id, plan_key, m.photo[-1].file_id)
        plan = PLANS[plan_key]
        
        text = f"🎉 Payment received!\n\nProof #{pid}\nPlan: {plan['emoji']} {plan['name']}\nAmount: {plan['price']}\n\nProcessing... You'll be notified!"
        
        try:
            await bot.send_photo(m.from_user.id, SUCCESS_IMAGE, caption=text)
        except:
            await m.answer(text)
        
        # Fast admin notification
        admin_text = f"💰 Payment #{pid}\n👤 {m.from_user.first_name} (@{m.from_user.username})\n🆔 {m.from_user.id}\nPlan: {plan['emoji']} {plan['name']} - {plan['price']}"
        asyncio.create_task(bot.send_message(ADMIN_ID, admin_text))
        asyncio.create_task(bot.send_photo(ADMIN_ID, m.photo[-1].file_id, caption=f"Payment #{pid} - {plan['name']} - {plan['price']}\nUser: {m.from_user.id}", reply_markup=kb_payment_actions(pid, m.from_user.id)))
        
    except Exception as e:
        log.error(f"Payment error: {e}")
        await m.answer("❌ Processing error. Try again.")

# Admin handlers
@dp.callback_query(F.data.startswith("approve_"))
async def approve(cq):
    if not is_admin(cq.from_user.id): await cq.answer("❌ Not admin", show_alert=True); return
    
    try:
        parts = cq.data.split("_")
        payment_id, user_id = parts[1], int(parts[2])
        
        payment = await get_payment(payment_id)
        if not payment: await cq.answer("❌ Payment not found", show_alert=True); return
        
        plan_key = payment["plan_key"]
        plan = PLANS[plan_key]
        
        await set_payment_status(payment_id, "approved")
        await set_subscription(user_id, plan_key, plan["days"])
        
        try:
            link = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
            user_msg = f"🎉 Payment Approved!\n\nYour {plan['emoji']} {plan['name']} is active!\nAmount: {plan['price']}\nDuration: {plan['days']} days\n\nJoin Channel:\n{link.invite_link}\n\nWelcome to Premium!"
        except:
            user_msg = f"🎉 Payment Approved!\n\nYour {plan['emoji']} {plan['name']} is active!\nAmount: {plan['price']}\nDuration: {plan['days']} days\n\nWelcome to Premium!"
        
        asyncio.create_task(bot.send_message(user_id, user_msg))
        
        try:
            await cq.message.edit_text(f"✅ APPROVED\nPayment #{payment_id}\n{plan['emoji']} {plan['name']} for user {user_id}")
        except:
            await cq.message.answer(f"✅ APPROVED - Payment #{payment_id}")
        
        await cq.answer("✅ Approved!")
        
    except Exception as e:
        log.error(f"Approval error: {e}")
        await cq.answer("❌ Error approving", show_alert=True)

@dp.callback_query(F.data.startswith("deny_"))
async def deny(cq):
    if not is_admin(cq.from_user.id): await cq.answer("❌ Not admin", show_alert=True); return
    
    try:
        parts = cq.data.split("_")
        payment_id, user_id = parts[1], int(parts[2])
        
        await set_payment_status(payment_id, "denied")
        
        user_msg = "❌ Payment not approved\n\nPlease upload clearer screenshot showing:\n• Payment success\n• Correct amount\n• Transaction details\n\nTry again: /start"
        asyncio.create_task(bot.send_message(user_id, user_msg))
        
        try:
            await cq.message.edit_text(f"❌ DENIED\nPayment #{payment_id} - User {user_id} notified")
        except:
            await cq.message.answer(f"❌ DENIED - Payment #{payment_id}")
        
        await cq.answer("❌ Denied!")
        
    except Exception as e:
        log.error(f"Denial error: {e}")
        await cq.answer("❌ Error denying", show_alert=True)

@dp.callback_query(F.data == "stats")
async def admin_stats(cq):
    if not is_admin(cq.from_user.id): await cq.answer("❌ Not admin", show_alert=True); return
    
    total, active, expired, pending = await get_stats()
    text = f"📊 Bot Statistics\n\n👥 Total: {total}\n✅ Active: {active}\n❌ Expired: {expired}\n⏳ Pending: {pending}\n\nUpdated: {datetime.now().strftime('%H:%M')}"
    await cq.message.answer(text)
    await cq.answer()

@dp.callback_query(F.data == "pending")
async def admin_pending(cq):
    if not is_admin(cq.from_user.id): await cq.answer("❌ Not admin", show_alert=True); return
    
    cursor = payments_col.find({"status": "pending"}).limit(10)
    payments = await cursor.to_list(10)
    
    if not payments:
        await cq.message.answer("✅ No pending payments!")
        await cq.answer()
        return
    
    for payment in payments:
        plan = PLANS[payment['plan_key']]
        text = f"💵 Payment #{str(payment['_id'])}\nUser: {payment['user_id']}\nPlan: {plan['emoji']} {plan['name']}\nAmount: {plan['price']}\nTime: {payment['created_at'].strftime('%d %b, %H:%M')}"
        await cq.message.answer(text, reply_markup=kb_payment_actions(str(payment['_id']), payment['user_id']))
    
    await cq.answer(f"📋 {len(payments)} pending payments")

@dp.callback_query(F.data == "users")
async def admin_users(cq):
    if not is_admin(cq.from_user.id): await cq.answer("❌ Not admin", show_alert=True); return
    
    cursor = users_col.find({}, {"user_id": 1, "plan_key": 1, "status": 1}).limit(20)
    users = await cursor.to_list(20)
    
    lines = ["👥 Recent Users (Top 20)\n"]
    for i, user in enumerate(users, 1):
        plan_info = PLANS.get(user.get("plan_key"), {"name": "None", "emoji": "⚪"})
        status = user.get("status", "none")
        lines.append(f"{i}. {user['user_id']} - {plan_info['emoji']} {plan_info['name']} - {status}")
    
    await cq.message.answer("\n".join(lines))
    await cq.answer(f"📋 {len(users)} users shown")

@dp.callback_query(F.data == "broadcast")
async def broadcast_start(cq, state: FSMContext):
    if not is_admin(cq.from_user.id): await cq.answer("❌ Not admin", show_alert=True); return
    
    total, _, _, _ = await get_stats()
    await cq.message.answer(f"📢 Broadcast to {total} users\nSend your message:")
    await state.set_state(BCast.waiting_text)
    await cq.answer()

@dp.message(BCast.waiting_text)
async def broadcast_send(m, state: FSMContext):
    if not is_admin(m.from_user.id): await state.clear(); return
    
    cursor = users_col.find({}, {"user_id": 1})
    users = await cursor.to_list(None)
    
    await m.answer(f"📤 Broadcasting to {len(users)} users...")
    
    sent = failed = 0
    for user in users:
        try:
            await bot.send_message(user["user_id"], f"📢 Announcement\n\n{m.text}\n\n──────────\n💎 Premium Bot")
            sent += 1
            await asyncio.sleep(0.02)  # Super fast rate limit
        except:
            failed += 1
    
    await m.answer(f"📢 Broadcast Complete!\n✅ Sent: {sent}\n❌ Failed: {failed}")
    await state.clear()

# FIXED: Admin reply system - handles both /reply and /replay commands
@dp.message(Command("reply"))
@dp.message(Command("replay"))  # Added support for /replay command
async def admin_reply(m):
    if not is_admin(m.from_user.id): 
        return
    
    try:
        # Get the command text and split it
        command_text = m.text
        parts = command_text.split(maxsplit=2)
        
        if len(parts) < 3:
            await m.answer("❌ USAGE:\n/reply <user_id> <your_message>\n/replay <user_id> <your_message>\n\nExample:\n/reply 123456789 Hello, thanks for contacting support!")
            return
        
        command, user_id_str, reply_text = parts[0], parts[1], parts[2]
        
        try:
            user_id = int(user_id_str)
        except ValueError:
            await m.answer("❌ INVALID USER ID\n\nUser ID must be a number\nUsage: /reply <user_id> <message>")
            return
        
        # Format reply message for user
        user_reply_message = f"""💬 SUPPORT RESPONSE

{reply_text}

──────────────────
🎧 Premium Support Team
💬 Need more help? Just send another message!"""
        
        # Try to send message to user
        try:
            await bot.send_message(user_id, user_reply_message)
            await m.answer(f"✅ REPLY SENT SUCCESSFULLY TO USER {user_id}\n\n📩 Your message: {reply_text[:100]}{'...' if len(reply_text) > 100 else ''}")
            log.info(f"Admin reply sent to user {user_id} via {command}: {reply_text[:50]}...")
            
        except Exception as send_error:
            error_msg = str(send_error)
            if "chat not found" in error_msg.lower():
                await m.answer(f"❌ USER {user_id} NOT FOUND\n\nUser may have:\n• Blocked the bot\n• Deleted their account\n• Never started the bot")
            elif "blocked" in error_msg.lower():
                await m.answer(f"❌ USER {user_id} HAS BLOCKED THE BOT\n\nCannot send reply to blocked users.")
            else:
                await m.answer(f"❌ FAILED TO SEND TO USER {user_id}\n\nError: {error_msg}")
            
            log.error(f"Failed to send admin reply to user {user_id}: {send_error}")
        
    except Exception as e:
        await m.answer(f"❌ COMMAND ERROR: {e}")
        log.error(f"Admin reply command error: {e}")

# OPTIMIZED: Lightweight expiry worker
async def expiry_worker():
    while True:
        try:
            now = datetime.now(timezone.utc)
            
            # Process expired subscriptions
            expired_users = await users_col.find({"status": "active", "end_at": {"$lte": now}}, {"user_id": 1}).to_list(None)
            
            if expired_users:
                user_ids = [user["user_id"] for user in expired_users]
                await users_col.update_many({"user_id": {"$in": user_ids}}, {"$set": {"status": "expired"}})
                
                for user_id in user_ids:
                    try:
                        await bot.ban_chat_member(CHANNEL_ID, user_id)
                        await bot.unban_chat_member(CHANNEL_ID, user_id)
                    except: pass
                    asyncio.create_task(bot.send_message(user_id, "❌ Subscription Expired\n\nRenew: /start"))
            
            # Process 3-day reminders
            reminder_date = now + timedelta(days=3)
            reminder_users = await users_col.find({
                "status": "active", 
                "end_at": {"$lte": reminder_date, "$gt": now},
                "reminded_3d": {"$ne": True}
            }, {"user_id": 1, "end_at": 1}).to_list(None)
            
            for user in reminder_users:
                days_left = (user["end_at"] - now).days
                asyncio.create_task(bot.send_message(user["user_id"], f"⏰ Subscription expires in {days_left} days!\nRenew: /start"))
                asyncio.create_task(users_col.update_one({"user_id": user["user_id"]}, {"$set": {"reminded_3d": True}}))
                    
        except Exception as e:
            log.error(f"Expiry worker error: {e}")
        
        await asyncio.sleep(1800)  # 30 minutes

async def main():
    try:
        await mongo_client.admin.command('ping')
        log.info("✅ MongoDB connected with connection pooling")
        
        asyncio.create_task(expiry_worker())
        log.info("✅ Optimized expiry worker started")
        
        log.info("🚀 Starting SUPER FAST Premium Bot")
        await dp.start_polling(bot, skip_updates=True)
        
    except Exception as e:
        log.error(f"❌ Start error: {e}")
        raise

if __name__ == "__main__":
    if API_TOKEN == "TEST_TOKEN":
        raise RuntimeError("❌ Set API_TOKEN")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("✅ Bot stopped")
