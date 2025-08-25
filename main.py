# premium_subscription_bot.py

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
log = logging.getLogger("subbot")

# Environment
API_TOKEN    = os.getenv("API_TOKEN")
ADMIN_ID     = int(os.getenv("ADMIN_ID"))
CHANNEL_ID   = int(os.getenv("CHANNEL_ID"))
UPI_ID       = os.getenv("UPI_ID")
QR_CODE_URL  = os.getenv("QR_CODE_URL")
WELCOME_IMG  = os.getenv("WELCOME_IMAGE")
PLANS_IMG    = os.getenv("PLANS_IMAGE")
OFFERS_IMG   = os.getenv("OFFERS_IMAGE")
SUCCESS_IMG  = os.getenv("SUCCESS_IMAGE")
UPGRADE_IMG  = os.getenv("UPGRADE_IMAGE")
MONGO_URI    = os.getenv("MONGO_URI")

# MongoDB
client      = AsyncIOMotorClient(MONGO_URI, maxPoolSize=10, minPoolSize=2)
db          = client["premium_bot"]
users_col   = db["users"]
payments_col= db["payments"]
tickets_col = db["support_tickets"]

bot = Bot(token=API_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# Subscription Plans
PLANS = {
    "plan1": {"name":"1 Month",    "price":"₹99",   "days":30,    "emoji":"🟢"},
    "plan2": {"name":"6 Months",   "price":"₹399",  "days":180,   "emoji":"🟡"},
    "plan3": {"name":"1 Year",     "price":"₹1999", "days":365,   "emoji":"🔥"},
    "plan4": {"name":"Lifetime",   "price":"₹2999", "days":36500,"emoji":"💎"},
}
last_selected_plan: Dict[int,str]={}

# Support FSM
class BCast(StatesGroup):
    waiting_text = State()

# Helpers
def is_admin(uid): return uid==ADMIN_ID

async def upsert_user(user: types.User):
    await users_col.update_one(
        {"user_id":user.id},
        {"$set":{"username":user.username,"first_name":user.first_name,"last_name":user.last_name,"updated_at":datetime.now(timezone.utc)},
         "$setOnInsert":{"plan_key":None,"start_at":None,"end_at":None,"status":"none","created_at":datetime.now(timezone.utc),"reminded_3d":False}},
        upsert=True)

async def get_user(uid):
    return await users_col.find_one({"user_id":uid})

async def set_subscription(uid, plan_key, days):
    now=datetime.now(timezone.utc)
    end=now+timedelta(days=days)
    await users_col.update_one({"user_id":uid},{"$set":{"plan_key":plan_key,"start_at":now,"end_at":end,"status":"active","reminded_3d":False}})
    return now,end

async def add_payment(uid,plan_key,file_id):
    r=await payments_col.insert_one({"user_id":uid,"plan_key":plan_key,"file_id":file_id,"created_at":datetime.now(timezone.utc),"status":"pending"})
    return str(r.inserted_id)

async def set_payment_status(pid,status):
    await payments_col.update_one({"_id":ObjectId(pid)},{"$set":{"status":status}})

async def get_payment(pid):
    return await payments_col.find_one({"_id":ObjectId(pid)})

async def add_ticket(uid,msg):
    r=await tickets_col.insert_one({"user_id":uid,"messages":[],"closed":False,"created":datetime.now(timezone.utc)})
    await tickets_col.update_one({"_id":r.inserted_id},{"$push":{"messages":{"from":"user","text":msg,"time":datetime.now(timezone.utc)}}})
    return str(r.inserted_id)

async def append_ticket_message(tid, sender, msg):
    await tickets_col.update_one({"_id":ObjectId(tid)},{"$push":{"messages":{"from":sender,"text":msg,"time":datetime.now(timezone.utc)}}})

async def get_stats():
    st=await users_col.aggregate([{"$group":{"_id":"$status","count":{"$sum":1}}}]).to_list(None)
    d={doc["_id"]:doc["count"] for doc in st}
    pending=await payments_col.count_documents({"status":"pending"})
    total=sum(d.values())
    return total,d.get("active",0),d.get("expired",0),pending

# UI Keyboards
def kb_user_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("🚀 Upgrade Premium",callback_data="buy")],
        [InlineKeyboardButton("📊 My Subscription",callback_data="my"),
         InlineKeyboardButton("💬 Support",callback_data="support")],
        [InlineKeyboardButton("🎁 Special Offers",callback_data="offers")]
    ])

def kb_plans():
    kb=InlineKeyboardMarkup(row_width=1)
    for k,p in PLANS.items(): kb.insert(InlineKeyboardButton(f"{p['emoji']} {p['name']} - {p['price']}",callback_data=f"plan_{k}"))
    kb.add(InlineKeyboardButton("⬅️ Back",callback_data="menu"))
    return kb

def kb_payment_options(plan_key):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("💳 UPI",callback_data=f"upi_{plan_key}"),InlineKeyboardButton("📱 QR",callback_data=f"qr_{plan_key}")],
        [InlineKeyboardButton("📸 Upload Proof",callback_data=f"upload_{plan_key}")],
        [InlineKeyboardButton("⬅️ Back",callback_data="buy")]
    ])

def kb_payment_actions(pid,uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✅ Approve",callback_data=f"approve_{pid}_{uid}"),InlineKeyboardButton("❌ Deny",callback_data=f"deny_{pid}_{uid}")]]
    )

def kb_admin_tickets(tickets):
    kb=InlineKeyboardMarkup(row_width=1)
    for t in tickets:
        status="closed" if t["closed"] else "open"
        kb.insert(InlineKeyboardButton(f"#{t['_id']} – {t['user_id']} ({status})",callback_data=f"adm_ticket_{t['_id']}"))
    return kb

def kb_ticket_actions(tid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton("✏️ Reply",callback_data=f"adm_reply_{tid}"),InlineKeyboardButton("✅ Close",callback_data=f"adm_close_{tid}")]
    ])

# Fast send/edit
async def send_photo(cid,url,txt,kb=None):
    try: await bot.send_photo(cid,url,caption=txt,reply_markup=kb)
    except: await bot.send_message(cid,txt,reply_markup=kb)

async def edit_or_send(cq,txt,photo=None,kb=None):
    try:
        if photo:
            await cq.message.delete()
            await send_photo(cq.from_user.id,photo,txt,kb)
        else:
            await cq.message.edit_text(txt,reply_markup=kb)
    except:
        if photo: await send_photo(cq.from_user.id,photo,txt,kb)
        else: await cq.message.answer(txt,reply_markup=kb)

# Handlers

@dp.message(CommandStart())
async def start(m):
    asyncio.create_task(upsert_user(m.from_user))
    txt=f"👋 Hello {m.from_user.first_name}!\n\n🌟 Premium Benefits:\n• Unlimited downloads\n• Ad-free experience\n• Priority support\n\n🚀 /buy to upgrade!"
    await send_photo(m.from_user.id,WELCOME_IMG,txt,kb_user_menu())

@dp.callback_query(F.data=="menu")
async def menu(cq):
    await edit_or_send(cq,f"🏠 Welcome {cq.from_user.first_name}! Choose:",WELCOME_IMG,kb_user_menu())
    await cq.answer()

@dp.callback_query(F.data=="buy")
async def buy(cq):
    await edit_or_send(cq,"💎 Premium Plans\nChoose:",PLANS_IMG,kb_plans())
    await cq.answer()

@dp.callback_query(F.data.startswith("plan_"))
async def plan_select(cq):
    pk=cq.data[5:]
    last_selected_plan[cq.from_user.id]=pk
    p=PLANS[pk]
    daily=float(p["price"][1:])/p["days"]
    txt=f"🎯 {p['emoji']} {p['name']}\n💰 {p['price']} ({daily:.1f}/day)\n⏰ {p['days']} days\n\nSelect payment:"
    await edit_or_send(cq,txt,None,kb_payment_options(pk))
    await cq.answer()

@dp.callback_query(F.data.startswith("upi_"))
async def upi(cq):
    pk=cq.data[4:];p=PLANS[pk];amt=p["price"][1:]
    txt=f"💳 UPI Payment\nPlan: {p['emoji']} {p['name']}\nAmount: {p['price']}\nCopy UPI ID below and pay."
    await edit_or_send(cq,txt,None,kb_payment_options(pk))
    upi_msg=f"<b>UPI ID:</b> <code>{UPI_ID}</code>\n<b>Amount:</b> <code>{amt}</code>"
    await bot.send_message(cq.from_user.id,upi_msg,parse_mode=ParseMode.HTML)
    await cq.answer()

@dp.callback_query(F.data.startswith("qr_"))
async def qr(cq):
    pk=cq.data[3:];p=PLANS[pk]
    txt=f"📱 QR Payment\nPlan: {p['emoji']} {p['name']}\nAmount: {p['price']}"
    await edit_or_send(cq,txt,QR_CODE_URL,kb_payment_options(pk))
    await cq.answer()

@dp.callback_query(F.data.startswith("upload_"))
async def upload(cq):
    pk=cq.data[7:];last_selected_plan[cq.from_user.id]=pk
    p=PLANS[pk]
    txt=f"📸 Upload payment proof for {p['emoji']} {p['name']} - {p['price']}"
    await edit_or_send(cq,txt,None)
    await cq.answer()

@dp.message(F.photo)
async def proof(m):
    if is_admin(m.from_user.id): return
    pk=last_selected_plan.get(m.from_user.id)
    if not pk: return await m.answer("Select plan: /buy")
    pid=await add_payment(m.from_user.id,pk,m.photo[-1].file_id)
    p=PLANS[pk]
    txt=f"🎉 Received proof #{pid} for {p['emoji']} {p['name']}\nProcessing..."
    try: await bot.send_photo(m.from_user.id,SUCCESS_IMG,caption=txt)
    except: await m.answer(txt)
    # admin notify
    admin_txt=f"💰 Payment #{pid}\n👤 {m.from_user.id}\nPlan: {p['emoji']} {p['name']} {p['price']}"
    await bot.send_message(ADMIN_ID,admin_txt,reply_markup=kb_payment_actions(pid,m.from_user.id))

# Support system
@dp.callback_query(F.data=="support")
async def support(cq):
    await edit_or_send(cq,"💬 Support: Send any message, we'll reply.",None,kb_user_menu())
    await cq.answer()

@dp.message(F.text & ~F.command)
async def support_msg(m):
    if is_admin(m.from_user.id): return
    tid=await add_ticket(m.from_user.id,m.text)
    # admin notify
    admin_txt=f"🎫 Ticket #{tid}\n👤 {m.from_user.id}\n{m.text}"
    await bot.send_message(ADMIN_ID,admin_txt,reply_markup=kb_admin_tickets([{"_id":tid,"user_id":m.from_user.id,"closed":False}]))
    await m.answer(f"✅ Ticket #{tid} created!")

@dp.callback_query(F.data.startswith("adm_ticket_"))
async def adm_ticket(cq):
    if cq.from_user.id!=ADMIN_ID: return
    tid=cq.data.split("_")[2]
    ticket=await tickets_col.find_one({"_id":ObjectId(tid)})
    msg=f"🎫 Ticket #{tid}\n👤 {ticket['user_id']}\n\n"
    for msg_ in ticket["messages"]:
        who="Admin" if msg_["from"]=="admin" else "User"
        msg+=f"{who}: {msg_['text']}\n"
    await bot.send_message(ADMIN_ID,msg,reply_markup=kb_ticket_actions(tid))
    await cq.answer()

@dp.callback_query(F.data.startswith("adm_reply_"))
async def adm_reply_start(cq):
    if cq.from_user.id!=ADMIN_ID: return
    tid=cq.data.split("_")[2]
    await bot.send_message(ADMIN_ID,f"✏️ Type reply for Ticket #{tid}")
    await dp.current_state(user=ADMIN_ID).set_state(f"reply_{tid}")
    await cq.answer()

@dp.message(F.text & F.regexp(r"reply_\w+"))
async def adm_reply(m: types.Message, state: FSMContext):
    state_str=await state.get_state()
    tid=state_str.split("_")[1]
    await append_ticket_message(tid,"admin",m.text)
    ticket=await tickets_col.find_one({"_id":ObjectId(tid)})
    await bot.send_message(ticket["user_id"],f"💬 Reply for Ticket #{tid}:\n\n{m.text}")
    await m.answer("✅ Reply sent!")
    await state.clear()

@dp.callback_query(F.data.startswith("adm_close_"))
async def adm_close(cq):
    if cq.from_user.id!=ADMIN_ID: return
    tid=cq.data.split("_")[2]
    await tickets_col.update_one({"_id":ObjectId(tid)},{"$set":{"closed":True}})
    ticket=await tickets_col.find_one({"_id":ObjectId(tid)})
    await bot.send_message(ticket["user_id"],f"✅ Ticket #{tid} closed by admin.")
    await cq.answer("Closed!")

# Run
if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot,skip_updates=True))
