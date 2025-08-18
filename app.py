import os
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
    logging.getLogger("fxbank_bot_boot").error("BOT_TOKEN is missing!")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "fxbank-secret").strip()
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"

BANK_PASSWORD = os.getenv("BANK_PASSWORD", "bank123")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "10000"))

# Where to set webhook (Render gives RENDER_EXTERNAL_HOSTNAME)
WEBHOOK_BASE = os.getenv("WEBHOOK_URL") or (
    f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}" if os.getenv("RENDER_EXTERNAL_HOSTNAME") else ""
)

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("fxbank_bot")

# ===================== FASTAPI =====================
app = FastAPI(title="FXBankBot", version="1.3.0")

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

# ===================== RUNTIME STORAGE (in-memory demo) =====================
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
        currency_to: Optional[str],  # UAH для buy/sell; другая валюта для конверсии
        rate: float,                 # курс клиента
        amount_side: Optional[str] = None,  # только для конверсии: "sell"|"buy"
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
        self.amount_side = amount_side
        self.status = "new"          # new | accepted | rejected | order

    def summary(self) -> str:
        if self.operation == "конвертация":
            side_txt = " (сумма продажи)" if self.amount_side == "sell" else (" (сумма покупки)" if self.amount_side == "buy" else "")
            line = f"{self.amount} {self.currency_from} → {self.currency_to}{side_txt}"
        else:
            line = f"{self.operation} {self.amount} {self.currency_from} (против UAH)"
        tg = f" (@{self.client_telegram})" if self.client_telegram else ""
        return (
            f"📌 <b>Заявка #{self.id}</b>\n"
            f"👤 Клиент: {self.client_name}{tg}\n"
            f"💱 Операция: {line}\n"
            f"📊 Курс клиента (BASE/QUOTE): {self.rate}\n"
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

def ikb_amount_side() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ввожу сумму ПРОДАЖИ (BASE)", callback_data="as:sell")],
        [InlineKeyboardButton(text="Ввожу сумму ПОКУПКИ (QUOTE)", callback_data="as:buy")],
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
    # Базовый набор — в будущем подменим на Bloomberg/LSEG
    return {
        "USD/UAH": 41.25,
        "EUR/UAH": 45.10,
        "PLN/UAH": 10.60,
        "EUR/USD": 1.0920,
        "USD/PLN": 3.8760,
        "EUR/PLN": 4.2326,
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

async def safe_cb_answer(cb: CallbackQuery, text: Optional[str] = None, show_alert: bool = False):
    with suppress(Exception):
        await cb.answer(text, show_alert=show_alert)

# ===================== COMMANDS & COMMON =====================
@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    try:
        await state.clear()
        user_roles[message.from_user.id] = user_roles.get(message.from_user.id, "client")
        await message.answer("👋 Добро пожаловать в FXBankBot!\nВыберите роль:", reply_markup=ikb_role())
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
            return await message.answer("❌ Укажите пароль: /bank пароль")
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
            return await safe_cb_answer(callback, "Неизвестная роль", show_alert=True)
        user_roles[callback.from_user.id] = role
        if role == "bank":
            await callback.message.edit_text("Роль установлена: 🏦 Банк")
            await callback.message.answer("Меню банка:", reply_markup=kb_main_bank())
        else:
            await callback.message.edit_text("Роль установлена: 👤 Клиент")
            await callback.message.answer("Меню клиента:", reply_markup=kb_main_client())
        await safe_cb_answer(callback)
    except Exception as e:
        logger.error(f"cq_role failed: {e}")
        await safe_cb_answer(callback, "Ошибка", show_alert=True)

# ===================== CLIENT FSM (states) =====================
class ClientFSM(StatesGroup):
    entering_client_name = State()
    choosing_deal = State()
    entering_currency_from = State()
    entering_currency_to = State()     # только для конверсии
    choosing_amount_side = State()     # только для конверсии: sell/buy
    entering_amount = State()
    entering_rate = State()
# ===================== CLIENT HANDLERS =====================

@router.message(F.text == "➕ Новая заявка")
async def new_request(message: Message, state: FSMContext):
    try:
        await state.clear()
        await state.set_state(ClientFSM.entering_client_name)
        await message.answer("👤 Введите ваше имя или название компании:")
    except Exception as e:
        logger.error(f"new_request failed: {e}")
        await message.answer("⚠️ Ошибка при создании заявки.")

@router.message(ClientFSM.entering_client_name)
async def fsm_client_name(message: Message, state: FSMContext):
    try:
        await state.update_data(client_name=message.text.strip())
        await state.set_state(ClientFSM.choosing_deal)
        await message.answer("Выберите тип сделки:", reply_markup=ikb_deal_type())
    except Exception as e:
        logger.error(f"fsm_client_name failed: {e}")
        await message.answer("⚠️ Ошибка при вводе имени.")

@router.callback_query(F.data.startswith("deal:"))
async def cq_deal(callback: CallbackQuery, state: FSMContext):
    try:
        deal_type = callback.data.split(":")[1]
        if deal_type == "buy":
            await state.update_data(operation="покупка", currency_to="UAH")
        elif deal_type == "sell":
            await state.update_data(operation="продажа", currency_to="UAH")
        elif deal_type == "convert":
            await state.update_data(operation="конвертация")
            await state.set_state(ClientFSM.entering_currency_from)
            await callback.message.edit_text("Введите валюту, которую хотите ПРОДАТЬ (например, USD):")
            return await safe_cb_answer(callback)
        else:
            return await safe_cb_answer(callback, "❌ Неизвестный тип сделки", show_alert=True)

        await state.set_state(ClientFSM.entering_currency_from)
        await callback.message.edit_text("Введите валюту, которую хотите купить/продать (например, USD):")
        await safe_cb_answer(callback)
    except Exception as e:
        logger.error(f"cq_deal failed: {e}")
        await safe_cb_answer(callback, "⚠️ Ошибка", show_alert=True)

@router.message(ClientFSM.entering_currency_from)
async def fsm_currency_from(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        operation = data.get("operation")
        currency_from = message.text.strip().upper()
        await state.update_data(currency_from=currency_from)

        if operation == "конвертация":
            await state.set_state(ClientFSM.entering_currency_to)
            await message.answer("Введите валюту, которую хотите ПОЛУЧИТЬ (например, EUR):")
        else:
            await state.set_state(ClientFSM.entering_amount)
            await message.answer(f"Введите сумму в {currency_from}:")
    except Exception as e:
        logger.error(f"fsm_currency_from failed: {e}")
        await message.answer("⚠️ Ошибка при вводе валюты.")

@router.message(ClientFSM.entering_currency_to)
async def fsm_currency_to(message: Message, state: FSMContext):
    try:
        await state.update_data(currency_to=message.text.strip().upper())
        await state.set_state(ClientFSM.choosing_amount_side)
        await message.answer("Укажите, какую сумму вводите:", reply_markup=ikb_amount_side())
    except Exception as e:
        logger.error(f"fsm_currency_to failed: {e}")
        await message.answer("⚠️ Ошибка при вводе второй валюты.")

@router.callback_query(F.data.startswith("as:"))
async def cq_amount_side(callback: CallbackQuery, state: FSMContext):
    try:
        side = callback.data.split(":")[1]
        if side not in ("sell", "buy"):
            return await safe_cb_answer(callback, "❌ Некорректный выбор", show_alert=True)
        await state.update_data(amount_side=side)
        await state.set_state(ClientFSM.entering_amount)
        await callback.message.edit_text("Введите сумму:")
        await safe_cb_answer(callback)
    except Exception as e:
        logger.error(f"cq_amount_side failed: {e}")
        await safe_cb_answer(callback, "⚠️ Ошибка", show_alert=True)

@router.message(ClientFSM.entering_amount)
async def fsm_amount(message: Message, state: FSMContext):
    try:
        try:
            amount = float(message.text.replace(",", "."))
        except ValueError:
            return await message.answer("❌ Введите число, например: 1000.50")

        await state.update_data(amount=amount)
        await state.set_state(ClientFSM.entering_rate)
        await message.answer("Введите ваш курс (BASE/QUOTE):")
    except Exception as e:
        logger.error(f"fsm_amount failed: {e}")
        await message.answer("⚠️ Ошибка при вводе суммы.")

@router.message(ClientFSM.entering_rate)
async def fsm_rate(message: Message, state: FSMContext):
    try:
        try:
            rate = float(message.text.replace(",", "."))
        except ValueError:
            return await message.answer("❌ Введите число, например: 41.25")

        data = await state.get_data()
        await state.update_data(rate=rate)

        # Собираем заявку
        order = Order(
            client_id=message.from_user.id,
            client_telegram=message.from_user.username or "",
            client_name=data.get("client_name", "Без имени"),
            operation=data["operation"],
            amount=data["amount"],
            currency_from=data["currency_from"],
            currency_to=data.get("currency_to"),
            rate=rate,
            amount_side=data.get("amount_side"),
        )
        orders[order.id] = order

        await state.clear()
        await message.answer("✅ Ваша заявка создана:\n" + order.summary())

        # Отправляем банку
        for uid, role in user_roles.items():
            if role == "bank":
                await reply_safe(uid, "📨 Новая заявка:\n" + order.summary(),
                                 reply_markup=ikb_bank_order(order.id))
    except Exception as e:
        logger.error(f"fsm_rate failed: {e}")
        await message.answer("⚠️ Ошибка при вводе курса.")
# ===================== BANK HANDLERS =====================

@router.message(F.text == "📋 Все заявки")
async def bank_all_orders(message: Message):
    try:
        if user_role(message.from_user.id) != "bank":
            return await message.answer("⛔ У вас нет доступа. Войдите как банк: /bank <пароль>")
        if not orders:
            return await message.answer("📭 Заявок пока нет.")
        # Покажем по одной, чтобы inline-кнопки работали у каждой
        for order in orders.values():
            await message.answer(order.summary(), reply_markup=ikb_bank_order(order.id))
    except Exception as e:
        logger.error(f"bank_all_orders failed: {e}")
        await message.answer("⚠️ Ошибка при получении заявок.")

@router.callback_query(F.data.startswith("accept:"))
async def cb_accept(callback: CallbackQuery):
    try:
        if user_role(callback.from_user.id) != "bank":
            return await safe_cb_answer(callback, "Нет доступа", show_alert=True)
        oid = int(callback.data.split(":")[1])
        o = orders.get(oid)
        if not o:
            return await safe_cb_answer(callback, "Заявка не найдена", show_alert=True)
        o.status = "accepted"
        with suppress(Exception):
            await callback.message.edit_text(o.summary())
        await safe_cb_answer(callback, "Заявка принята ✅")
        with suppress(Exception):
            await bot.send_message(o.client_id, f"✅ Ваша заявка #{o.id} принята банком.")
    except Exception as e:
        logger.error(f"cb_accept failed: {e}")
        await safe_cb_answer(callback, "Ошибка", show_alert=True)

@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(callback: CallbackQuery):
    try:
        if user_role(callback.from_user.id) != "bank":
            return await safe_cb_answer(callback, "Нет доступа", show_alert=True)
        oid = int(callback.data.split(":")[1])
        o = orders.get(oid)
        if not o:
            return await safe_cb_answer(callback, "Заявка не найдена", show_alert=True)
        o.status = "rejected"
        with suppress(Exception):
            await callback.message.edit_text(o.summary())
        await safe_cb_answer(callback, "Заявка отклонена ❌")
        with suppress(Exception):
            await bot.send_message(o.client_id, f"❌ Ваша заявка #{o.id} отклонена банком.")
    except Exception as e:
        logger.error(f"cb_reject failed: {e}")
        await safe_cb_answer(callback, "Ошибка", show_alert=True)

@router.callback_query(F.data.startswith("order:"))
async def cb_order(callback: CallbackQuery):
    try:
        if user_role(callback.from_user.id) != "bank":
            return await safe_cb_answer(callback, "Нет доступа", show_alert=True)
        oid = int(callback.data.split(":")[1])
        o = orders.get(oid)
        if not o:
            return await safe_cb_answer(callback, "Заявка не найдена", show_alert=True)
        o.status = "order"
        with suppress(Exception):
            await callback.message.edit_text(o.summary())
        await safe_cb_answer(callback, "Сохранено как ордер 📌")
        with suppress(Exception):
            await bot.send_message(o.client_id, f"📌 Ваша заявка #{o.id} сохранена как ордер.")
    except Exception as e:
        logger.error(f"cb_order failed: {e}")
        await safe_cb_answer(callback, "Ошибка", show_alert=True)

# ===================== FALLBACK =====================

@router.message()
async def fallback(message: Message, state: FSMContext):
    try:
        cur = await state.get_state()
        if cur:
            await message.answer(
                f"Сейчас я жду данные для состояния <b>{cur}</b>.\n"
                f"Если хотите выйти — используйте /cancel."
            )
        else:
            role = user_role(message.from_user.id)
            kb = kb_main_bank() if role == "bank" else kb_main_client()
            await message.answer("Не понял. Используйте меню или /start.", reply_markup=kb)
    except Exception as e:
        logger.error(f"fallback failed: {e}")

# ===================== FASTAPI WEBHOOK =====================

@app.on_event("startup")
async def on_startup():
    logger.info("Starting up application...")
    # Проверка Redis (не обязательно падать, но залогируем)
    try:
        await redis_conn.ping()
        logger.info("Redis connected OK.")
    except Exception as e:
        logger.warning(f"Redis ping failed: {e}")

    # Ставим вебхук, если известна базовая ссылка
    if WEBHOOK_BASE:
        url = f"{WEBHOOK_BASE}{WEBHOOK_PATH}"
        try:
            await bot.set_webhook(
                url,
                secret_token=WEBHOOK_SECRET,
                allowed_updates=["message", "callback_query"],
            )
            logger.info(f"Webhook set to {url}")
        except Exception as e:
            logger.error(f"Failed to set webhook: {e}")
    else:
        logger.warning("WEBHOOK_BASE is empty — webhook is not set (local/dev run).")
    logger.info("Startup complete.")

@app.on_event("shutdown")
async def on_shutdown():
    with suppress(Exception):
        await bot.delete_webhook()
    logger.info("Shutdown complete.")

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception as e:
        logger.error(f"Invalid webhook JSON: {e}")
        return {"ok": False}
    try:
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"feed_webhook_update failed: {e}")
        return {"ok": False}

@app.get("/")
async def health():
    return {"status": "ok", "service": "FXBankBot", "webhook_path": WEBHOOK_PATH}

# ===================== LOCAL RUN =====================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
