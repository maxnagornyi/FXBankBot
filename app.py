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
app = FastAPI(title="FXBankBot", version="1.2.0")

# ===================== REDIS (FSM) =====================
try:
    redis_conn = redis.from_url(REDIS_URL)
    storage = RedisStorage(redis_conn)
except Exception as e:
    logger.error(f"Redis init failed: {e}")
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

async def safe_answer(callback: CallbackQuery, text: Optional[str] = None, show_alert: bool = False):
    try:
        await callback.answer(text, show_alert=show_alert)
    except Exception as e:
        # Игнорируем "query is too old" и т.п., просто логируем
        logger.debug(f"callback.answer error: {e}")

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
            await safe_answer(callback, "Неизвестная роль", show_alert=True)
            return
        user_roles[callback.from_user.id] = role
        if role == "bank":
            await callback.message.edit_text("Роль установлена: 🏦 Банк")
            await callback.message.answer("Меню банка:", reply_markup=kb_main_bank())
        else:
            await callback.message.edit_text("Роль установлена: 👤 Клиент")
            await callback.message.answer("Меню клиента:", reply_markup=kb_main_client())
        await safe_answer(callback)
    except Exception as e:
        logger.error(f"cq_role failed: {e}")
        await safe_answer(callback, "Ошибка", show_alert=True)

# ===================== CLIENT FSM =====================
class ClientFSM(StatesGroup):
    entering_client_name = State()
    choosing_deal = State()
    entering_currency_from = State()
    entering_currency_to = State()
    entering_amount = State()
    entering_rate = State()

@router.message(F.text == "➕ Новая заявка")
async def new_order(message: Message, state: FSMContext):
    try:
        # Только клиент может создавать заявки
        if user_role(message.from_user.id) != "client":
            return await message.answer("⛔ Создавать заявки может только роль 'Клиент'.", reply_markup=kb_main_client())
        await state.set_state(ClientFSM.entering_client_name)
        await message.answer("✍️ Введите название клиента (компания/ФИО):", reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        logger.error(f"new_order failed: {e}")
        await message.answer("⚠️ Не удалось начать создание заявки.")

@router.message(ClientFSM.entering_client_name)
async def fsm_client_name(message: Message, state: FSMContext):
    try:
        client_name = (message.text or "").strip()
        if not client_name:
            return await message.answer("❌ Введите непустое имя клиента.")
        await state.update_data(client_name=client_name)
        await state.set_state(ClientFSM.choosing_deal)
        await message.answer("Выберите тип сделки:", reply_markup=ikb_deal_type())
    except Exception as e:
        logger.error(f"fsm_client_name failed: {e}")
        await message.answer("⚠️ Ошибка. Попробуйте ещё раз.")

@router.callback_query(ClientFSM.choosing_deal, F.data.in_(["deal:buy", "deal:sell", "deal:convert"]))
async def fsm_choose_deal(callback: CallbackQuery, state: FSMContext):
    try:
        deal = callback.data.split(":")[1]
        await state.update_data(deal=deal)
        await state.set_state(ClientFSM.entering_currency_from)
        if deal == "convert":
            await callback.message.answer("Введите валюту, которую ПРОДАЁМ (пример: USD):")
        else:
            await callback.message.answer("Введите валюту сделки (пример: USD):")
        await safe_answer(callback)
    except Exception as e:
        logger.error(f"fsm_choose_deal failed: {e}")
        await safe_answer(callback, "Ошибка", show_alert=True)

@router.message(ClientFSM.entering_currency_from)
async def fsm_currency_from(message: Message, state: FSMContext):
    try:
        cur = (message.text or "").upper().strip()
        if not cur or len(cur) < 3:
            return await message.answer("❌ Укажите код валюты, пример: USD, EUR, UAH.")
        await state.update_data(currency_from=cur)
        data = await state.get_data()
        if data.get("deal") == "convert":
            await state.set_state(ClientFSM.entering_currency_to)
            await message.answer("Введите валюту, которую ПОКУПАЕМ (пример: EUR):")
        else:
            await state.update_data(currency_to="UAH")
            await state.set_state(ClientFSM.entering_amount)
            await message.answer("Введите сумму (число):")
    except Exception as e:
        logger.error(f"fsm_currency_from failed: {e}")
        await message.answer("⚠️ Ошибка. Попробуйте ещё раз.")

@router.message(ClientFSM.entering_currency_to)
async def fsm_currency_to(message: Message, state: FSMContext):
    try:
        cur = (message.text or "").upper().strip()
        if not cur or len(cur) < 3:
            return await message.answer("❌ Укажите код валюты, пример: USD, EUR.")
        await state.update_data(currency_to=cur)
        await state.set_state(ClientFSM.entering_amount)
        await message.answer("Введите сумму (число):")
    except Exception as e:
        logger.error(f"fsm_currency_to failed: {e}")
        await message.answer("⚠️ Ошибка. Попробуйте ещё раз.")

@router.message(ClientFSM.entering_amount)
async def fsm_amount(message: Message, state: FSMContext):
    try:
        val = float((message.text or "").replace(",", "."))
        if val <= 0:
            raise ValueError("amount<=0")
        await state.update_data(amount=val)
        await state.set_state(ClientFSM.entering_rate)
        await message.answer(
            "Введите КУРС (ваш желаемый). Пара — BASE/QUOTE.\n"
            "Примеры:\n"
            "• Покупка/Продажа USD против UAH → курс USD/UAH\n"
            "• Конверсия USD→EUR → курс USD/EUR"
        )
    except ValueError:
        await message.answer("❌ Введите положительное число. Пример: 1000.50")
    except Exception as e:
        logger.error(f"fsm_amount failed: {e}")
        await message.answer("⚠️ Ошибка. Попробуйте ещё раз.")
# ==============================
# FSM: Client - продолжение
# ==============================
@router.message(ClientFSM.rate)
async def fsm_rate(message: types.Message, state: FSMContext):
    try:
        rate = float(message.text)
    except ValueError:
        await message.answer("❌ Курс должен быть числом. Попробуйте еще раз.")
        return

    await state.update_data(rate=rate)
    data = await state.get_data()

    order = Order(
        client_name=data["client_name"],
        operation=data["operation"],
        base_currency=data["base_currency"],
        quote_currency=data.get("quote_currency", "UAH"),
        amount=float(data["amount"]),
        rate=rate,
    )

    await state.update_data(order=order)
    await state.set_state(ClientFSM.confirm)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить", callback_data="confirm_order"
                ),
                InlineKeyboardButton(
                    text="❌ Отмена", callback_data="cancel_order"
                ),
            ]
        ]
    )
    await message.answer(
        f"📋 Ваша заявка:\n\n{order}", reply_markup=kb
    )


# ==============================
# FSM: Подтверждение
# ==============================
@router.callback_query(F.data == "confirm_order")
async def confirm_order(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order: Order = data.get("order")

    if not order:
        await callback.answer("❌ Ошибка: заявка не найдена.", show_alert=True)
        return

    # сохраняем в Redis
    order_key = f"order:{callback.from_user.id}:{int(callback.message.date.timestamp())}"
    await redis.set(order_key, order.model_dump_json(), ex=3600)

    await callback.message.answer(
        "✅ Заявка сохранена и отправлена банку.\nОжидайте ответа."
    )
    await state.clear()


@router.callback_query(F.data == "cancel_order")
async def cancel_order(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("❌ Заявка отменена.")
    await state.clear()


# ==============================
# FSM: Bank actions
# ==============================
@router.callback_query(F.data.startswith("accept:"))
async def cq_accept(callback: types.CallbackQuery):
    order_id = callback.data.split(":")[1]
    data = await redis.get(order_id)
    if not data:
        await callback.answer("❌ Заявка не найдена.", show_alert=True)
        return

    order = Order.model_validate_json(data)
    await callback.message.answer(f"✅ Заявка принята:\n\n{order}")


@router.callback_query(F.data.startswith("reject:"))
async def cq_reject(callback: types.CallbackQuery):
    order_id = callback.data.split(":")[1]
    data = await redis.get(order_id)
    if not data:
        await callback.answer("❌ Заявка не найдена.", show_alert=True)
        return

    order = Order.model_validate_json(data)
    await callback.message.answer(f"❌ Заявка отклонена:\n\n{order}")


# ==============================
# FastAPI endpoints
# ==============================
@app.on_event("startup")
async def on_startup():
    global redis
    redis = aioredis.from_url(
        REDIS_URL, decode_responses=True, encoding="utf-8"
    )
    logger.info("Redis connected OK.")

    # Webhook
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")


@app.on_event("shutdown")
async def on_shutdown():
    await bot.session.close()
    await redis.close()
    logger.info("Shutdown complete.")


@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
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
