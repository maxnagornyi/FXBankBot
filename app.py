import os
import asyncio
import logging
from typing import Dict, Any, Optional
from contextlib import suppress

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.client.default import DefaultBotProperties
import redis.asyncio as redis

# ---------------------- ЛОГИ ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("fxbank_bot")

# ---------------------- НАСТРОЙКИ ----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    logger.error("BOT_TOKEN is not set in environment!")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "fxbank-secret").strip()
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"

RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
if RENDER_EXTERNAL_URL:
    WEBHOOK_BASE = RENDER_EXTERNAL_URL
elif RENDER_EXTERNAL_HOSTNAME:
    WEBHOOK_BASE = f"https://{RENDER_EXTERNAL_HOSTNAME}"
else:
    WEBHOOK_BASE = ""

# ---------------------- БОТ / ДИСПЕТЧЕР / ХРАНИЛИЩЕ ----------------------
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
router = Router()

redis_conn = redis.from_url(REDIS_URL)
storage = RedisStorage(redis_conn)
dp = Dispatcher(storage=storage)
dp.include_router(router)

# ---------------------- IN-MEMORY ----------------------
user_roles: Dict[int, str] = {}

class Order:
    counter = 0
    def __init__(self, client_id, client_telegram, client_name,
                 operation, amount, currency_from, currency_to, rate):
        Order.counter += 1
        self.id = Order.counter
        self.client_id = client_id
        self.client_telegram = client_telegram
        self.client_name = client_name
        self.operation = operation
        self.amount = amount
        self.currency_from = currency_from
        self.currency_to = currency_to
        self.rate = rate
        self.status = "new"

    def summary(self) -> str:
        if self.operation == "конвертация":
            op_text = f"{self.amount} {self.currency_from} → {self.currency_to}"
        else:
            op_text = f"{self.operation} {self.amount} {self.currency_from}"
        tg = f" (@{self.client_telegram})" if self.client_telegram else ""
        return (
            f"📌 <b>Заявка #{self.id}</b>\n"
            f"👤 Клиент: {self.client_name}{tg}\n"
            f"💱 Операция: {op_text}\n"
            f"📊 Курс клиента: {self.rate}\n"
            f"📍 Статус: {self.status}"
        )

orders: Dict[int, Order] = {}

# ---------------------- КЛАВИАТУРЫ ----------------------
def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Новая заявка")],
            [KeyboardButton(text="💱 Курсы")],
        ],
        resize_keyboard=True
    )

def role_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Клиент", callback_data="role_client")],
        [InlineKeyboardButton(text="🏦 Банк", callback_data="role_bank")]
    ])

def deal_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Купить валюту", callback_data="deal_buy")],
        [InlineKeyboardButton(text="Продать валюту", callback_data="deal_sell")],
        [InlineKeyboardButton(text="Конверсия", callback_data="deal_convert")]
    ])

def bank_order_kb(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{order_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{order_id}"),
        ],
        [
            InlineKeyboardButton(text="📌 Ордер", callback_data=f"order:{order_id}")
        ]
    ])

# ---------------------- FSM ----------------------
class ClientFSM(StatesGroup):
    entering_client_name = State()
    choosing_deal = State()
    entering_currency_from = State()
    entering_currency_to = State()
    entering_amount = State()
    entering_rate = State()

# ---------------------- КУРСЫ ----------------------
def get_rates() -> Dict[str, float]:
    return {
        "USD/UAH": 41.25,
        "EUR/UAH": 45.10,
        "PLN/UAH": 10.60,
        "EUR/USD": 1.0920,
        "USD/PLN": 3.8760,
        "EUR/PLN": 4.2326,
    }

def format_rates() -> str:
    r = get_rates()
    return "\n".join([f"{k}: {v}" for k, v in r.items()])

async def send_safe(chat_id: int, text: str, **kwargs):
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logger.error(f"send_safe error: {e}")

# ---------------------- ХЕНДЛЕРЫ ----------------------
# /start, /rate, /cancel, выбор роли, создание заявки и т.д.
# (оставляем всё как было в предыдущей версии)

# ---------------------- BANK CALLBACKS ----------------------
@router.callback_query(F.data.startswith("accept:"))
async def cb_accept(call: CallbackQuery):
    # ...
    pass

@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: CallbackQuery):
    # ...
    pass

@router.callback_query(F.data.startswith("order:"))
async def cb_order(call: CallbackQuery):
    # ...
    pass

# ---------------------- FALLBACK ----------------------
@router.message()
async def fallback_handler(message: Message, state: FSMContext):
    # ...

# ---------------------- FASTAPI ----------------------
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    logger.info("Starting up application...")
    if WEBHOOK_BASE:
        url = f"{WEBHOOK_BASE}{WEBHOOK_PATH}"
        try:
            await bot.set_webhook(url, allowed_updates=["message","callback_query"], secret_token=WEBHOOK_SECRET)
            logger.info(f"Webhook set to {url}")
        except Exception as e:
            logger.error(f"Failed to set webhook: {e}")
    else:
        logger.warning("WEBHOOK_BASE is empty — webhook not set")
    logger.info("Startup complete.")

@app.on_event("shutdown")
async def on_shutdown():
    with suppress(Exception):
        await bot.delete_webhook()
    logger.info("Shutdown complete.")

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    update = await request.json()
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}

@app.get("/")
async def health():
    return {"status": "ok", "service": "FXBankBot", "webhook_path": WEBHOOK_PATH}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
