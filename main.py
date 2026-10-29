import os
import hashlib
import hmac
import json
import sqlite3
import uuid
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
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
POLICY_URL = os.getenv("POLICY_URL", "https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-08-12-99")
OFFER_URL = os.getenv("OFFER_URL", "https://telegra.ph/PUBLICHNAYA-OFERTA-08-12-15")
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "10000"))
ALLOW_TEST = os.getenv("ALLOW_TEST_PAYMENTS", "0") == "1"

BRAND = "OMNIRO VPN"
TG_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:48]

# Тарифы (можно менять)
TARIFFS = {
    "1m": {"name": "1 месяц", "days": 30, "price": 199, "traffic_gb": 0},
    "3m": {"name": "3 месяца", "days": 90, "price": 499, "traffic_gb": 0},
    "6m": {"name": "6 месяцев", "days": 180, "price": 899, "traffic_gb": 0},
    "12m": {"name": "12 месяцев", "days": 365, "price": 1499, "traffic_gb": 0},
}

# ==================== DB ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance REAL DEFAULT 0,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            key_name TEXT,
            config TEXT,
            tariff TEXT,
            expires_at TEXT,
            created_at TEXT,
            is_gift INTEGER DEFAULT 0,
            gifted_to INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            order_id TEXT PRIMARY KEY,
            user_id INTEGER,
            amount REAL,
            tariff TEXT,
            status TEXT,
            payment_id TEXT,
            created_at TEXT,
            is_gift INTEGER DEFAULT 0,
            gift_to_username TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY,
            discount_percent INTEGER,
            max_uses INTEGER,
            used INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        )
    """)
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect(DB_PATH)

# ==================== H1CLOUD ====================
async def h1_create_client(name: str, days: int, traffic_gb: int = 0) -> dict:
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        payload = {"name": name, "days": days}
        if traffic_gb > 0:
            payload["traffic_limit_gb"] = traffic_gb
        r = await client.post(
            f"{H1_URL}/create",
            headers={"Authorization": f"Bearer {H1_TOKEN}"},
            json=payload
        )
        r.raise_for_status()
        return r.json()

async def h1_get_client(name: str) -> Optional[dict]:
    async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
        try:
            r = await client.get(
                f"{H1_URL}/clients/{name}",
                headers={"Authorization": f"Bearer {H1_TOKEN}"}
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
            return data.get("client") or data
        except:
            return None

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
        "metadata": {"user_id": user_id}
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

def verify_rollypay_signature(timestamp: str, body: bytes, signature: str) -> bool:
    mac = hmac.new(RP_SECRET.encode(), digestmod=hashlib.sha256)
    mac.update(timestamp.encode())
    mac.update(b".")
    mac.update(body)
    expected = mac.hexdigest()
    return hmac.compare_digest(expected, signature)

# ==================== BOT ====================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть Mini App", web_app=WebAppInfo(url=MINI_APP_URL))],
        [InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy")],
        [InlineKeyboardButton(text="🔑 Мои подписки", callback_data="mysubs")],
        [InlineKeyboardButton(text="🎁 Подарить", callback_data="gift")],
        [InlineKeyboardButton(text="💬 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}" if SUPPORT_USERNAME else "https://t.me/")],
    ])

@dp.message(CommandStart())
async def cmd_start(message: Message):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, created_at) VALUES (?, ?, ?, ?)",
              (message.from_user.id, message.from_user.username, message.from_user.first_name, datetime.now().isoformat()))
    conn.commit()
    conn.close()

    await message.answer(
        f"<b>Добро пожаловать в {BRAND}!</b>\n\n"
        "Быстрый, стабильный и анонимный VPN.\n"
        "Открой Mini App — там всё удобно и красиво 👇",
        reply_markup=main_keyboard()
    )

@dp.callback_query(F.data == "buy")
async def cb_buy(callback: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{t['name']} — {t['price']} ₽", callback_data=f"tariff_{k}")]
        for k, t in TARIFFS.items()
    ] + [[InlineKeyboardButton(text="◀️ Назад", callback_data="back")]])
    await callback.message.edit_text("Выбери тариф:", reply_markup=kb)

@dp.callback_query(F.data.startswith("tariff_"))
async def cb_tariff(callback: types.CallbackQuery):
    tariff_key = callback.data.split("_")[1]
    tariff = TARIFFS[tariff_key]
    order_id = f"ord_{callback.from_user.id}_{uuid.uuid4().hex[:8]}"

    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO payments (order_id, user_id, amount, tariff, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (order_id, callback.from_user.id, tariff["price"], tariff_key, "created", datetime.now().isoformat()))
    conn.commit()
    conn.close()

    try:
        pay = await create_payment(order_id, tariff["price"], f"{BRAND} — {tariff['name']}", callback.from_user.id)
        pay_url = pay.get("pay_url")
        await callback.message.edit_text(
            f"<b>{tariff['name']}</b>\nСумма: <b>{tariff['price']} ₽</b>\n\nНажми кнопку ниже для оплаты:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="buy")]
            ])
        )
    except Exception as e:
        await callback.message.edit_text(f"Ошибка создания платежа: {e}")

@dp.callback_query(F.data == "mysubs")
async def cb_mysubs(callback: types.CallbackQuery):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT key_name, expires_at, tariff FROM subscriptions WHERE user_id = ? ORDER BY id DESC", (callback.from_user.id,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        text = "У тебя пока нет активных подписок."
    else:
        text = "<b>Твои подписки:</b>\n\n"
        for name, exp, tar in rows:
            text += f"• <code>{name}</code>\n  Тариф: {TARIFFS.get(tar, {}).get('name', tar)}\n  До: {exp[:10]}\n\n"

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
    ]))

@dp.callback_query(F.data == "back")
async def cb_back(callback: types.CallbackQuery):
    await callback.message.edit_text(
        f"<b>{BRAND}</b>\nВыбери действие:",
        reply_markup=main_keyboard()
    )

@dp.callback_query(F.data == "gift")
async def cb_gift(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "Чтобы подарить подписку — открой Mini App и выбери «Подарить».\n"
        "Или напиши в поддержку.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Mini App", web_app=WebAppInfo(url=MINI_APP_URL))],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
        ])
    )

# ==================== FASTAPI ====================
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(dp.start_polling(bot))

@app.get("/")
async def index():
    return FileResponse("index.html")

@app.get("/api/user")
async def api_user(request: Request):
    # Простая проверка initData (упрощённо)
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    # В реальном проекте нужно полноценно валидировать
    user_id = request.query_params.get("user_id")
    if not user_id:
        raise HTTPException(400, "user_id required")
    user_id = int(user_id)

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT username, first_name FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    c.execute("SELECT key_name, expires_at, tariff, config FROM subscriptions WHERE user_id = ? ORDER BY id DESC", (user_id,))
    subs = c.fetchall()
    conn.close()

    return {
        "user_id": user_id,
        "username": row[0] if row else None,
        "first_name": row[1] if row else None,
        "subscriptions": [
            {"key_name": s[0], "expires_at": s[1], "tariff": s[2], "config": s[3]}
            for s in subs
        ],
        "tariffs": TARIFFS
    }

@app.post("/api/create_payment")
async def api_create_payment(data: dict):
    user_id = data.get("user_id")
    tariff_key = data.get("tariff")
    promo = data.get("promo", "").upper().strip()
    is_gift = data.get("is_gift", False)
    gift_to = data.get("gift_to")

    if tariff_key not in TARIFFS:
        raise HTTPException(400, "Invalid tariff")

    tariff = TARIFFS[tariff_key]
    price = tariff["price"]

    # Промокод
    if promo:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT discount_percent, max_uses, used, active FROM promocodes WHERE code = ?", (promo,))
        row = c.fetchone()
        if row and row[3] and row[2] < row[1]:
            discount = row[0]
            price = max(1, int(price * (100 - discount) / 100))
            c.execute("UPDATE promocodes SET used = used + 1 WHERE code = ?", (promo,))
            conn.commit()
        conn.close()

    order_id = f"ord_{user_id}_{uuid.uuid4().hex[:8]}"
    conn = get_db()
    c = conn.cursor()
    c.execute("""INSERT INTO payments (order_id, user_id, amount, tariff, status, created_at, is_gift, gift_to_username)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
              (order_id, user_id, price, tariff_key, "created", datetime.now().isoformat(), 1 if is_gift else 0, gift_to))
    conn.commit()
    conn.close()

    pay = await create_payment(order_id, price, f"{BRAND} — {tariff['name']}", user_id)
    return {"pay_url": pay.get("pay_url"), "order_id": order_id, "amount": price}

@app.post("/webhook/rollypay")
async def webhook_rollypay(request: Request, x_signature: str = Header(None), x_timestamp: str = Header(None)):
    body = await request.body()
    if not verify_rollypay_signature(x_timestamp or "", body, x_signature or ""):
        raise HTTPException(403, "Invalid signature")

    data = json.loads(body)
    if data.get("event_type") != "payment.paid" and data.get("status") != "paid":
        return {"ok": True}

    order_id = data.get("order_id")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id, tariff, status, is_gift, gift_to_username FROM payments WHERE order_id = ?", (order_id,))
    row = c.fetchone()
    if not row or row[2] == "paid":
        conn.close()
        return {"ok": True}

    user_id, tariff_key, _, is_gift, gift_to = row
    tariff = TARIFFS[tariff_key]

    # Создаём ключ
    key_name = f"u{user_id}_{uuid.uuid4().hex[:6]}"
    try:
        result = await h1_create_client(key_name, tariff["days"], tariff.get("traffic_gb", 0))
        config = result.get("config") or result.get("link") or result.get("vless") or str(result)
    except Exception as e:
        config = f"Ошибка создания ключа: {e}"
        key_name = "error"

    expires = (datetime.now() + timedelta(days=tariff["days"])).isoformat()

    c.execute("UPDATE payments SET status = 'paid', payment_id = ? WHERE order_id = ?",
              (data.get("payment_id"), order_id))
    c.execute("""INSERT INTO subscriptions (user_id, key_name, config, tariff, expires_at, created_at, is_gift)
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (user_id, key_name, config, tariff_key, expires, datetime.now().isoformat(), is_gift))
    conn.commit()
    conn.close()

    # Отправляем ключ пользователю
    try:
        text = (
            f"<b>✅ Оплата прошла успешно!</b>\n\n"
            f"Тариф: <b>{tariff['name']}</b>\n"
            f"Ключ: <code>{key_name}</code>\n\n"
            f"<b>Конфиг:</b>\n<code>{config}</code>\n\n"
            f"Срок до: {expires[:10]}"
        )
        await bot.send_message(user_id, text)
    except:
        pass

    return {"ok": True}

@app.get("/api/promocode")
async def check_promo(code: str):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT discount_percent, max_uses, used, active FROM promocodes WHERE code = ?", (code.upper(),))
    row = c.fetchone()
    conn.close()
    if row and row[3] and row[2] < row[1]:
        return {"valid": True, "discount": row[0]}
    return {"valid": False}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
