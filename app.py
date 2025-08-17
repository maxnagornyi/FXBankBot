import logging
import os
import uvicorn
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

# --------------------------------------
# Настройки
# --------------------------------------
TOKEN = os.getenv("BOT_TOKEN", "TEST:TOKEN")
WEBHOOK_SECRET = "fxbank-secret"
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"https://fxbankbot.onrender.com{WEBHOOK_PATH}"

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
BANK_PASSWORD = os.getenv("BANK_PASSWORD", "bank123")

# --------------------------------------
# Инициализация
# --------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("FXBankBot")

bot = Bot(token=TOKEN, parse_mode="HTML")
storage = RedisStorage.from_url(REDIS_URL)
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

app = FastAPI()

# --------------------------------------
# Хранилище заявок
# --------------------------------------
orders = {}
order_counter = 0

class Order:
    def __init__(self, client, operation, amount, currency, rate):
        global order_counter
        order_counter += 1
        self.id = order_counter
        self.client = client
        self.operation = operation
        self.amount = amount
        self.currency = currency
        self.rate = rate
        self.status = "new"

    def summary(self):
        return (f"Заявка #{self.id}
"
                f"Клиент: {self.client}
"
                f"Операция: {self.operation}
"
                f"Сумма: {self.amount} {self.currency}
"
                f"Курс клиента: {self.rate}
"
                f"Статус: {self.status}")

# --------------------------------------
# FSM для клиента
# --------------------------------------
class ClientStates(StatesGroup):
    choosing_operation = State()
    entering_amount = State()
    entering_currency = State()
    entering_rate = State()

# --------------------------------------
# FSM для банка
# --------------------------------------
bank_users = set()

# --------------------------------------
# Клавиатуры
# --------------------------------------
def client_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Новая заявка")],
            [KeyboardButton(text="💱 Курсы")]
        ],
        resize_keyboard=True
    )

def bank_main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Все заявки")]
        ],
        resize_keyboard=True
    )

def order_inline_menu(order_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{order_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{order_id}")],
        [InlineKeyboardButton(text="📌 Ордер", callback_data=f"order:{order_id}")]
    ])

# --------------------------------------
# Команды клиента
# --------------------------------------
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if message.from_user.id in bank_users:
        await message.answer("🏦 Вы вошли как банк.", reply_markup=bank_main_menu())
    else:
        await message.answer("👋 Добро пожаловать! Выберите действие:", reply_markup=client_main_menu())

@router.message(F.text == "💱 Курсы")
@router.message(Command("rate"))
async def cmd_rate(message: Message):
    await message.answer(
        "💱 Текущие курсы (заглушка):
"
        "USD/UAH = 41.25
"
        "EUR/UAH = 45.10
"
        "PLN/UAH = 10.60
"
        "EUR/USD = 1.0920
"
        "PLN/USD = 0.2580"
    )

@router.message(F.text == "➕ Новая заявка")
async def new_order(message: Message, state: FSMContext):
    await state.set_state(ClientStates.choosing_operation)
    await message.answer("Выберите тип операции: покупка / продажа / конверсия")

@router.message(ClientStates.choosing_operation)
async def process_operation(message: Message, state: FSMContext):
    await state.update_data(operation=message.text)
    await state.set_state(ClientStates.entering_amount)
    await message.answer("Введите сумму")

@router.message(ClientStates.entering_amount)
async def process_amount(message: Message, state: FSMContext):
    await state.update_data(amount=message.text)
    await state.set_state(ClientStates.entering_currency)
    await message.answer("Введите валюту (например USD, EUR, PLN)")

@router.message(ClientStates.entering_currency)
async def process_currency(message: Message, state: FSMContext):
    await state.update_data(currency=message.text)
    await state.set_state(ClientStates.entering_rate)
    await message.answer("Введите ваш курс")

@router.message(ClientStates.entering_rate)
async def process_rate(message: Message, state: FSMContext):
    data = await state.get_data()
    order = Order(
        client=message.from_user.first_name,
        operation=data["operation"],
        amount=data["amount"],
        currency=data["currency"],
        rate=message.text
    )
    orders[order.id] = order
    await message.answer(f"✅ Заявка #{order.id} создана!

{order.summary()}", reply_markup=client_main_menu())
    await state.clear()

# --------------------------------------
# Команды банка
# --------------------------------------
@router.message(Command("bank"))
async def bank_login(message: Message):
    parts = message.text.split()
    if len(parts) == 2 and parts[1] == BANK_PASSWORD:
        bank_users.add(message.from_user.id)
        await message.answer("🏦 Вы вошли как банк.", reply_markup=bank_main_menu())
    else:
        await message.answer("❌ Неверный пароль.")

@router.message(F.text == "📋 Все заявки")
async def list_orders(message: Message):
    if message.from_user.id not in bank_users:
        await message.answer("⛔ У вас нет доступа.")
        return
    if not orders:
        await message.answer("📭 Заявок пока нет.")
    for order in orders.values():
        await message.answer(order.summary(), reply_markup=order_inline_menu(order.id))

@router.callback_query(F.data.startswith("accept"))
async def cb_accept(call: CallbackQuery):
    order_id = int(call.data.split(":")[1])
    if order_id in orders:
        orders[order_id].status = "accepted"
        await call.message.edit_text(orders[order_id].summary())
        await call.answer("Заявка принята ✅")

@router.callback_query(F.data.startswith("reject"))
async def cb_reject(call: CallbackQuery):
    order_id = int(call.data.split(":")[1])
    if order_id in orders:
        orders[order_id].status = "rejected"
        await call.message.edit_text(orders[order_id].summary())
        await call.answer("Заявка отклонена ❌")

@router.callback_query(F.data.startswith("order"))
async def cb_order(call: CallbackQuery):
    order_id = int(call.data.split(":")[1])
    if order_id in orders:
        orders[order_id].status = "order"
        await call.message.edit_text(orders[order_id].summary())
        await call.answer("Заявка сохранена как ордер 📌")

# --------------------------------------
# Служебные команды
# --------------------------------------
@router.message(Command("ping"))
async def ping(message: Message):
    await message.answer("pong")

@router.message(Command("status"))
async def status(message: Message):
    await message.answer("🔎 Bot is running. Storage: Redis ok.")

@router.message(Command("restart"))
async def restart(message: Message):
    await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    await message.answer("♻️ Перезапуск: вебхук переустановлен.")

# --------------------------------------
# FastAPI endpoints
# --------------------------------------
@app.on_event("startup")
async def on_startup():
    logger.info("Connected to Redis, using RedisStorage.")
    await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()
    logger.info("Shutdown complete.")

@app.post(WEBHOOK_PATH)
async def webhook_handler(request: Request):
    update = await request.json()
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "ok", "bot": "FXBankBot"}

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
