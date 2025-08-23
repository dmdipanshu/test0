import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from aiogram.utils.markdown import escape_md

# ───────────────────────── Logging ─────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("subbot")

# ───────────────────────── Config (ENV based for Koyeb) ─────────────────────────
API_TOKEN = os.getenv("API_TOKEN") or "TEST_TOKEN"
ADMIN_ID = int(os.getenv("ADMIN_ID") or "123456789")
CHANNEL_ID = int(os.getenv("CHANNEL_ID") or "-10012345678")
UPI_ID = os.getenv("UPI_ID") or "yourupi@upi"
QR_CODE_URL = os.getenv("QR_CODE_URL") or "https://example.com/qr.png"

if API_TOKEN == "TEST_TOKEN":
    raise RuntimeError("❌ API_TOKEN not set! Please configure environment variables.")

bot = Bot(API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ───────────────────────── Plans ─────────────────────────
PLANS = {
    "plan1": {"name": "1 Month",  "price": "₹99",   "days": 30},
    "plan2": {"name": "6 Months", "price": "₹199",  "days": 180},
    "plan3": {"name": "1 Year",   "price": "₹1999", "days": 365},
    "plan4": {"name": "Lifetime", "price": "₹2999", "days": 36500},
}
last_selected_plan: Dict[int, str] = {}

# ───────────────────────── SQLite (ephemeral in Koyeb) ─────────────────────────
DB = "/tmp/subs.db"

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            plan_key TEXT,
            start_at TEXT,
            end_at TEXT,
            status TEXT,
            created_at TEXT,
            reminded_3d INTEGER DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS payments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan_key TEXT,
            file_id TEXT,
            created_at TEXT,
            status TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS tickets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            message TEXT,
            status TEXT,
            created_at TEXT
        )""")
        c.commit()

def upsert_user(usr: types.User):
    with db() as c:
        now = datetime.now(timezone.utc).isoformat()
        c.execute(
            """INSERT INTO users(user_id,username,first_name,last_name,plan_key,start_at,end_at,status,created_at)
               VALUES(?,?,?,?,NULL,NULL,NULL,'none',?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username,
                 first_name=excluded.first_name,
                 last_name=excluded.last_name
            """,
            (usr.id, usr.username, usr.first_name, usr.last_name, now),
        )
        c.commit()

def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def list_users(limit: int = 1000):
    with db() as c:
        return c.execute("SELECT * FROM users ORDER BY COALESCE(end_at,'') DESC LIMIT ?", (limit,)).fetchall()

def set_status(user_id: int, status: str):
    with db() as c:
        c.execute("UPDATE users SET status=? WHERE user_id=?", (status, user_id))
        c.commit()

def set_subscription(user_id: int, plan_key: str, days: int):
    now = datetime.now(timezone.utc)
    row = get_user(user_id)
    if row and row["end_at"]:
        try:
            current_end = datetime.fromisoformat(row["end_at"])
        except Exception:
            current_end = now
        base = current_end if (row["status"] == "active" and current_end > now) else now
        end = base + timedelta(days=days)
    else:
        end = now + timedelta(days=days)
    with db() as c:
        c.execute("""UPDATE users SET plan_key=?, start_at=?, end_at=?, status='active', reminded_3d=0
                     WHERE user_id=?""",
                  (plan_key, now.isoformat(), end.isoformat(), user_id))
        c.commit()
    return now, end

def add_payment(user_id: int, plan_key: str, file_id: str) -> int:
    with db() as c:
        c.execute("""INSERT INTO payments(user_id, plan_key, file_id, created_at, status)
                     VALUES(?,?,?,?, 'pending')""",
                  (user_id, plan_key, file_id, datetime.now(timezone.utc).isoformat()))
        pid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        c.commit()
        return pid

def set_payment_status(payment_id: int, status: str):
    with db() as c:
        c.execute("UPDATE payments SET status=? WHERE id=?", (status, payment_id))
        c.commit()

def pending_payments(limit: int = 10):
    with db() as c:
        return c.execute("SELECT * FROM payments WHERE status='pending' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

def add_ticket(user_id: int, message: str) -> int:
    with db() as c:
        c.execute("""INSERT INTO tickets(user_id,message,status,created_at)
                     VALUES(?,?,'open',?)""",
                  (user_id, message, datetime.now(timezone.utc).isoformat()))
        tid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        c.commit()
        return tid

def stats():
    with db() as c:
        total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        active = c.execute("SELECT COUNT(*) n FROM users WHERE status='active'").fetchone()["n"]
        expired = c.execute("SELECT COUNT(*) n FROM users WHERE status='expired'").fetchone()["n"]
        pend = c.execute("SELECT COUNT(*) n FROM payments WHERE status='pending'").fetchone()["n"]
        return total, active, expired, pend

# ───────────────────────── UI helpers ─────────────────────────
def kb_user_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Buy Subscription", callback_data="menu:buy")],
        [InlineKeyboardButton(text="📦 My Plan", callback_data="menu:my")],
        [InlineKeyboardButton(text="📞 Contact Support", callback_data="menu:support")],
        [InlineKeyboardButton(text="🛠 Admin Panel", callback_data="admin:menu")],
    ])

def kb_plans() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{PLANS['plan1']['name']} - {PLANS['plan1']['price']}", callback_data="plan:plan1")],
        [InlineKeyboardButton(text=f"{PLANS['plan2']['name']} - {PLANS['plan2']['price']}", callback_data="plan:plan2")],
        [InlineKeyboardButton(text=f"{PLANS['plan3']['name']} - {PLANS['plan3']['price']}", callback_data="plan:plan3")],
        [InlineKeyboardButton(text=f"{PLANS['plan4']['name']} - {PLANS['plan4']['price']}", callback_data="plan:plan4")],
    ])

def kb_after_plan(plan_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 I Paid — Send Screenshot", callback_data=f"pay:ask:{plan_key}")],
        [InlineKeyboardButton(text="⬅️ Choose Other Plan", callback_data="menu:buy")],
    ])

def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⌛ Pending Payments", callback_data="admin:pending")],
        [InlineKeyboardButton(text="👥 Users", callback_data="admin:users")],
        [InlineKeyboardButton(text="📊 Stats", callback_data="admin:stats")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin:broadcast")],
    ])

def kb_payment_actions(payment_id: int, user_id: int) -> InlineKeyboardMarkup:
    r1 = [
        InlineKeyboardButton(text=f"✅ {PLANS['plan1']['name']}", callback_data=f"admin:approve:{payment_id}:{user_id}:plan1"),
        InlineKeyboardButton(text=f"✅ {PLANS['plan2']['name']}", callback_data=f"admin:approve:{payment_id}:{user_id}:plan2"),
    ]
    r2 = [
        InlineKeyboardButton(text=f"✅ {PLANS['plan3']['name']}", callback_data=f"admin:approve:{payment_id}:{user_id}:plan3"),
        InlineKeyboardButton(text=f"✅ {PLANS['plan4']['name']}", callback_data=f"admin:approve:{payment_id}:{user_id}:plan4"),
    ]
    r3 = [InlineKeyboardButton(text="❌ Deny", callback_data=f"admin:deny:{payment_id}:{user_id}")]
    r4 = [InlineKeyboardButton(text="💬 Quick Reply", callback_data=f"admin:reply:{user_id}")]
    return InlineKeyboardMarkup(inline_keyboard=[r1, r2, r3, r4])

def fmt_dt(dtiso: Optional[str]) -> str:
    if not dtiso:
        return "—"
    return datetime.fromisoformat(dtiso).astimezone().strftime("%Y-%m-%d %H:%M")

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

# Helper function to safely escape user input for Markdown
def safe_escape(text: str) -> str:
    """Escape special characters for Markdown parsing"""
    if not text:
        return "-"
    # Replace problematic characters that can break Markdown
    text = str(text)
    text = text.replace("_", "\\_")
    text = text.replace("*", "\\*")
    text = text.replace("`", "\\`")
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    text = text.replace("(", "\\(")
    text = text.replace(")", "\\)")
    return text

# ───────────────────────── FSM for broadcast ─────────────────────────
class BCast(StatesGroup):
    waiting_text = State()

# ───────────────────────── User Flow ─────────────────────────
@dp.message(CommandStart())
async def on_start(m: types.Message):
    upsert_user(m.from_user)
    await m.answer("Welcome! Choose an option:", reply_markup=kb_user_menu())

@dp.callback_query(F.data == "menu:buy")
async def on_buy(cq: types.CallbackQuery):
    await cq.message.answer("Pick a plan:", reply_markup=kb_plans())
    await cq.answer()

@dp.callback_query(F.data.startswith("plan:"))
async def on_plan(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[1]
    last_selected_plan[cq.from_user.id] = plan_key
    caption = (
        f"✅ *{PLANS[plan_key]['name']}*\n"
        f"💰 {PLANS[plan_key]['price']}\n\n"
        f"📲 Pay UPI: `{UPI_ID}`\n"
        f"Or scan this QR.\n\n"
        f"Then tap **I Paid — Send Screenshot** and upload your proof."
    )
    await cq.message.answer_photo(QR_CODE_URL, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_after_plan(plan_key))
    await cq.answer()

@dp.callback_query(F.data.startswith("pay:ask:"))
async def on_pay_ask(cq: types.CallbackQuery):
    plan_key = cq.data.split(":")[2]
    last_selected_plan[cq.from_user.id] = plan_key
    await bot.send_message(cq.from_user.id, f"📤 Send your payment *screenshot* now.\nSelected: {PLANS[plan_key]['name']}", parse_mode=ParseMode.MARKDOWN)
    await cq.answer()

@dp.callback_query(F.data == "menu:my")
async def on_my_plan(cq: types.CallbackQuery):
    r = get_user(cq.from_user.id)
    if not r or r["status"] != "active":
        await cq.message.answer("❌ No active subscription.\nUse *Buy Subscription* to get access.", parse_mode=ParseMode.MARKDOWN)
    else:
        await cq.message.answer(
            f"📦 *My Plan*\n"
            f"Plan: {PLANS.get(r['plan_key'], {'name':'—'})['name']}\n"
            f"Start: {fmt_dt(r['start_at'])}\n"
            f"End:   {fmt_dt(r['end_at'])}\n"
            f"Status: {r['status']}",
            parse_mode=ParseMode.MARKDOWN
        )
    await cq.answer()

@dp.callback_query(F.data == "menu:support")
async def on_support(cq: types.CallbackQuery):
    await bot.send_message(cq.from_user.id, "📞 Please type your question/issue. I'll forward it to support.")
    await cq.answer()

@dp.message(F.text & (F.from_user.id != ADMIN_ID))
async def on_user_text(m: types.Message):
    if m.text.startswith("/"):
        return
    upsert_user(m.from_user)
    tid = add_ticket(m.from_user.id, m.text)
    
    # Safe escaping for user input
    username = safe_escape(m.from_user.username or "")
    user_text = safe_escape(m.text)
    
    await bot.send_message(
        ADMIN_ID,
        f"📩 *Support Ticket #{tid}*\n"
        f"User: @{username} (`{m.from_user.id}`)\n\n"
        f"{user_text}",
        parse_mode=ParseMode.MARKDOWN
    )
    await m.answer(f"✅ Sent to support. Ticket ID: #{tid}")

# FIXED: Payment proof handler with proper escaping
@dp.message(F.photo & (F.from_user.id != ADMIN_ID))
async def on_payment_photo(m: types.Message):
    plan_key = last_selected_plan.get(m.from_user.id, "plan1")
    pid = add_payment(m.from_user.id, plan_key, m.photo[-1].file_id)
    
    # Safe escaping for username and plan name
    username = safe_escape(m.from_user.username or "")
    plan_name = safe_escape(PLANS[plan_key]['name'])
    
    await bot.send_message(
        ADMIN_ID,
        f"💵 *Payment Proof #{pid}* from `{m.from_user.id}` (@{username})\n"
        f"Selected: {plan_name}",
        parse_mode=ParseMode.MARKDOWN
    )
    await bot.send_photo(ADMIN_ID, m.photo[-1].file_id, reply_markup=kb_payment_actions(pid, m.from_user.id))
    await m.answer("✅ Screenshot received. Admin will review shortly.")

@dp.callback_query(F.data == "admin:menu")
async def admin_menu(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("Admins only.", show_alert=True); return
    await cq.message.answer("🛠 Admin Panel", reply_markup=kb_admin_menu())
    await cq.answer()

@dp.callback_query(F.data == "admin:pending")
async def admin_pending(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("Admins only.", show_alert=True); return
    rows = pending_payments(10)
    if not rows:
        await cq.message.answer("✅ No pending payments.")
        await cq.answer(); return
    for r in rows:
        plan_name = safe_escape(PLANS[r['plan_key']]['name'])
        cap = f"💵 Payment #{r['id']} from `{r['user_id']}` (pending)\nSelected: {plan_name}"
        await cq.message.answer(cap, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_payment_actions(r["id"], r["user_id"]))
    await cq.answer()

@dp.callback_query(F.data.startswith("admin:approve:"))
async def admin_approve(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("Admins only.", show_alert=True); return
    _, _, pid, uid, plan_key = cq.data.split(":")
    pid = int(pid); uid = int(uid)
    if plan_key not in PLANS:
        await cq.answer("Unknown plan.", show_alert=True); return
    set_payment_status(pid, "approved")
    _, end = set_subscription(uid, plan_key, PLANS[plan_key]["days"])
    try:
        link = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
        await bot.send_message(uid,
            f"🎉 Payment approved!\nPlan: {PLANS[plan_key]['name']}\n"
            f"Valid till: {end.astimezone().strftime('%Y-%m-%d %H:%M')}\n"
            f"👉 Join: {link.invite_link}")
    except Exception as e:
        log.error(f"Invite link error: {e}")
        await bot.send_message(uid,
            f"🎉 Payment approved!\nPlan: {PLANS[plan_key]['name']}\nValid till: {end.astimezone().strftime('%Y-%m-%d %H:%M')}")
    await cq.message.answer(f"✅ Approved payment #{pid} for user {uid} → {PLANS[plan_key]['name']}")
    await cq.answer("Approved.")

@dp.callback_query(F.data.startswith("admin:deny:"))
async def admin_deny(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("Admins only.", show_alert=True); return
    _, _, pid, uid = cq.data.split(":")
    set_payment_status(int(pid), "denied")
    try:
        await bot.send_message(int(uid), "❌ Your payment proof was not approved. Please contact support.")
    except Exception:
        pass
    await cq.message.answer(f"❌ Denied payment #{pid} for user {uid}.")
    await cq.answer("Denied.")

@dp.callback_query(F.data == "admin:users")
async def admin_users(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("Admins only.", show_alert=True); return
    rows = list_users(50)
    if not rows:
        await cq.message.answer("No users yet.")
        await cq.answer(); return
    lines = []
    for r in rows:
        plan = PLANS.get(r["plan_key"], {"name": "—"})["name"] if r["plan_key"] else "—"
        username = safe_escape(r['username'] or '')
        lines.append(f"`{r['user_id']}` @{username} | {plan} | {fmt_dt(r['end_at'])} | {r['status']}")
    await cq.message.answer("👥 *Users (top 50)*\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    await cq.answer()

@dp.callback_query(F.data == "admin:stats")
async def admin_stats(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("Admins only.", show_alert=True); return
    total, active, expired, pending = stats()
    await cq.message.answer(
        f"📊 *Stats*\nUsers: {total}\nActive: {active}\nExpired: {expired}\nPending payments: {pending}",
        parse_mode=ParseMode.MARKDOWN
    )
    await cq.answer()

@dp.callback_query(F.data == "admin:broadcast")
async def bc_start(cq: types.CallbackQuery, state: FSMContext):
    if not is_admin(cq.from_user.id):
        await cq.answer("Admins only.", show_alert=True); return
    await cq.message.answer("✍️ Send the broadcast message (text).")
    await state.set_state(BCast.waiting_text)
    await cq.answer()

@dp.message(BCast.waiting_text)
async def bc_send(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.clear(); return
    with db() as c:
        rows = c.execute("SELECT user_id FROM users").fetchall()
    sent = 0; fail = 0
    for r in rows:
        try:
            await bot.send_message(r["user_id"], m.text)
            sent += 1
        except Exception:
            fail += 1
    await m.answer(f"📢 Broadcast done. Sent: {sent}, Failed: {fail}")
    await state.clear()

@dp.callback_query(F.data.startswith("admin:reply:"))
async def admin_reply_hint(cq: types.CallbackQuery):
    if not is_admin(cq.from_user.id):
        await cq.answer("Admins only.", show_alert=True); return
    uid = int(cq.data.split(":")[2])
    await cq.message.answer(f"Reply with:\n`/reply {uid} <message>`", parse_mode=ParseMode.MARKDOWN)
    await cq.answer()

@dp.message(Command("reply"))
async def admin_reply_cmd(m: types.Message):
    if not is_admin(m.from_user.id): return
    try:
        _, uid, text = m.text.split(maxsplit=2)
        await bot.send_message(int(uid), f"📞 Support:\n{text}")
        await m.answer("✅ Sent.")
    except Exception:
        await m.answer("Usage: /reply <user_id> <message>")

# ───────────────────────── Auto-Expiry Worker ─────────────────────────
async def expiry_worker():
    while True:
        try:
            now = datetime.now(timezone.utc)
            with db() as c:
                rows = c.execute("SELECT * FROM users").fetchall()
            for r in rows:
                uid, status, end_at, reminded = r["user_id"], r["status"], r["end_at"], r["reminded_3d"]
                if not end_at: continue
                try:
                    end = datetime.fromisoformat(end_at)
                except: continue
                if status == "active" and not reminded and end > now and (end - now) <= timedelta(days=3):
                    try:
                        await bot.send_message(uid, "⏳ Your subscription expires in ~3 days. Renew soon.")
                        with db() as c:
                            c.execute("UPDATE users SET reminded_3d=1 WHERE user_id=?", (uid,))
                            c.commit()
                    except: pass
                if end <= now and status != "expired":
                    with db() as c:
                        c.execute("UPDATE users SET status='expired' WHERE user_id=?", (uid,))
                        c.commit()
                    try:
                        await bot.ban_chat_member(CHANNEL_ID, uid)
                        await bot.unban_chat_member(CHANNEL_ID, uid)
                    except: pass
                    try:
                        await bot.send_message(uid, "❌ Your subscription expired. Use /start to renew.")
                    except: pass
        except Exception as e:
            log.exception(f"expiry_worker error: {e}")
        await asyncio.sleep(1800)

# ───────────────────────── Main ─────────────────────────
async def main():
    init_db()
    log.info("Bot starting on Koyeb ✅")
    asyncio.create_task(expiry_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped.")
