import asyncio
import hashlib
import hmac
import html
import json
import logging
import math
import os
import random
import string
import time
import uuid

import aiosqlite
from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(i) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip()]
RP_KEY = os.getenv("ROLLYPAY_API_KEY", "")
RP_SECRET = os.getenv("ROLLYPAY_SIGNING_SECRET", "")
RP_TERMINAL = os.getenv("ROLLYPAY_TERMINAL_ID", "")
RP_BASE = os.getenv("ROLLYPAY_BASE_URL", "https://rollypay.io")
ALLOW_TEST = os.getenv("ALLOW_TEST_PAYMENTS", "0") == "1"
H1_URL = os.getenv("H1CLOUD_API_URL", "").rstrip("/")
H1_TOKEN = os.getenv("H1CLOUD_API_TOKEN", "")
BASE_URL = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "10000"))
TG_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:48]
BRAND = "VPN SERVICE"
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://botvpnzayou.onrender.com")

PLANS = {
    "m1": ("1 месяц", 30, 79),
    "m3": ("3 месяца", 90, 219),
    "m6": ("6 месяцев", 180, 399),
}

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

db = None
http = None

def esc(s):
    return html.escape(str(s or ""))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, created INTEGER, promo_code TEXT,
  banned INTEGER DEFAULT 0, vpn_name TEXT, expires_at INTEGER DEFAULT 0,
  sub_link TEXT, balance REAL DEFAULT 0, invited_by INTEGER
);
CREATE TABLE IF NOT EXISTS orders(
  order_id TEXT PRIMARY KEY, user_id INTEGER, target_id INTEGER, plan TEXT, days INTEGER, amount REAL,
  promo TEXT, payment_id TEXT, pay_url TEXT, status TEXT, created INTEGER, paid_at INTEGER
);
CREATE TABLE IF NOT EXISTS promos(
  code TEXT PRIMARY KEY, percent INTEGER, max_uses INTEGER DEFAULT 0, used INTEGER DEFAULT 0, active INTEGER DEFAULT 1
);
"""

async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()

async def q1(sql, *a):
    async with db.execute(sql, a) as c:
        return await c.fetchone()

async def qa(sql, *a):
    async with db.execute(sql, a) as c:
        return await c.fetchall()

async def ex(sql, *a):
    async with db.execute(sql, a) as c:
        n = c.rowcount
    await db.commit()
    return n

async def upsert_user(uid, username):
    await ex("INSERT OR IGNORE INTO users(id,username,created) VALUES(?,?,?)", uid, username, int(time.time()))
    await ex("UPDATE users SET username=? WHERE id=?", username, uid)

async def get_user(uid):
    return await q1("SELECT * FROM users WHERE id=?", uid)

async def h1(method, path, **kw):
    try:
        async with http.request(
            method, H1_URL + path, headers={"X-API-Key": H1_TOKEN, "Authorization": f"Bearer {H1_TOKEN}"}, **kw
        ) as r:
            text = await r.text()
            try:
                data = json.loads(text)
            except ValueError:
                data = {"raw": text}
            if r.status >= 400:
                raise Exception(f"{method} {path} -> {r.status}: {text[:300]}")
            return data
    except Exception as e:
        raise e

LINK_KEYS = ("sub_url", "subscription_url", "subscription", "sub_link", "sub", "link", "url")
LINK_PREFIX = ("http://", "https://", "vless://", "vmess://", "trojan://", "ss://", "hysteria2://")

def find_link(d):
    if isinstance(d, dict):
        for k in LINK_KEYS:
            v = d.get(k)
            if isinstance(v, str) and v.startswith(LINK_PREFIX):
                return v
        for v in d.values():
            r = find_link(v)
            if r:
                return r
    elif isinstance(d, list):
        for v in d:
            r = find_link(v)
            if r:
                return r
    return None

async def grant(uid: int, days: int) -> tuple:
    u = await get_user(uid)
    now = int(time.time())
    new_exp = max(now, (u["expires_at"] if u else 0) or 0) + days * 86400
    total_days = math.ceil((new_exp - now) / 86400)
    name = (u["vpn_name"] if u else None) or f"tg{uid}"
    payload = {"name": name, "days": total_days}
    
    data = None
    try:
        data = await h1("POST", "/create", json=payload)
    except Exception as e:
        if "409" in str(e) or "user_already_exists" in str(e):
            try:
                data = await h1("PATCH", "/edit", json=payload)
            except Exception as e2:
                raise e2
        else:
            try:
                data = await h1("PATCH", "/edit", json=payload)
            except Exception:
                raise e

    if data is None:
        raise Exception("Не удалось получить ключ подписки")

    link = find_link(data)
    if not link:
        try:
            link = find_link(await h1("GET", "/info", params={"name": name}))
        except Exception:
            pass
            
    await ex(
        "UPDATE users SET vpn_name=?, expires_at=?, sub_link=COALESCE(?, sub_link) WHERE id=?",
        name, new_exp, link, uid,
    )
    return new_exp, link

async def rp(method, path, **kw):
    headers = {"X-API-Key": RP_KEY, "X-Nonce": str(uuid.uuid4())}
    async with http.request(method, RP_BASE + path, headers=headers, **kw) as r:
        text = await r.text()
        if r.status >= 400:
            raise RuntimeError(f"Payment {r.status}: {text[:300]}")
        return json.loads(text)

async def rp_create_payment(order_id, amount, desc, uid, me_username):
    body = {
        "amount": f"{amount:.2f}",
        "payment_currency": "RUB",
        "order_id": order_id,
        "description": desc,
        "customer_id": str(uid),
        "success_redirect_url": f"https://t.me/{me_username}",
        "fail_redirect_url": f"https://t.me/{me_username}",
        "metadata": {"user_id": uid},
    }
    if RP_TERMINAL:
        body["terminal_id"] = RP_TERMINAL
    return await rp("POST", "/api/v1/payments", json=body)

def main_kb(uid):
    b = InlineKeyboardBuilder()
    if MINI_APP_URL:
        b.button(text="Открыть приложение", web_app=WebAppInfo(url=MINI_APP_URL))
    b.button(text="Купить подписку", callback_data="buy")
    b.button(text="Мои подписки", callback_data="my_subs")
    b.button(text="Рефералы", callback_data="refs")
    b.button(text="Поддержка", callback_data="support")
    b.adjust(1, 2, 2)
    return b.as_markup()

@router.message(CommandStart())
async def start(m: Message):
    await upsert_user(m.from_user.id, m.from_user.username)
    await m.answer(f"Добро пожаловать в {BRAND}.\n\nБыстрый и стабильный VPN без ограничений.", reply_markup=main_kb(m.from_user.id))

@router.callback_query(F.data == "menu")
async def menu_cb(cb: CallbackQuery):
    await cb.message.edit_text(f"Главное меню {BRAND}:", reply_markup=main_kb(cb.from_user.id))
    await cb.answer()

@router.callback_query(F.data == "buy")
async def buy_cb(cb: CallbackQuery):
    b = InlineKeyboardBuilder()
    for k, (name, _, price) in PLANS.items():
        b.button(text=f"{name} - {price} руб", callback_data=f"plan:{k}")
    b.button(text="Назад", callback_data="menu")
    b.adjust(1)
    await cb.message.edit_text("Выберите тарифный план:", reply_markup=b.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("plan:"))
async def plan_selected(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    if key not in PLANS:
        return await cb.answer("Тариф не найден", show_alert=True)
    name, days, price = PLANS[key]
    uid = cb.from_user.id
    order_id = f"o{uid}_{int(time.time())}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}"
    me = (await bot.me()).username
    try:
        p = await rp_create_payment(order_id, price, f"{BRAND}: {name}", uid, me)
    except Exception as e:
        log.error("Payment creation error: %s", e)
        return await cb.answer("Ошибка создания платежа", show_alert=True)
    
    await ex(
        "INSERT INTO orders(order_id,user_id,target_id,plan,days,amount,payment_id,pay_url,status,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
        order_id, uid, uid, key, days, price, p["payment_id"], p["pay_url"], "pending", int(time.time()),
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Оплатить {price} руб", url=p["pay_url"])],
        [InlineKeyboardButton(text="Проверить оплату", callback_data=f"chk:{order_id}")],
        [InlineKeyboardButton(text="Назад", callback_data="buy")]
    ])
    await cb.message.edit_text(f"Заказ создан.\nТариф: {name}\nСумма: {price} руб\n\nНажмите кнопку для оплаты:", reply_markup=kb)
    await cb.answer()

async def process_paid_order(order_id):
    o = await q1("SELECT * FROM orders WHERE order_id=?", order_id)
    if not o or o["status"] == "paid":
        return
    await ex("UPDATE orders SET status='paid', paid_at=? WHERE order_id=?", int(time.time()), order_id)
    target_id = o["target_id"] or o["user_id"]
    await upsert_user(target_id, None)
    
    is_gift = (o["target_id"] and o["target_id"] != o["user_id"])
    
    try:
        new_exp, link = await grant(target_id, o["days"])
        b = InlineKeyboardBuilder()
        if link:
            b.button(text="Добавить в приложение", url=link)
        b.button(text="Мои подписки", callback_data="my_subs")
        b.button(text="Главное меню", callback_data="menu")
        b.adjust(1)
        
        if is_gift:
            gift_text = f"Вам подарили подписку VPN.\nДействует до: {time.strftime('%d.%m.%Y', time.gmtime(new_exp))}\n\nКлюч:\n{link}"
            try:
                await bot.send_message(target_id, gift_text, reply_markup=b.as_markup())
            except Exception:
                pass
            buyer_text = f"Подарок успешно отправлен пользователю {target_id}."
            try:
                await bot.send_message(o["user_id"], buyer_text, reply_markup=main_kb(o["user_id"]))
            except Exception:
                pass
        else:
            text = f"Оплата прошла успешно.\nПодписка активна до {time.strftime('%d.%m.%Y', time.gmtime(new_exp))}\n\nКлюч подключения:\n{link}"
            await bot.send_message(target_id, text, reply_markup=b.as_markup())
    except Exception as e:
        log.error("Grant error: %s", e)

@router.callback_query(F.data.startswith("chk:"))
async def check_payment(cb: CallbackQuery):
    order_id = cb.data.split(":", 1)[1]
    o = await q1("SELECT * FROM orders WHERE order_id=? AND user_id=?", order_id, cb.from_user.id)
    if not o:
        return await cb.answer("Заказ не найден", show_alert=True)
    if o["status"] == "paid":
        return await cb.answer("Уже оплачено", show_alert=True)
    try:
        p = await rp("GET", f"/api/v1/payments/{o['payment_id']}")
    except Exception:
        return await cb.answer("Не удалось проверить статус", show_alert=True)
    if p.get("status") == "paid":
        await process_paid_order(order_id)
        return await cb.answer("Оплата подтверждена. Ключ отправлен.")
    await cb.answer("Оплата еще не поступила", show_alert=True)

@router.callback_query(F.data == "my_subs")
async def my_subs_cb(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    now = int(time.time())
    if u and u["expires_at"] and u["expires_at"] > now:
        text = f"Активная подписка до: {time.strftime('%d.%m.%Y', time.gmtime(u['expires_at']))}\n\nКлюч подключения:\n{u['sub_link'] or 'Отсутствует'}"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="menu")]])
        await cb.message.edit_text(text, reply_markup=kb)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Купить", callback_data="buy"), InlineKeyboardButton(text="Назад", callback_data="menu")]])
        await cb.message.edit_text("У вас нет активных подписок.", reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data == "refs")
async def refs_cb(cb: CallbackQuery):
    link = f"https://t.me/{(await bot.me()).username}?start=ref_{cb.from_user.id}"
    text = f"Реферальная программа\n\nПриглашайте друзей по вашей ссылке:\n{link}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="menu")]])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data == "support")
async def support_cb(cb: CallbackQuery):
    text = "Служба поддержки\n\nЕсли у вас возникли вопросы, обратитесь к администратору."
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="menu")]])
    if SUPPORT_USERNAME:
        kb.inline_keyboard.insert(0, [InlineKeyboardButton(text="Написать в Telegram", url=f"https://t.me/{SUPPORT_USERNAME}")])
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()

@router.message(F.web_app_data)
async def web_app_data_handler(m: Message):
    try:
        data = json.loads(m.web_app_data.data)
        action = data.get("action")
        if action == "buy_plan":
            key = data.get("plan")
            price = data.get("price")
            mode = data.get("mode", "self")
            target_id = int(data.get("target_id")) if data.get("target_id") else m.from_user.id
            
            if key in PLANS:
                name, days, _ = PLANS[key]
                uid = m.from_user.id
                order_id = f"o{uid}_{int(time.time())}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}"
                me = (await bot.me()).username
                
                desc = f"{BRAND} (Подарок): {name}" if mode == "gift" else f"{BRAND}: {name}"
                p = await rp_create_payment(order_id, price, desc, uid, me)
                
                await ex(
                    "INSERT INTO orders(order_id,user_id,target_id,plan,days,amount,payment_id,pay_url,status,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    order_id, uid, target_id, key, days, price, p["payment_id"], p["pay_url"], "pending", int(time.time()),
                )
                
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"Оплатить {price} руб", url=p["pay_url"])],
                    [InlineKeyboardButton(text="Проверить оплату", callback_data=f"chk:{order_id}")]
                ])
                await m.answer(f"Сформирован заказ{' на подарок' if mode=='gift' else ''}:\nТариф: {name}\nСумма: {price} руб", reply_markup=kb)
        elif action == "open_my_subs":
            u = await get_user(m.from_user.id)
            now = int(time.time())
            if u and u["expires_at"] and u["expires_at"] > now:
                await m.answer(f"Подписка активна до: {time.strftime('%d.%m.%Y', time.gmtime(u['expires_at']))}\n\nКлюч:\n{u['sub_link']}")
            else:
                await m.answer("У вас нет активных подписок.")
        elif action == "open_promos":
            await m.answer("Введите промокод в чате с ботом.")
        elif action == "open_referrals":
            link = f"https://t.me/{(await bot.me()).username}?start=ref_{m.from_user.id}"
            await m.answer(f"Ваша реферальная ссылка:\n{link}")
        elif action == "open_support":
            await m.answer("Обратитесь в поддержку для решения вопросов.")
    except Exception as e:
        log.error("web_app_data error: %s", e)

async def on_startup(bot: Bot):
    global http
    await init_db()
    http = ClientSession(timeout=ClientTimeout(total=25))
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/tg", secret_token=TG_SECRET)
    await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])

def main():
    dp.startup.register(on_startup)
    app = web.Application()
    async def index_handler(request):
        return web.FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))
    app.router.add_get("/", index_handler)
    app.router.add_get("/index.html", index_handler)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=TG_SECRET).register(app, path="/tg")
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
