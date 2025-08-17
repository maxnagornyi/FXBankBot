import asyncio
import hashlib
import logging
import os
import ssl
from decimal import Decimal
from typing import Optional, Literal

from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.types import Message, Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage, DefaultKeyBuilder
from aiogram.client.default import DefaultBotProperties
from redis.asyncio import Redis

# ------------------------
# Logging
# ------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("FXBankBot")

# ------------------------
# Env variables
# ------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Environment variable BOT_TOKEN is required.")

WEBHOOK_BASE = os.getenv("WEBHOOK_URL")  # e.g. https://your-app.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "fxbankbot-secret")
REDIS_URL = os.getenv("REDIS_URL")
PORT = int(os.getenv("PORT", "10000"))
HOST = "0.0.0.0"

WEBHOOK_PATH = f"/webhook/{hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:18]}"

# ------------------------
# FastAPI app
# ------------------------
app = FastAPI(title="FXBankBot")

# ------------------------
# Aiogram core
# ------------------------
bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None
router = Router()

Mode = Literal["webhook", "polling"]
app.state.mode: Optional[Mode] = None
app.state.polling_task: Optional[asyncio.Task] = None

# ------------------------
# Keyboards
# ------------------------
KB_MAIN = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Покупка"), KeyboardButton(text="Продажа"), KeyboardButton(text="Конверсия")],
        [KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
)

def kb_conversion(sell_cur: str, buy_cur: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=f"Хочу продать {sell_cur}"), KeyboardButton(text=f"Хочу купить {buy_cur}")],
            [KeyboardButton(text="Назад"), KeyboardButton(text="Отмена")],
        ],
        resize_keyboard=True,
    )

# ------------------------
# FSM States
# ------------------------
class DealFSM(StatesGroup):
    client_name = State()
    operation = State()
    buy_currency = State()
    sell_currency = State()
    conv_sell_currency = State()
    conv_buy_currency = State()
    amount_mode = State()
    amount_value = State()

# ------------------------
# Utils
# ------------------------
async def try_build_storage():
    if not REDIS_URL:
        return MemoryStorage()
    try:
        conn_kwargs = {"encoding": "utf-8", "decode_responses": True}
        if REDIS_URL.startswith("rediss://") and os.getenv("REDIS_SSL_NO_VERIFY") == "1":
            conn_kwargs["ssl_cert_reqs"] = ssl.CERT_NONE
        redis = Redis.from_url(REDIS_URL, **conn_kwargs)
        await redis.ping()
        return RedisStorage(redis=redis, key_builder=DefaultKeyBuilder(with_bot_id=True, prefix="fxbank"))
    except Exception as e:
        logger.warning(f"Redis недоступен: {e}")
        return MemoryStorage()

def parse_decimal(value: str) -> Decimal:
    value = value.strip().replace(" ", "").replace(",", ".")
    return Decimal(value)

# ------------------------
# Handlers
# ------------------------
@router.message(CommandStart())
@router.message(Command("start"))
@router.message(StateFilter("*"), F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(DealFSM.client_name)
    await message.answer(
        "Привет! Я FXBankBot.\n\nВведите <b>название клиента</b>:",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove(),
    )

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Заявка отменена. Чтобы начать заново — /start.", reply_markup=ReplyKeyboardRemove())

# ---- Client name ----
@router.message(DealFSM.client_name, F.text)
async def client_name(message: Message, state: FSMContext):
    await state.update_data(client_name=message.text.strip())
    await state.set_state(DealFSM.operation)
    await message.answer("Выберите тип операции:", reply_markup=KB_MAIN)

# ---- Operation choice ----
@router.message(DealFSM.operation, F.text.lower().in_(("покупка",)))
async def op_buy(message: Message, state: FSMContext):
    await state.update_data(operation="buy")
    await state.set_state(DealFSM.buy_currency)
    await message.answer("Покупка: укажите валюту, которую хотите купить (например: USD, EUR).")

@router.message(DealFSM.operation, F.text.lower().in_(("продажа",)))
async def op_sell(message: Message, state: FSMContext):
    await state.update_data(operation="sell")
    await state.set_state(DealFSM.sell_currency)
    await message.answer("Продажа: укажите валюту, которую хотите продать (например: USD, EUR).")

@router.message(DealFSM.operation, F.text.lower().in_(("конверсия",)))
async def op_conv(message: Message, state: FSMContext):
    await state.update_data(operation="conversion")
    await state.set_state(DealFSM.conv_sell_currency)
    await message.answer("Конверсия: укажите валюту, которую продаёте (например: EUR).")

# ---- Buy ----
@router.message(DealFSM.buy_currency, F.text)
async def buy_currency(message: Message, state: FSMContext):
    cur = message.text.strip().upper()
    await state.update_data(buy_currency=cur)
    await state.set_state(DealFSM.amount_value)
    await message.answer(f"Введите сумму {cur}, которую хотите купить:")

# ---- Sell ----
@router.message(DealFSM.sell_currency, F.text)
async def sell_currency(message: Message, state: FSMContext):
    cur = message.text.strip().upper()
    await state.update_data(sell_currency=cur)
    await state.set_state(DealFSM.amount_value)
    await message.answer(f"Введите сумму {cur}, которую хотите продать:")

# ---- Conversion ----
@router.message(DealFSM.conv_sell_currency, F.text)
async def conv_sell_currency(message: Message, state: FSMContext):
    cur = message.text.strip().upper()
    await state.update_data(conv_sell_currency=cur)
    await state.set_state(DealFSM.conv_buy_currency)
    await message.answer("Укажите валюту, которую хотите купить (например: USD).")

@router.message(DealFSM.conv_buy_currency, F.text)
async def conv_buy_currency(message: Message, state: FSMContext):
    data = await state.get_data()
    sell_cur = data.get("conv_sell_currency")
    buy_cur = message.text.strip().upper()
    await state.update_data(conv_buy_currency=buy_cur)
    await state.set_state(DealFSM.amount_mode)
    await message.answer(
        f"Как фиксируем заявку?\n"
        f"• Хочу продать {sell_cur}\n"
        f"• Хочу купить {buy_cur}",
        reply_markup=kb_conversion(sell_cur, buy_cur),
    )

@router.message(DealFSM.amount_mode, F.text)
async def conv_amount_mode(message: Message, state: FSMContext):
    text = message.text.strip().lower()
    data = await state.get_data()
    sell_cur = data.get("conv_sell_currency")
    buy_cur = data.get("conv_buy_currency")
    if text == f"хочу продать {sell_cur}".lower():
        await state.update_data(amount_mode="conv_sell")
        await state.set_state(DealFSM.amount_value)
        await message.answer(f"Введите сумму {sell_cur}, которую хотите продать:", reply_markup=ReplyKeyboardRemove())
    elif text == f"хочу купить {buy_cur}".lower():
        await state.update_data(amount_mode="conv_buy")
        await state.set_state(DealFSM.amount_value)
        await message.answer(f"Введите сумму {buy_cur}, которую хотите купить:", reply_markup=ReplyKeyboardRemove())
    else:
        await message.answer("Выберите один из вариантов.", reply_markup=kb_conversion(sell_cur, buy_cur))

# ---- Amount input ----
@router.message(DealFSM.amount_value, F.text)
async def amount_value(message: Message, state: FSMContext):
    try:
        amount = parse_decimal(message.text)
    except Exception:
        await message.answer("Введите корректное число, например 1000000.")
        return

    data = await state.get_data()
    client = data.get("client_name")
    op = data.get("operation")

    await state.clear()

    if op == "buy":
        cur = data.get("buy_currency")
        text = f"✅ Заявка\nКлиент: {client}\nОперация: Покупка\nКупить: {amount} {cur}"
    elif op == "sell":
        cur = data.get("sell_currency")
        text = f"✅ Заявка\nКлиент: {client}\nОперация: Продажа\nПродать: {amount} {cur}"
    else:  # conversion
        sell_cur = data.get("conv_sell_currency")
        buy_cur = data.get("conv_buy_currency")
        mode = data.get("amount_mode")
        if mode == "conv_sell":
            text = f"✅ Заявка\nКлиент: {client}\nОперация: Конверсия\nПродать: {amount} {sell_cur} → Купить {buy_cur}"
        else:
            text = f"✅ Заявка\nКлиент: {client}\nОперация: Конверсия\nКупить: {amount} {buy_cur} ← Продать {sell_cur}"

    await message.answer(text, reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="/start")]], resize_keyboard=True
    ))

# ------------------------
# FastAPI endpoints
# ------------------------
class Health(BaseModel):
    status: str = "ok"
    mode: Optional[str] = None

@app.get("/", response_model=Health)
async def healthcheck():
    return Health(status="ok", mode=app.state.mode)

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            raise HTTPException(status_code=403)
    raw = await request.json()
    update = Update.model_validate(raw, context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response(status_code=200)

# ------------------------
# Startup / Shutdown
# ------------------------
@app.on_event("startup")
async def on_startup():
    global bot, dp
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    storage = await try_build_storage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    if WEBHOOK_BASE:
        full_url = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH
        await bot.set_webhook(url=full_url, secret_token=WEBHOOK_SECRET, drop_pending_updates=True)
        app.state.mode = "webhook"
    else:
        app.state.mode = "polling"
        asyncio.create_task(dp.start_polling(bot))

@app.on_event("shutdown")
async def on_shutdown():
    if bot:
        await bot.session.close()

# ------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
