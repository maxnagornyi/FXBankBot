import os
import logging
import asyncio
from typing import Dict, Optional
from contextlib import suppress

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
import redis.asyncio as redis

# ---------------------- CONFIG ----------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "fxbank-secret")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
BANK_PASSWORD = os.getenv("BANK_PASSWORD", "bank123")
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "10000"))

# ---------------------- LOGGING ----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("fxbank_bot")

# ---------------------- FASTAPI ----------------------
app = FastAPI()

# ---------------------- REDIS STORAGE ----------------------
redis_conn = redis.from_url(REDIS_URL)
storage = RedisStorage(redis=redis_conn)

# ---------------------- BOT / DISPATCHER ----------------------
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ---------------------- ROLES ----------------------
user_roles: Dict[int, str] = {}  # user_id -> "client" / "bank"

# ---------------------- FSM ----------------------
class NewOrder(StatesGroup):
    choosing_type = State()
    entering_amount = State()
    entering_currency_from = State()
    entering_currency_to = State()
    entering_rate = State()
    confirming = State()

# ---------------------- DATA STRUCTURES ----------------------
class Order:
    counter = 0

    def __init__(
        self,
        client_id: int,
        client_name: str,
        operation: str,
        amount: float,
        currency_from: str,
        currency_to: Optional[str],
        rate: float,
    ):
        Order.counter += 1
        self.id = Order.counter
        self.client_id = client_id
        self.client_name = client_name
        self.operation = operation  # buy, sell, convert
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
        return (
            f"📌 <b>Заявка #{self.id}</b>\n"
            f"👤 Клиент: {self.client_name}\n"
            f"💱 Операция: {op_text}\n"
            f"📊 Курс клиента: {self.rate}\n"
            f"📍 Статус: {self.status}"
        )

# ---------------------- STORAGE ----------------------
orders: Dict[int, Order] = {}

# ---------------------- KEYBOARDS ----------------------
client_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Новая заявка")],
        [KeyboardButton(text="💱 Курсы")],
    ],
    resize_keyboard=True,
)

bank_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Все заявки")],
        [KeyboardButton(text="💱 Курсы")],
    ],
    resize_keyboard=True,
)

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

# ---------------------- MOCK RATES ----------------------
def get_mock_rates() -> Dict[str, float]:
    return {
        "USD/UAH": 41.25,
        "EUR/UAH": 45.10,
        "PLN/UAH": 10.60,
        "EUR/USD": 1.0920,
        "PLN/USD": 0.2580,
        "EUR/PLN": 4.23,
    }

# ---------------------- HANDLERS ----------------------
@router.message(Command("start"))
async def cmd_start(message: Message):
    try:
        role = user_roles.get(message.from_user.id, "client")
        if role == "bank":
            await message.answer("🏦 Вы вошли как банк.", reply_markup=bank_kb)
        else:
            user_roles[message.from_user.id] = "client"
            await message.answer("👋 Добро пожаловать!\nВы вошли как клиент.", reply_markup=client_kb)
    except Exception as e:
        logger.error(f"cmd_start failed: {e}")

@router.message(Command("rate"))
async def cmd_rate(message: Message):
    try:
        rates = get_mock_rates()
        text = "💱 Текущие курсы:\n" + "\n".join([f"{k} = {v}" for k, v in rates.items()])
        await message.answer(text)
    except Exception as e:
        logger.error(f"cmd_rate failed: {e}")

@router.message(Command("bank"))
async def cmd_bank(message: Message):
    try:
        parts = message.text.strip().split()
        if len(parts) < 2:
            return await message.answer("❌ Укажите пароль: /bank <пароль>")
        if parts[1] == BANK_PASSWORD:
            user_roles[message.from_user.id] = "bank"
            await message.answer("🏦 Успешный вход. Вы вошли как банк.", reply_markup=bank_kb)
        else:
            await message.answer("❌ Неверный пароль.")
    except Exception as e:
        logger.error(f"cmd_bank failed: {e}")

# ---------------------- NEW ORDER ----------------------
@router.message(F.text == "➕ Новая заявка")
async def new_order(message: Message, state: FSMContext):
    await state.set_state(NewOrder.choosing_type)
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Покупка"), KeyboardButton(text="Продажа")],
            [KeyboardButton(text="Конвертация")]
        ],
        resize_keyboard=True
    )
    await message.answer("Выберите тип операции:", reply_markup=kb)

@router.message(NewOrder.choosing_type)
async def choose_type(message: Message, state: FSMContext):
    operation = message.text.lower()
    if operation not in ["покупка", "продажа", "конвертация"]:
        return await message.answer("❌ Выберите из предложенных кнопок.")
    await state.update_data(operation=operation)
    await state.set_state(NewOrder.entering_amount)
    await message.answer("Введите сумму:", reply_markup=types.ReplyKeyboardRemove())

@router.message(NewOrder.entering_amount)
async def enter_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        return await message.answer("❌ Введите число.")
    await state.update_data(amount=amount)
    await state.set_state(NewOrder.entering_currency_from)
    await message.answer("Введите валюту (например USD, EUR, UAH):")

@router.message(NewOrder.entering_currency_from)
async def enter_currency_from(message: Message, state: FSMContext):
    data = await state.get_data()
    operation = data["operation"]
    currency_from = message.text.upper()
    if operation == "конвертация":
        await state.update_data(currency_from=currency_from)
        await state.set_state(NewOrder.entering_currency_to)
        return await message.answer("Введите валюту для получения (например USD, EUR):")
    else:
        await state.update_data(currency_from=currency_from, currency_to="UAH")
        await state.set_state(NewOrder.entering_rate)
        return await message.answer("Введите курс:")

@router.message(NewOrder.entering_currency_to)
async def enter_currency_to(message: Message, state: FSMContext):
    currency_to = message.text.upper()
    await state.update_data(currency_to=currency_to)
    await state.set_state(NewOrder.entering_rate)
    await message.answer("Введите курс:")

@router.message(NewOrder.entering_rate)
async def enter_rate(message: Message, state: FSMContext):
    try:
        rate = float(message.text.replace(",", "."))
    except ValueError:
        return await message.answer("❌ Введите число.")
    data = await state.get_data()
    order = Order(
        client_id=message.from_user.id,
        client_name=message.from_user.first_name,
        operation=data["operation"],
        amount=data["amount"],
        currency_from=data["currency_from"],
        currency_to=data.get("currency_to"),
        rate=rate,
    )
    orders[order.id] = order
    await state.clear()
    await message.answer(f"✅ Заявка создана:\n{order.summary()}", reply_markup=client_kb)

    # уведомляем банк
    for uid, role in user_roles.items():
        if role == "bank":
            with suppress(Exception):
                await bot.send_message(uid, f"🔔 Новая заявка:\n{order.summary()}", reply_markup=bank_order_kb(order.id))

# ---------------------- BANK ----------------------
@router.message(F.text == "📋 Все заявки")
async def bank_all_orders(message: Message):
    if user_roles.get(message.from_user.id) != "bank":
        return await message.answer("⛔ У вас нет доступа.")
    if not orders:
        return await message.answer("📭 Заявок пока нет.")
    for order in orders.values():
        await message.answer(order.summary(), reply_markup=bank_order_kb(order.id))

@router.callback_query(F.data.startswith("accept:"))
async def cb_accept(call: CallbackQuery):
    if user_roles.get(call.from_user.id) != "bank":
        return await call.answer("Нет доступа", show_alert=True)
    oid = int(call.data.split(":")[1])
    o = orders.get(oid)
    if not o:
        return await call.answer("Заявка не найдена", show_alert=True)
    o.status = "accepted"
    await call.message.edit_text(o.summary())
    await call.answer("Заявка принята ✅")
    with suppress(Exception):
        await bot.send_message(o.client_id, f"✅ Ваша заявка #{o.id} принята банком.")

@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: CallbackQuery):
    if user_roles.get(call.from_user.id) != "bank":
        return await call.answer("Нет доступа", show_alert=True)
    oid = int(call.data.split(":")[1])
    o = orders.get(oid)
    if not o:
        return await call.answer("Заявка не найдена", show_alert=True)
    o.status = "rejected"
    await call.message.edit_text(o.summary())
    await call.answer("Заявка отклонена ❌")
    with suppress(Exception):
        await bot.send_message(o.client_id, f"❌ Ваша заявка #{o.id} отклонена банком.")

@router.callback_query(F.data.startswith("order:"))
async def cb_order(call: CallbackQuery):
    if user_roles.get(call.from_user.id) != "bank":
        return await call.answer("Нет доступа", show_alert=True)
    oid = int(call.data.split(":")[1])
    o = orders.get(oid)
    if not o:
        return await call.answer("Заявка не найдена", show_alert=True)
    o.status = "order"
    await call.message.edit_text(o.summary())
    await call.answer("Заявка сохранена как ордер 📌")
    with suppress(Exception):
        await bot.send_message(o.client_id, f"📌 Ваша заявка #{o.id} сохранена как ордер.")

# ---------------------- FastAPI + Webhook ----------------------
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"

@app.on_event("startup")
async def on_startup():
    logger.info("Starting up application...")
    try:
        await redis_conn.ping()
        logger.info("Redis connected OK.")
    except Exception as e:
        logger.warning(f"Redis недоступен: {e}")

    base = os.getenv("WEBHOOK_URL")
    if not base:
        host = os.getenv("RENDER_EXTERNAL_HOSTNAME")
        if host:
            base = f"https://{host}"
    if base:
        url = f"{base}{WEBHOOK_PATH}"
        try:
            await bot.set_webhook(url, secret_token=WEBHOOK_SECRET, allowed_updates=["message", "callback_query"])
            logger.info(f"Webhook set to {url}")
        except Exception as e:
            logger.error(f"Failed to set webhook: {e}")
    else:
        logger.warning("WEBHOOK_URL/RENDER_EXTERNAL_HOSTNAME не заданы — вебхук не установлен")
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
    return {"status": "ok", "service": "FXBankBot"}

# ---------------------- Запуск ----------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
