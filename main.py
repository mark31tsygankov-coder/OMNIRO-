
import os
import json
import time
import hmac
import hashlib
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import JSONResponse
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, WebAppInfo, InlineKeyboardMarkup,
    InlineKeyboardButton
)
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("omniro")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
APP_URL = os.getenv("APP_URL", "").rstrip("/")
DATABASE = os.getenv("DATABASE", "omniro.db")

# Укажи настоящие реквизиты в переменных окружения.
ROLLYPAY_API_KEY = os.getenv("ROLLYPAY_API_KEY", "")
ROLLYPAY_API_URL = os.getenv(
    "ROLLYPAY_API_URL", "https://api.rollypay.io"
).rstrip("/")
ROLLYPAY_TERMINAL_ID = os.getenv("ROLLYPAY_TERMINAL_ID", "")

H1_API_URL = os.getenv(
    "H1_API_URL", "https://s-25154.pl7.h1clayd.click/api"
).rstrip("/")
H1_API_TOKEN = os.getenv("H1_API_TOKEN", "")
# Заполняется после сверки с документацией именно твоей панели.
H1_CREATE_PATH = os.getenv("H1_CREATE_PATH", "")

if not BOT_TOKEN:
    raise RuntimeError("Укажи BOT_TOKEN в переменных окружения.")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)
app = FastAPI(title="OMNIRO VPN API")

TARIFFS = {
    "1": {"months": 1, "price": 79},
    "3": {"months": 3, "price": 237},
    "6": {"months": 6, "price": 474},
}


def db():
    con = sqlite3.connect(DATABASE)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            vpn_key TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            gift_to INTEGER DEFAULT 0,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS payments (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            tariff TEXT NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            pay_url TEXT DEFAULT '',
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS promos (
            code TEXT PRIMARY KEY,
            discount INTEGER NOT NULL DEFAULT 0,
            days INTEGER NOT NULL DEFAULT 0,
            uses_left INTEGER NOT NULL DEFAULT 100
        );
        CREATE TABLE IF NOT EXISTS promo_uses (
            user_id INTEGER NOT NULL,
            code TEXT NOT NULL,
            PRIMARY KEY(user_id, code)
        );
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            created_at TEXT
        );
        """)


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


def upsert_user(user_id, username=""):
    with db() as con:
        con.execute(
            """INSERT INTO users(user_id, username, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username""",
            (user_id, username or "", iso(utcnow()))
        )


def validate_init_data(init_data: str):
    """Проверяет подпись Telegram Mini App."""
    if not init_data:
        raise HTTPException(401, "Открой приложение через Telegram")

    data = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Нет подписи Telegram")

    check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(data.items())
    )
    secret = hmac.new(
        b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    calculated = hmac.new(
        secret, check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Неверная подпись Telegram")

    auth_date = int(data.get("auth_date", "0"))
    if abs(time.time() - auth_date) > 86400:
        raise HTTPException(401, "Сессия Telegram устарела")

    try:
        user = json.loads(data["user"])
    except (KeyError, ValueError):
        raise HTTPException(401, "Не удалось определить пользователя")

    upsert_user(int(user["id"]), user.get("username", ""))
    return user


async def get_user(x_telegram_init_data: str = Header(default="")):
    return validate_init_data(x_telegram_init_data)


class PromoRequest(BaseModel):
    code: str


class BuyRequest(BaseModel):
    tariff: str
    promo: str = ""
    gift_to: int = 0


class TicketRequest(BaseModel):
    message: str


class GiftRequest(BaseModel):
    recipient_id: int
    tariff: str = "1"


@app.on_event("startup")
async def startup():
    init_db()
    import asyncio
    asyncio.create_task(dp.start_polling(bot))


@app.on_event("shutdown")
async def shutdown():
    await bot.session.close()


@router.message(CommandStart())
async def start(message: Message):
    upsert_user(message.from_user.id, message.from_user.username)
    keyboard = None
    if APP_URL.startswith("https://"):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Открыть OMNIRO VPN",
                web_app=WebAppInfo(url=APP_URL)
            )
        ]])
    await message.answer(
        "OMNIRO VPN\n\n"
        "Покупка подписки, промокоды, подарки и поддержка — "
        "в личном кабинете.",
        reply_markup=keyboard
    )


@app.get("/api/config")
async def config():
    return {
        "name": "OMNIRO VPN",
        "tariffs": TARIFFS,
        "legal": {
            "privacy": "https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-08-12-99",
            "offer": "https://telegra.ph/PUBLICHNAYA-OFERTA-08-12-15"
        }
    }


@app.get("/api/me")
async def me(user=__import__("fastapi").Depends(get_user)):
    uid = int(user["id"])
    with db() as con:
        subs = con.execute(
            """SELECT id, expires_at, vpn_key, status, gift_to
            FROM subscriptions WHERE user_id=? ORDER BY id DESC""",
            (uid,)
        ).fetchall()
        payments = con.execute(
            """SELECT id, amount, status, pay_url, created_at
            FROM payments WHERE user_id=? ORDER BY created_at DESC LIMIT 20""",
            (uid,)
        ).fetchall()
        tickets = con.execute(
            """SELECT id, message, status, created_at
            FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 20""",
            (uid,)
        ).fetchall()
    return {
        "user": {"id": uid, "username": user.get("username", "")},
        "subscriptions": [dict(x) for x in subs],
        "payments": [dict(x) for x in payments],
        "tickets": [dict(x) for x in tickets]
    }


@app.post("/api/promo")
async def promo(body: PromoRequest,
                user=__import__("fastapi").Depends(get_user)):
    code = body.code.strip().upper()
    with db() as con:
        row = con.execute(
            "SELECT * FROM promos WHERE code=?", (code,)
        ).fetchone()
        used = con.execute(
            "SELECT 1 FROM promo_uses WHERE user_id=? AND code=?",
            (int(user["id"]), code)
        ).fetchone()
    if not row or row["uses_left"] <= 0:
        raise HTTPException(400, "Промокод не найден или закончился")
    if used:
        raise HTTPException(400, "Ты уже использовал этот промокод")
    return {
        "code": code,
        "discount": row["discount"],
        "days": row["days"],
        "message": "Промокод действителен"
    }


@app.post("/api/buy")
async def buy(body: BuyRequest,
              user=__import__("fastapi").Depends(get_user)):
    if body.tariff not in TARIFFS:
        raise HTTPException(400, "Неизвестный тариф")

    tariff = TARIFFS[body.tariff]
    amount = tariff["price"]
    uid = int(user["id"])
    code = body.promo.strip().upper()

    if code:
        with db() as con:
            promo = con.execute(
                "SELECT * FROM promos WHERE code=?", (code,)
            ).fetchone()
            used = con.execute(
                "SELECT 1 FROM promo_uses WHERE user_id=? AND code=?",
                (uid, code)
            ).fetchone()
        if not promo or promo["uses_left"] <= 0 or used:
            raise HTTPException(400, "Промокод недействителен")
        amount = max(0, amount - amount * promo["discount"] // 100)

    if not ROLLYPAY_API_KEY or not ROLLYPAY_TERMINAL_ID:
        raise HTTPException(
            503, "Оплата не настроена: добавь реквизиты RollyPay"
        )

    # ВНИМАНИЕ: сверь endpoint и формат авторизации с документацией
    # своего кабинета RollyPay перед включением реальных платежей.
    payment_id = f"omniro-{uid}-{int(time.time())}"
    payload = {
        "terminal_id": ROLLYPAY_TERMINAL_ID,
        "amount": amount,
        "currency": "RUB",
        "order_id": payment_id,
        "description": f"OMNIRO VPN — {tariff['months']} мес.",
        "callback_url": f"{APP_URL}/payments/callback",
        "success_url": APP_URL
    }
    headers = {"Authorization": f"Bearer {ROLLYPAY_API_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{ROLLYPAY_API_URL}/api/v1/payments",
                json=payload,
                headers=headers
            )
        response.raise_for_status()
        result = response.json()
    except Exception:
        log.exception("RollyPay payment creation failed")
        raise HTTPException(
            502, "Не удалось создать платёж. Проверь настройки RollyPay."
        )

    pay_url = result.get("pay_url") or result.get("payment_url")
    if not pay_url:
        log.error("Unexpected RollyPay response: %s", result)
        raise HTTPException(502, "Провайдер не вернул ссылку оплаты")

    with db() as con:
        con.execute(
            """INSERT INTO payments(id,user_id,tariff,amount,pay_url,created_at)
            VALUES(?,?,?,?,?,?)""",
            (payment_id, uid, body.tariff, amount, pay_url, iso(utcnow()))
        )
    return {"payment_id": payment_id, "amount": amount, "pay_url": pay_url}


@app.post("/payments/callback")
async def payment_callback(request: Request):
    raw = await request.body()
    payload = await request.json()

    # Не активируем подписку по неподтверждённому webhook.
    # Добавь проверку подписи по точному алгоритму из документации RollyPay.
    # До проверки подписи этот обработчик не меняет статус платежа.
    log.warning("Received payment callback; signature verification required")
    log.info("Callback fields: %s", list(payload.keys()))
    return JSONResponse(
        {"ok": True, "note": "Webhook signature verification must be configured"},
        status_code=200
    )


@app.post("/api/support")
async def support(body: TicketRequest,
                  user=__import__("fastapi").Depends(get_user)):
    message = body.message.strip()
    if len(message) < 3 or len(message) > 2000:
        raise HTTPException(400, "Сообщение должно быть от 3 до 2000 символов")
    with db() as con:
        cur = con.execute(
            "INSERT INTO tickets(user_id,message,created_at) VALUES(?,?,?)",
            (int(user["id"]), message, iso(utcnow()))
        )
        ticket_id = cur.lastrowid

    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"Новая заявка OMNIRO VPN #{ticket_id}\n"
                f"Пользователь: {user['id']}\n\n{message}"
            )
        except Exception:
            log.exception("Cannot notify admin")
    return {"ok": True, "ticket_id": ticket_id}


@app.post("/api/gift")
async def gift(body: GiftRequest,
               user=__import__("fastapi").Depends(get_user)):
    if body.tariff not in TARIFFS:
        raise HTTPException(400, "Неизвестный тариф")
    if body.recipient_id <= 0:
        raise HTTPException(400, "Укажи корректный Telegram ID получателя")
    # Подарок оформляется как покупка, но получатель должен быть известен.
    # После подтверждённой оплаты активируй подписку на получателя.
    return {
        "ok": False,
        "message": "Подарки доступны после настройки подтверждения оплаты и выдачи ключей."
    }


@app.post("/api/admin/promo")
async def create_promo(request: Request,
                       user=__import__("fastapi").Depends(get_user)):
    if not ADMIN_ID or int(user["id"]) != ADMIN_ID:
        raise HTTPException(403, "Недостаточно прав")
    body = await request.json()
    code = str(body.get("code", "")).strip().upper()
    discount = int(body.get("discount", 0))
    days = int(body.get("days", 0))
    uses = int(body.get("uses", 100))
    if not code or not 0 <= discount <= 100 or uses < 1 or days < 0:
        raise HTTPException(400, "Проверь параметры промокода")
    with db() as con:
        con.execute(
            """INSERT OR REPLACE INTO promos(code,discount,days,uses_left)
            VALUES(?,?,?,?)""", (code, discount, days, uses)
        )
    return {"ok": True, "code": code}


@app.get("/health")
async def health():
    return {"ok": True, "service": "OMNIRO VPN"}
