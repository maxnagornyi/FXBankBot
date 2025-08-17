import os
import logging
import asyncio
from contextlib import suppress
from decimal import Decimal
from typing import Optional, Dict, List

from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.types import (
    Message, Update, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.redis import RedisStorage, DefaultKeyBuilder
from aiogram.fsm.storage.memory import MemoryStorage

import redis.asyncio as aioredis


# =========================
# ENV & Logging
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://fxbankbot.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "fxbank-secret")
REDIS_URL = os.getenv("REDIS_URL")      # rediss://default:pass@host:6379
BANK_PASSWORD = os.getenv("BANK_PASSWORD", "letmein")

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "10000"))

# Anti-silence toggles
ASYNC_UPDATES = os.getenv("ASYNC_UPDATES", "true").lower() == "true"
STRICT_HEADER = os.getenv("STRICT_HEADER", "false").lower() == "true"
ENABLE_WATCHDOG = os.getenv("ENABLE_WATCHDOG", "true").lower() == "true"
WEBHOOK_WATCHDOG_INTERVAL = int(os.getenv("WEBHOOK_WATCHDOG_INTERVAL", "60"))

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("FXBankBot")


# =========================
# FastAPI app
# =========================
app = FastAPI(title="FXBankBot")
app.state.mode: Optional[str] = None
app.state.watchdog_task: Optional[asyncio.Task] = None

# =========================
# Aiogram globals
# =========================
bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None
router = Router()

# =========================
# Bank role sessions
# =========================
BANK_USERS = set()  # telegram user_ids with bank role (in-memory sessions)

# =========================
# FSM States (client & bank)
# =========================
class DealFSM(StatesGroup):
    client_name = State()
    operation_type = State()      # buy / sell / convert
    currency_from = State()       # for sell or convert
    currency_to = State()         # for buy or convert
    conversion_mode = State()     # for convert: "sell" or "buy"
    amount = State()              # numeric string
    rate = State()                # numeric string

class BankFSM(StatesGroup):
    counter_order_id = State()    # waiting to input counter rate for this order
    waiting_counter_rate = State()


# =========================
# Helpers
# =========================
def parse_decimal(txt: str) -> Decimal:
    s = txt.strip().replace(" ", "").replace(",", ".")
    return Decimal(s)

async def build_storage():
    if not REDIS_URL:
        log.info("REDIS_URL not set: MemoryStorage")
        return MemoryStorage()
    try:
        redis = aioredis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
        await redis.ping()
        log.info("Connected to Redis, using RedisStorage.")
        return RedisStorage(redis=redis, key_builder=DefaultKeyBuilder(with_bot_id=True, prefix="fxbank"))
    except Exception as e:
        log.warning(f"Redis unavailable: {e} — fallback to MemoryStorage")
        return MemoryStorage()

def redis_conn() -> Optional[aioredis.Redis]:
    if isinstance(dp.storage, RedisStorage):
        return dp.storage.redis
    return None


# =========================
# Orders storage (Redis + memory fallback)
# =========================
ORDER_COUNTER_KEY = "order:counter"

def order_key(order_id: int) -> str:
    return f"order:{order_id}"

def orders_index_all() -> str:
    return "orders:all"

def orders_index_status(status: str) -> str:
    return f"orders:status:{status}"

# In-memory fallback
_MEM_ORDERS: Dict[int, Dict] = {}
_MEM_COUNTER: int = 0

async def _mem_next_id() -> int:
    global _MEM_COUNTER
    _MEM_COUNTER += 1
    return _MEM_COUNTER

async def order_next_id(r: Optional[aioredis.Redis]) -> int:
    if r:
        return int(await r.incr(ORDER_COUNTER_KEY))
    return await _mem_next_id()

async def order_create(payload: Dict) -> int:
    r = redis_conn()
    oid = await order_next_id(r)
    data = {
        "id": str(oid),
        "status": payload.get("status", "new"),
        "client_id": str(payload["client_id"]),
        "client_name": payload.get("client_name", ""),
        "operation": payload.get("operation", ""),
        "currency_from": payload.get("currency_from", "") or "",
        "currency_to": payload.get("currency_to", "") or "",
        "conversion_mode": payload.get("conversion_mode", "") or "",
        "amount": payload.get("amount", ""),
        "rate": payload.get("rate", ""),
        "proposed_rate": payload.get("proposed_rate", ""),
    }
    if r:
        await r.hset(order_key(oid), mapping=data)
        await r.sadd(orders_index_all(), oid)
        await r.sadd(orders_index_status(data["status"]), oid)
    else:
        _MEM_ORDERS[oid] = data
    return oid

async def order_get(oid: int) -> Optional[Dict]:
    r = redis_conn()
    if r:
        data = await r.hgetall(order_key(oid))
        return data or None
    return _MEM_ORDERS.get(oid)

async def order_change_status(oid: int, new_status: str, extra: Dict = None) -> Optional[Dict]:
    r = redis_conn()
    if r:
        k = order_key(oid)
        exists = await r.exists(k)
        if not exists:
            return None
        old_status = await r.hget(k, "status")
        pipe = r.pipeline()
        pipe.hset(k, "status", new_status)
        if extra:
            pipe.hset(k, mapping=extra)
        pipe.srem(orders_index_status(old_status or "new"), oid)
        pipe.sadd(orders_index_status(new_status), oid)
        await pipe.execute()
        return await r.hgetall(k)
    # memory flow
    o = _MEM_ORDERS.get(oid)
    if not o:
        return None
    o["status"] = new_status
    if extra:
        o.update(extra)
    _MEM_ORDERS[oid] = o
    return o

async def orders_list(status: Optional[str] = None, limit: int = 50, client_id: Optional[int] = None) -> List[Dict]:
    r = redis_conn()
    if r:
        if status and status != "all":
            ids = list(await r.smembers(orders_index_status(status)))
        else:
            ids = list(await r.smembers(orders_index_all()))
        try:
            ids_sorted = sorted((int(x) for x in ids))
        except Exception:
            ids_sorted = ids
        result: List[Dict] = []
        for oid in ids_sorted[:limit]:
            data = await r.hgetall(order_key(int(oid)))
            if data:
                if client_id and str(client_id) != data.get("client_id"):
                    continue
                result.append(data)
        return result
    # memory flow
    items = list(_MEM_ORDERS.items())
    items.sort(key=lambda kv: int(kv[0]))
    rows: List[Dict] = []
    for oid, data in items:
        if status and status != "all" and data.get("status") != status:
            continue
        if client_id and str(client_id) != data.get("client_id"):
            continue
        rows.append(data)
        if len(rows) >= limit:
            break
    return rows


# =========================
# Keyboards
# =========================
client_main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💱 Курсы"), KeyboardButton(text="➕ Новая заявка")],
        [KeyboardButton(text="📋 Мои заявки")],
    ],
    resize_keyboard=True
)

client_order_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💵 Продажа"), KeyboardButton(text="💸 Покупка")],
        [KeyboardButton(text="🔄 Конверсия")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True
)

bank_main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Все заявки"), KeyboardButton(text="🆕 Новые заявки")],
        [KeyboardButton(text="✅ Принятые"), KeyboardButton(text="❌ Отклонённые")],
        [KeyboardButton(text="📌 Ордеры"), KeyboardButton(text="💱 Курсы")],
    ],
    resize_keyboard=True
)

def bank_order_actions_kb(oid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять", callback_data=f"bank:accept:{oid}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"bank:reject:{oid}"),
            ],
            [
                InlineKeyboardButton(text="💹 Контр-курс", callback_data=f"bank:counter:{oid}"),
                InlineKeyboardButton(text="📌 Оформить ордер", callback_data=f"bank:order:{oid}"),
            ],
        ]
    )

def client_counter_choice_kb(oid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Принять курс", callback_data=f"client:accept:{oid}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"client:reject:{oid}"),
            ]
        ]
    )


# =========================
# Handlers — CLIENT (with FSM)
# =========================
@router.message(CommandStart())
@router.message(Command("start"))
@router.message(StateFilter("*"), F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(DealFSM.client_name)
    await message.answer(
        "👋 Привет! Я FXBankBot.\n\n"
        "Сначала укажи <b>название клиента</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=client_main_kb
    )

# Универсальный запуск "Новая заявка"
@router.message(Command("new"))
@router.message(F.text == "➕ Новая заявка")
async def start_new_order(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("client_name"):
        await state.set_state(DealFSM.client_name)
        return await message.answer(
            "📝 Введите <b>название клиента</b> для начала заявки.",
            parse_mode=ParseMode.HTML
        )
    await state.set_state(DealFSM.operation_type)
    await message.answer("Выберите тип операции:", reply_markup=client_order_kb)

@router.message(DealFSM.client_name, F.text)
async def h_client_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        return await message.answer("Название клиента не может быть пустым. Введите ещё раз.")
    await state.update_data(client_name=name)
    # если была «отложенная» кнопка — игнорируем, просто показываем меню выбора
    await state.set_state(DealFSM.operation_type)
    await message.answer("Выберите тип операции:", reply_markup=client_order_kb)

# выбор типа операции из любого состояния
@router.message(StateFilter(None), F.text.in_(["💵 Продажа", "💸 Покупка", "🔄 Конверсия"]))
@router.message(StateFilter("*"), F.text.in_(["💵 Продажа", "💸 Покупка", "🔄 Конверсия"]))
async def op_choice_any_state(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("client_name"):
        await state.clear()
        await state.set_state(DealFSM.client_name)
        return await message.answer("📝 Введите <b>название клиента</b>.", parse_mode=ParseMode.HTML)

    await state.set_state(DealFSM.operation_type)
    t = message.text
    if t == "💵 Продажа":
        await state.update_data(operation="sell")
        await state.set_state(DealFSM.currency_from)
        return await message.answer("Какую валюту хотите <b>продать</b>? (например: USD, EUR)", parse_mode=ParseMode.HTML)
    if t == "💸 Покупка":
        await state.update_data(operation="buy")
        await state.set_state(DealFSM.currency_to)
        return await message.answer("Какую валюту хотите <b>купить</b>? (например: USD, EUR)", parse_mode=ParseMode.HTML)
    if t == "🔄 Конверсия":
        await state.update_data(operation="convert")
        await state.set_state(DealFSM.currency_from)
        return await message.answer("Конверсия: укажите валюту, которую <b>продаёте</b> (например: EUR)", parse_mode=ParseMode.HTML)

@router.message(F.text == "⬅️ Назад")
async def back_to_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=client_main_kb)

# --- SELL / CONVERT from ---
@router.message(DealFSM.currency_from, F.text)
async def h_currency_from(message: Message, state: FSMContext):
    cur = message.text.strip().upper()
    if len(cur) not in (3, 4):
        return await message.answer("Некорректный код валюты. Пример: USD, EUR.")
    await state.update_data(currency_from=cur)
    data = await state.get_data()
    op = data.get("operation")
    if op == "sell":
        await state.set_state(DealFSM.amount)
        return await message.answer(f"Введите сумму {cur}, которую хотите <b>продать</b>.", parse_mode=ParseMode.HTML)
    if op == "convert":
        await state.set_state(DealFSM.currency_to)
        return await message.answer("Укажите валюту, которую <b>хотите купить</b> (например: USD).", parse_mode=ParseMode.HTML)

# --- BUY / CONVERT target ---
@router.message(DealFSM.currency_to, F.text)
async def h_currency_to(message: Message, state: FSMContext):
    cur = message.text.strip().upper()
    if len(cur) not in (3, 4):
        return await message.answer("Некорректный код валюты. Пример: USD, EUR.")
    await state.update_data(currency_to=cur)
    data = await state.get_data()
    op = data.get("operation")
    if op == "buy":
        await state.set_state(DealFSM.amount)
        return await message.answer(f"Введите сумму {cur}, которую хотите <b>купить</b>.", parse_mode=ParseMode.HTML)
    if op == "convert":
        await state.set_state(DealFSM.conversion_mode)
        sell_cur = data.get("currency_from")
        buy_cur = cur
        return await message.answer(
            "Как зафиксируем заявку по конверсии?\n"
            f"1️⃣ Сколько <b>продаёте</b> {sell_cur}\n"
            f"2️⃣ Сколько <b>покупаете</b> {buy_cur}",
            parse_mode=ParseMode.HTML,
        )

@router.message(DealFSM.conversion_mode, F.text)
async def h_conv_mode(message: Message, state: FSMContext):
    t = message.text.strip()
    if t.startswith("1"):
        await state.update_data(conversion_mode="sell")
        await state.set_state(DealFSM.amount)
        data = await state.get_data()
        return await message.answer(f"Введите сумму {data.get('currency_from')} для <b>продажи</b>.", parse_mode=ParseMode.HTML)
    if t.startswith("2"):
        await state.update_data(conversion_mode="buy")
        await state.set_state(DealFSM.amount)
        data = await state.get_data()
        return await message.answer(f"Введите сумму {data.get('currency_to')} для <b>покупки</b>.", parse_mode=ParseMode.HTML)
    await message.answer("Введите 1 (сколько продаёте) или 2 (сколько покупаете).")

@router.message(DealFSM.amount, F.text)
async def h_amount(message: Message, state: FSMContext):
    try:
        parse_decimal(message.text)
    except Exception:
        return await message.answer("Введите корректное число, например 1000000 или 1 000 000,00.")
    await state.update_data(amount=message.text.strip())
    await state.set_state(DealFSM.rate)
    await message.answer("Введите <b>курс</b> для этой операции.", parse_mode=ParseMode.HTML)

@router.message(DealFSM.rate, F.text)
async def h_rate(message: Message, state: FSMContext):
    try:
        parse_decimal(message.text)
    except Exception:
        return await message.answer("Курс должен быть числом. Пример: 41.25")
    await state.update_data(rate=message.text.strip())
    data = await state.get_data()

    payload = {
        "client_id": message.from_user.id,
        "client_name": data.get("client_name"),
        "operation": data.get("operation"),
        "currency_from": data.get("currency_from") or "",
        "currency_to": data.get("currency_to") or "",
        "conversion_mode": data.get("conversion_mode") or "",
        "amount": data.get("amount"),
        "rate": data.get("rate"),
        "status": "new",
    }
    oid = await order_create(payload)
    order = await order_get(oid)
    await state.clear()

    # summary
    op = order["operation"]
    if op == "buy":
        summary = (
            f"✅ Заявка #{oid}\n"
            f"Клиент: {order['client_name']}\n"
            f"Операция: Покупка\n"
            f"Купить: {order['amount']} {order['currency_to']}\n"
            f"Курс: {order['rate']}\n"
            f"Статус: {order['status']}"
        )
    elif op == "sell":
        summary = (
            f"✅ Заявка #{oid}\n"
            f"Клиент: {order['client_name']}\n"
            f"Операция: Продажа\n"
            f"Продать: {order['amount']} {order['currency_from']}\n"
            f"Курс: {order['rate']}\n"
            f"Статус: {order['status']}"
        )
    else:
        mode = "Продаю" if (order.get("conversion_mode") or "") == "sell" else "Покупаю"
        cur = order["currency_from"] if mode == "Продаю" else order["currency_to"]
        summary = (
            f"✅ Заявка #{oid}\n"
            f"Клиент: {order['client_name']}\n"
            f"Операция: Конверсия {order['currency_from']}→{order['currency_to']}\n"
            f"{mode}: {order['amount']} {cur}\n"
            f"Курс: {order['rate']}\n"
            f"Статус: {order['status']}"
        )

    await message.answer(summary)

    # notify bank users
    for uid in list(BANK_USERS):
        with suppress(Exception):
            await bot.send_message(uid, f"📥 Новая заявка #{oid}\n\n{summary}", reply_markup=bank_order_actions_kb(oid))


# =========================
# Rates stub (common)
# =========================
@router.message(Command("rate"))
@router.message(F.text == "💱 Курсы")
async def cmd_rate(message: Message):
    text = (
        "💱 Текущие курсы (заглушка):\n"
        "USD/UAH = 41.25\n"
        "EUR/UAH = 45.10\n"
        "PLN/UAH = 10.60\n"
        "EUR/USD = 1.0920\n"
        "PLN/USD = 0.2580\n"
    )
    await message.answer(text)


# =========================
# CLIENT — "Мои заявки"
# =========================
@router.message(F.text == "📋 Мои заявки")
async def client_my_orders(message: Message):
    lst = await orders_list(status="all", client_id=message.from_user.id)
    if not lst:
        return await message.answer("Пока нет заявок.")
    lines = ["📋 Ваши заявки:"]
    for o in lst:
        op = o.get("operation")
        if op == "buy":
            desc = f"Покупка {o.get('amount')} {o.get('currency_to')}"
        elif op == "sell":
            desc = f"Продажа {o.get('amount')} {o.get('currency_from')}"
        else:
            cm = o.get("conversion_mode")
            if cm == "sell":
                desc = f"Конверсия {o.get('currency_from')}→{o.get('currency_to')} | продаёте {o.get('amount')} {o.get('currency_from')}"
            else:
                desc = f"Конверсия {o.get('currency_from')}→{o.get('currency_to')} | покупаете {o.get('amount')} {o.get('currency_to')}"
        pr = o.get("proposed_rate")
        pr_txt = f", предложенный курс: {pr}" if pr else ""
        lines.append(f"#{o.get('id')}: {desc} @ {o.get('rate')} | {o.get('status')}{pr_txt}")
    await message.answer("\n".join(lines))


# =========================
# Bank role — login & lists
# =========================
@router.message(Command("bank"))
async def bank_login(message: Message):
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) == 2 and parts[1] == BANK_PASSWORD:
        BANK_USERS.add(message.from_user.id)
        return await message.answer("🏦 Вы вошли как банк.", reply_markup=bank_main_kb)
    await message.answer("❌ Неверный пароль. Используйте: /bank <пароль>")

def _render_order_line(o: Dict) -> str:
    op = o.get("operation")
    if op == "buy":
        desc = f"Покупка {o.get('amount')} {o.get('currency_to')}"
    elif op == "sell":
        desc = f"Продажа {o.get('amount')} {o.get('currency_from')}"
    else:
        cm = o.get("conversion_mode")
        if cm == "sell":
            desc = f"Конверсия {o.get('currency_from')}→{o.get('currency_to')} | продаёт {o.get('amount')} {o.get('currency_from')}"
        else:
            desc = f"Конверсия {o.get('currency_from')}→{o.get('currency_to')} | покупает {o.get('amount')} {o.get('currency_to')}"
    rate = o.get("rate", "")
    status = o.get("status", "")
    pid = o.get("id")
    pr = o.get("proposed_rate")
    pr_txt = f" | предложен {pr}" if pr else ""
    return f"#{pid}: {desc} @ {rate} | {status}{pr_txt}"

@router.message(Command("orders"))
@router.message(F.text.in_(["📋 Все заявки", "🆕 Новые заявки", "✅ Принятые", "❌ Отклонённые", "📌 Ордеры"]))
async def bank_orders(message: Message):
    if message.from_user.id not in BANK_USERS:
        return await message.answer("⛔ Доступ запрещён.")
    label_map = {
        "📋 Все заявки": "all",
        "🆕 Новые заявки": "new",
        "✅ Принятые": "accepted",
        "❌ Отклонённые": "rejected",
        "📌 Ордеры": "confirmed"
    }
    status = None
    if message.text in label_map:
        status = label_map[message.text]
    else:
        # /orders [status]
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) == 2:
            st = parts[1].strip().lower()
            if st in {"all", "new", "accepted", "rejected", "confirmed"}:
                status = st
            else:
                return await message.answer("Использование: /orders [all|new|accepted|rejected|confirmed]")
        else:
            status = "new"

    lst = await orders_list(status=None if status == "all" else status)
    if not lst:
        return await message.answer("Заявок нет.")
    # инлайн список заявок
    kb_rows = []
    row: List[InlineKeyboardButton] = []
    for o in lst:
        oid = int(o["id"])
        row.append(InlineKeyboardButton(text=f"#{oid} | {o['status']}", callback_data=f"bank:view:{oid}"))
        if len(row) == 2:
            kb_rows.append(row)
            row = []
    if row:
        kb_rows.append(row)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await message.answer("📋 Выберите заявку:", reply_markup=kb)

# Просмотр заявки и действия — callback
@router.callback_query(F.data.startswith("bank:view:"))
async def bank_view_order(cb: CallbackQuery):
    if cb.from_user.id not in BANK_USERS:
        return await cb.answer("Нет доступа", show_alert=True)
    oid = int(cb.data.split(":")[2])
    o = await order_get(oid)
    if not o:
        return await cb.answer("Заявка не найдена", show_alert=True)
    # текст карточки
    op = o.get("operation")
    if op == "buy":
        desc = f"Купить: {o.get('amount')} {o.get('currency_to')}"
    elif op == "sell":
        desc = f"Продать: {o.get('amount')} {o.get('currency_from')}"
    else:
        cm = o.get("conversion_mode")
        if cm == "sell":
            desc = f"Конверсия {o.get('currency_from')}→{o.get('currency_to')} | продаёт {o.get('amount')} {o.get('currency_from')}"
        else:
            desc = f"Конверсия {o.get('currency_from')}→{o.get('currency_to')} | покупает {o.get('amount')} {o.get('currency_to')}"
    pr = o.get("proposed_rate")
    pr_txt = f"\nПредложенный курс: {pr}" if pr else ""
    text = (
        f"Заявка #{oid}\n"
        f"Клиент: {o.get('client_name')}\n"
        f"Операция: {op}\n"
        f"{desc}\n"
        f"Курс клиента: {o.get('rate')}\n"
        f"Статус: {o.get('status')}{pr_txt}"
    )
    await cb.message.edit_text(text)
    await cb.message.edit_reply_markup(reply_markup=bank_order_actions_kb(oid))
    await cb.answer()

# Действия банка: принять/отклонить/контр/ордер
@router.callback_query(F.data.startswith("bank:accept:"))
async def bank_accept_cb(cb: CallbackQuery):
    if cb.from_user.id not in BANK_USERS:
        return await cb.answer("Нет доступа", show_alert=True)
    oid = int(cb.data.split(":")[2])
    o = await order_get(oid)
    if not o:
        return await cb.answer("Заявка не найдена", show_alert=True)
    o = await order_change_status(oid, "accepted", {"proposed_rate": ""})
    await cb.answer("Заявка принята")
    with suppress(Exception):
        await bot.send_message(int(o["client_id"]), f"🏦 Ваша заявка #{oid} принята банком.")
    return await bank_view_order(cb)

@router.callback_query(F.data.startswith("bank:reject:"))
async def bank_reject_cb(cb: CallbackQuery):
    if cb.from_user.id not in BANK_USERS:
        return await cb.answer("Нет доступа", show_alert=True)
    oid = int(cb.data.split(":")[2])
    o = await order_get(oid)
    if not o:
        return await cb.answer("Заявка не найдена", show_alert=True)
    # отклоняем без предложения курса
    o = await order_change_status(oid, "rejected", {"proposed_rate": ""})
    await cb.answer("Заявка отклонена")
    with suppress(Exception):
        await bot.send_message(int(o["client_id"]), f"🏦 Ваша заявка #{oid} отклонена банком.")
    return await bank_view_order(cb)

@router.callback_query(F.data.startswith("bank:order:"))
async def bank_order_cb(cb: CallbackQuery):
    if cb.from_user.id not in BANK_USERS:
        return await cb.answer("Нет доступа", show_alert=True)
    oid = int(cb.data.split(":")[2])
    o = await order_get(oid)
    if not o:
        return await cb.answer("Заявка не найдена", show_alert=True)
    o = await order_change_status(oid, "confirmed")
    await cb.answer("Заявка переведена в ордер")
    with suppress(Exception):
        await bot.send_message(int(o["client_id"]), f"🏦 Ваша заявка #{oid} переведена в статус ордера.")
    return await bank_view_order(cb)

# Контр-курс — запрос курса
@router.callback_query(F.data.startswith("bank:counter:"))
async def bank_counter_cb(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in BANK_USERS:
        return await cb.answer("Нет доступа", show_alert=True)
    oid = int(cb.data.split(":")[2])
    o = await order_get(oid)
    if not o:
        return await cb.answer("Заявка не найдена", show_alert=True)
    await state.set_state(BankFSM.waiting_counter_rate)
    await state.update_data(counter_order_id=oid)
    await cb.answer()
    await cb.message.reply(f"💹 Введите контр-курс для заявки #{oid} (число).")

@router.message(BankFSM.waiting_counter_rate, F.text)
async def bank_counter_rate_input(message: Message, state: FSMContext):
    if message.from_user.id not in BANK_USERS:
        await state.clear()
        return await message.answer("⛔ Доступ запрещён.")
    data = await state.get_data()
    oid = int(data.get("counter_order_id", 0))
    if not oid:
        await state.clear()
        return await message.answer("Не найдена заявка для контр-курса.")
    try:
        rate = parse_decimal(message.text)
    except Exception:
        return await message.answer("Курс должен быть числом. Пример: 40.25")
    o = await order_get(oid)
    if not o:
        await state.clear()
        return await message.answer("Заявка не найдена.")
    # меняем статус на rejected и записываем proposed_rate
    o = await order_change_status(oid, "rejected", {"proposed_rate": str(rate)})
    await message.answer(f"Отправлен контр-курс {rate} по заявке #{oid}. Статус: rejected")
    with suppress(Exception):
        await bot.send_message(
            int(o["client_id"]),
            f"🏦 Банк предлагает новый курс по вашей заявке #{oid}: <b>{rate}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=client_counter_choice_kb(oid)
        )
    await state.clear()

# Клиент отвечает на контр-курс
@router.callback_query(F.data.startswith("client:accept:"))
async def client_accept_counter(cb: CallbackQuery):
    oid = int(cb.data.split(":")[2])
    o = await order_get(oid)
    if not o or str(cb.from_user.id) != str(o.get("client_id")):
        return await cb.answer("Недоступно", show_alert=True)
    # принятие контр-курса → статус accepted и меняем rate на proposed_rate
    new_rate = o.get("proposed_rate") or o.get("rate")
    o = await order_change_status(oid, "accepted", {"rate": new_rate, "proposed_rate": ""})
    await cb.answer("Курс принят")
    await cb.message.edit_reply_markup(reply_markup=None)
    with suppress(Exception):
        # уведомим банк: всем активным BANK_USERS
        for uid in list(BANK_USERS):
            await bot.send_message(uid, f"✅ Клиент принял контр-курс по заявке #{oid}. Итоговый курс: {new_rate}")
    return

@router.callback_query(F.data.startswith("client:reject:"))
async def client_reject_counter(cb: CallbackQuery):
    oid = int(cb.data.split(":")[2])
    o = await order_get(oid)
    if not o or str(cb.from_user.id) != str(o.get("client_id")):
        return await cb.answer("Недоступно", show_alert=True)
    # клиент отказался от контр-курса — просто очищаем proposed_rate, статус оставим rejected
    o = await order_change_status(oid, "rejected", {"proposed_rate": ""})
    await cb.answer("Вы отклонили предложение")
    await cb.message.edit_reply_markup(reply_markup=None)
    with suppress(Exception):
        for uid in list(BANK_USERS):
            await bot.send_message(uid, f"❌ Клиент отклонил контр-курс по заявке #{oid}.")
    return


# =========================
# Service utils
# =========================
@router.message(Command("status"))
async def cmd_status(message: Message):
    storage_type = type(dp.storage).__name__ if dp else "unknown"
    text = f"🔎 Mode: <b>{app.state.mode}</b>\nStorage: <b>{storage_type}</b>"
    if isinstance(dp.storage, RedisStorage):
        try:
            pong = await dp.storage.redis.ping()
            text += f"\nRedis ping: <b>{'ok' if pong else 'fail'}</b>"
        except Exception as e:
            text += f"\nRedis error: <code>{e!r}</code>"
    await message.answer(text, parse_mode=ParseMode.HTML)

@router.message(Command("restart"))
async def cmd_restart(message: Message, state: FSMContext):
    await state.clear()
    with suppress(Exception):
        await bot.delete_webhook(drop_pending_updates=True)
    full = f"{WEBHOOK_URL.rstrip('/')}{WEBHOOK_PATH}"
    await bot.set_webhook(url=full, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
    await message.answer("♻️ Перезапуск: вебхук переустановлен.")

@router.message(Command("ping"))
async def cmd_ping(message: Message):
    await message.answer("pong")


# =========================
# FastAPI endpoints
# =========================
class Health(BaseModel):
    status: str = "ok"
    mode: Optional[str] = None

@app.get("/", response_model=Health)
async def health():
    return Health(status="ok", mode=app.state.mode)

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    # Проверка секрета в заголовке:
    # - при STRICT_HEADER=True — строго
    # - при STRICT_HEADER=False — мягко (если заголовок присутствует и не совпал — 403; если отсутствует — пропускаем)
    if STRICT_HEADER:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret token header")
    else:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret and secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret token header")

    data = await request.json()
    try:
        upd = Update.model_validate(data, context={"bot": bot})
    except Exception as e:
        log.warning("Bad update payload: %r", e, exc_info=True)
        return Response(status_code=200)

    async def _process_update(update: Update):
        try:
            await dp.feed_update(bot, update)
        except Exception as e:
            log.error("Update processing failed: %r", e, exc_info=True)

    if ASYNC_UPDATES:
        asyncio.create_task(_process_update(upd))
        return Response(status_code=200)
    else:
        await _process_update(upd)
        return Response(status_code=200)


# =========================
# Webhook watchdog (фоновая проверка)
# =========================
async def webhook_watchdog():
    await asyncio.sleep(5)
    target = f"{WEBHOOK_URL.rstrip('/')}{WEBHOOK_PATH}"
    while True:
        try:
            info = await bot.get_webhook_info()
            if not info.url or info.url != target:
                log.warning("Webhook mismatch (%s) -> resetting to %s", info.url, target)
                with suppress(Exception):
                    await bot.delete_webhook(drop_pending_updates=False)
                await bot.set_webhook(url=target, secret_token=WEBHOOK_SECRET, drop_pending_updates=False)
                log.info("Webhook re-set to %s", target)
        except Exception as e:
            log.warning("Watchdog check failed: %r", e)
        await asyncio.sleep(WEBHOOK_WATCHDOG_INTERVAL)


# =========================
# FastAPI lifecycle
# =========================
@app.on_event("startup")
async def on_startup():
    global bot, dp
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    storage = await build_storage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)  # подключаем роутеры ОДИН РАЗ

    # ставим вебхук
    full = f"{WEBHOOK_URL.rstrip('/')}{WEBHOOK_PATH}"
    with suppress(Exception):
        await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(
        url=full,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
    )
    app.state.mode = "webhook"
    log.info(f"Webhook set to {full}")

    # запускаем watchdog (если включён)
    if ENABLE_WATCHDOG:
        app.state.watchdog_task = asyncio.create_task(webhook_watchdog())

@app.on_event("shutdown")
async def on_shutdown():
    if app.state.watchdog_task:
        app.state.watchdog_task.cancel()
    with suppress(Exception):
        await bot.session.close()
    log.info("Shutdown complete.")
