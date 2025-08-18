import os
import asyncio
import logging
from typing import Dict, Any

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.enums import ParseMode
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.filters import Command
import redis.asyncio as redis

# ---------------------- ЛОГИ ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("fxbank_bot")

# ---------------------- НАСТРОЙКИ ----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "fxbank-secret")
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", "https://fxbankbot.onrender.com") + WEBHOOK_PATH
PORT = int(os.getenv("PORT", "10000"))
HOST = "0.0.0.0"

# ---------------------- ИНИЦИАЛИЗАЦИЯ ----------------------
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
router = Router()

# FSM через Redis
redis_conn = redis.from_url(REDIS_URL)
storage = RedisStorage(redis_conn)
dp = Dispatcher(storage=storage)
dp.include_router(router)

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

# ---------------------- FSM ----------------------
class ClientFSM(StatesGroup):
    entering_client_name = State()
    choosing_deal = State()
    entering_currency_from = State()
    entering_currency_to = State()
    entering_amount = State()

# ---------------------- КУРСЫ (заглушка) ----------------------
def get_rates() -> Dict[str, float]:
    return {
        "USDUAH": 40.0,
        "EURUAH": 44.0,
        "PLNUAH": 10.0,
        "EURUSD": 1.08,
        "EURPLN": 4.3,
        "USDPLN": 4.0,
    }

def format_rates() -> str:
    rates = get_rates()
    return "\n".join([f"{pair}: {rate}" for pair, rate in rates.items()])

# ---------------------- ХЕНДЛЕРЫ ----------------------
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    try:
        await state.clear()
        await message.answer("Добро пожаловать в FXBankBot!\nВыберите роль:", reply_markup=role_kb())
    except Exception as e:
        logger.error(f"cmd_start failed: {e}")

@router.message(Command("rate"))
@router.message(F.text == "💱 Курсы")
async def cmd_rate(message: Message):
    try:
        await message.answer("Текущие курсы:\n" + format_rates())
    except Exception as e:
        logger.error(f"cmd_rate failed: {e}")

@router.callback_query(F.data == "role_client")
async def cq_role_client(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ClientFSM.entering_client_name)
    await callback.message.answer("Введите название вашей компании или ФИО:", reply_markup=ReplyKeyboardRemove())
    await callback.answer()

@router.callback_query(F.data == "role_bank")
async def cq_role_bank(callback: CallbackQuery):
    await callback.message.answer("Роль 'Банк' выбрана.\nПока доступны только курсы и заявки.", reply_markup=main_kb())
    await callback.answer()

@router.message(F.text == "➕ Новая заявка")
async def new_deal_entry(message: Message, state: FSMContext):
    try:
        user_data = await state.get_data()
        if "client_name" not in user_data:
            await state.set_state(ClientFSM.entering_client_name)
            await message.answer("Введите название вашей компании или ФИО:")
            return
        await message.answer("Выберите тип сделки:", reply_markup=deal_type_kb())
        await state.set_state(ClientFSM.choosing_deal)
    except Exception as e:
        logger.error(f"new_deal_entry failed: {e}")

@router.message(ClientFSM.entering_client_name)
async def process_client_name(message: Message, state: FSMContext):
    await state.update_data(client_name=message.text)
    await state.set_state(ClientFSM.choosing_deal)
    await message.answer(f"Спасибо, {message.text}! Теперь выберите тип сделки:", reply_markup=deal_type_kb())

@router.callback_query(ClientFSM.choosing_deal, F.data.in_(["deal_buy", "deal_sell", "deal_convert"]))
async def cq_choose_deal(callback: CallbackQuery, state: FSMContext):
    try:
        deal_type = callback.data
        await state.update_data(deal_type=deal_type)
        if deal_type == "deal_convert":
            await state.set_state(ClientFSM.entering_currency_from)
            await callback.message.answer("Введите валюту, которую хотите продать (например: USD):")
        else:
            await state.set_state(ClientFSM.entering_currency_from)
            await callback.message.answer("Введите валюту сделки (например: USD):")
        await callback.answer()
    except Exception as e:
        logger.error(f"cq_choose_deal failed: {e}")

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено. Главное меню:", reply_markup=main_kb())

@router.message()
async def fallback_handler(message: Message, state: FSMContext):
    cur_state = await state.get_state()
    if cur_state:
        await message.answer(f"Сейчас я жду данные для состояния <b>{cur_state}</b>.\nЕсли хотите выйти, напишите /cancel.")
    else:
        await message.answer("Не понимаю сообщение. Используйте меню или команду /start.")

# ---------------------- FASTAPI ----------------------
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    logger.info("Starting up application...")
    try:
        await bot.set_webhook(WEBHOOK_URL, allowed_updates=["message", "callback_query"])
        logger.info(f"Webhook set to {WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.error(f"webhook failed: {e}")
    return {"ok": True}

@app.get("/")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
