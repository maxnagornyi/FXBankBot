import os
import logging
import asyncio
from typing import Dict, Optional

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.filters import Command
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
logger = logging.getLogger("FXBankBot")

# ---------------------- FASTAPI ----------------------
app = FastAPI()

# ---------------------- REDIS STORAGE ----------------------
redis_conn = redis.from_url(REDIS_URL)
storage = RedisStorage(redis=redis_conn)

# ---------------------- BOT / DISPATCHER ----------------------
bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ---------------------- ROLES ----------------------
user_roles: Dict[int, str] = {}  # user_id -> "client" / "bank"

# ---------------------- FSM ----------------------
class NewOrder(StatesGroup):
    choosing_type = State()
    entering_amount = State()
    entering_currency = State()
    entering_rate = State()
    confirming = State()

# ---------------------- DATA STRUCTURES ----------------------
class Order:
    counter = 0

    def __init__(self, client_id: int, client_name: str, operation: str, amount: float, currency: str, rate: float):
        Order.counter += 1
        self.id = Order.counter
        self.client_id = client_id
        self.client_name = client_name
        self.operation = operation  # buy, sell, convert
        self.amount = amount
        self.currency = currency
        self.rate = rate
        self.status = "new"

    def summary(self) -> str:
        return (
            f"📌 <b>Заявка #{self.id}</b>\n"
            f"👤 Клиент: {self.client_name}\n"
            f"💱 Операция: {self.operation}\n"
            f"💵 Сумма: {self.amount} {self.currency}\n"
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

# ---------------------- HANDLERS ----------------------
@router.message(Command("start"))
async def cmd_start(message: Message):
    role = user_roles.get(message.from_user.id, "client")
    if role == "bank":
        await message.answer("🏦 Вы вошли как банк.", reply_markup=bank_kb)
    else:
        user_roles[message.from_user.id] = "client"
        await message.answer("👋 Добро пожаловать!\nВы вошли как клиент.", reply_markup=client_kb)

@router.message(Command("ping"))
async def cmd_ping(message: Message):
    await message.answer("pong")

@router.message(Command("restart"))
async def cmd_restart(message: Message):
    url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/webhook/{WEBHOOK_SECRET}"
    try:
        await bot.set_webhook(url)
        await message.answer("♻️ Перезапуск: вебхук переустановлен.")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка при установке вебхука: {e}")

@router.message(Command("rate"))
async def cmd_rate(message: Message):
    text = (
        "💱 Текущие курсы (заглушка):\n"
        "USD/UAH = 41.25\n"
        "EUR/UAH = 45.10\n"
        "PLN/UAH = 10.60\n"
        "EUR/USD = 1.0920\n"
        "PLN/USD = 0.2580"
    )
    await message.answer(text)

@router.message(Command("bank"))
async def cmd_bank(message: Message):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("❌ Укажите пароль: /bank <пароль>")
        return
    if parts[1] == BANK_PASSWORD:
        user_roles[message.from_user.id] = "bank"
        await message.answer("🏦 Успешный вход. Вы вошли как банк.", reply_markup=bank_kb)
    else:
        await message.answer("❌ Неверный пароль.")

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
        await message.answer("❌ Выберите из предложенных кнопок.")
        return
    await state.update_data(operation=operation)
    await state.set_state(NewOrder.entering_amount)
    await message.answer("Введите сумму:", reply_markup=types.ReplyKeyboardRemove())

@router.message(NewOrder.entering_amount)
async def enter_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Введите число.")
        return
    await state.update_data(amount=amount)
    await state.set_state(NewOrder.entering_currency)
    await message.answer("Введите валюту (например USD, EUR):")

@router.message(NewOrder.entering_currency)
async def enter_currency(message: Message, state: FSMContext):
    currency = message.text.upper()
    await state.update_data(currency=currency)
    await state.set_state(NewOrder.entering_rate)
    await message.answer("Введите курс:")

@router.message(NewOrder.entering_rate)
async def enter_rate(message: Message, state: FSMContext):
    try:
        rate = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Введите число.")
        return
    await state.update_data(rate=rate)
    data = await state.get_data()
    order = Order(
        client_id=message.from_user.id,
        client_name=message.from_user.first_name,
        operation=data["operation"],
        amount=data["amount"],
        currency=data["currency"],
        rate=rate
    )
    orders[order.id] = order
    await state.clear()
    await message.answer(f"✅ Заявка создана:\n{order.summary()}", reply_markup=client_kb)

    # уведомляем банк
    for uid, role in user_roles.items():
        if role == "bank":
            try:
                await bot.send_message(uid, f"🔔 Новая заявка:\n{order.summary()}", reply_markup=bank_order_kb(order.id))
            except Exception as e:
                logger.error(f"Ошибка при уведомлении банка: {e}")
# ---------------------- BANK: список заявок ----------------------
@router.message(F.text == "📋 Все заявки")
async def bank_all_orders(message: Message):
    if user_roles.get(message.from_user.id) != "bank":
        return await message.answer("⛔ У вас нет доступа.")
    if not orders:
        return await message.answer("📭 Заявок пока нет.")
    for order in orders.values():
        await message.answer(order.summary(), reply_markup=bank_order_kb(order.id))


# ---------------------- INLINE CALLBACKS (банк) ----------------------
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
    # уведомим клиента
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
    """
    Переинициализируем бота корректно для aiogram 3.7:
    - parse_mode задаём через DefaultBotProperties
    - перевешиваем router на новый Dispatcher
    - ставим webhook
    """
    from aiogram.enums import ParseMode
    from aiogram.client.default import DefaultBotProperties
    global bot, dp

    # построим storage из уже заданного redis_conn (если не поднимется — aiogram сам бросит исключение на командах)
    try:
        await redis_conn.ping()
        storage = RedisStorage(redis=redis_conn)
        logging.info("Redis OK — FSM будет храниться в RedisStorage")
    except Exception as e:
        logging.warning(f"Redis недоступен ({e}) — переключаемся на память")
        from aiogram.fsm.storage.memory import MemoryStorage
        storage = MemoryStorage()

    # корректная инициализация под 3.7
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    # вычислим URL вебхука
    base = os.getenv("WEBHOOK_URL")
    if not base:
        host = os.getenv("RENDER_EXTERNAL_HOSTNAME")
        if host:
            base = f"https://{host}"
    if base:
        url = f"{base}{WEBHOOK_PATH}"
        await bot.set_webhook(url, secret_token=WEBHOOK_SECRET)
        logging.info(f"Webhook set to {url}")
    else:
        logging.warning("WEBHOOK_URL/RENDER_EXTERNAL_HOSTNAME не заданы — вебхук не установлен")


@app.on_event("shutdown")
async def on_shutdown():
    with suppress(Exception):
        await bot.delete_webhook()
    logging.info("Shutdown complete.")


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    update = await request.json()
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}


@app.get("/")
async def health():
    # Healthcheck для Render
    return {"status": "ok", "service": "FXBankBot"}


# ---------------------- Запуск ----------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
