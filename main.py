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
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

# ───────────────────────── Настройки ─────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "8708312537:AAG1yrOZc5SWyl2JFXxsNQKE4ga8-Dl7ZRA")
ADMIN_IDS = [int(i) for i in os.getenv("ADMIN_IDS", "8995385837").split(",") if i.strip()]
RP_KEY = os.getenv("ROLLYPAY_API_KEY", "Y9ZGC5UCRFgCloE-uluVczR7o12ju6GTb-oRyHaBqRk")
RP_SECRET = os.getenv("ROLLYPAY_SIGNING_SECRET", "akjiGkcHyj6dt_WmzbSYeBBorTOnvZ1ucGQzl0BFGdk")
RP_TERMINAL = os.getenv("ROLLYPAY_TERMINAL_ID", "d21c4db8-e234-4a9b-a00a-2194420af7df")
RP_BASE = os.getenv("ROLLYPAY_BASE_URL", "https://rollypay.io")
ALLOW_TEST = os.getenv("ALLOW_TEST_PAYMENTS", "0") == "1"
H1_URL = os.getenv("H1CLOUD_API_URL", "https://s-25154.pl7.h1clayd.click/api").rstrip("/")
H1_TOKEN = os.getenv("H1CLOUD_API_TOKEN", "2ead850e5fe347fa9ed81f7838d64fc5590facdea2b94737ba7fff788bd55bb8")
BASE_URL = (os.getenv("BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").lstrip("@")
POLICY_URL = os.getenv("POLICY_URL", "https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-08-12-99")
OFFER_URL = os.getenv("OFFER_URL", "https://telegra.ph/PUBLICHNAYA-OFERTA-08-12-15")
DB_PATH = os.getenv("DB_PATH", "bot.db")
PORT = int(os.getenv("PORT", "10000"))
TG_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:48]
BRAND = "OMNIRO VPN"

MINI_APP_URL = os.getenv("MINI_APP_URL", "https://botvpnzayou.onrender.com")

PLANS = {
    "m1": ("1 месяц", 30, 79),
    "m3": ("3 месяца", 90, 219),
    "m6": ("6 месяцев", 180, 399),
}

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))
dp.include_router(admin)
dp.include_router(router)

db: aiosqlite.Connection = None
http: ClientSession = None


def esc(s) -> str:
    return html.escape(str(s or ""))


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, created INTEGER, promo_code TEXT,
  banned INTEGER DEFAULT 0, vpn_name TEXT, expires_at INTEGER DEFAULT 0,
  sub_link TEXT, notified_for INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders(
  order_id TEXT PRIMARY KEY, user_id INTEGER, target_id INTEGER, plan TEXT, days INTEGER, amount REAL,
  promo TEXT, payment_id TEXT, pay_url TEXT, status TEXT, created INTEGER, paid_at INTEGER
);
CREATE TABLE IF NOT EXISTS promos(
  code TEXT PRIMARY KEY, percent INTEGER, max_uses INTEGER DEFAULT 0,
  used INTEGER DEFAULT 0, active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS promo_uses(code TEXT, user_id INTEGER, PRIMARY KEY(code, user_id));
"""


async def init_db():
    global db
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
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


async def ex(sql, *a) -> int:
    async with db.execute(sql, a) as c:
        n = c.rowcount
    await db.commit()
    return n


async def upsert_user(uid, username):
    await ex("INSERT OR IGNORE INTO users(id,username,created) VALUES(?,?,?)", uid, username, int(time.time()))
    await ex("UPDATE users SET username=? WHERE id=?", username, uid)


async def get_user(uid):
    return await q1("SELECT * FROM users WHERE id=?", uid)


class H1Error(Exception):
    pass


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
                raise H1Error(f"{method} {path} -> {r.status}: {text[:300]}")
            return data
    except H1Error:
        raise
    except Exception as e:
        raise H1Error(f"{method} {path} failed: {e}")


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


async def grant(uid: int, days: int) -> int:
    u = await get_user(uid)
    now = int(time.time())
    new_exp = max(now, (u["expires_at"] if u else 0) or 0) + days * 86400
    total_days = math.ceil((new_exp - now) / 86400)
    name = (u["vpn_name"] if u else None) or f"tg{uid}"
    payload = {"name": name, "days": total_days}
    
    data = None
    try:
        data = await h1("POST", "/create", json=payload)
    except H1Error as e:
        if "409" in str(e) or "user_already_exists" in str(e):
            log.info("H1 user %s already exists, switching to PATCH /edit", name)
            try:
                data = await h1("PATCH", "/edit", json=payload)
            except H1Error as e2:
                log.warning("H1 edit fallback failed: %s", e2)
                raise e2
        else:
            log.warning("H1 create error: %s", e)
            try:
                data = await h1("PATCH", "/edit", json=payload)
            except Exception:
                raise e

    if data is None:
        raise H1Error("Не удалось получить ответ от H1Cloud при выдаче подписки")

    link = find_link(data)
    if not link:
        try:
            link = find_link(await h1("GET", "/info", params={"name": name}))
        except H1Error as e:
            log.warning("H1 info: %s", e)
            
    await ex(
        "UPDATE users SET vpn_name=?, expires_at=?, sub_link=COALESCE(?, sub_link) WHERE id=?",
        name, new_exp, link, uid,
    )
    return new_exp


async def rp(method, path, **kw):
    headers = {"X-API-Key": RP_KEY, "X-Nonce": str(uuid.uuid4())}
    async with http.request(method, RP_BASE + path, headers=headers, **kw) as r:
        text = await r.text()
        if r.status >= 400:
            raise RuntimeError(f"RollyPay {r.status}: {text[:300]}")
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


async def get_promo_for(uid):
    u = await get_user(uid)
    if not u or not u["promo_code"]:
        return None
    p = await q1("SELECT * FROM promos WHERE code=?", u["promo_code"])
    if p and p["active"] and (p["max_uses"] == 0 or p["used"] < p["max_uses"]):
        return p
    return None


def calc(price, promo):
    pct = promo["percent"] if promo else 0
    return max(1.0, round(price * (100 - pct) / 100, 2)), pct


def fmt_date(ts):
    return time.strftime("%d.%m.%Y %H:%M", time.gmtime(ts)) + " UTC"


async def notify_admins(text):
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, text)
        except Exception:
            pass


async def process_paid(order_id: str, amount=None) -> bool:
    o = await q1("SELECT * FROM orders WHERE order_id=?", order_id)
    if not o:
        log.warning("paid callback for unknown order %s", order_id)
        return True
    if amount is not None:
        try:
            if abs(float(amount) - o["amount"]) > 0.01:
                await notify_admins(f"⚠️ Сумма не совпала: заказ {esc(order_id)}, ждали {o['amount']}, пришло {esc(amount)}. Подписка НЕ выдана.")
                return True
        except ValueError:
            pass
    claimed = await ex(
        "UPDATE orders SET status='processing' WHERE order_id=? AND status IN ('pending','failed','expired','canceled')",
        order_id,
    )
    if not claimed:
        return True
    
    target_id = o["target_id"] or o["user_id"]
    await upsert_user(target_id, None)

    try:
        new_exp = await grant(target_id, o["days"])
    except Exception as e:
        log.exception("grant failed")
        await ex("UPDATE orders SET status='failed' WHERE order_id=?", order_id)
        await notify_admins(f"❌ Оплачено, но не удалось выдать VPN\nЗаказ: <code>{esc(order_id)}</code>\nЮзер: <code>{target_id}</code>\n{esc(e)}")
        return False

    await ex("UPDATE orders SET status='paid', paid_at=? WHERE order_id=?", int(time.time()), order_id)
    if o["promo"]:
        await ex("UPDATE promos SET used=used+1 WHERE code=?", o["promo"])
        await ex("INSERT OR IGNORE INTO promo_uses(code,user_id) VALUES(?,?)", o["promo"], o["user_id"])
        await ex("UPDATE users SET promo_code=NULL WHERE id=?", o["user_id"])
    
    u = await get_user(target_id)
    link = u["sub_link"]
    
    b = InlineKeyboardBuilder()
    if link:
        b.button(text="➕ Добавить в приложение", url=link)
    b.button(text="📦 Мои подписки", callback_data="my_subs")
    b.button(text="⬅️ Главное меню", callback_data="menu")
    b.adjust(1)

    is_gift = (o["target_id"] and o["target_id"] != o["user_id"])
    
    if is_gift:
        gift_text = f"🎁 <b>Вам подарили подписку VPN!</b>\nПодписка активна до <b>{fmt_date(new_exp)}</b>"
        try:
            await bot.send_message(target_id, gift_text, reply_markup=b.as_markup())
        except Exception:
            pass
        buyer_text = f"✅ <b>Подарок успешно отправлен!</b>\nПользователь <code>{target_id}</code> получил подписку до <b>{fmt_date(new_exp)}</b>."
        try:
            await bot.send_message(o["user_id"], buyer_text, reply_markup=main_kb(o["user_id"]))
        except Exception:
            pass
    else:
        text = (
            f"✅ <b>Оплата получена!</b>\nПодписка активна до <b>{fmt_date(new_exp)}</b>\n\n"
            "Выбери действие ниже, чтобы настроить подключение:"
        )
        try:
            await bot.send_message(o["user_id"], text, reply_markup=b.as_markup())
        except Exception:
            pass
    return True


def main_kb(uid):
    b = InlineKeyboardBuilder()
    if MINI_APP_URL:
        b.button(text="⚡ OMNIRO App", web_app=WebAppInfo(url=MINI_APP_URL))
    
    b.button(text="🛒 Купить VPN", callback_data="buy")
    b.button(text="🎁 Подарить VPN", callback_data="gift")
    b.button(text="👤 Профиль", callback_data="profile")
    b.button(text="📦 Мои подписки", callback_data="my_subs")
    b.button(text="🎟 Промокод", callback_data="promo")
    b.button(text="🆘 Поддержка", callback_data="support")
    b.button(text="📄 Документы", callback_data="docs")
    if uid in ADMIN_IDS:
        b.button(text="⚙️ Админ-панель", callback_data="adm")
    b.adjust(1, 2, 2, 2, 1)
    return b.as_markup()


def back_kb(to="menu"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=to)]])


async def show(cb: CallbackQuery, text, kb=None):
    try:
        await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await cb.message.answer(text, reply_markup=kb, disable_web_page_preview=True)


class Guard(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and not u.is_bot:
            await upsert_user(u.id, u.username)
            if u.id not in ADMIN_IDS:
                r = await q1("SELECT banned FROM users WHERE id=?", u.id)
                if r and r["banned"]:
                    return
        return await handler(event, data)


dp.message.outer_middleware(Guard())
dp.callback_query.outer_middleware(Guard())


class S(StatesGroup):
    promo = State()
    support = State()
    gift_target = State()


WELCOME = f"👋 Добро пожаловать в <b>{BRAND}</b>!\n\n⚡ Быстрый и стабильный VPN\n🔒 Без логов, без ограничений скорости\n📱 Все устройства\n\nВыбери действие:"


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(WELCOME, reply_markup=main_kb(m.from_user.id))


@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, WELCOME, main_kb(cb.from_user.id))
    await cb.answer()


@router.callback_query(F.data == "docs")
async def docs(cb: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Политика конфиденциальности", url=POLICY_URL)],
        [InlineKeyboardButton(text="📜 Публичная оферта", url=OFFER_URL)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])
    await show(cb, "📄 <b>Документы</b>", kb)
    await cb.answer()


@router.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    now = int(time.time())
    n = (await q1("SELECT COUNT(*) c FROM orders WHERE user_id=? AND status='paid'", u["id"]))["c"]
    if u["expires_at"] and u["expires_at"] > now:
        left = math.ceil((u["expires_at"] - now) / 86400)
        sub = f"🟢 Активна до <b>{fmt_date(u['expires_at'])}</b> (осталось ~{left} дн.)"
    elif u["expires_at"]:
        sub = f"🔴 Истекла {fmt_date(u['expires_at'])}"
    else:
        sub = "⚪ Нет подписки"
    text = f"👤 <b>Профиль</b>\n\nID: <code>{u['id']}</code>\nПодписка: {sub}\nПокупок: {n}"
    if u["promo_code"]:
        text += f"\n🎟 Активный промокод: <code>{esc(u['promo_code'])}</code>"
    
    b = InlineKeyboardBuilder()
    b.button(text="📦 Мои подписки", callback_data="my_subs")
    b.button(text="🛒 Купить / продлить", callback_data="buy")
    b.button(text="⬅️ Назад", callback_data="menu")
    b.adjust(1)
    await show(cb, text, b.as_markup())
    await cb.answer()


@router.callback_query(F.data == "my_subs")
async def my_subs(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    now = int(time.time())
    
    if not u or not u["expires_at"] or u["expires_at"] <= now:
        text = "📦 <b>Мои подписки</b>\n\nУ вас пока нет активных подписок. Выберите тариф для покупки."
        b = InlineKeyboardBuilder()
        b.button(text="🛒 Купить VPN", callback_data="buy")
        b.button(text="⬅️ Назад", callback_data="menu")
        b.adjust(1)
        return await show(cb, text, b.as_markup())

    left = math.ceil((u["expires_at"] - now) / 86400)
    link = u["sub_link"] or "Ссылка пока не сформирована, обратитесь в поддержку."
    
    text = (
        f"📦 <b>Ваша активная подписка:</b>\n\n"
        f"🟢 Статус: Активна\n"
        f"⏳ Срок действия: до <b>{fmt_date(u['expires_at'])}</b> (~{left} дн.)\n\n"
        f"🔑 <b>Ссылка для подключения:</b>\n<code>{esc(link)}</code>\n\n"
        f"<i>Вставьте её в любое поддерживаемое приложение (v2rayN, Hiddify, Streisand, Happ и т.д.).</i>"
    )
    
    b = InlineKeyboardBuilder()
    if u["sub_link"]:
        b.button(text="➕ Добавить в приложение", url=u["sub_link"])
    b.button(text="🛒 Продлить подписку", callback_data="buy")
    b.button(text="⬅️ В меню", callback_data="menu")
    b.adjust(1)
    await show(cb, text, b.as_markup())
    await cb.answer()


@router.callback_query(F.data == "buy")
async def buy(cb: CallbackQuery):
    promo = await get_promo_for(cb.from_user.id)
    b = InlineKeyboardBuilder()
    for k, (name, days, price) in PLANS.items():
        amount, pct = calc(price, promo)
        label = f"{name} — {amount:g} ₽" + (f" (−{pct}%)" if pct else "")
        b.button(text=label, callback_data=f"plan:{k}")
    b.button(text="🎟 Ввести промокод", callback_data="promo")
    b.button(text="⬅️ Назад", callback_data="menu")
    b.adjust(1)
    text = "🛒 <b>Выбери тариф</b>\nОплата картой / СБП / криптой. Подписка активируется автоматически."
    if promo:
        text += f"\n\n🎟 Промокод <code>{esc(promo['code'])}</code>: скидка {promo['percent']}%"
    await show(cb, text, b.as_markup())
    await cb.answer()


@router.callback_query(F.data == "gift")
async def gift_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.gift_target)
    await show(cb, "🎁 <b>Подарить подписку</b>\n\nОтправь Telegram ID пользователя, которому хочешь подарить подписку:", back_kb("menu"))
    await cb.answer()


@router.message(S.gift_target)
async def gift_target_set(m: Message, state: FSMContext):
    try:
        target_id = int((m.text or "").strip())
    except ValueError:
        return await m.answer("❌ Введите числовой Telegram ID пользователю (например: 123456789).", reply_markup=back_kb("menu"))
    
    await state.update_data(target_id=target_id)
    await state.clear()
    
    promo = await get_promo_for(m.from_user.id)
    b = InlineKeyboardBuilder()
    for k, (name, days, price) in PLANS.items():
        amount, pct = calc(price, promo)
        label = f"🎁 {name} — {amount:g} ₽"
        b.button(text=label, callback_data=f"gift_plan:{target_id}:{k}")
    b.button(text="⬅️ Отмена", callback_data="menu")
    b.adjust(1)
    
    await m.answer(f"🎁 Выберите тариф для подарка пользователю <code>{target_id}</code>:", reply_markup=b.as_markup())


@router.callback_query(F.data.startswith("gift_plan:"))
async def gift_plan(cb: CallbackQuery):
    _, target_id_str, key = cb.data.split(":")
    target_id = int(target_id_str)
    if key not in PLANS:
        return await cb.answer("Тариф не найден", show_alert=True)
    name, days, price = PLANS[key]
    promo = await get_promo_for(cb.from_user.id)
    amount, _ = calc(price, promo)
    uid = cb.from_user.id
    order_id = f"g{uid}_{int(time.time())}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}"
    me = (await bot.me()).username
    try:
        p = await rp_create_payment(order_id, amount, f"{BRAND} (Подарок для {target_id}): {name}", uid, me)
    except Exception as e:
        log.exception("create payment")
        await notify_admins(f"❌ Ошибка создания платежа: {esc(e)}")
        return await cb.answer("Не удалось создать платёж, попробуй позже", show_alert=True)
    
    await ex(
        "INSERT INTO orders(order_id,user_id,target_id,plan,days,amount,promo,payment_id,pay_url,status,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        order_id, uid, target_id, key, days, amount, promo["code"] if promo else None, p["payment_id"], p["pay_url"], "pending", int(time.time()),
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить подарок {amount:g} ₽", url=p["pay_url"])],
        [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"chk:{order_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
    ])
    await show(cb, f"🎁 <b>Заказ подарка</b>\nПолучатель: <code>{target_id}</code>\nТариф: {name}\nСумма: <b>{amount:g} ₽</b>\n\nПосле оплаты подписка сразу активируется у получателя!", kb)
    await cb.answer()


@router.callback_query(F.data.startswith("plan:"))
async def plan(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    if key not in PLANS:
        return await cb.answer("Тариф не найден", show_alert=True)
    name, days, price = PLANS[key]
    promo = await get_promo_for(cb.from_user.id)
    amount, _ = calc(price, promo)
    uid = cb.from_user.id
    order_id = f"o{uid}_{int(time.time())}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}"
    me = (await bot.me()).username
    try:
        p = await rp_create_payment(order_id, amount, f"{BRAND}: {name}", uid, me)
    except Exception as e:
        log.exception("create payment")
        await notify_admins(f"❌ Ошибка создания платежа: {esc(e)}")
        return await cb.answer("Не удалось создать платёж, попробуй позже", show_alert=True)
    await ex(
        "INSERT INTO orders(order_id,user_id,target_id,plan,days,amount,promo,payment_id,pay_url,status,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        order_id, uid, uid, key, days, amount, promo["code"] if promo else None, p["payment_id"], p["pay_url"], "pending", int(time.time()),
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатить {amount:g} ₽", url=p["pay_url"])],
        [InlineKeyboardButton(text="✅ Я оплатил(а) — проверить", callback_data=f"chk:{order_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="buy")],
    ])
    await show(cb, f"🧾 <b>Заказ</b>\nТариф: {name}\nСумма: <b>{amount:g} ₽</b>\n\nНажми «Оплатить». После оплаты подписка придёт автоматически (или нажми «проверить»).", kb)
    await cb.answer()


@router.callback_query(F.data.startswith("chk:"))
async def check(cb: CallbackQuery):
    order_id = cb.data.split(":", 1)[1]
    o = await q1("SELECT * FROM orders WHERE order_id=? AND user_id=?", order_id, cb.from_user.id)
    if not o:
        return await cb.answer("Заказ не найден", show_alert=True)
    if o["status"] == "paid":
        return await cb.answer("Уже оплачено ✅ Смотри «Мои подписки»", show_alert=True)
    try:
        p = await rp("GET", f"/api/v1/payments/{o['payment_id']}")
    except Exception:
        return await cb.answer("Не удалось проверить, попробуй через минуту", show_alert=True)
    if p.get("status") == "paid":
        await process_paid(order_id, p.get("amount"))
        return await cb.answer("Оплата найдена! Выдаю подписку…")
    await cb.answer(f"Пока не оплачено (статус: {p.get('status')})", show_alert=True)


@router.message(F.web_app_data)
async def web_app_data_handler(m: Message):
    try:
        data = json.loads(m.web_app_data.data)
        action = data.get("action")
        if action == "buy_plan":
            key = data.get("plan")
            if key in PLANS:
                name, days, price = PLANS[key]
                uid = m.from_user.id
                order_id = f"o{uid}_{int(time.time())}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}"
                me = (await bot.me()).username
                p = await rp_create_payment(order_id, price, f"{BRAND}: {name}", uid, me)
                await ex(
                    "INSERT INTO orders(order_id,user_id,target_id,plan,days,amount,promo,payment_id,pay_url,status,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    order_id, uid, uid, key, days, price, None, p["payment_id"], p["pay_url"], "pending", int(time.time()),
                )
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"💳 Оплатить {price} ₽", url=p["pay_url"])],
                    [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"chk:{order_id}")]
                ])
                await m.answer(f"🧾 Сформирован заказ из приложения:\nТариф: <b>{name}</b>\nСумма: <b>{price} ₽</b>", reply_markup=kb)
        elif action == "open_support":
            await m.answer("🆘 Напишите ваш вопрос в поддержку:", reply_markup=back_kb("menu"))
    except Exception as e:
        log.error("web_app_data error: %s", e)


@router.callback_query(F.data == "promo")
async def promo_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.promo)
    await show(cb, "🎟 Отправь промокод одним сообщением:", back_kb("menu"))
    await cb.answer()


@router.message(S.promo)
async def promo_set(m: Message, state: FSMContext):
    code = (m.text or "").strip().upper()
    p = await q1("SELECT * FROM promos WHERE code=?", code)
    if not p or not p["active"]:
        return await m.answer("❌ Промокод не найден или отключён. Попробуй ещё раз или вернись в меню.", reply_markup=back_kb())
    if p["max_uses"] and p["used"] >= p["max_uses"]:
        return await m.answer("❌ Лимит активаций промокода исчерпан.", reply_markup=back_kb())
    if await q1("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", code, m.from_user.id):
        return await m.answer("❌ Ты уже использовал(а) этот промокод.", reply_markup=back_kb())
    await ex("UPDATE users SET promo_code=? WHERE id=?", code, m.from_user.id)
    await state.clear()
    b = InlineKeyboardBuilder()
    b.button(text="🛒 Купить со скидкой", callback_data="buy")
    b.button(text="⬅️ В меню", callback_data="menu")
    b.adjust(1)
    await m.answer(f"✅ Промокод применён: скидка <b>{p['percent']}%</b> на следующую покупку.", reply_markup=b.as_markup())


@router.callback_query(F.data == "support")
async def support(cb: CallbackQuery, state: FSMContext):
    b = InlineKeyboardBuilder()
    if SUPPORT_USERNAME:
        b.button(text="💬 Написать в Telegram", url=f"https://t.me/{SUPPORT_USERNAME}")
    b.button(text="✉️ Написать здесь (в боте)", callback_data="support_write")
    b.button(text="⬅️ Назад", callback_data="menu")
    b.adjust(1)
    await show(cb, "🆘 <b>Поддержка</b>\nОпиши проблему — ответим как можно скорее.", b.as_markup())
    await cb.answer()


@router.callback_query(F.data == "support_write")
async def support_write(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.support)
    await show(cb, "✉️ Напиши сообщение для поддержки (одним сообщением):", back_kb("support"))
    await cb.answer()


@router.message(S.support)
async def support_msg(m: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Ответить", callback_data=f"ans:{m.from_user.id}")]])
    head = f"🆘 Обращение от <code>{m.from_user.id}</code> @{esc(m.from_user.username)}"
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, head)
            await m.copy_to(a, reply_markup=kb)
        except Exception:
            pass
    await m.answer("✅ Сообщение отправлено. Ответ придёт сюда.", reply_markup=main_kb(m.from_user.id))


class A(StatesGroup):
    promo_new = State()
    promo_del = State()
    grant = State()
    bc = State()
    find = State()
    ban = State()
    unban = State()
    reply = State()


def adm_kb():
    b = InlineKeyboardBuilder()
    for t, d in [
        ("📊 Статистика", "adm:stats"), ("🎟 Промокоды", "adm:promos"),
        ("🎁 Выдать подписку", "adm:grant"), ("📣 Рассылка", "adm:bc"),
        ("🔎 Найти юзера", "adm:find"), ("🚫 Бан", "adm:ban"),
        ("♻️ Разбан", "adm:unban"), ("🛠 Диагностика H1Cloud", "adm:h1"),
    ]:
        b.button(text=t, callback_data=d)
    b.button(text="⬅️ В меню", callback_data="menu")
    b.adjust(2, 2, 2, 1, 1)
    return b.as_markup()


@admin.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⚙️ <b>Админ-панель</b>", reply_markup=adm_kb())


@admin.callback_query(F.data == "adm")
async def admin_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "⚙️ <b>Админ-панель</b>", adm_kb())
    await cb.answer()


PROMPTS = {
    "promo_new": (A.promo_new, "Создание промокода. Формат:\n<code>КОД ПРОЦЕНТ МАКС_АКТИВАЦИЙ</code>\nНапример: <code>WELCOME 20 100</code> (0 = без лимита)"),
    "promo_del": (A.promo_del, "Отправь код промокода для удаления:"),
    "grant": (A.grant, "Формат: <code>ID_ПОЛЬЗОВАТЕЛЯ ДНЕЙ</code>\nНапример: <code>123456789 30</code>"),
    "bc": (A.bc, "Отправь текст рассылки (HTML разрешён). Уйдёт всем пользователям:"),
    "find": (A.find, "Отправь Telegram ID пользователя:"),
    "ban": (A.ban, "Отправь ID для бана:"),
    "unban": (A.unban, "Отправь ID для разбана:"),
}


@admin.callback_query(F.data.startswith("adm:"))
async def admin_actions(cb: CallbackQuery, state: FSMContext):
    act = cb.data.split(":")[1]
    if act in PROMPTS:
        st, text = PROMPTS[act]
        await state.set_state(st)
        await show(cb, text, back_kb("adm"))
    elif act == "stats":
        now = int(time.time())
        users = (await q1("SELECT COUNT(*) c FROM users"))["c"]
        active = (await q1("SELECT COUNT(*) c FROM users WHERE expires_at>?", now))["c"]
        paid = await q1("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM orders WHERE status='paid'")
        day = await q1("SELECT COUNT(*) c, COALESCE(SUM(amount),0) s FROM orders WHERE status='paid' AND paid_at>?", now - 86400)
        failed = (await q1("SELECT COUNT(*) c FROM orders WHERE status='failed'"))["c"]
        await show(cb, f"📊 <b>Статистика</b>\n\nПользователей: {users}\nАктивных подписок: {active}\nОплат всего: {paid['c']} на {paid['s']:.0f} ₽\nЗа 24ч: {day['c']} на {day['s']:.0f} ₽\nНевыданных (failed): {failed}", back_kb("adm"))
    elif act == "promos":
        rows = await qa("SELECT * FROM promos ORDER BY rowid DESC LIMIT 40")
        t = "🎟 <b>Промокоды</b>\n\n" + ("\n".join(f"<code>{esc(r['code'])}</code> −{r['percent']}% · {r['used']}/{r['max_uses'] or '∞'}" for r in rows) or "Пусто")
        b = InlineKeyboardBuilder()
        b.button(text="➕ Создать", callback_data="adm:promo_new")
        b.button(text="🗑 Удалить", callback_data="adm:promo_del")
        b.button(text="⬅️ Назад", callback_data="adm")
        b.adjust(2, 1)
        await show(cb, t, b.as_markup())
    elif act == "h1":
        try:
            st = await h1("GET", "/status")
            cl = await h1("GET", "/clients")
            txt = f"<b>/status</b>\n<pre>{esc(json.dumps(st, ensure_ascii=False)[:700])}</pre>\n<b>/clients</b>\n<pre>{esc(json.dumps(cl, ensure_ascii=False)[:900])}</pre>"
        except H1Error as e:
            txt = f"❌ {esc(e)}"
        await show(cb, txt, back_kb("adm"))
    await cb.answer()


@admin.message(A.promo_new)
async def a_promo_new(m: Message, state: FSMContext):
    try:
        code, pct, mx = (m.text or "").split()
        code, pct, mx = code.upper(), int(pct), int(mx)
        assert 1 <= pct <= 99 and mx >= 0
    except Exception:
        return await m.answer("Неверный формат. Пример: <code>WELCOME 20 100</code> (процент 1–99)")
    await ex("INSERT OR REPLACE INTO promos(code,percent,max_uses,used,active) VALUES(?,?,?,COALESCE((SELECT used FROM promos WHERE code=?),0),1)", code, pct, mx, code)
    await state.clear()
    await m.answer(f"✅ Промокод <code>{esc(code)}</code> −{pct}% (лимит {mx or '∞'})", reply_markup=adm_kb())


@admin.message(A.promo_del)
async def a_promo_del(m: Message, state: FSMContext):
    n = await ex("DELETE FROM promos WHERE code=?", (m.text or "").strip().upper())
    await state.clear()
    await m.answer("🗑 Удалён" if n else "Не найден", reply_markup=adm_kb())


@admin.message(A.grant)
async def a_grant(m: Message, state: FSMContext):
    try:
        uid, days = (int(x) for x in (m.text or "").split())
    except Exception:
        return await m.answer("Формат: <code>ID ДНЕЙ</code>")
    await upsert_user(uid, None)
    try:
        exp = await grant(uid, days)
    except Exception as e:
        return await m.answer(f"❌ {esc(e)}")
    await state.clear()
    u = await get_user(uid)
    try:
        await bot.send_message(uid, f"🎁 Тебе выдана подписка на {days} дн. Действует до {fmt_date(exp)}.\n" + (f"<code>{esc(u['sub_link'])}</code>" if u["sub_link"] else ""))
    except Exception:
        pass
    await m.answer(f"✅ Выдано. До {fmt_date(exp)}", reply_markup=adm_kb())


@admin.message(A.bc)
async def a_bc(m: Message, state: FSMContext):
    await state.clear()
    ids = [r["id"] for r in await qa("SELECT id FROM users WHERE banned=0")]
    ok = 0
    await m.answer(f"Рассылаю {len(ids)} пользователям…")
    for i in ids:
        try:
            await bot.send_message(i, m.html_text or m.text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"✅ Доставлено: {ok}/{len(ids)}", reply_markup=adm_kb())


@admin.message(A.find)
async def a_find(m: Message, state: FSMContext):
    try:
        u = await get_user(int((m.text or "").strip()))
    except ValueError:
        u = None
    if not u:
        return await m.answer("Не найден")
    await state.clear()
    n = (await q1("SELECT COUNT(*) c FROM orders WHERE user_id=? AND status='paid'", u["id"]))["c"]
    await m.answer(
        f"👤 <code>{u['id']}</code> @{esc(u['username'])}\nБан: {'да' if u['banned'] else 'нет'}\nДо: {fmt_date(u['expires_at']) if u['expires_at'] else '—'}\nVPN-имя: {esc(u['vpn_name'])}\nОплат: {n}\nПромокод: {esc(u['promo_code'])}",
        reply_markup=adm_kb(),
    )


@admin.message(A.ban)
@admin.message(A.unban)
async def a_ban(m: Message, state: FSMContext):
    cur = await state.get_state()
    try:
        uid = int((m.text or "").strip())
    except ValueError:
        return await m.answer("Нужен числовой ID")
    flag = 1 if cur == A.ban.state else 0
    n = await ex("UPDATE users SET banned=? WHERE id=?", flag, uid)
    await state.clear()
    await m.answer(("🚫 Забанен" if flag else "♻️ Разбанен") if n else "Пользователь не найден", reply_markup=adm_kb())


@admin.callback_query(F.data.startswith("ans:"))
async def a_ans(cb: CallbackQuery, state: FSMContext):
    await state.set_state(A.reply)
    await state.update_data(to=int(cb.data.split(":")[1]))
    await cb.message.answer("Напиши ответ пользователю:")
    await cb.answer()


@admin.message(A.reply)
async def a_reply(m: Message, state: FSMContext):
    to = (await state.get_data())["to"]
    await state.clear()
    try:
        await bot.send_message(to, "💬 <b>Ответ поддержки:</b>\n\n" + (m.html_text or ""))
        await m.answer("✅ Отправлено")
    except Exception as e:
        await m.answer(f"❌ {esc(e)}")


async def rp_webhook(request: web.Request):
    raw = await request.read()
    ts = request.headers.get("X-Timestamp", "")
    sig = request.headers.get("X-Signature", "")
    exp = hmac.new(RP_SECRET.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(exp.encode(), sig.strip().lower().encode()):
        log.warning("bad rollypay signature")
        return web.Response(status=403, text="bad signature")
    try:
        ev = json.loads(raw)
    except ValueError:
        return web.Response(status=400)
    if ev.get("test") and not ALLOW_TEST:
        return web.Response(text="ok")
    status, order_id = ev.get("status"), ev.get("order_id")
    log.info("rollypay %s %s", ev.get("event_type"), order_id)
    if status == "paid":
        ok = await process_paid(order_id, ev.get("amount"))
        return web.Response(status=200 if ok else 500, text="ok" if ok else "retry")
    if status in ("expired", "canceled"):
        await ex("UPDATE orders SET status=? WHERE order_id=? AND status='pending'", status, order_id)
    elif status in ("refunded", "chargeback"):
        await notify_admins(f"⚠️ {esc(status)} по заказу <code>{esc(order_id)}</code>. Проверь и при необходимости отключи клиента.")
    return web.Response(text="ok")


async def expiry_loop():
    while True:
        try:
            now = int(time.time())
            for u in await qa("SELECT id, expires_at FROM users WHERE expires_at>? AND expires_at<? AND notified_for!=expires_at", now, now + 3 * 86400):
                try:
                    await bot.send_message(u["id"], f"⏳ Подписка {BRAND} заканчивается {fmt_date(u['expires_at'])}. Продли в меню «Купить VPN».", reply_markup=main_kb(u["id"]))
                except Exception:
                    pass
                await ex("UPDATE users SET notified_for=? WHERE id=?", u["expires_at"], u["id"])
        except Exception:
            log.exception("expiry loop")
        await asyncio.sleep(6 * 3600)


async def on_startup(bot: Bot):
    global http
    await init_db()
    http = ClientSession(timeout=ClientTimeout(total=25))
    if not BASE_URL:
        log.error("BASE_URL / RENDER_EXTERNAL_URL не задан — вебхуки работать не будут")
        return
    await bot.set_webhook(f"{BASE_URL}/tg", secret_token=TG_SECRET, drop_pending_updates=False, allowed_updates=["message", "callback_query"])
    await bot.set_my_commands([BotCommand(command="start", description="Главное меню")])
    if RP_TERMINAL:
        try:
            await rp("PUT", f"/api/v1/terminals/{RP_TERMINAL}", json={"callback_url": f"{BASE_URL}/rollypay/webhook"})
            log.info("RollyPay callback_url set")
        except Exception as e:
            log.warning("не удалось выставить callback_url автоматически: %s", e)
    asyncio.create_task(expiry_loop())


async def on_shutdown(bot: Bot):
    if http:
        await http.close()
    if db:
        await db.close()


def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    app = web.Application()
    
    async def index_handler(request):
        return web.FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))
        
    app.router.add_get("/", index_handler)
    app.router.add_get("/index.html", index_handler)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    app.router.add_post("/rollypay/webhook", rp_webhook)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=TG_SECRET).register(app, path="/tg")
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()