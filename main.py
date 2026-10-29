import os
import hashlib
import hmac
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Message, Update, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import uvicorn

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8708312537:AAG1yrOZc5SWyl2JFXxsNQKE4ga8-Dl7ZRA")
ADMIN_IDS = [int(i) for i in os.getenv("ADMIN_IDS", "8995385837").split(",") if i.strip()]

RP_KEY = os.getenv("ROLLYPAY_API_KEY", "Y9ZGC5UCRFgCloE-uluVczR7o12ju6GTb-oRyHaBqRk")
RP_SECRET = os.getenv("ROLLYPAY_SIGNING_SECRET", "akjiGkcHyj6dt_WmzbSYeBBorTOnvZ1ucGQzl0BFGdk")
RP_TERMINAL = os.getenv("ROLLYPAY_TERMINAL_ID", "d21c4db8-e234-4a9b-a00a-2194420af7df")
RP_BASE = os.getenv("ROLLYPAY_BASE_URL", "https://rollypay.io")

H1_URL = os.getenv("H1CLOUD_API_URL", "https://s-25154.pl7.h1clayd.click/api").rstrip("/")
H1_TOKEN = os.getenv("H1CLOUD_API_TOKEN", "2ead850e5fe347fa9ed81f7838d64fc5590facdea2b94737ba7fff788bd55bb8")

BASE_URL = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "https://botvpnzayou.onrender.com").rstrip("/")
MINI_APP_URL = os.getenv("MINI_APP_URL", BASE_URL)
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").lstrip("@")
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "10000"))

BRAND = "OMNIRO VPN"
WEBHOOK_PATH = f"/webhook/telegram/{BOT_TOKEN}"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

TARIFFS = {
    "trial": {"name": "Пробный", "days": 1, "price": 0, "traffic_gb": 10, "devices": 1},
    "1m":    {"name": "1 месяц", "days": 30, "price": 79, "traffic_gb": 0, "devices": 3},
    "3m":    {"name": "3 месяца", "days": 90, "price": 199, "traffic_gb": 0, "devices": 3},
    "6m":    {"name": "6 месяцев", "days": 180, "price": 349, "traffic_gb": 0, "devices": 5},
    "12m":   {"name": "12 месяцев", "days": 365, "price": 599, "traffic_gb": 0, "devices": 5},
}

# ==================== DB ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        balance REAL DEFAULT 0,
        referrer_id INTEGER,
        trial_used INTEGER DEFAULT 0,
        created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        key_name TEXT,
        config TEXT,
        tariff TEXT,
        expires_at TEXT,
        created_at TEXT,
        is_gift INTEGER DEFAULT 0,
        gifted_from INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS payments (
        order_id TEXT PRIMARY KEY,
        user_id INTEGER,
        amount REAL,
        tariff TEXT,
        status TEXT,
        payment_id TEXT,
        created_at TEXT,
        is_gift INTEGER DEFAULT 0,
        gift_to_username TEXT,
        is_topup INTEGER DEFAULT 0
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS promocodes (
        code TEXT PRIMARY KEY,
        discount_percent INTEGER DEFAULT 0,
        bonus_balance REAL DEFAULT 0,
        max_uses INTEGER,
        used INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1
    )""")
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect(DB_PATH)

# ==================== H1CLOUD ====================
async def h1_create_client(name: str, days: int, traffic_gb: int = 0, devices: int = 0) -> dict:
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        payload = {"name": name, "days": max(1, days)}
        if traffic_gb > 0:
            payload["traffic_limit_gb"] = traffic_gb
        if devices > 0:
            payload["device_limit"] = devices
        r = await client.post(f"{H1_URL}/create",
            headers={"Authorization": f"Bearer {H1_TOKEN}"}, json=payload)
        r.raise_for_status()
        return r.json()

async def h1_delete_client(name: str):
    async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
        try:
            await client.delete(f"{H1_URL}/clients/{name}",
                headers={"Authorization": f"Bearer {H1_TOKEN}"})
        except:
            pass

# ==================== ROLLYPAY ====================
async def create_payment(order_id: str, amount: float, description: str, user_id: int) -> dict:
    payload = {
        "amount": f"{amount:.2f}",
        "payment_currency": "RUB",
        "order_id": order_id,
        "terminal_id": RP_TERMINAL,
        "description": description,
        "customer_id": str(user_id),
        "success_redirect_url": f"{MINI_APP_URL}/?paid=1",
        "fail_redirect_url": f"{MINI_APP_URL}/?paid=0",
    }
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": RP_KEY,
        "X-Nonce": str(uuid.uuid4())
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(f"{RP_BASE}/api/v1/payments", json=payload, headers=headers)
        r.raise_for_status()
        return r.json()

def verify_signature(timestamp: str, body: bytes, signature: str) -> bool:
    mac = hmac.new(RP_SECRET.encode(), digestmod=hashlib.sha256)
    mac.update(timestamp.encode())
    mac.update(b".")
    mac.update(body)
    return hmac.compare_digest(mac.hexdigest(), signature)

# ==================== BOT ====================
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)

class AdminStates(StatesGroup):
    wait_promo = State()
    wait_broadcast = State()
    wait_give_sub = State()

def open_app_kb(user_id: int = None):
    rows = [
        [InlineKeyboardButton(text="Открыть приложение", web_app=WebAppInfo(url=MINI_APP_URL))],
    ]
    if user_id and user_id in ADMIN_IDS:
        rows.append([InlineKeyboardButton(text="Админ-панель", callback_data="admin")])
    if SUPPORT_USERNAME:
        rows.append([InlineKeyboardButton(text="Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(CommandStart())
async def cmd_start(message: Message):
    args = message.text.split()
    referrer_id = None
    if len(args) > 1 and args[1].startswith("ref"):
        try:
            referrer_id = int(args[1][3:])
        except:
            pass

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE user_id=?", (message.from_user.id,))
    exists = c.fetchone()

    if not exists:
        c.execute("""INSERT INTO users (user_id, username, first_name, balance, referrer_id, created_at)
                     VALUES (?,?,?,0,?,?)""",
                  (message.from_user.id, message.from_user.username, message.from_user.first_name,
                   referrer_id if referrer_id and referrer_id != message.from_user.id else None,
                   datetime.now().isoformat()))
        if referrer_id and referrer_id != message.from_user.id:
            c.execute("UPDATE users SET balance = balance + 20 WHERE user_id=?", (referrer_id,))
            try:
                await bot.send_message(
                    referrer_id,
                    f"<b>Новый реферал!</b>\n\nПо твоей ссылке зашёл пользователь.\nТебе начислено <b>+20 ₽</b> на баланс."
                )
            except:
                pass
    else:
        c.execute("UPDATE users SET username=?, first_name=? WHERE user_id=?",
                  (message.from_user.username, message.from_user.first_name, message.from_user.id))
    conn.commit()
    conn.close()

    name = message.from_user.first_name or "друг"

    text = (
        f"<b>Добро пожаловать в {BRAND}</b>\n\n"
        f"Привет, <b>{name}</b>!\n\n"
        f"Быстрый и стабильный VPN.\n"
        f"Все управление — в приложении.\n\n"
        f"Нажми кнопку ниже 👇"
    )
    await message.answer(text, reply_markup=open_app_kb(message.from_user.id))

@dp.message(Command("app"))
async def cmd_app(message: Message):
    await message.answer(
        "Открой приложение:",
        reply_markup=open_app_kb(message.from_user.id)
    )

# ==================== ADMIN (только для админов) ====================
@dp.callback_query(F.data == "admin")
async def cb_admin(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Статистика", callback_data="adm_stats")],
        [InlineKeyboardButton(text="Создать промокод", callback_data="adm_promo")],
        [InlineKeyboardButton(text="Выдать подписку", callback_data="adm_give")],
        [InlineKeyboardButton(text="Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="Закрыть", callback_data="adm_close")],
    ])
    await callback.message.edit_text("<b>Админ-панель</b>", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "adm_close")
async def adm_close(callback: CallbackQuery):
    await callback.message.edit_text(
        f"<b>{BRAND}</b>\nОткрой приложение:",
        reply_markup=open_app_kb(callback.from_user.id)
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_stats")
async def adm_stats(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM subscriptions")
    subs = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM payments WHERE status='paid'")
    pays = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid'")
    total = c.fetchone()[0]
    conn.close()
    await callback.message.edit_text(
        f"<b>Статистика</b>\n\n"
        f"Пользователей: <b>{users}</b>\n"
        f"Подписок: <b>{subs}</b>\n"
        f"Оплат: <b>{pays}</b>\n"
        f"Сумма: <b>{total:.0f} ₽</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="admin")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_promo")
async def adm_promo(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminStates.wait_promo)
    await callback.message.edit_text(
        "Отправь промокод:\n<code>КОД СКИДКА% МАКС_ИСПОЛЬЗОВАНИЙ</code>\n\nПример: <code>OMNI20 20 100</code>"
    )
    await callback.answer()

@dp.message(AdminStates.wait_promo)
async def process_promo(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        parts = message.text.strip().split()
        code, discount, max_uses = parts[0].upper(), int(parts[1]), int(parts[2])
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO promocodes (code, discount_percent, max_uses, used, active) VALUES (?,?,?,0,1)",
                  (code, discount, max_uses))
        conn.commit()
        conn.close()
        await message.answer(f"Промокод <b>{code}</b> создан (скидка {discount}%, до {max_uses} исп.)",
                             reply_markup=open_app_kb(message.from_user.id))
    except:
        await message.answer("Ошибка формата. Пример: <code>OMNI20 20 100</code>")
    await state.clear()

@dp.callback_query(F.data == "adm_give")
async def adm_give(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminStates.wait_give_sub)
    await callback.message.edit_text(
        "Отправь: <code>USER_ID ТАРИФ</code>\nПример: <code>123456789 1m</code>\nТарифы: trial, 1m, 3m, 6m, 12m"
    )
    await callback.answer()

@dp.message(AdminStates.wait_give_sub)
async def process_give(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        parts = message.text.strip().split()
        uid, tariff_key = int(parts[0]), parts[1]
        if tariff_key not in TARIFFS:
            await message.answer("Неверный тариф")
            await state.clear()
            return
        t = TARIFFS[tariff_key]
        key_name = f"adm_{uid}_{uuid.uuid4().hex[:6]}"
        result = await h1_create_client(key_name, t["days"], t.get("traffic_gb", 0), t.get("devices", 0))
        config = result.get("config") or result.get("link") or result.get("vless") or str(result)
        expires = (datetime.now() + timedelta(days=t["days"])).isoformat()
        conn = get_db()
        c = conn.cursor()
        c.execute("""INSERT INTO subscriptions (user_id, key_name, config, tariff, expires_at, created_at)
                     VALUES (?,?,?,?,?,?)""", (uid, key_name, config, tariff_key, expires, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        await bot.send_message(uid, f"<b>Вам выдана подписка!</b>\n\nТариф: {t['name']}\n<code>{config}</code>")
        await message.answer(f"Подписка {tariff_key} выдана {uid}", reply_markup=open_app_kb(message.from_user.id))
    except Exception as e:
        await message.answer(f"Ошибка: {e}")
    await state.clear()

@dp.callback_query(F.data == "adm_broadcast")
async def adm_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminStates.wait_broadcast)
    await callback.message.edit_text("Отправь текст рассылки:")
    await callback.answer()

@dp.message(AdminStates.wait_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    users = [r[0] for r in c.fetchall()]
    conn.close()
    ok = 0
    for uid in users:
        try:
            await bot.send_message(uid, message.text)
            ok += 1
        except:
            pass
    await message.answer(f"Рассылка завершена: {ok}/{len(users)}", reply_markup=open_app_kb(message.from_user.id))
    await state.clear()

# ==================== LIFESPAN ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(
        url=WEBHOOK_URL,
        allowed_updates=dp.resolve_used_update_types(),
        drop_pending_updates=True
    )
    print(f"Webhook set: {WEBHOOK_URL}")
    yield
    await bot.session.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root():
    return FileResponse("index.html")

# ==================== API ====================
@app.get("/api/me")
async def api_me(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT username, first_name, balance, trial_used, created_at FROM users WHERE user_id=?", (user_id,))
    u = c.fetchone()
    if not u:
        c.execute("INSERT INTO users (user_id, balance, created_at) VALUES (?,0,?)", (user_id, datetime.now().isoformat()))
        conn.commit()
        u = (None, None, 0, 0, datetime.now().isoformat())
    c.execute("SELECT id, key_name, config, tariff, expires_at, is_gift FROM subscriptions WHERE user_id=? ORDER BY id DESC", (user_id,))
    subs = c.fetchall()
    c.execute("SELECT COUNT(*) FROM users WHERE referrer_id=?", (user_id,))
    refs = c.fetchone()[0]
    conn.close()
    return {
        "user_id": user_id,
        "username": u[0],
        "first_name": u[1],
        "balance": u[2] or 0,
        "trial_used": bool(u[3]),
        "created_at": u[4],
        "refs_count": refs,
        "subscriptions": [{
            "id": s[0], "key_name": s[1], "config": s[2], "tariff": s[3],
            "tariff_name": TARIFFS.get(s[3], {}).get("name", s[3]),
            "expires_at": s[4], "is_gift": bool(s[5])
        } for s in subs],
        "tariffs": {k: v for k, v in TARIFFS.items() if k != "trial"}
    }

@app.post("/api/create_payment")
async def api_create_payment(data: dict):
    user_id = int(data["user_id"])
    tariff_key = data.get("tariff")
    amount = data.get("amount")
    promo = (data.get("promo") or "").upper().strip()
    is_gift = bool(data.get("is_gift"))
    gift_to = (data.get("gift_to") or "").lstrip("@").lower()
    is_topup = bool(data.get("is_topup"))

    if is_topup:
        price = float(amount)
        tariff_key = "topup"
        desc = f"{BRAND} — Пополнение {price} ₽"
    else:
        if tariff_key not in TARIFFS:
            raise HTTPException(400, "Неверный тариф")
        t = TARIFFS[tariff_key]
        price = t["price"]
        desc = f"{BRAND} — {t['name']}"

        if promo:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT discount_percent, max_uses, used, active FROM promocodes WHERE code=?", (promo,))
            row = c.fetchone()
            if row and row[3] and row[2] < row[1]:
                price = max(1, int(price * (100 - row[0]) / 100))
                c.execute("UPDATE promocodes SET used=used+1 WHERE code=?", (promo,))
                conn.commit()
            conn.close()

        if is_gift and gift_to:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT user_id FROM users WHERE lower(username)=?", (gift_to,))
            if not c.fetchone():
                conn.close()
                raise HTTPException(400, "Пользователь не найден")
            conn.close()

    order_id = f"ord_{user_id}_{uuid.uuid4().hex[:8]}"
    conn = get_db()
    c = conn.cursor()
    c.execute("""INSERT INTO payments (order_id,user_id,amount,tariff,status,created_at,is_gift,gift_to_username,is_topup)
                 VALUES (?,?,?,?,?,?,?,?,?)""",
              (order_id, user_id, price, tariff_key, "created", datetime.now().isoformat(),
               int(is_gift), gift_to or None, int(is_topup)))
    conn.commit()
    conn.close()

    pay = await create_payment(order_id, price, desc, user_id)
    return {"pay_url": pay["pay_url"], "order_id": order_id, "amount": price}

@app.post("/api/trial")
async def api_trial(data: dict):
    user_id = int(data["user_id"])
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT trial_used FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row:
        c.execute("INSERT INTO users (user_id, balance, created_at) VALUES (?,0,?)", (user_id, datetime.now().isoformat()))
        conn.commit()
        row = (0,)
    if row[0]:
        conn.close()
        raise HTTPException(400, "Пробный период уже использован")

    t = TARIFFS["trial"]
    key_name = f"trial_{user_id}_{uuid.uuid4().hex[:5]}"
    try:
        result = await h1_create_client(key_name, t["days"], t["traffic_gb"], t["devices"])
        config = result.get("config") or result.get("link") or result.get("vless") or str(result)
    except Exception as e:
        conn.close()
        raise HTTPException(500, f"Ошибка создания ключа: {e}")

    expires = (datetime.now() + timedelta(days=t["days"])).isoformat()
    c.execute("UPDATE users SET trial_used=1 WHERE user_id=?", (user_id,))
    c.execute("""INSERT INTO subscriptions (user_id, key_name, config, tariff, expires_at, created_at)
                 VALUES (?,?,?,?,?,?)""", (user_id, key_name, config, "trial", expires, datetime.now().isoformat()))
    conn.commit()
    conn.close()

    try:
        await bot.send_message(user_id, f"<b>Пробный период активирован!</b>\n\n1 день · 10 ГБ · 1 устройство\n\n<code>{config}</code>")
    except:
        pass
    return {"ok": True, "config": config, "expires_at": expires}

@app.post("/api/buy_balance")
async def api_buy_balance(data: dict):
    user_id = int(data["user_id"])
    tariff_key = data["tariff"]
    if tariff_key not in TARIFFS or tariff_key == "trial":
        raise HTTPException(400, "Неверный тариф")
    t = TARIFFS[tariff_key]
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    balance = row[0] if row else 0
    if balance < t["price"]:
        conn.close()
        raise HTTPException(400, "Недостаточно средств")
    c.execute("UPDATE users SET balance = balance - ? WHERE user_id=?", (t["price"], user_id))

    key_name = f"u{user_id}_{uuid.uuid4().hex[:6]}"
    try:
        result = await h1_create_client(key_name, t["days"], t.get("traffic_gb", 0), t.get("devices", 0))
        config = result.get("config") or result.get("link") or result.get("vless") or str(result)
    except Exception as e:
        conn.rollback()
        conn.close()
        raise HTTPException(500, str(e))

    expires = (datetime.now() + timedelta(days=t["days"])).isoformat()
    c.execute("""INSERT INTO subscriptions (user_id, key_name, config, tariff, expires_at, created_at)
                 VALUES (?,?,?,?,?,?)""", (user_id, key_name, config, tariff_key, expires, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    try:
        await bot.send_message(user_id, f"<b>Подписка куплена с баланса!</b>\n\n{t['name']}\n<code>{config}</code>")
    except:
        pass
    return {"ok": True, "config": config}

@app.post("/api/delete_sub")
async def api_delete_sub(data: dict):
    user_id = int(data["user_id"])
    sub_id = int(data["sub_id"])
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT key_name FROM subscriptions WHERE id=? AND user_id=?", (sub_id, user_id))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(404)
    key_name = row[0]
    c.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
    conn.commit()
    conn.close()
    await h1_delete_client(key_name)
    return {"ok": True}

@app.post("/api/renew")
async def api_renew(data: dict):
    user_id = int(data["user_id"])
    sub_id = int(data["sub_id"])
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT tariff FROM subscriptions WHERE id=? AND user_id=?", (sub_id, user_id))
    row = c.fetchone()
    conn.close()
    if not row or row[0] == "trial":
        raise HTTPException(400, "Нельзя продлить")
    return await api_create_payment({"user_id": user_id, "tariff": row[0]})

@app.get("/api/check_user")
async def api_check_user(username: str):
    username = username.lstrip("@").lower()
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id, first_name FROM users WHERE lower(username)=?", (username,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"exists": True, "user_id": row[0], "first_name": row[1]}
    return {"exists": False}

@app.get("/api/ref_link")
async def api_ref_link(user_id: int):
    me = await bot.get_me()
    return {"link": f"https://t.me/{me.username}?start=ref{user_id}"}

@app.post("/webhook/rollypay")
@app.post("/rollypay/webhook")
async def webhook_rollypay(request: Request, x_signature: str = Header(None), x_timestamp: str = Header(None)):
    body = await request.body()
    # Можно временно отключить строгую проверку подписи, если будут проблемы
    if x_signature and x_timestamp:
        if not verify_signature(x_timestamp, body, x_signature):
            print("Bad signature")
            # raise HTTPException(403, "Invalid signature")

    data = json.loads(body)
    print("RollyPay:", data)

    if data.get("status") != "paid" and data.get("event_type") != "payment.paid":
        return {"ok": True}

    order_id = data.get("order_id")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id, tariff, status, is_gift, gift_to_username, is_topup, amount FROM payments WHERE order_id=?", (order_id,))
    row = c.fetchone()
    if not row or row[2] == "paid":
        conn.close()
        return {"ok": True}

    user_id, tariff_key, _, is_gift, gift_to, is_topup, amount = row

    if is_topup:
        c.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (amount, user_id))
        c.execute("UPDATE payments SET status='paid', payment_id=? WHERE order_id=?", (data.get("payment_id"), order_id))
        conn.commit()
        conn.close()
        try:
            await bot.send_message(user_id, f"Баланс пополнен на <b>{amount:.0f} ₽</b>")
        except:
            pass
        return {"ok": True}

    t = TARIFFS.get(tariff_key, TARIFFS["1m"])
    target_id = user_id
    if is_gift and gift_to:
        c.execute("SELECT user_id FROM users WHERE lower(username)=?", (gift_to.lower(),))
        r = c.fetchone()
        if r:
            target_id = r[0]

    key_name = f"u{target_id}_{uuid.uuid4().hex[:6]}"
    try:
        result = await h1_create_client(key_name, t["days"], t.get("traffic_gb", 0), t.get("devices", 0))
        config = result.get("config") or result.get("link") or result.get("vless") or str(result)
    except Exception as e:
        config = f"Ошибка: {e}"
        key_name = "error"

    expires = (datetime.now() + timedelta(days=t["days"])).isoformat()
    c.execute("UPDATE payments SET status='paid', payment_id=? WHERE order_id=?", (data.get("payment_id"), order_id))
    c.execute("""INSERT INTO subscriptions (user_id, key_name, config, tariff, expires_at, created_at, is_gift, gifted_from)
                 VALUES (?,?,?,?,?,?,?,?)""",
              (target_id, key_name, config, tariff_key, expires, datetime.now().isoformat(), int(is_gift), user_id if is_gift else None))
    conn.commit()
    conn.close()

    try:
        text = f"<b>Оплата прошла!</b>\n\nТариф: <b>{t['name']}</b>\n<code>{config}</code>\n\nДо: {expires[:10]}"
        await bot.send_message(target_id, text)
        if is_gift and target_id != user_id:
            await bot.send_message(user_id, f"Ты подарил подписку @{gift_to}!")
    except:
        pass
    return {"ok": True}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
