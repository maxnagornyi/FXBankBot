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
from aiogram.types import Message, Update
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

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_WATCHDOG_INTERVAL = int(os.getenv("WEBHOOK_WATCHDOG_INTERVAL", "60"))  # сек

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
# FSM States
# =========================
class DealFSM(StatesGroup):
    client_name = State()
    operation_type = State()      # buy / sell / convert
    currency_from = State()       # for sell or convert
    currency_to = State()         # for buy or convert
    conversion_mode = State()     # for convert: "sell" or "buy"
    amount = State()              # numeric string
    rate = State()                # numeric string


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

async def orders_list(status: Optional[str] = None, limit: int = 50) -> List[Dict]:
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
                result.append(data)
        return result
    # memory flow
    items = list(_MEM_ORDERS.items())
    items.sort(key=lambda kv: int(kv[0]))
    rows: List[Dict] = []
    for oid, data in items:
        if status and status != "all" and data.get("status") != status:
            continue
        rows.append(data)
        if len(rows) >= limit:
            break
    return rows


# =========================
# Handlers — client
# =========================
@router.message(CommandStart())
@router.message(Command("start"))
@router.message(StateFilter("*"), F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(DealFSM.client_name)
    await message.answer(
        "👋 Привет! Я FXBankBot.\n\n"
        "Начнём заявку. Сначала укажи <b>название клиента</b>.",
        parse_mode=ParseMode.HTML,
    )

@router.message(DealFSM.client_name, F.text)
async def h_client_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        return await message.answer("Название клиента не может быть пустым. Введите ещё раз.")
    await state.update_data(client_name=name)
    await state.set_state(DealFSM.operation_type)
    await message.answer(
        "Выберите тип операции (можно цифрой):\n"
        "1️⃣ Покупка валюты за UAH\n"
        "2️⃣ Продажа валюты за UAH\n"
        "3️⃣ Конверсия (валюта → валюта)"
    )

@router.message(DealFSM.operation_type, F.text)
async def h_operation(message: Message, state: FSMContext):
    choice = message.text.strip().lower()
    if choice.startswith("1") or "покуп" in choice:
        await state.update_data(operation="buy")
        await state.set_state(DealFSM.currency_to)
        return await message.answer("Какую валюту хотите <b>купить</b>? (например: USD, EUR)", parse_mode=ParseMode.HTML)
    if choice.startswith("2") or "прод" in choice:
        await state.update_data(operation="sell")
        await state.set_state(DealFSM.currency_from)
        return await message.answer("Какую валюту хотите <b>продать</b>? (например: USD, EUR)", parse_mode=ParseMode.HTML)
    if choice.startswith("3") or "конверс" in choice:
        await state.update_data(operation="convert")
        await state.set_state(DealFSM.currency_from)
        return await message.answer("Конверсия: укажите валюту, которую <b>продаёте</b> (например: EUR)", parse_mode=ParseMode.HTML)
    await message.answer("Выберите 1 (Покупка), 2 (Продажа) или 3 (Конверсия).")

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
    await message.answer("Выберите 1 (сколько продаёте) или 2 (сколько покупаете).")

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
            await bot.send_message(uid, f"📥 Новая заявка #{oid}\n\n{summary}")


# =========================
# Rates stub
# =========================
@router.message(Command("rate"))
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
# Bank role
# =========================
@router.message(Command("bank"))
async def bank_login(message: Message):
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) == 2 and parts[1] == BANK_PASSWORD:
        BANK_USERS.add(message.from_user.id)
        return await message.answer("🏦 Вы вошли как банк.")
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
    return f"#{pid}: {desc} @ {rate} | {status}"

@router.message(Command("orders")))
async def bank_orders(message: Message):
    if message.from_user.id not in BANK_USERS:
        return await message.answer("⛔ Доступ запрещён.")
    parts = message.text.strip().split(maxsplit=1)
    status = "new"
    if len(parts) == 2:
        st = parts[1].strip().lower()
        if st in {"all", "new", "accepted", "rejected", "confirmed"}:
            status = st
        else:
            return await message.answer("Использование: /orders [all|new|accepted|rejected|confirmed]")
    lst = await orders_list(status=None if status == "all" else status)
    if not lst:
        return await message.answer("Заявок нет.")
    lines = ["📋 Заявки:"]
    for o in lst:
        lines.append(_render_order_line(o))
    await message.answer("\n".join(lines))

@router.message(Command("accept"))
async def bank_accept(message: Message):
    if message.from_user.id not in BANK_USERS:
        return await message.answer("⛔ Доступ запрещён.")
    parts = message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer("Использование: /accept <id>")
    oid = int(parts[1])
    o = await order_get(oid)
    if not o:
        return await message.answer("Нет такой заявки.")
    o = await order_change_status(oid, "accepted")
    await message.answer(f"✅ Заявка #{oid} принята.")
    with suppress(Exception):
        await bot.send_message(int(o["client_id"]), f"🏦 Ваша заявка #{oid} принята банком.")

@router.message(Command("reject"))
async def bank_reject(message: Message):
    if message.from_user.id not in BANK_USERS:
        return await message.answer("⛔ Доступ запрещён.")
    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        return await message.answer("Использование: /reject <id> <предложенный_курс>")
    oid = int(parts[1])
    new_rate = parts[2].strip()
    o = await order_get(oid)
    if not o:
        return await message.answer("Нет такой заявки.")
    o = await order_change_status(oid, "rejected", {"proposed_rate": new_rate})
    await message.answer(f"❌ Заявка #{oid} отклонена. Предложен курс {new_rate}.")
    with suppress(Exception):
        await bot.send_message(int(o["client_id"]), f"🏦 Ваша заявка #{oid} отклонена. Новый курс: {new_rate}")

@router.message(Command("confirm"))
async def bank_confirm(message: Message):
    if message.from_user.id not in BANK_USERS:
        return await message.answer("⛔ Доступ запрещён.")
    parts = message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await message.answer("Использование: /confirm <id>")
    oid = int(parts[1])
    o = await order_get(oid)
    if not o:
        return await message.answer("Нет такой заявки.")
    o = await order_change_status(oid, "confirmed")
    await message.answer(f"📌 Заявка #{oid} переведена в статус ордера.")
    with suppress(Exception):
        await bot.send_message(int(o["client_id"]), f"🏦 Ваша заявка #{oid} переведена в статус ордера.")


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

@router.message(Command("alive"))
async def cmd_alive(message: Message):
    await message.answer("✅ alive")


# =========================
# Global error handler (aiogram)
# =========================
@router.errors()
async def aiogram_error_handler(event, exception):
    log.error("Handler error: %r", exception, exc_info=True)
    # отправляем пользователю мягкое сообщение, если контекст есть
    try:
        if hasattr(event, "update") and event.update.message:
            with suppress(Exception):
                await bot.send_message(event.update.message.chat.id, "❗️Произошла ошибка при обработке. Попробуйте ещё раз.")
    except Exception:
        pass
    # важно: НЕ пробрасываем исключение дальше — чтобы не ронять диспетчер


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
    # мягкая проверка заголовка: если он есть и не совпал — 403; если отсутствует — пропускаем
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret and secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret token header")

    data = await request.json()
    try:
        upd = Update.model_validate(data, context={"bot": bot})
    except Exception as e:
        log.warning("Bad update payload: %r", e, exc_info=True)
        return Response(status_code=200)

    # обработаем update асинхронно, чтобы ответить Telegram мгновенно (и не уронить процесс)
    async def _process_update(update: Update):
        try:
            await dp.feed_update(bot, update)
        except Exception as e:
            log.error("Update processing failed: %r", e, exc_info=True)

    asyncio.create_task(_process_update(upd))
    return Response(status_code=200)


# =========================
# Webhook watchdog (фоновая проверка)
# =========================
async def webhook_watchdog():
    # Периодически проверяем, что вебхук на месте, и восстанавливаем при необходимости
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

    # запускаем watchdog
    app.state.watchdog_task = asyncio.create_task(webhook_watchdog())

@app.on_event("shutdown")
async def on_shutdown():
    if app.state.watchdog_task:
        app.state.watchdog_task.cancel()
    with suppress(Exception):
        await bot.session.close()
    log.info("Shutdown complete.")
