import os
import asyncio
import logging
from typing import Dict, Optional
from contextlib import suppress

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
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

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    # Не падаем, но логируем — на Render токен должен быть задан
    logging.getLogger("fxbank_bot_boot").error("BOT_TOKEN env var is missing!")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "fxbank-secret").strip()
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"

BANK_PASSWORD = os.getenv("BANK_PASSWORD", "bank123")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "10000"))

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("fxbank_bot")

# ===================== FASTAPI =====================
app = FastAPI(title="FXBankBot", version="1.0")

# ===================== REDIS (FSM) =====================
try:
    redis_conn = redis.from_url(REDIS_URL)
    storage = RedisStorage(redis_conn)
except Exception as e:
    logger.error(f"Redis init failed: {e}")
    # На крайний случай можно было бы MemoryStorage, но по требованию — Redis
    raise

# ===================== AIROGRAM CORE =====================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ===================== RUNTIME STORAGE =====================
# Простая in-memory модель (демо). При перезапуске теряется — ок для MVP.
user_roles: Dict[int, str] = {}  # user_id -> "client" | "bank"

class Order:
    counter = 0

    def __init__(
        self,
        client_id: int,
        client_telegram: str,
        client_name: str,
        operation: str,              # "покупка" | "продажа" | "конвертация"
        amount: float,
        currency_from: str,
        currency_to: Optional[str],  # для конверсии или UAH для buy/sell
        rate: float,                 # курс клиента (обязательно)
    ):
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
        self.status = "new"          # new | accepted | rejected | order

    def summary(self) -> str:
        if self.operation == "конвертация":
            line = f"{self.amount} {self.currency_from} → {self.currency_to}"
        else:
            line = f"{self.operation} {self.amount} {self.currency_from} (против UAH)"
        tg = f" (@{self.client_telegram})" if self.client_telegram else ""
        return (
            f"📌 <b>Заявка #{self.id}</b>\n"
            f"👤 Клиент: {self.client_name}{tg}\n"
            f"💱 Операция: {line}\n"
            f"📊 Курс клиента: {self.rate}\n"
            f"📍 Статус: {self.status}"
        )

orders: Dict[int, Order] = {}

# ===================== KEYBOARDS =====================
def kb_main_client() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Новая заявка")],
            [KeyboardButton(text="💱 Курсы")],
        ],
        resize_keyboard=True,
    )

def kb_main_bank() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Все заявки")],
            [KeyboardButton(text="💱 Курсы")],
        ],
        resize_keyboard=True,
    )

def ikb_role() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👤 Я клиент", callback_data="role:client"),
            InlineKeyboardButton(text="🏦 Я банк", callback_data="role:bank"),
        ]
    ])

def ikb_deal_type() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Купить валюту", callback_data="deal:buy")],
        [InlineKeyboardButton(text="Продать валюту", callback_data="deal:sell")],
        [InlineKeyboardButton(text="Конверсия (валюта→валюта)", callback_data="deal:convert")],
    ])

def ikb_bank_order(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{order_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{order_id}"),
        ],
        [
            InlineKeyboardButton(text="📌 Сохранить как ордер", callback_data=f"order:{order_id}")
        ]
    ])

# ===================== RATES (STUB) =====================
def get_stub_rates() -> Dict[str, float]:
    # Базовый набор (можно заменить на Bloomberg/LSEG)
    return {
        "USD/UAH": 41.25,
        "EUR/UAH": 45.10,
        "PLN/UAH": 10.60,
        "EUR/USD": 1.0920,
        "USD/PLN": 3.8760,   # инверсия от 0.2580
        "EUR/PLN": 4.2326,   # EUR/USD * USD/PLN
    }

def format_rates_text() -> str:
    r = get_stub_rates()
    return "\n".join([f"{k} = {v}" for k, v in r.items()])

# ===================== HELPERS =====================
async def reply_safe(chat_id: int, text: str, **kwargs):
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logger.error(f"send_message error: {e}")

def user_role(user_id: int) -> str:
    return user_roles.get(user_id, "client")

# ===================== COMMANDS & COMMON =====================
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    try:
        await state.clear()
        # по умолчанию роль — клиент
        user_roles[message.from_user.id] = user_roles.get(message.from_user.id, "client")
        await message.answer(
            "👋 Добро пожаловать в FXBankBot!\nВыберите роль:",
            reply_markup=ikb_role(),
        )
    except Exception as e:
        logger.error(f"/start failed: {e}")
        await message.answer("⚠️ Ошибка при /start")

@router.message(Command("menu"))
async def cmd_menu(message: Message):
    try:
        role = user_role(message.from_user.id)
        if role == "bank":
            await message.answer("🏦 Меню банка:", reply_markup=kb_main_bank())
        else:
            await message.answer("👤 Меню клиента:", reply_markup=kb_main_client())
    except Exception as e:
        logger.error(f"/menu failed: {e}")
        await message.answer("⚠️ Ошибка при отображении меню.")

@router.message(Command("rate"))
@router.message(F.text == "💱 Курсы")
async def cmd_rate(message: Message):
    try:
        await message.answer("💱 Текущие курсы (заглушка):\n" + format_rates_text())
    except Exception as e:
        logger.error(f"/rate failed: {e}")
        await message.answer("⚠️ Не удалось получить курсы.")

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    try:
        await state.clear()
        role = user_role(message.from_user.id)
        kb = kb_main_bank() if role == "bank" else kb_main_client()
        await message.answer("Действие отменено. Главное меню:", reply_markup=kb)
    except Exception as e:
        logger.error(f"/cancel failed: {e}")
        await message.answer("⚠️ Ошибка отмены.")

@router.message(Command("bank"))
async def cmd_bank(message: Message):
    try:
        parts = (message.text or "").strip().split(maxsplit=1)
        if len(parts) < 2:
            return await message.answer("❌ Укажите пароль: /bank <пароль>")
        if parts[1] == BANK_PASSWORD:
            user_roles[message.from_user.id] = "bank"
            await message.answer("🏦 Успешный вход. Вы вошли как банк.", reply_markup=kb_main_bank())
        else:
            await message.answer("❌ Неверный пароль.")
    except Exception as e:
        logger.error(f"/bank failed: {e}")
        await message.answer("⚠️ Ошибка входа банка.")

@router.callback_query(F.data.startswith("role:"))
async def cq_role(callback: CallbackQuery):
    try:
        _, role = callback.data.split(":")
        if role not in ("client", "bank"):
            await callback.answer("Неизвестная роль", show_alert=True)
            return
        user_roles[callback.from_user.id] = role
        if role == "bank":
            await callback.message.edit_text("Роль установлена: 🏦 Банк", reply_markup=None)
            await callback.message.answer("Меню банка:", reply_markup=kb_main_bank())
        else:
            await callback.message.edit_text("Роль установлена: 👤 Клиент", reply_markup=None)
            await callback.message.answer("Меню клиента:", reply_markup=kb_main_client())
        await callback.answer()
    except Exception as e:
        logger.error(f"cq_role failed: {e}")
        with suppress(Exception):
            await callback.answer("Ошибка", show_alert=True)

# ===================== CLIENT FSM =====================
class ClientFSM(StatesGroup):
    entering_client_name = State()
    choosing_deal = State()
    entering_currency_from = State()
    entering_currency_to = State()
    entering_amount = State()
    entering_rate = State()
# ==========================
# Client FSM (client flow)
# ==========================
class ClientFSM(StatesGroup):
    waiting_for_client_name = State()
    waiting_for_operation = State()
    waiting_for_currency_from = State()
    waiting_for_currency_to = State()
    waiting_for_amount = State()
    waiting_for_rate = State()

# Кнопка "➕ Новая заявка"
@router.message(F.text == "➕ Новая заявка")
async def new_order(message: Message, state: FSMContext):
    await state.set_state(ClientFSM.waiting_for_client_name)
    await message.answer("Введите название клиента:")

# Ввод имени клиента
@router.message(ClientFSM.waiting_for_client_name)
async def process_client_name(message: Message, state: FSMContext):
    await state.update_data(client_name=message.text.strip())
    await state.set_state(ClientFSM.waiting_for_operation)
    await message.answer("Выберите операцию:", reply_markup=operation_keyboard())

# Выбор операции
@router.message(ClientFSM.waiting_for_operation, F.text.in_(["Купить", "Продать", "Конверсия"]))
async def process_operation(message: Message, state: FSMContext):
    operation = message.text
    await state.update_data(operation=operation)

    if operation == "Конверсия":
        await state.set_state(ClientFSM.waiting_for_currency_from)
        await message.answer("Укажите валюту, которую продаём (например, USD):")
    else:
        await state.set_state(ClientFSM.waiting_for_currency_to)
        await message.answer("Укажите валюту (например, USD):")

# Валюта продажи (для конверсии)
@router.message(ClientFSM.waiting_for_currency_from)
async def process_currency_from(message: Message, state: FSMContext):
    await state.update_data(currency_from=message.text.strip().upper())
    await state.set_state(ClientFSM.waiting_for_currency_to)
    await message.answer("Укажите валюту, которую покупаем (например, EUR):")

# Валюта покупки (или основная для покупки/продажи)
@router.message(ClientFSM.waiting_for_currency_to)
async def process_currency_to(message: Message, state: FSMContext):
    await state.update_data(currency_to=message.text.strip().upper())
    await state.set_state(ClientFSM.waiting_for_amount)
    await message.answer("Введите сумму:")

# Сумма
@router.message(ClientFSM.waiting_for_amount)
async def process_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введите корректное число.")
        return

    await state.update_data(amount=amount)
    await state.set_state(ClientFSM.waiting_for_rate)
    await message.answer("Введите курс сделки:")

# Курс
@router.message(ClientFSM.waiting_for_rate)
async def process_rate(message: Message, state: FSMContext):
    try:
        rate = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введите корректное число.")
        return

    await state.update_data(rate=rate)
    data = await state.get_data()

    # Генерация ID заявки
    import uuid
    order_id = str(uuid.uuid4())[:8]

    # Сохраняем заявку в Redis
    order = {
        "id": order_id,
        "client_name": data["client_name"],
        "operation": data["operation"],
        "currency_from": data.get("currency_from"),
        "currency_to": data["currency_to"],
        "amount": data["amount"],
        "rate": data["rate"],
        "status": "pending",
    }
    await redis.hset("orders", order_id, str(order))

    await message.answer(
        f"✅ Заявка создана:\n"
        f"Клиент: {order['client_name']}\n"
        f"Операция: {order['operation']}\n"
        f"Из валюты: {order.get('currency_from', 'UAH')}\n"
        f"В валюту: {order['currency_to']}\n"
        f"Сумма: {order['amount']}\n"
        f"Курс: {order['rate']}\n"
        f"ID: {order_id}"
    )
    await state.clear()
# ==========================
# Bank logic (approve/reject orders)
# ==========================
@router.message(F.text == "📋 Все заявки")
async def list_orders(message: Message):
    orders = await redis.hgetall("orders")
    if not orders:
        await message.answer("❌ Заявок нет.")
        return

    for oid, odata in orders.items():
        order = eval(odata)  # упрощённо, лучше JSON
        text = (
            f"📝 Заявка {order['id']}\n"
            f"Клиент: {order['client_name']}\n"
            f"Операция: {order['operation']}\n"
            f"Из валюты: {order.get('currency_from', 'UAH')}\n"
            f"В валюту: {order['currency_to']}\n"
            f"Сумма: {order['amount']}\n"
            f"Курс: {order['rate']}\n"
            f"Статус: {order['status']}"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Принять", callback_data=f"accept:{order['id']}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{order['id']}"),
                ]
            ]
        )
        await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("accept:"))
async def accept_order(callback: CallbackQuery):
    order_id = callback.data.split(":")[1]
    odata = await redis.hget("orders", order_id)
    if not odata:
        await callback.answer("❌ Заявка не найдена.", show_alert=True)
        return

    order = eval(odata)
    order["status"] = "accepted"
    await redis.hset("orders", order_id, str(order))

    await callback.message.edit_text(f"✅ Заявка {order_id} принята.")
    await callback.answer("Принято")


@router.callback_query(F.data.startswith("reject:"))
async def reject_order(callback: CallbackQuery):
    order_id = callback.data.split(":")[1]
    odata = await redis.hget("orders", order_id)
    if not odata:
        await callback.answer("❌ Заявка не найдена.", show_alert=True)
        return

    order = eval(odata)
    order["status"] = "rejected"
    await redis.hset("orders", order_id, str(order))

    await callback.message.edit_text(f"❌ Заявка {order_id} отклонена.")
    await callback.answer("Отклонено")


# ==========================
# Startup & Webhook
# ==========================
app = FastAPI()
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{WEBAPP_URL}{WEBHOOK_PATH}"


@app.on_event("startup")
async def on_startup():
    logger.info("Starting up application...")
    # Подключение Redis
    global redis
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    logger.info("Redis connected OK.")

    # Ставим вебхук
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")
    logger.info("Startup complete.")


@app.on_event("shutdown")
async def on_shutdown():
    await bot.session.close()
    await redis.close()


@app.post(WEBHOOK_PATH)
async def webhook(update: dict):
    telegram_update = Update(**update)
    await dp.feed_update(bot, telegram_update)
    return {"ok": True}


@app.get("/")
async def root():
    return {"status": "ok", "bot": "FXBankBot"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 10000)),
        reload=False,
    )
