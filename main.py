import os
import hashlib
import hmac
import json
import sqlite3
import uuid
import asyncio
from datetime import datetime, timedelta

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
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

BRAND = "OMNIRO VPN"

# Цены от 79
TARIFFS = {
    "1m":  {"name": "1 месяц",    "days": 30,  "price": 79},
    "3m":  {"name": "3 месяца",   "days": 90,  "price": 199},
    "6m":  {"name": "6 месяцев",  "days": 180, "price": 349},
    "12m": {"name": "12 месяцев", "days": 365, "price": 599},
}

# ==================== DB ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
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
        gift_to_username TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS promocodes (
        code TEXT PRIMARY KEY,
        discount_percent INTEGER,
        max_uses INTEGER,
        used INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1
    )""")
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect(DB_PATH)

# ==================== H1CLOUD ====================
async def h1_create_client(name: str, days: int) -> dict:
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        r = await client.post(
            f"{H1_URL}/create",
            headers={"Authorization": f"Bearer {H1_TOKEN}"},
            json={"name": name, "days": days}
        )
        r.raise_for_status()
        return r.json()

async def h1_delete_client(name: str):
    async with httpx.AsyncClient(timeout=20.0, verify=False) as client:
        try:
            await client.delete(
                f"{H1_URL}/clients/{name}",
                headers={"Authorization": f"Bearer {H1_TOKEN}"}
            )
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
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть приложение", web_app=WebAppInfo(url=MINI_APP_URL))],
        [
            InlineKeyboardButton(text="💳 Купить", callback_data="buy"),
            InlineKeyboardButton(text="🔑 Мои подписки", callback_data="mysubs")
        ],
        [InlineKeyboardButton(text="🎁 Подарить", callback_data="gift")],
        [InlineKeyboardButton(text="💬 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}" if SUPPORT_USERNAME else "https://t.me/")],
    ])

@dp.message(CommandStart())
async def cmd_start(message: Message):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, created_at) VALUES (?, ?, ?, ?)",
            (message.from_user.id, message.from_user.username, message.from_user.first_name, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB error in start:", e)

    text = (
        f"<b>Добро пожаловать в {BRAND}!</b>\n\n"
        "Быстрый, стабильный и анонимный VPN.\n\n"
        "Открой приложение — там удобный интерфейс, ключи и QR-коды."
    )
    await message.answer(text, reply_markup=main_kb())

@dp.callback_query(F.data == "buy")
async def cb_buy(callback: types.CallbackQuery):
    kb = [
        [InlineKeyboardButton(text=f"{t['name']} — {t['price']} ₽", callback_data=f"t_{k}")]
        for k, t in TARIFFS.items()
    ]
    kb.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back")])
    await callback.message.edit_text("Выбери тариф:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(F.data.startswith("t_"))
async def cb_tariff(callback: types.CallbackQuery):
    key = callback.data[2:]
    t = TARIFFS[key]
    order_id = f"ord_{callback.from_user.id}_{uuid.uuid4().hex[:8]}"

    conn = get_db()
    c = conn.cursor()
    c.execute(
        "INSERT INTO payments (order_id, user_id, amount, tariff, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (order_id, callback.from_user.id, t["price"], key, "created", datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

    try:
        pay = await create_payment(order_id, t["price"], f"{BRAND} — {t['name']}", callback.from_user.id)
        await callback.message.edit_text(
            f"<b>{t['name']}</b>\nСумма: <b>{t['price']} ₽</b>\n\nНажми кнопку ниже для оплаты:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Оплатить", url=pay["pay_url"])],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="buy")]
            ])
        )
    except Exception as e:
        await callback.message.edit_text(f"Ошибка создания платежа:\n<code>{e}</code>")
    await callback.answer()

@dp.callback_query(F.data == "mysubs")
async def cb_mysubs(callback: types.CallbackQuery):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT key_name, expires_at, tariff FROM subscriptions WHERE user_id = ? ORDER BY id DESC", (callback.from_user.id,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        text = "У тебя пока нет активных подписок.\n\nКупи первую в приложении или через кнопку «Купить»."
    else:
        text = "<b>Твои подписки:</b>\n\n"
        for name, exp, tar in rows:
            text += f"• <code>{name}</code>\n  {TARIFFS.get(tar, {}).get('name', tar)} · до {exp[:10]}\n\n"

    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Открыть приложение", web_app=WebAppInfo(url=MINI_APP_URL))],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "gift")
async def cb_gift(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "Подарить подписку удобнее в приложении.\nТам можно выбрать друга по @username.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Открыть приложение", web_app=WebAppInfo(url=MINI_APP_URL))],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "back")
async def cb_back(callback: types.CallbackQuery):
    await callback.message.edit_text(
        f"<b>{BRAND}</b>\n\nВыбери действие:",
        reply_markup=main_kb()
    )
    await callback.answer()

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
async def on_startup():
    init_db()
    # Запускаем бота в фоне
    asyncio.create_task(dp.start_polling(bot, skip_updates=True))

@app.get("/")
async def root():
    return FileResponse("index.html")

@app.get("/api/me")
async def api_me(user_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT username, first_name, created_at FROM users WHERE user_id = ?", (user_id,))
    u = c.fetchone()
    c.execute("""SELECT id, key_name, config, tariff, expires_at, is_gift 
                 FROM subscriptions WHERE user_id = ? ORDER BY id DESC""", (user_id,))
    subs = c.fetchall()
    conn.close()
    return {
        "user_id": user_id,
        "username": u[0] if u else None,
        "first_name": u[1] if u else None,
        "created_at": u[2] if u else None,
        "subscriptions": [
            {
                "id": s[0],
                "key_name": s[1],
                "config": s[2],
                "tariff": s[3],
                "tariff_name": TARIFFS.get(s[3], {}).get("name", s[3]),
                "expires_at": s[4],
                "is_gift": bool(s[5])
            } for s in subs
        ],
        "tariffs": TARIFFS
    }

@app.post("/api/create_payment")
async def api_create_payment(data: dict):
    user_id = int(data["user_id"])
    tariff_key = data["tariff"]
    promo = (data.get("promo") or "").upper().strip()
    is_gift = bool(data.get("is_gift"))
    gift_to = (data.get("gift_to") or "").lstrip("@").lower()

    if tariff_key not in TARIFFS:
        raise HTTPException(400, "Неверный тариф")

    t = TARIFFS[tariff_key]
    price = t["price"]

    if promo:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT discount_percent, max_uses, used, active FROM promocodes WHERE code = ?", (promo,))
        row = c.fetchone()
        if row and row[3] and row[2] < row[1]:
            price = max(1, int(price * (100 - row[0]) / 100))
            c.execute("UPDATE promocodes SET used = used + 1 WHERE code = ?", (promo,))
            conn.commit()
        conn.close()

    if is_gift and gift_to:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE lower(username) = ?", (gift_to,))
        row = c.fetchone()
        conn.close()
        if not row:
            raise HTTPException(400, "Пользователь с таким @username не найден. Он должен сначала написать /start боту.")

    order_id = f"ord_{user_id}_{uuid.uuid4().hex[:8]}"
    conn = get_db()
    c = conn.cursor()
    c.execute("""INSERT INTO payments (order_id, user_id, amount, tariff, status, created_at, is_gift, gift_to_username)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
              (order_id, user_id, price, tariff_key, "created", datetime.now().isoformat(), int(is_gift), gift_to or None))
    conn.commit()
    conn.close()

    pay = await create_payment(order_id, price, f"{BRAND} — {t['name']}", user_id)
    return {"pay_url": pay["pay_url"], "order_id": order_id, "amount": price}

@app.post("/api/delete_sub")
async def api_delete_sub(data: dict):
    user_id = int(data["user_id"])
    sub_id = int(data["sub_id"])
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT key_name FROM subscriptions WHERE id = ? AND user_id = ?", (sub_id, user_id))
    row = c.fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Подписка не найдена")
    key_name = row[0]
    c.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
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
    c.execute("SELECT tariff FROM subscriptions WHERE id = ? AND user_id = ?", (sub_id, user_id))
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Подписка не найдена")
    return await api_create_payment({"user_id": user_id, "tariff": row[0]})

@app.get("/api/check_user")
async def api_check_user(username: str):
    username = username.lstrip("@").lower()
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id, first_name FROM users WHERE lower(username) = ?", (username,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"exists": True, "user_id": row[0], "first_name": row[1]}
    return {"exists": False}

@app.post("/webhook/rollypay")
async def webhook_rollypay(request: Request, x_signature: str = Header(None), x_timestamp: str = Header(None)):
    body = await request.body()
    if not verify_signature(x_timestamp or "", body, x_signature or ""):
        raise HTTPException(403, "Invalid signature")

    data = json.loads(body)
    if data.get("status") != "paid" and data.get("event_type") != "payment.paid":
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
    t = TARIFFS[tariff_key]

    target_id = user_id
    if is_gift and gift_to:
        c.execute("SELECT user_id FROM users WHERE lower(username) = ?", (gift_to.lower(),))
        r = c.fetchone()
        if r:
            target_id = r[0]

    key_name = f"u{target_id}_{uuid.uuid4().hex[:6]}"
    try:
        result = await h1_create_client(key_name, t["days"])
        config = (
            result.get("config")
            or result.get("link")
            or result.get("vless")
            or result.get("uri")
            or result.get("client", {}).get("config")
            or str(result)
        )
    except Exception as e:
        config = f"Ошибка создания ключа: {e}"
        key_name = "error"

    expires = (datetime.now() + timedelta(days=t["days"])).isoformat()

    c.execute("UPDATE payments SET status = 'paid', payment_id = ? WHERE order_id = ?", (data.get("payment_id"), order_id))
    c.execute("""INSERT INTO subscriptions (user_id, key_name, config, tariff, expires_at, created_at, is_gift, gifted_from)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
              (target_id, key_name, config, tariff_key, expires, datetime.now().isoformat(), int(is_gift), user_id if is_gift else None))
    conn.commit()
    conn.close()

    try:
        text = (
            f"<b>Оплата прошла успешно!</b>\n\n"
            f"Тариф: <b>{t['name']}</b>\n"
            f"Ключ: <code>{key_name}</code>\n\n"
            f"<b>Конфиг:</b>\n<code>{config}</code>\n\n"
            f"Действует до: {expires[:10]}"
        )
        await bot.send_message(target_id, text)
        if is_gift and target_id != user_id:
            await bot.send_message(user_id, f"Ты успешно подарил подписку @{gift_to}!")
    except Exception as e:
        print("Notify error:", e)

    return {"ok": True}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
