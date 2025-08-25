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
client       = AsyncIOMotorClient(MONGO_URI, maxPoolSize=10, minPoolSize=2)
db           = client["premium_bot"]
users_col    = db["users"]
payments_col = db["payments"]
tickets_col  = db["support_tickets"]

bot = Bot(token=API_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# Plans
PLANS = {
    "plan1": {"name":"1 Month",  "price":"₹99",   "days":30,    "emoji":"🟢"},
    "plan2": {"name":"6 Months", "price":"₹399",  "days":180,   "emoji":"🟡"},
    "plan3": {"name":"1 Year",   "price":"₹1999", "days":365,   "emoji":"🔥"},
    "plan4": {"name":"Lifetime", "price":"₹2999", "days":36500,"emoji":"💎"},
}
last_plan: Dict[int,str]={}

# FSM
class BCast(StatesGroup):
    waiting_text = State()

def is_admin(uid): return uid==ADMIN_ID

# Database
async def upsert_user(u:types.User):
    await users_col.update_one(
        {"user_id":u.id},
        {"$set":{"username":u.username,"first_name":u.first_name,"last_name":u.last_name,"updated":datetime.now(timezone.utc)},
         "$setOnInsert":{"plan":None,"start":None,"end":None,"status":"none","created":datetime.now(timezone.utc),"reminded":False}},
        upsert=True)

async def get_user(uid): return await users_col.find_one({"user_id":uid})

async def set_sub(uid,plan_key,days):
    now=datetime.now(timezone.utc)
    end=now+timedelta(days=days)
    await users_col.update_one({"user_id":uid},{"$set":{"plan":plan_key,"start":now,"end":end,"status":"active","reminded":False}})
    return now,end

async def add_payment(uid,pk,fid):
    r=await payments_col.insert_one({"user_id":uid,"plan":pk,"file":fid,"time":datetime.now(timezone.utc),"status":"pending"})
    return str(r.inserted_id)

async def update_payment(pid,status):
    await payments_col.update_one({"_id":ObjectId(pid)},{"$set":{"status":status}})

async def get_payment(pid): return await payments_col.find_one({"_id":ObjectId(pid)})

async def add_ticket(uid,msg):
    r=await tickets_col.insert_one({"user_id":uid,"msgs":[],"closed":False,"created":datetime.now(timezone.utc)})
    await tickets_col.update_one({"_id":r.inserted_id},{"$push":{"msgs":{"from":"user","txt":msg,"time":datetime.now(timezone.utc)}}})
    return str(r.inserted_id)

async def append_ticket(tid,frm,txt):
    await tickets_col.update_one({"_id":ObjectId(tid)},{"$push":{"msgs":{"from":frm,"txt":txt,"time":datetime.now(timezone.utc)}}})

async def get_stats():
    st=await users_col.aggregate([{"$group":{"_id":"$status","c":{"$sum":1}}}]).to_list(None)
    d={i["_id"]:i["c"] for i in st}
    p=await payments_col.count_documents({"status":"pending"})
    t=sum(d.values())
    return t,d.get("active",0),d.get("expired",0),p

# UI
def kb_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Upgrade", "buy")],
                                 [InlineKeyboardButton("📊 My Sub", "my"),InlineKeyboardButton("💬 Support","support")],
                                 [InlineKeyboardButton("🎁 Offers","offers")]])
def kb_plans():
    kb=InlineKeyboardMarkup()
    for k,p in PLANS.items(): kb.add(InlineKeyboardButton(f"{p['emoji']} {p['name']} - {p['price']}",f"plan_{k}"))
    kb.add(InlineKeyboardButton("⬅️ Back","menu")); return kb
def kb_payopt(pk):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 UPI",f"upi_{pk}"),InlineKeyboardButton("📱 QR",f"qr_{pk}")],
        [InlineKeyboardButton("📸 Upload",f"up_{pk}")],
        [InlineKeyboardButton("⬅️ Plans","buy")]
    ])
def kb_payact(pid,uid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve",f"ap_{pid}_{uid}"),InlineKeyboardButton("❌ Deny",f"dn_{pid}_{uid}")]])
def kb_tix(t):
    kb=InlineKeyboardMarkup()
    for x in t: kb.add(InlineKeyboardButton(f"#{x['_id']} – {x['user_id']} ({'closed' if x['closed'] else 'open'})",f"tk_{x['_id']}"))
    return kb
def kb_tixact(tid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Reply",f"tr_{tid}"),InlineKeyboardButton("✅ Close",f"tc_{tid}")]])

# Fast send/edit
async def sp(cid,url,txt,kb=None):
    try: await bot.send_photo(cid,url,caption=txt,reply_markup=kb)
    except: await bot.send_message(cid,txt,reply_markup=kb)
async def ed(cq,txt,ph=None,kb=None):
    try:
        if ph: await cq.message.delete(); await sp(cq.from_user.id,ph,txt,kb)
        else: await cq.message.edit_text(txt,reply_markup=kb)
    except:
        if ph: await sp(cq.from_user.id,ph,txt,kb)
        else: await cq.message.answer(txt,reply_markup=kb)

# Handlers
@dp.message(CommandStart())
async def st(m):
    asyncio.create_task(upsert_user(m.from_user))
    txt=f"👋 Hi {m.from_user.first_name}!\n/—> buy to upgrade"
    await sp(m.from_user.id,WELCOME_IMG,txt,kb_menu())

@dp.callback_query(F.data=="menu")
async def mk(cq): await ed(cq,f"🏠 Menu",WELCOME_IMG,kb_menu()); await cq.answer()

@dp.callback_query(F.data=="buy")
async def bw(cq): await ed(cq,"💎 Plans",PLANS_IMG,kb_plans()); await cq.answer()

@dp.callback_query(F.data.startswith("plan_"))
async def ps(cq):
    pk=cq.data[5:]
    last_plan[cq.from_user.id]=pk
    p=PLANS[pk];daily=float(p["price"][1:])/p["days"]
    txt=f"{p['emoji']} {p['name']} {p['price']}\n{daily:.1f}/day\n\nChoose..."
    await ed(cq,txt,None,kb_payopt(pk)); await cq.answer()

@dp.callback_query(F.data.startswith("upi_"))
async def up(cq):
    pk=cq.data[4:];p=PLANS[pk];amt=p["price"][1:]
    txt=f"💳 UPI for {p['name']}\n"
    await ed(cq,txt,None,kb_payopt(pk))
    msg=f"<b>UPI ID:</b> <code>{UPI_ID}</code>\n<b>Amount:</b> <code>{amt}</code>"
    await bot.send_message(cq.from_user.id,msg,parse_mode=ParseMode.HTML)
    await cq.answer()

@dp.callback_query(F.data.startswith("qr_"))
async def qr(cq):
    pk=cq.data[3:];p=PLANS[pk]
    txt=f"📱 QR {p['name']} {p['price']}"
    await ed(cq,txt,QR_CODE_URL,kb_payopt(pk)); await cq.answer()

@dp.callback_query(F.data.startswith("up_"))
async def upf(cq):
    pk=cq.data[3:];last_plan[cq.from_user.id]=pk
    await ed(cq,f"📸 Upload proof for {PLANS[pk]['name']}");await cq.answer()

@dp.message(F.photo)
async def pr(m):
    if is_admin(m.from_user.id): return
    pk=last_plan.get(m.from_user.id)
    if not pk: return await m.answer("❌ /buy first")
    pid=await add_payment(m.from_user.id,pk,m.photo[-1].file_id);p=PLANS[pk]
    txt=f"🎉 Proof #{pid} for {p['name']}\nProcessing..."
    try: await bot.send_photo(m.from_user.id,SUCCESS_IMG,caption=txt)
    except: await m.answer(txt)
    atxt=f"💰 #{pid} by {m.from_user.id}\n{p['name']} {p['price']}"
    await bot.send_message(ADMIN_ID,atxt,reply_markup=kb_payact(pid,m.from_user.id))

@dp.callback_query(F.data.startswith("ap_"))
async def appr(cq):
    if not is_admin(cq.from_user.id): return
    _,pid,uid_s=cq.data.split("_");uid=int(uid_s)
    pay=await get_payment(pid);pk=pay["plan"];p=PLANS[pk]
    await update_payment(pid,"approved");await set_sub(uid,pk,p["days"])
    try:
        link=await bot.create_chat_invite_link(CHANNEL_ID,member_limit=1)
        msg=f"♪ You’re premium! Join: {link.invite_link}"
    except: msg="♪ Premium active!"
    asyncio.create_task(bot.send_message(uid,msg))
    await cq.message.edit_text(f"✅ Approved #{pid}");await cq.answer()

@dp.callback_query(F.data.startswith("dn_"))
async def dn(cq):
    if not is_admin(cq.from_user.id): return
    _,pid,uid_s=cq.data.split("_");uid=int(uid_s)
    await update_payment(pid,"denied")
    asyncio.create_task(bot.send_message(uid,"❌ Proof denied. Please re-upload clear screenshot."))
    await cq.message.edit_text(f"❌ Denied #{pid}");await cq.answer()

# Support
@dp.callback_query(F.data=="support")
async def spst(cq):
    await ed(cq,f"💬 Send support message",None,kb_menu()); await cq.answer()

@dp.message(F.text & ~F.command)
async def supmsg(m):
    if is_admin(m.from_user.id): return
    tid=await add_ticket(m.from_user.id,m.text)
    admin_txt=f"🎫 Ticket #{tid}\n👤 {m.from_user.id}\n{m.text}"
    await bot.send_message(ADMIN_ID,admin_txt,reply_markup=kb_tix([{"_id":tid,"user_id":m.from_user.id,"closed":False}]))
    await m.answer(f"✅ Ticket #{tid} created!")

@dp.callback_query(F.data.startswith("tk_"))
async def tcket(cq):
    if cq.from_user.id!=ADMIN_ID: return
    tid=cq.data[3:];t=await tickets_col.find_one({"_id":ObjectId(tid)})
    msg=f"🎫 #{tid} from {t['user_id']}\n"
    for m in t["msgs"]:who="Admin" if m["from"]=="admin" else "User";msg+=f"{who}: {m['txt']}\n"
    await bot.send_message(ADMIN_ID,msg,reply_markup=kb_tixact(tid));await cq.answer()

@dp.callback_query(F.data.startswith("tr_"))
async def tr(cq):
    if cq.from_user.id!=ADMIN_ID: return
    tid=cq.data[3:]
    await bot.send_message(ADMIN_ID,f"✏️ Reply to #{tid}") 
    await dp.current_state(user=ADMIN_ID).set_state(f"reply_{tid}")
    await cq.answer()

@dp.message(F.text & F.regexp(r"reply_\w+"))
async def trply(m, state:FSMContext):
    st=await state.get_state();tid=st.split("_")[1]
    await append_ticket(tid,"admin",m.text)
    t=await tickets_col.find_one({"_id":ObjectId(tid)})
    await bot.send_message(t["user_id"],f"💬 Reply #{tid}:\n\n{m.text}")
    await m.answer("✅ Reply sent!"); await state.clear()

@dp.callback_query(F.data.startswith("tc_"))
async def tc(cq):
    if cq.from_user.id!=ADMIN_ID: return
    tid=cq.data[3:]
    await tickets_col.update_one({"_id":ObjectId(tid)},{"$set":{"closed":True}})
    t=await tickets_col.find_one({"_id":ObjectId(tid)})
    await bot.send_message(t["user_id"],f"✅ Ticket #{tid} closed")
    await cq.answer("Closed")

# Expiry
async def expiry_task():
    while True:
        now=datetime.now(timezone.utc)
        ex=await users_col.find({"status":"active","end":{"$lte":now}}).to_list(None)
        for u in ex:
            await users_col.update_one({"user_id":u["user_id"]},{"$set":{"status":"expired"}})
            try:await bot.ban_chat_member(CHANNEL_ID,u["user_id"]);await bot.unban_chat_member(CHANNEL_ID,u["user_id"])
            except: pass
            asyncio.create_task(bot.send_message(u["user_id"],"❌ Expired\nRenew: /buy"))
        remd=now+timedelta(days=3)
        rm=await users_col.find({"status":"active","end":{"$lte":remd,"$gt":now},"reminded":False}).to_list(None)
        for u in rm:
            days=(u["end"]-now).days
            asyncio.create_task(bot.send_message(u["user_id"],f"⏰ Expires in {days} days.\nRenew: /buy"))
            await users_col.update_one({"user_id":u["user_id"]},{"$set":{"reminded":True}})
        await asyncio.sleep(1800)

async def main():
    await client.admin.command('ping')
    log.info("MongoDB OK")
    asyncio.create_task(expiry_task())
    log.info("Expiry worker running")
    await dp.start_polling(bot,skip_updates=True)

if __name__=="__main__":
    asyncio.run(main())
