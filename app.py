import os
import logging
import asyncio
from contextlib import suppress

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.redis import RedisStorage, DefaultKeyBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.types import Message, Update
from aiogram.fsm.state import StatesGroup, State

import redis.asyncio as redis
from aiogram.client.default import DefaultBotProperties


# ----------------------------------------
# Настройки
# ----------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "fxbank-secret")
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("FXBankBot")


# ----------------------------------------
# FSM
# ----------------------------------------
class DealFSM(StatesGroup):
    client_name = State()
    operation_type = State()   # покупка / продажа / конверсия
    currency_from = State()
    currency_to = State()
    conversion_mode = State()  # хочу продать / хочу купить
    amount = State()
    confirm = State()


# ----------------------------------------
# Init bot
# ----------------------------------------
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

# Dispatcher + Storage
async def get_storage():
    if REDIS_URL:
        try:
            redis_client = redis.from_url(REDIS_URL)
            await redis_client.ping()
            logger.info("Connected to Redis, using RedisStorage.")
            return RedisStorage(redis=redis_client, key_builder=DefaultKeyBuilder())
        except Exception as e:
            logger.warning(f"Redis недоступен: {e} — переключаюсь на MemoryStorage")
    return MemoryStorage()


storage = None
dp = Dispatcher()
router = Router()
dp.include_router(router)


# ----------------------------------------
# Handlers
# ----------------------------------------
@router.message(CommandStart())
@router.message(Command("start"))
@router.message(StateFilter("*"), F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(DealFSM.client_name)
    await message.answer(
        "Привет! Я FXBankBot.\n\n"
        "Давай оформим заявку. Сначала укажи <b>название клиента</b>."
    )


@router.message(DealFSM.client_name)
async def client_name_entered(message: Message, state: FSMContext):
    await state.update_data(client_name=message.text)
    await state.set_state(DealFSM.operation_type)
    await message.answer(
        "Выберите тип операции:\n"
        "1️⃣ Покупка валюты за UAH\n"
        "2️⃣ Продажа валюты за UAH\n"
        "3️⃣ Конверсия (валюта → валюта)"
    )


@router.message(DealFSM.operation_type)
async def choose_operation(message: Message, state: FSMContext):
    choice = message.text.strip().lower()
    if choice.startswith("1") or "покуп" in choice:
        await state.update_data(operation="buy")
        await state.set_state(DealFSM.currency_to)
        await message.answer("Какую валюту хотите <b>купить</b>? (например: USD, EUR)")
    elif choice.startswith("2") or "прод" in choice:
        await state.update_data(operation="sell")
        await state.set_state(DealFSM.currency_from)
        await message.answer("Какую валюту хотите <b>продать</b>? (например: USD, EUR)")
    elif choice.startswith("3") or "конверс" in choice:
        await state.update_data(operation="convert")
        await state.set_state(DealFSM.currency_from)
        await message.answer("Укажите валюту, которую <b>продаёте</b> (например: USD)")
    else:
        await message.answer("Выберите 1 (Покупка), 2 (Продажа) или 3 (Конверсия).")


@router.message(DealFSM.currency_from)
async def currency_from_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    op = data.get("operation")

    await state.update_data(currency_from=message.text.upper())

    if op == "sell":
        await state.set_state(DealFSM.amount)
        await message.answer("Введите сумму, которую хотите <b>продать</b>.")
    elif op == "convert":
        await state.set_state(DealFSM.currency_to)
        await message.answer("Укажите валюту, которую <b>хотите купить</b> (например: EUR).")


@router.message(DealFSM.currency_to)
async def currency_to_entered(message: Message, state: FSMContext):
    data = await state.get_data()
    op = data.get("operation")

    await state.update_data(currency_to=message.text.upper())

    if op == "buy":
        await state.set_state(DealFSM.amount)
        await message.answer("Введите сумму, которую хотите <b>купить</b>.")
    elif op == "convert":
        await state.set_state(DealFSM.conversion_mode)
        await message.answer(
            "Хотите указать:\n"
            "1️⃣ Сколько продаёте\n"
            "2️⃣ Сколько покупаете"
        )


@router.message(DealFSM.conversion_mode)
async def conversion_mode_entered(message: Message, state: FSMContext):
    choice = message.text.strip()
    if choice.startswith("1"):
        await state.update_data(conversion_mode="sell")
        await state.set_state(DealFSM.amount)
        await message.answer("Введите сумму, которую хотите <b>продать</b>.")
    elif choice.startswith("2"):
        await state.update_data(conversion_mode="buy")
        await state.set_state(DealFSM.amount)
        await message.answer("Введите сумму, которую хотите <b>купить</b>.")
    else:
        await message.answer("Выберите 1 (продаю) или 2 (покупаю).")


@router.message(DealFSM.amount)
async def amount_entered(message: Message, state: FSMContext):
    await state.update_data(amount=message.text)
    data = await state.get_data()

    text = (
        f"✅ Заявка оформлена:\n"
        f"Клиент: {data.get('client_name')}\n"
        f"Операция: {data.get('operation')}\n"
        f"Валюта с: {data.get('currency_from')}\n"
        f"Валюта на: {data.get('currency_to')}\n"
        f"Сумма: {data.get('amount')}"
    )
    await state.clear()
    await message.answer(text)


# ----------------------------------------
# /status и /restart
# ----------------------------------------
@router.message(Command("status"))
async def cmd_status(message: Message):
    text = f"🔎 Mode: webhook\nStorage: {'RedisStorage' if isinstance(storage, RedisStorage) else 'MemoryStorage'}"
    if isinstance(storage, RedisStorage):
        try:
            pong = await storage.redis.ping()
            text += f"\nRedis ping: {'ok' if pong else 'fail'}"
        except Exception as e:
            text += f"\nRedis error: {e}"
    await message.answer(text)


@router.message(Command("restart"))
async def cmd_restart(message: Message, state: FSMContext):
    await state.clear()
    with suppress(Exception):
        await bot.delete_webhook(drop_pending_updates=True)
    full_url = f"{WEBHOOK_URL}/webhook/{WEBHOOK_SECRET}"
    await bot.set_webhook(url=full_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
    await message.answer("♻️ Бот перезапущен.")


# ----------------------------------------
# FastAPI app
# ----------------------------------------
app = FastAPI()


@app.on_event("startup")
async def on_startup():
    global storage, dp
    storage = await get_storage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    # webhook
    full_url = f"{WEBHOOK_URL}/webhook/{WEBHOOK_SECRET}"
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(url=full_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
    logger.info("Bot started in webhook mode.")


@app.on_event("shutdown")
async def on_shutdown():
    with suppress(Exception):
        await bot.session.close()
    logger.info("Bot stopped.")


@app.get("/")
async def healthcheck():
    return {"status": "ok", "mode": "webhook"}


@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    if token != WEBHOOK_SECRET:
        return Response(status_code=403)
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response(status_code=200)
