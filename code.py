import os
import logging
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
import signal
import sys

import uvicorn
from fastapi import FastAPI

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# ───────────────────────── Logging ─────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("subbot")

# ───────────────────────── Config from Env ─────────────────────────
def load_config() -> dict:
    cfg = {
        "API_TOKEN": os.getenv("API_TOKEN"),
        "ADMIN_ID": os.getenv("ADMIN_ID"),
        "CHANNEL_ID": os.getenv("CHANNEL_ID"),
        "UPI_ID": os.getenv("UPI_ID"),
        "QR_CODE_URL": os.getenv("QR_CODE_URL"),
    }
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise ValueError(f"Missing environment variables: {', '.join(missing)}")

    try:
        cfg["ADMIN_ID"] = int(cfg["ADMIN_ID"])
        cfg["CHANNEL_ID"] = int(cfg["CHANNEL_ID"])
    except Exception:
        raise ValueError("ADMIN_ID and CHANNEL_ID must be integer values")

    return cfg

cfg = load_config()
API_TOKEN = cfg["API_TOKEN"]
ADMIN_ID = cfg["ADMIN_ID"]
CHANNEL_ID = cfg["CHANNEL_ID"]
UPI_ID = cfg["UPI_ID"]
QR_CODE_URL = cfg["QR_CODE_URL"]

# ───────────────────────── Bot & Dispatcher ─────────────────────────
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ───────────────────────── FastAPI (health / optional web) ─────────────────────────
app = FastAPI()

@app.get("/")
async def root():
    try:
        me = await bot.get_me()
        return {"status": "ok", "bot": me.username}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}

# ───────────────────────── Plans & Memory ─────────────────────────
PLANS = {
    "plan1": {"name": "1 Month",  "price": "₹99",   "days": 30},
    "plan2": {"name": "6 Months", "price": "₹199",  "days": 180},
    "plan3": {"name": "1 Year",   "price": "₹1999", "days": 365},
    "plan4": {"name": "Lifetime", "price": "₹2999", "days": 36500},
}

# Keep last user-selected plan in memory
last_selected_plan: dict[int, str] = {}

# ───────────────────────── SQLite ─────────────────────────
DB = "subs.db"

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, check_same_thread=False, timeout=20)
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

# ───────────────────────── DB Helpers ─────────────────────────
def upsert_user(usr: types.User):
    with db() as c:
        now = datetime.now(timezone.utc).isoformat()
        try:
            c.execute(
                """INSERT INTO users(user_id,username,first_name,last_name,plan_key,start_at,end_at,status,created_at,reminded_3d)
                   VALUES(?,?,?,?,NULL,NULL,NULL,'none',?,0)
                   ON CONFLICT(user_id) DO UPDATE SET
                     username=excluded.username,
                     first_name=excluded.first_name,
                     last_name=excluded.last_name
                """,
                (usr.id, usr.username, usr.first_name, usr.last_name, now),
            )
            c.commit()
        except sqlite3.Error as e:
            log.error(f"Database error in upsert_user: {e}")

def get_user(user_id: int) -> Optional[sqlite3.Row]:
    try:
        with db() as c:
            return c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    except sqlite3.Error as e:
        log.error(f"Database error in get_user: {e}")
        return None

def list_users(limit: int = 1000):
    try:
        with db() as c:
            return c.execute("SELECT * FROM users ORDER BY COALESCE(end_at,'') DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error as e:
        log.error(f"Database error in list_users: {e}")
        return []

def set_status(user_id: int, status: str):
    try:
        with db() as c:
            c.execute("UPDATE users SET status=? WHERE user_id=?", (status, user_id))
            c.commit()
    except sqlite3.Error as e:
        log.error(f"Database error in set_status: {e}")

def set_subscription(user_id: int, plan_key: str, days: int):
    now = datetime.now(timezone.utc)
    row = get_user(user_id)
    if row and row["end_at"]:
        try:
            current_end = datetime.fromisoformat(row["end_at"])
        except Exception:
            current_end = now
        base = current_end if (row["status"] == "active" and current_end > now) else now
        start = now
        end = base + timedelta(days=days)
    else:
        start = now
        end = now + timedelta(days=days)

    try:
        with db() as c:
            c.execute("""UPDATE users SET plan_key=?, start_at=?, end_at=?, status='active', reminded_3d=0
                         WHERE user_id=?""",
                      (plan_key, start.isoformat(), end.isoformat(), user_id))
            c.commit()
    except sqlite3.Error as e:
        log.error(f"Database error in set_subscription: {e}")
    
    return start, end

def add_payment(user_id: int, plan_key: str, file_id: str) -> int:
    try:
        with db() as c:
            c.execute("""INSERT INTO payments(user_id, plan_key, file_id, created_at, status)
                         VALUES(?,?,?,?, 'pending')""",
                      (user_id, plan_key, file_id, datetime.now(timezone.utc).isoformat()))
            pid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            c.commit()
            return pid
    except sqlite3.Error as e:
        log.error(f"Database error in add_payment: {e}")
        return 0

def set_payment_status(payment_id: int, status: str):
    try:
        with db() as c:
            c.execute("UPDATE payments SET status=? WHERE id=?", (status, payment_id))
            c.commit()
    except sqlite3.Error as e:
        log.error(f"Database error in set_payment_status: {e}")

def pending_payments(limit: int = 10):
    try:
        with db() as c:
            return c.execute("SELECT * FROM payments WHERE status='pending' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error as e:
        log.error(f"Database error in pending_payments: {e}")
        return []

def add_ticket(user_id: int, message: str) -> int:
    try:
        with db() as c:
            c.execute("""INSERT INTO tickets(user_id,message,status,created_at)
                         VALUES(?,?,'open',?)""",
                      (user_id, message, datetime.now(timezone.utc).isoformat()))
            tid = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            c.commit()
            return tid
    except sqlite3.Error as e:
        log.error(f"Database error in add_ticket: {e}")
        return 0

def mark_reminded(user_id: int):
    try:
        with db() as c:
            c.execute("UPDATE users SET reminded_3d=1 WHERE user_id=?", (user_id,))
            c.commit()
    except sqlite3.Error as e:
        log.error(f"Database error in mark_reminded: {e}")

def stats():
    try:
        with db() as c:
            total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            active = c.execute("SELECT COUNT(*) n FROM users WHERE status='active'").fetchone()["n"]
            expired = c.execute("SELECT COUNT(*) n FROM users WHERE status='expired'").fetchone()["n"]
            pend = c.execute("SELECT COUNT(*) n FROM payments WHERE status='pending'").fetchone()["n"]
            return total, active, expired, pend
    except sqlite3.Error as e:
        log.error(f"Database error in stats: {e}")
        return 0, 0, 0, 0

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
    try:
        return datetime.fromisoformat(dtiso).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

# ───────────────────────── FSM for broadcast ─────────────────────────
class Broadcast(StatesGroup):
    waiting_text = State()

class BCast(StatesGroup):
    waiting_text = State()

# ───────────────────────── Handlers ─────────────────────────
@dp.message(CommandStart())
async def on_start(m: types.Message):
    try:
        upsert_user(m.from_user)
        await m.answer("Welcome! Choose an option:", reply_markup=kb_user_menu())
    except Exception as e:
        log.error(f"Error in on_start: {e}")
        await m.answer("Welcome! Service is starting up, please try again in a moment.")

@dp.callback_query(F.data == "menu:buy")
async def on_buy(cq: types.CallbackQuery):
    try:
        await cq.message.answer("Pick a plan:", reply_markup=kb_plans())
        await cq.answer()
    except Exception as e:
        log.error(f"Error in on_buy: {e}")
        await cq.answer("Error occurred, please try again.")

@dp.callback_query(F.data.startswith("plan:"))
async def on_plan(cq: types.CallbackQuery):
    try:
        plan_key = cq.data.split(":")[1]
        if plan_key not in PLANS:
            await cq.answer("Invalid plan selected.")
            return
            
        last_selected_plan[cq.from_user.id] = plan_key
        caption = (
            f"✅ *{PLANS[plan_key]['name']}*\n"
            f"💰 {PLANS[plan_key]['price']}\n\n"
            f"📲 Pay UPI: `{UPI_ID}`\n"
            f"Or scan this QR.\n\n"
            f"Then tap **I Paid — Send Screenshot** and upload your proof."
        )
        try:
            await cq.message.answer_photo(QR_CODE_URL, caption=caption, parse_mode="Markdown", reply_markup=kb_after_plan(plan_key))
        except Exception:
            # fallback if photo fails
            await cq.message.answer(caption, parse_mode="Markdown", reply_markup=kb_after_plan(plan_key))
        await cq.answer()
    except Exception as e:
        log.error(f"Error in on_plan: {e}")
        await cq.answer("Error occurred, please try again.")

@dp.callback_query(F.data.startswith("pay:ask:"))
async def on_pay_ask(cq: types.CallbackQuery):
    try:
        plan_key = cq.data.split(":")[2]
        if plan_key not in PLANS:
            await cq.answer("Invalid plan selected.")
            return
            
        last_selected_plan[cq.from_user.id] = plan_key
        await bot.send_message(cq.from_user.id, f"📤 Send your payment *screenshot* now.\nSelected: {PLANS[plan_key]['name']}", parse_mode="Markdown")
        await cq.answer()
    except Exception as e:
        log.error(f"Error in on_pay_ask: {e}")
        await cq.answer("Error occurred, please try again.")

@dp.callback_query(F.data == "menu:my")
async def on_my_plan(cq: types.CallbackQuery):
    try:
        r = get_user(cq.from_user.id)
        if not r or r["status"] != "active":
            await cq.message.answer("❌ No active subscription.\nUse *Buy Subscription* to get access.", parse_mode="Markdown")
        else:
            plan_name = PLANS.get(r['plan_key'], {'name':'—'})['name'] if r['plan_key'] else "—"
            await cq.message.answer(
                f"📦 *My Plan*\n"
                f"Plan: {plan_name}\n"
                f"Start: {fmt_dt(r['start_at'])}\n"
                f"End:   {fmt_dt(r['end_at'])}\n"
                f"Status: {r['status']}",
                parse_mode="Markdown"
            )
        await cq.answer()
    except Exception as e:
        log.error(f"Error in on_my_plan: {e}")
        await cq.answer("Error occurred, please try again.")

@dp.callback_query(F.data == "menu:support")
async def on_support(cq: types.CallbackQuery):
    try:
        await bot.send_message(cq.from_user.id, "📞 Please type your question/issue. I'll forward it to support.")
        await cq.answer()
    except Exception as e:
        log.error(f"Error in on_support: {e}")
        await cq.answer("Error occurred, please try again.")

# Any plain text from user → support ticket (ignore commands)
@dp.message(F.text & (F.from_user.id != ADMIN_ID))
async def on_user_text(m: types.Message):
    try:
        if m.text.startswith("/"):
            return
        upsert_user(m.from_user)
        tid = add_ticket(m.from_user.id, m.text)
        if tid > 0:
            await bot.send_message(
                ADMIN_ID,
                f"📩 *Support Ticket #{tid}*\nUser: @{m.from_user.username or m.from_user.id} (`{m.from_user.id}`)\n\n{m.text}",
                parse_mode="Markdown"
            )
            await m.answer(f"✅ Sent to support. Ticket ID: #{tid}")
        else:
            await m.answer("❌ Failed to create support ticket. Please try again later.")
    except Exception as e:
        log.error(f"Error in on_user_text: {e}")
        await m.answer("Error occurred while sending your message to support.")

# Payment proof (photo)
@dp.message(F.photo & (F.from_user.id != ADMIN_ID))
async def on_payment_photo(m: types.Message):
    try:
        plan_key = last_selected_plan.get(m.from_user.id, "plan1")
        if plan_key not in PLANS:
            await m.answer("❌ Please select a plan first using /start")
            return
            
        pid = add_payment(m.from_user.id, plan_key, m.photo[-1].file_id)
        if pid > 0:
            await bot.send_message(
                ADMIN_ID,
                f"💵 *Payment Proof #{pid}* from `{m.from_user.id}` (@{m.from_user.username or '-'})\n"
                f"Selected: {PLANS[plan_key]['name']}",
                parse_mode="Markdown"
            )
            await bot.send_photo(ADMIN_ID, m.photo[-1].file_id, reply_markup=kb_payment_actions(pid, m.from_user.id))
            await m.answer("✅ Screenshot received. Admin will review shortly.")
        else:
            await m.answer("❌ Failed to process your payment proof. Please try again.")
    except Exception as e:
        log.error(f"Error in on_payment_photo: {e}")
        await m.answer("Error occurred while processing your payment proof.")

# ───────────────────────── Admin Panel ─────────────────────────
@dp.callback_query(F.data == "admin:menu")
async def admin_menu(cq: types.CallbackQuery):
    try:
        if not is_admin(cq.from_user.id):
            await cq.answer("Admins only.", show_alert=True)
            return
        await cq.message.answer("🛠 Admin Panel", reply_markup=kb_admin_menu())
        await cq.answer()
    except Exception as e:
        log.error(f"Error in admin_menu: {e}")
        await cq.answer("Error occurred, please try again.")

@dp.callback_query(F.data == "admin:pending")
async def admin_pending(cq: types.CallbackQuery):
    try:
        if not is_admin(cq.from_user.id):
            await cq.answer("Admins only.", show_alert=True)
            return
        rows = pending_payments(10)
        if not rows:
            await cq.message.answer("✅ No pending payments.")
            await cq.answer()
            return
        for r in rows:
            plan_name = PLANS.get(r['plan_key'], {'name':'Unknown'})['name']
            cap = f"💵 Payment #{r['id']} from `{r['user_id']}` (pending)\nSelected: {plan_name}"
            await cq.message.answer(cap, reply_markup=kb_payment_actions(r["id"], r["user_id"]))
        await cq.answer()
    except Exception as e:
        log.error(f"Error in admin_pending: {e}")
        await cq.answer("Error occurred, please try again.")

@dp.callback_query(F.data.startswith("admin:approve:"))
async def admin_approve(cq: types.CallbackQuery):
    try:
        if not is_admin(cq.from_user.id):
            await cq.answer("Admins only.", show_alert=True)
            return
        parts = cq.data.split(":")
        if len(parts) != 5:
            await cq.answer("Invalid action format.", show_alert=True)
            return
            
        _, _, pid, uid, plan_key = parts
        pid = int(pid)
        uid = int(uid)
        
        if plan_key not in PLANS:
            await cq.answer("Unknown plan.", show_alert=True)
            return

        set_payment_status(pid, "approved")
        _, end = set_subscription(uid, plan_key, PLANS[plan_key]["days"])

        # Create one-time invite link
        try:
            link = await bot.create_chat_invite_link(CHANNEL_ID, member_limit=1)
            await bot.send_message(uid,
                f"🎉 Payment approved!\nPlan: {PLANS[plan_key]['name']}\n"
                f"Valid till: {end.astimezone().strftime('%Y-%m-%d %H:%M')}\n"
                f"👉 Join: {link.invite_link}")
        except Exception as e:
            log.error(f"Invite link error: {e}")
            await bot.send_message(uid,
                f"🎉 Payment approved!\nPlan: {PLANS[plan_key]['name']}\n"
                f"Valid till: {end.astimezone().strftime('%Y-%m-%d %H:%M')}")

        await cq.message.answer(f"✅ Approved payment #{pid} for user {uid} → {PLANS[plan_key]['name']}")
        await cq.answer("Approved.")
    except Exception as e:
        log.error(f"Error in admin_approve: {e}")
        await cq.answer("Error occurred while approving payment.")

@dp.callback_query(F.data.startswith("admin:deny:"))
async def admin_deny(cq: types.CallbackQuery):
    try:
        if not is_admin(cq.from_user.id):
            await cq.answer("Admins only.", show_alert=True)
            return
        parts = cq.data.split(":")
        if len(parts) != 4:
            await cq.answer("Invalid action format.", show_alert=True)
            return
            
        _, _, pid, uid = parts
        set_payment_status(int(pid), "denied")
        try:
            await bot.send_message(int(uid), "❌ Your payment proof was not approved. Please contact support.")
        except Exception:
            pass
        await cq.message.answer(f"❌ Denied payment #{pid} for user {uid}.")
        await cq.answer("Denied.")
    except Exception as e:
        log.error(f"Error in admin_deny: {e}")
        await cq.answer("Error occurred while denying payment.")

@dp.callback_query(F.data == "admin:users")
async def admin_users(cq: types.CallbackQuery):
    try:
        if not is_admin(cq.from_user.id):
            await cq.answer("Admins only.", show_alert=True)
            return
        rows = list_users(50)
        if not rows:
            await cq.message.answer("No users yet.")
            await cq.answer()
            return
        lines = []
        for r in rows:
            plan_name = PLANS.get(r["plan_key"], {"name": "—"})["name"] if r["plan_key"] else "—"
            lines.append(f"`{r['user_id']}` @{r['username'] or '-'} | {plan_name} | {fmt_dt(r['end_at'])} | {r['status']}")
        text = "👥 *Users (top 50)*\n" + "\n".join(lines)
        if len(text) > 4000:  # Telegram message limit
            text = text[:4000] + "..."
        await cq.message.answer(text, parse_mode="Markdown")
        await cq.answer()
    except Exception as e:
        log.error(f"Error in admin_users: {e}")
        await cq.answer("Error occurred, please try again.")

@dp.callback_query(F.data == "admin:stats")
async def admin_stats(cq: types.CallbackQuery):
    try:
        if not is_admin(cq.from_user.id):
            await cq.answer("Admins only.", show_alert=True)
            return
        total, active, expired, pending = stats()
        await cq.message.answer(
            f"📊 *Stats*\nUsers: {total}\nActive: {active}\nExpired: {expired}\nPending payments: {pending}",
            parse_mode="Markdown"
        )
        await cq.answer()
    except Exception as e:
        log.error(f"Error in admin_stats: {e}")
        await cq.answer("Error occurred, please try again.")

# Broadcast
@dp.callback_query(F.data == "admin:broadcast")
async def bc_start(cq: types.CallbackQuery, state: FSMContext):
    try:
        if not is_admin(cq.from_user.id):
            await cq.answer("Admins only.", show_alert=True)
            return
        await cq.message.answer("✍️ Send the broadcast message (text).")
        await state.set_state(BCast.waiting_text)
        await cq.answer()
    except Exception as e:
        log.error(f"Error in bc_start: {e}")
        await cq.answer("Error occurred, please try again.")

@dp.message(BCast.waiting_text)
async def bc_send(m: types.Message, state: FSMContext):
    try:
        if not is_admin(m.from_user.id):
            await state.clear()
            return
        with db() as c:
            rows = c.execute("SELECT user_id FROM users").fetchall()
        sent = 0
        fail = 0
        for r in rows:
            try:
                await bot.send_message(r["user_id"], m.text)
                sent += 1
                # Small delay to avoid hitting rate limits
                await asyncio.sleep(0.05)
            except Exception:
                fail += 1
        await m.answer(f"📢 Broadcast done. Sent: {sent}, Failed: {fail}")
        await state.clear()
    except Exception as e:
        log.error(f"Error in bc_send: {e}")
        await m.answer("Error occurred during broadcast.")
        await state.clear()

# Quick admin reply
@dp.callback_query(F.data.startswith("admin:reply:"))
async def admin_reply_hint(cq: types.CallbackQuery):
    try:
        if not is_admin(cq.from_user.id):
            await cq.answer("Admins only.", show_alert=True)
            return
        uid = int(cq.data.split(":")[2])
        await cq.message.answer(f"Reply with:\n`/reply {uid} <message>`", parse_mode="Markdown")
        await cq.answer()
    except Exception as e:
        log.error(f"Error in admin_reply_hint: {e}")
        await cq.answer("Error occurred, please try again.")

@dp.message(Command("reply"))
async def admin_reply_cmd(m: types.Message):
    try:
        if not is_admin(m.from_user.id):
            return
        parts = m.text.split(maxsplit=2)
        if len(parts) < 3:
            await m.answer("Usage: /reply <user_id> <message>")
            return
        _, uid, text = parts
        await bot.send_message(int(uid), f"📞 Support:\n{text}")
        await m.answer("✅ Sent.")
    except Exception as e:
        log.error(f"Error in admin_reply_cmd: {e}")
        await m.answer("Error occurred while sending reply.")

# ───────────────────────── Auto-Expiry Worker ─────────────────────────
async def expiry_worker():
    """Every 30 min:
       - 3-day reminders
       - mark expired
       - remove expired from channel (ban/unban)
    """
    log.info("Starting expiry worker...")
    while True:
        try:
            now = datetime.now(timezone.utc)
            rows = list_users(10000)
            
            for r in rows:
                uid = r["user_id"]
                end_at = r["end_at"]
                status = r["status"]
                reminded = r.get("reminded_3d", 0)

                if end_at:
                    try:
                        end = datetime.fromisoformat(end_at)
                    except Exception:
                        continue

                    # 3-day reminder
                    if status == "active" and not reminded and end > now and (end - now) <= timedelta(days=3):
                        try:
                            await bot.send_message(uid, "⏳ Your subscription expires in ~3 days. Renew via /start.")
                            mark_reminded(uid)
                            log.info(f"Sent 3-day reminder to user {uid}")
                        except Exception as e:
                            log.error(f"Failed to send reminder to user {uid}: {e}")

                    # Expired
                    if end <= now and status != "expired":
                        set_status(uid, "expired")
                        log.info(f"Marked user {uid} as expired")
                        
                        # Try to remove from channel
                        try:
                            await bot.ban_chat_member(CHANNEL_ID, uid)
                            await bot.unban_chat_member(CHANNEL_ID, uid)
                            log.info(f"Removed user {uid} from channel")
                        except Exception as e:
                            log.error(f"Failed to remove user {uid} from channel: {e}")
                        
                        # Notify user
                        try:
                            await bot.send_message(uid, "❌ Your subscription expired. Use /start to renew.")
                        except Exception as e:
                            log.error(f"Failed to notify expired user {uid}: {e}")
                            
        except Exception as e:
            log.exception(f"expiry_worker error: {e}")
        
        await asyncio.sleep(1800)  # 30 minutes

# ───────────────────────── Signal handlers for graceful shutdown ─────────────────────────
def signal_handler(sig, frame):
    log.info(f"Received signal {sig}, shutting down gracefully...")
    sys.exit(0)

# ───────────────────────── Main runner ─────────────────────────
async def start_fastapi():
    """Start FastAPI server"""
    port = int(os.getenv("PORT", "8080"))
    config = uvicorn.Config(app=app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    log.info(f"Starting FastAPI server on port {port}")
    await server.serve()

async def main():
    """Main function to run both bot and web server concurrently"""
    try:
        log.info("Initializing database...")
        init_db()
        
        log.info("Starting services...")
        
        # Create tasks for bot polling, expiry worker, and web server
        tasks = [
            asyncio.create_task(dp.start_polling(bot)),
            asyncio.create_task(expiry_worker()),
            asyncio.create_task(start_fastapi())
        ]
        
        # Run all tasks concurrently
        await asyncio.gather(*tasks)
        
    except KeyboardInterrupt:
        log.info("Received keyboard interrupt, shutting down...")
    except Exception as e:
        log.exception(f"Unexpected error in main: {e}")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run the main function
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Application stopped by user")
    except Exception as e:
        log.exception(f"Fatal error: {e}")
        sys.exit(1)

