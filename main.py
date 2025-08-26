import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict
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
log = logging.getLogger("premium_bot")

# Set these in your environment or hardcode for testing:
API_TOKEN = os.getenv("API_TOKEN") or "paste_your_token"
ADMIN_ID = int(os.getenv("ADMIN_ID") or "123456789")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or "-10012345678")
UPI_ID = os.getenv("UPI_ID") or "yourupi@upi"
QR_CODE_URL = os.getenv("QR_CODE_URL") or "https://example.com/qr.png"
WELCOME_IMAGE = os.getenv("WELCOME_IMAGE") or "https://i.imgur.com/premium-welcome.jpg"
PLANS_IMAGE = os.getenv("PLANS_IMAGE") or "https://i.imgur.com/premium-plans.jpg"
OFFERS_IMAGE = os.getenv("OFFERS_IMAGE") or "https://i.imgur.com/special-offers.jpg"
SUCCESS_IMAGE = os.getenv("SUCCESS_IMAGE") or "https://i.imgur.com/success.jpg"
MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"

mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo['premiumbot']
users_col = db['users']
payments_col = db['payments']
tickets_col = db['tickets']

bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

PLANS = {
    "plan1": {"name": "1 Month",    "price": 99,    "days": 30, "emoji": "🟢"},
    "plan2": {"name": "6 Months",   "price": 399,   "days": 180,"emoji": "🟡"},
    "plan3": {"name": "1 Year",     "price": 1999,  "days": 365,"emoji": "🔥"},
    "plan4": {"name": "Lifetime",   "price": 2999,  "days": 36500, "emoji": "💎"},
}
last_plan: Dict[int, str] = {}
OPEN_TICKETS: Dict[int, str] = {}

# FSM for admin reply and broadcast
class AdminReply(StatesGroup):
    waiting_reply = State()
class Broadcast(StatesGroup):
    waiting_broadcast = State()

# Keyboards
def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Buy Premium", callback_data="buy")],
        [InlineKeyboardButton(text="📊 My Plan", callback_data="myplan"), 
         InlineKeyboardButton(text="💬 Support", callback_data="support")],
        [InlineKeyboardButton(text="🎁 Offers", callback_data="offers")],
        [InlineKeyboardButton(text="🛠 Admin", callback_data="admin")] if is_admin(ADMIN_ID) else []
    ])

def plans_keyboard():
    kb = []
    for k, p in PLANS.items():
        kb.append([InlineKeyboardButton(f"{p['emoji']} {p['name']} - Rs.{p['price']}", callback_data=f"plan_{k}")])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def payment_keyboard(plan_key):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("💳 UPI", callback_data=f"upi_{plan_key}"), InlineKeyboardButton("📱 QR", callback_data=f"qr_{plan_key}")],
        [InlineKeyboardButton("📸 Upload Proof", callback_data=f"upload_{plan_key}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="buy")]
    ])

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("💰 Payments", callback_data="admin_payments"), InlineKeyboardButton("📊 Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("🎫 Tickets", callback_data="admin_tickets"), InlineKeyboardButton("👥 Users", callback_data="admin_users")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")]
    ])

def payment_action_keyboard(payment_id, user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment_id}_{user_id}"),
         InlineKeyboardButton("❌ Deny", callback_data=f"deny_{payment_id}_{user_id}")]
    ])

def support_keyboard(ticket_id=None):
    if ticket_id:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("❌ Close Ticket", callback_data="support_close")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back")]
        ])
    else:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton("💬 New Ticket", callback_data="support_new")],
            [InlineKeyboardButton("⬅️ Back", callback_data="back")]
        ])

def admin_ticket_keyboard(ticket_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✏️ Reply", callback_data=f"admin_reply_{ticket_id}"),
         InlineKeyboardButton("❌ Close", callback_data=f"admin_close_{ticket_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin_tickets")]
    ])

# Utility
def is_admin(uid): return uid == ADMIN_ID

# Database helpers
async def upsert_user(user):
    await users_col.update_one({"user_id": user.id},
        {"$setOnInsert": {"plan": None, "start": None, "end": None, "status": "free", "created": datetime.now(timezone.utc)},
         "$set": {"username": user.username, "first_name": user.first_name, "updated": datetime.now(timezone.utc)}},
        upsert=True)

async def get_user(uid): return await users_col.find_one({"user_id": uid})

async def get_open_ticket(user_id): return await tickets_col.find_one({"user_id": user_id, "status": "open"})

# --- Bot handlers below ---

@dp.message(CommandStart())
async def cmd_start(m: types.Message):  # Main welcome
    await upsert_user(m.from_user)
    await bot.send_photo(m.chat.id, WELCOME_IMAGE, caption="Welcome to Premium Bot!", reply_markup=main_keyboard())

@dp.callback_query(F.data == "back")
async def back_menu(cq: types.CallbackQuery):
    await cq.message.edit_text("Choose an option below:", reply_markup=main_keyboard())
    await cq.answer()

@dp.callback_query(F.data == "buy")
async def choose_plan(cq: types.CallbackQuery):
    await cq.message.edit_text("Choose your subscription plan:", reply_markup=plans_keyboard())
    await cq.answer()

@dp.callback_query(F.data.startswith("plan_"))
async def select_plan(cq: types.CallbackQuery):
    plan_key = cq.data.replace("plan_", "")
    last_plan[cq.from_user.id] = plan_key
    p = PLANS[plan_key]
    await cq.message.edit_text(f"{p['emoji']} {p['name']} selected. Price: Rs.{p['price']}\nPick payment mode:", reply_markup=payment_keyboard(plan_key))
    await cq.answer()

@dp.callback_query(F.data.startswith("upi_"))
async def upi_pay(cq: types.CallbackQuery):
    plan_key = cq.data.replace("upi_", "")
    p = PLANS[plan_key]
    await cq.message.edit_text(
        f"UPI Payment\nPlan: {p['name']}\nAmount: Rs.{p['price']}\nCopy the UPI ID below and pay using your app.\nAfter payment, upload the screenshot.",
        reply_markup=payment_keyboard(plan_key))
    upi_id_text = f"UPI ID: {UPI_ID}\nAmount: {p['price']}\n\nLong press the UPI ID to copy.\n"
    await bot.send_message(cq.from_user.id, upi_id_text)
    await cq.answer()

@dp.callback_query(F.data.startswith("qr_"))
async def qr_pay(cq: types.CallbackQuery):
    plan_key = cq.data.replace("qr_", "")
    p = PLANS[plan_key]
    await bot.send_photo(cq.from_user.id, QR_CODE_URL, caption=f"QR Payment\nPlan: {p['name']}\nAmount: Rs.{p['price']}\nScan, pay, and then upload proof.", reply_markup=payment_keyboard(plan_key))
    await cq.answer()

@dp.callback_query(F.data.startswith("upload_"))
async def upload_proof(cq: types.CallbackQuery):
    plan_key = cq.data.replace("upload_", "")
    last_plan[cq.from_user.id] = plan_key
    p = PLANS[plan_key]
    await cq.message.edit_text(f"Upload screenshot for your payment: {p['name']} Rs.{p['price']}")
    await cq.answer()

@dp.message(F.photo)
async def payment_screenshot(m: types.Message):
    plan_key = last_plan.get(m.from_user.id)
    if not plan_key: return await m.reply("Please choose plan first!")
    pid = str((await payments_col.insert_one({
        "user_id": m.from_user.id, "plan": plan_key,
        "file": m.photo[-1].file_id, "status": "pending", "created": datetime.now(timezone.utc)
    })).inserted_id)
    plan = PLANS[plan_key]
    await bot.send_message(m.from_user.id, f"Your payment proof received. ID: #{pid[-6:]}\nAdmin will review soon.")
    capt = f"PAYMENT #{pid[-6:]}\nUser: {m.from_user.id}\nPlan: {plan['name']} Rs.{plan['price']}"
    await bot.send_message(ADMIN_ID, capt, reply_markup=payment_action_keyboard(pid, m.from_user.id))
    await bot.send_photo(ADMIN_ID, m.photo[-1].file_id, caption=f"Proof by {m.from_user.id}")

@dp.callback_query(F.data.startswith("approve_"))
async def admin_approve(cq: types.CallbackQuery):
    pid, user_id = cq.data.split("_")[1:]
    await payments_col.update_one({"_id": ObjectId(pid)}, {"$set": {"status": "approved"}})
    payment = await payments_col.find_one({"_id": ObjectId(pid)})
    plan_key = payment["plan"]
    plan = PLANS[plan_key]
    await users_col.update_one(
        {"user_id": int(user_id)},
        {"$set": {
            "plan": plan_key,
            "start": datetime.now(timezone.utc),
            "end": datetime.now(timezone.utc) + timedelta(days=plan["days"]),
            "status": "premium"
        }}
    )
    try:
        link = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
        msg = f"Payment approved! {plan['name']} now active. Join channel: {link.invite_link}"
    except:
        msg = f"Payment approved! {plan['name']} now active."
    await bot.send_message(int(user_id), msg)
    await cq.answer("Approved")

@dp.callback_query(F.data.startswith("deny_"))
async def admin_deny(cq: types.CallbackQuery):
    pid, user_id = cq.data.split("_")[1:]
    await payments_col.update_one({"_id": ObjectId(pid)}, {"$set": {"status": "denied"}})
    await bot.send_message(int(user_id), "Your proof was denied. Please upload a clear screenshot with all details.")
    await cq.answer("Denied")

# --- Support System ---
@dp.callback_query(F.data == "support")
async def support_start(cq: types.CallbackQuery):
    ticket = await get_open_ticket(cq.from_user.id)
    if ticket:
        tid = str(ticket['_id'])
        await cq.message.edit_text(f"You have an open support ticket. Send your next message to add to the ticket, or /close_ticket to close.", reply_markup=support_keyboard(tid))
        OPEN_TICKETS[cq.from_user.id] = tid
    else:
        await cq.message.edit_text("Send your message to open a support ticket.", reply_markup=support_keyboard())
    await cq.answer()

@dp.callback_query(F.data == "support_new")
async def support_create(cq: types.CallbackQuery):
    await cq.message.edit_text("Send your support message now. Admin will reply soon.", reply_markup=support_keyboard())
    await cq.answer()

@dp.callback_query(F.data == "support_close")
async def support_close(cq: types.CallbackQuery):
    ticket = await get_open_ticket(cq.from_user.id)
    if not ticket:
        await cq.answer("No open ticket found.", show_alert=True)
        return
    await tickets_col.update_one({"_id": ticket["_id"]}, {"$set": {"status": "closed", "closed": datetime.now(timezone.utc)}})
    del OPEN_TICKETS[cq.from_user.id]
    await cq.message.edit_text("Support ticket closed. Need more help? Start a new one anytime.", reply_markup=main_keyboard())
    await cq.answer()

@dp.message(F.text & ~F.command)
async def support_message(m: types.Message):
    ticket = await get_open_ticket(m.from_user.id)
    if ticket:
        tid = str(ticket['_id'])
        await tickets_col.update_one({"_id": ticket["_id"]}, {
            "$push": {"messages": {"from": "user", "text": m.text, "time": datetime.now(timezone.utc)}}
        })
        await bot.send_message(ADMIN_ID, f"Support Ticket #{tid[-6:]}:\nUser: {m.from_user.first_name} ({m.from_user.id})\nMessage: {m.text}\n/reply {tid} your_admin_reply")
        return await m.reply("Message added to ticket, admin will reply soon.")
    else:
        ticket_id = str((await tickets_col.insert_one({
            "user_id": m.from_user.id,
            "username": m.from_user.username,
            "first_name": m.from_user.first_name,
            "messages": [{"from": "user", "text": m.text, "time": datetime.now(timezone.utc)}],
            "status": "open",
            "created": datetime.now(timezone.utc)
        })).inserted_id)
        OPEN_TICKETS[m.from_user.id] = ticket_id
        await bot.send_message(ADMIN_ID, f"NEW SUPPORT TICKET #{ticket_id[-6:]} from {m.from_user.first_name} ({m.from_user.id}).\n/reply {ticket_id} admin_reply_here")
        await m.reply(f"Ticket opened! Admin will reply soon.")

@dp.message(Command("reply"))
async def admin_reply(m: types.Message):
    if not is_admin(m.from_user.id): return
    parts = m.text.split(maxsplit=2)
    if len(parts) < 3:
        return await m.reply("Format: /reply <ticket_id> <your message>")
    ticket_id, admin_text = parts[1], parts[2]
    ticket = await tickets_col.find_one({"_id": ObjectId(ticket_id)})
    if not ticket: return await m.reply("Ticket not found.")
    await tickets_col.update_one({"_id": ObjectId(ticket_id)}, {
        "$push": {"messages": {"from": "admin", "text": admin_text, "time": datetime.now(timezone.utc)}}
    })
    await bot.send_message(ticket["user_id"], f"Admin replied to your support ticket:\n\n{admin_text}")
    await m.reply("Reply sent to user.")

@dp.callback_query(F.data == "offers")
async def offers_show(cq: types.CallbackQuery):
    await cq.message.edit_text("6M: Save 33%\n1Y: Best Value\nLifetime: One-time pay", reply_markup=main_keyboard())
    await cq.answer()

@dp.callback_query(F.data == "myplan")
async def myplan(cq: types.CallbackQuery):
    user = await get_user(cq.from_user.id)
    if not user or user.get("status") != "premium":
        await cq.message.edit_text("No active plan. Upgrade to get all benefits.", reply_markup=main_keyboard())
        await cq.answer()
        return
    plan = PLANS.get(user["plan"], {"name": "Unknown"})
    remain = (user["end"] - datetime.now(timezone.utc)).days if user.get("end") else "?"
    await cq.message.edit_text(f"Your Plan: {plan['name']}\nDays left: {remain}", reply_markup=main_keyboard())
    await cq.answer()

@dp.callback_query(F.data == "admin")
async def admin_panel(cq: types.CallbackQuery):
    await cq.message.edit_text("Admin Panel", reply_markup=admin_keyboard())
    await cq.answer()

@dp.callback_query(F.data == "admin_stats")
async def stats_admin(cq: types.CallbackQuery):
    users = await users_col.count_documents({})
    active = await users_col.count_documents({"status": "premium"})
    pending = await payments_col.count_documents({"status": "pending"})
    tickets = await tickets_col.count_documents({"status": "open"})
    txt = f"Stats:\nUsers: {users}\nPremium: {active}\nPayments pending: {pending}\nOpen Tickets: {tickets}"
    await cq.message.reply(txt)
    await cq.answer()

@dp.callback_query(F.data == "admin_payments")
async def admin_payments(cq: types.CallbackQuery):
    q = payments_col.find({"status": "pending"}).sort("created", -1).limit(10)
    payments = await q.to_list(length=10)
    if not payments:
        await cq.message.reply("No pending payments.")
        return
    for p in payments:
        plan = PLANS[p["plan"]]
        await cq.message.reply(f"Payment #{str(p['_id'])[-6:]}\nUser: {p['user_id']}\nPlan: {plan['name']} Rs.{plan['price']}",
                              reply_markup=payment_action_keyboard(str(p['_id']), p['user_id']))
    await cq.answer()

@dp.callback_query(F.data == "admin_users")
async def admin_users(cq: types.CallbackQuery):
    users = await users_col.find().sort("created", -1).limit(20).to_list(20)
    reply = "Top 20 Users:\n"
    for u in users:
        st = u.get("status", "free")
        reply += f"{u['user_id']} {u.get('username','')} {st.upper()}\n"
    await cq.message.reply(reply)
    await cq.answer()

@dp.callback_query(F.data == "admin_tickets")
async def admin_tickets_view(cq: types.CallbackQuery):
    tickets = await tickets_col.find({"status": "open"}).sort("created", -1).limit(10).to_list(10)
    if not tickets:
        await cq.message.reply("No open tickets.")
        return
    for t in tickets:
        tid = str(t['_id'])
        txt = f"Ticket #{tid[-6:]}\nUser: {t.get('first_name', '')} ({t['user_id']})"
        await cq.message.reply(txt, reply_markup=admin_ticket_keyboard(tid))
    await cq.answer()

@dp.callback_query(F.data.startswith("admin_reply_"))
async def admin_status_reply(cq: types.CallbackQuery, state: FSMContext):
    ticket_id = cq.data.replace("admin_reply_", "")
    await state.set_state(AdminReply.waiting_reply)
    await state.update_data(ticket_id=ticket_id)
    await cq.message.reply(f"Type reply for ticket #{ticket_id[-6:]}")
    await cq.answer()

@dp.message(AdminReply.waiting_reply)
async def admin_reply_send(m: types.Message, state: FSMContext):
    data = await state.get_data()
    tid = data["ticket_id"]
    ticket = await tickets_col.find_one({"_id": ObjectId(tid)})
    if not ticket: return await m.reply("Ticket not found.")
    await tickets_col.update_one({"_id": ObjectId(tid)}, {
        "$push": {"messages": {"from": "admin", "text": m.text, "time": datetime.now(timezone.utc)}}
    })
    await bot.send_message(ticket["user_id"], f"Admin replied to your ticket:\n{m.text}")
    await m.reply("Reply sent.")
    await state.clear()

@dp.callback_query(F.data.startswith("admin_close_"))
async def admin_ticket_close(cq: types.CallbackQuery):
    tid = cq.data.replace("admin_close_", "")
    ticket = await tickets_col.find_one({"_id": ObjectId(tid)})
    if ticket:
        await tickets_col.update_one({"_id": ObjectId(tid)}, {"$set": {"status": "closed", "closed": datetime.now(timezone.utc)}})
        await bot.send_message(ticket["user_id"], "Your ticket is closed by admin.")
        await cq.message.edit_text("Ticket closed.")
    await cq.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(cq: types.CallbackQuery, state: FSMContext):
    await cq.message.reply("Send your broadcast message.")
    await state.set_state(Broadcast.waiting_broadcast)
    await cq.answer()

@dp.message(Broadcast.waiting_broadcast)
async def admin_broadcast_send(m: types.Message, state: FSMContext):
    users = await users_col.find({}, {"user_id": 1}).to_list(length=None)
    for u in users:
        try:
            await bot.send_message(u["user_id"], f"BROADCAST:\n{m.text}")
            await asyncio.sleep(0.03)
        except: pass
    await m.reply("Broadcast sent.")
    await state.clear()

async def expiry_worker():
    while True:
        now = datetime.now(timezone.utc)
        users = await users_col.find({"status": "premium"}).to_list(length=None)
        for user in users:
            end = user.get("end")
            if not end: continue
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if end <= now:
                await users_col.update_one({"user_id": user["user_id"]}, {"$set": {"status": "expired"}})
                try: await bot.ban_chat_member(CHANNEL_ID, user["user_id"]); await bot.unban_chat_member(CHANNEL_ID, user["user_id"])
                except: pass
                await bot.send_message(user["user_id"], "Your premium subscription expired.")
        await asyncio.sleep(1800)

async def main():
    await mongo.admin.command('ping')
    asyncio.create_task(expiry_worker())
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
