import os
import asyncio
import logging
from typing import Dict, Optional
from contextlib import suppress

import redis.asyncio as redis
import aiohttp
from fastapi import FastAPI, Request

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
from aiogram.dispatcher.middlewares.base import BaseMiddleware

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BANK_PASSWORD = os.getenv("BANK_PASSWORD", "bank123").strip()
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0").strip()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "fxbank-secret").strip()
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"

HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "10000"))

# URL сервиса
WEBHOOK_BASE = os.getenv("WEBHOOK_URL") or (
    f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}"
    if os.getenv("RENDER_EXTERNAL_HOSTNAME")
    else "https://fxbankbot.onrender.com"
)

# Watchdog и self-ping
WATCHDOG_INTERVAL = int(os.getenv("WEBHOOK_WATCHDOG_INTERVAL", "60"))  # сек
SELF_PING_ENABLE = os.getenv("SELF_PING_ENABLE", "false").lower() == "true"
SELF_PING_INTERVAL = int(os.getenv("SELF_PING_INTERVAL", "240"))  # сек

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | fxbank_bot | %(message)s",
)
logger = logging.getLogger("fxbank_bot")

# ===================== FASTAPI =====================
app = FastAPI(title="FXBankBot", version="1.9.0")

# ===================== REDIS (FSM) =====================
try:
    redis_conn = redis.from_url(REDIS_URL)
    storage = RedisStorage(redis_conn)
    logger.info("RedisStorage initialized.")
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

# ===================== MIDDLEWARE: лог каждого апдейта =====================
class LoggingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            if isinstance(event, types.Message):
                logger.info(f"MSG from {event.from_user.id} @{event.from_user.username}: "
                            f"text={repr(event.text)} state={await data['state'].get_state() if 'state' in data else None}")
            elif isinstance(event, types.CallbackQuery):
                logger.info(f"CB from {event.from_user.id} @{event.from_user.username}: "
                            f"data={repr(event.data)}")
            else:
                logger.info(f"UPDATE type={type(event)} received.")
        except Exception as e:
            logger.warning(f"LoggingMiddleware error: {e}")
        return await handler(event, data)

dp.update.outer_middleware(LoggingMiddleware())

# ===================== RUNTIME STORAGE =====================
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
        currency_to: Optional[str],  # UAH для buy/sell; валюта для convert
        rate: float,                 # курс клиента BASE/QUOTE
        amount_side: Optional[str] = None,  # для convert: "sell"|"buy"
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
            side_txt = (
                " (сумма продажи)"
                if self.amount_side == "sell"
                else (" (сумма покупки)" if self.amount_side == "buy" else "")
            )
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
    # Позже подключим поставщика (LSEG/Bloomberg)
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
def user_role(uid: int) -> str:
    return user_roles.get(uid, "client")

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
        kb = kb_main_bank() if role == "bank" else kb_main_client()
        await message.answer("📍 Главное меню:", reply_markup=kb)
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
        cur = await state.get_state()
        await state.clear()
        role = user_role(message.from_user.id)
        kb = kb_main_bank() if role == "bank" else kb_main_client()
        if cur:
            await message.answer("✅ Действие отменено. Главное меню:", reply_markup=kb)
        else:
            await message.answer("❌ Нет активного действия. Главное меню:", reply_markup=kb)
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

# ===================== CLIENT FSM =====================
class ClientFSM(StatesGroup):
    entering_client_name = State()
    choosing_deal = State()
    entering_currency_from = State()
    entering_currency_to = State()     # для конверсии
    choosing_amount_side = State()     # для конверсии: sell/buy
    entering_amount = State()
    entering_rate = State()

@router.message(F.text == "➕ Новая заявка")
async def new_request(message: Message, state: FSMContext):
    try:
        await state.clear()
        await state.set_state(ClientFSM.entering_client_name)
        await message.answer("👤 Введите ваше имя или название компании:", reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        logger.error(f"new_request failed: {e}")
        await message.answer("⚠️ Ошибка при создании заявки.")

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
        else:
            return await safe_cb_answer(callback, "❌ Неизвестный тип сделки", show_alert=True)

        await state.set_state(ClientFSM.entering_currency_from)
        if deal_type == "convert":
            await callback.message.edit_text("Введите валюту, которую хотите ПРОДАТЬ (пример: USD):")
        else:
            await callback.message.edit_text("Введите валюту сделки (пример: USD):")
        await safe_cb_answer(callback)
    except Exception as e:
        logger.error(f"cq_deal failed: {e}")
        await safe_cb_answer(callback, "⚠️ Ошибка", show_alert=True)

@router.message(ClientFSM.entering_currency_from)
async def fsm_currency_from(message: Message, state: FSMContext):
    try:
        cfrom = (message.text or "").upper().strip()
        if not cfrom or len(cfrom) < 3:
            return await message.answer("❌ Укажите код валюты, пример: USD, EUR, UAH.")
        await state.update_data(currency_from=cfrom)
        data = await state.get_data()
        if data.get("operation") == "конвертация":
            await state.set_state(ClientFSM.entering_currency_to)
            await message.answer("Введите валюту, которую хотите ПОЛУЧИТЬ (пример: EUR):")
        else:
            await state.update_data(currency_to="UAH")
            await state.set_state(ClientFSM.entering_amount)
            await message.answer(f"Введите сумму в {cfrom}:")
    except Exception as e:
        logger.error(f"fsm_currency_from failed: {e}")
        await message.answer("⚠️ Ошибка при вводе валюты.")

@router.message(ClientFSM.entering_currency_to)
async def fsm_currency_to(message: Message, state: FSMContext):
    try:
        cto = (message.text or "").upper().strip()
        if not cto or len(cto) < 3:
            return await message.answer("❌ Укажите код валюты, пример: USD, EUR.")
        await state.update_data(currency_to=cto)
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
            amount = float((message.text or "").replace(",", "."))
        except ValueError:
            return await message.answer("❌ Введите число, например: 1000.50")

        await state.update_data(amount=amount)
        await state.set_state(ClientFSM.entering_rate)
        await message.answer(
            "Введите ваш курс (BASE/QUOTE).\n"
            "Примеры:\n"
            "• Покупка/Продажа USD против UAH → курс USD/UAH\n"
            "• Конверсия USD→EUR → курс USD/EUR\n\n"
            "Можно оставить пусто — подставим заглушку."
        )
    except Exception as e:
        logger.error(f"fsm_amount failed: {e}")
        await message.answer("⚠️ Ошибка при вводе суммы.")

@router.message(ClientFSM.entering_rate)
async def fsm_rate(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        txt = (message.text or "").strip()
        if txt:
            try:
                rate = float(txt.replace(",", "."))
            except ValueError:
                return await message.answer("❌ Курс должен быть числом, например 41.25")
        else:
            base = data["currency_from"]
            quote = data.get("currency_to", "UAH")
            pair = f"{base}/{quote}"
            rate = get_stub_rates().get(pair, 1.0)

        await state.update_data(rate=rate)

        order = Order(
            client_id=message.from_user.id,
            client_telegram=message.from_user.username or "",
            client_name=data.get("client_name", "N/A"),
            operation=data["operation"],
            amount=data["amount"],
            currency_from=data["currency_from"],
            currency_to=data.get("currency_to"),
            rate=rate,
            amount_side=data.get("amount_side"),
        )
        orders[order.id] = order

        await state.clear()
        await message.answer("✅ Ваша заявка создана:\n\n" + order.summary(), reply_markup=kb_main_client())

        # Уведомим банк
        for uid, role in user_roles.items():
            if role == "bank":
                with suppress(Exception):
                    await bot.send_message(uid, "📥 Новая заявка:\n\n" + order.summary(),
                                           reply_markup=ikb_bank_order(order.id))
    except Exception as e:
        logger.error(f"fsm_rate failed: {e}")
        await message.answer("⚠️ Ошибка при вводе курса.")

# ===================== BANK FLOW =====================
@router.message(F.text == "📋 Все заявки")
async def bank_orders(message: Message):
    try:
        if user_role(message.from_user.id) != "bank":
            return await message.answer("❌ Эта команда доступна только банку.")
        if not orders:
            return await message.answer("📭 Нет заявок.")
        for order in orders.values():
            await message.answer(order.summary(), reply_markup=ikb_bank_order(order.id))
    except Exception as e:
        logger.error(f"bank_orders failed: {e}")
        await message.answer("⚠️ Ошибка при показе заявок.")

@router.callback_query(F.data.startswith("accept:"))
async def cq_accept(callback: CallbackQuery):
    try:
        oid = int(callback.data.split(":")[1])
        order = orders.get(oid)
        if not order:
            return await safe_cb_answer(callback, "❌ Заявка не найдена", show_alert=True)
        order.status = "accepted"
        await callback.message.edit_text(order.summary())
        await safe_cb_answer(callback, "✅ Заявка принята")

        with suppress(Exception):
            await bot.send_message(order.client_id, f"✅ Ваша заявка #{oid} принята банком.")
    except Exception as e:
        logger.error(f"cq_accept failed: {e}")
        await safe_cb_answer(callback, "⚠️ Ошибка", show_alert=True)

@router.callback_query(F.data.startswith("reject:"))
async def cq_reject(callback: CallbackQuery):
    try:
        oid = int(callback.data.split(":")[1])
        order = orders.get(oid)
        if not order:
            return await safe_cb_answer(callback, "❌ Заявка не найдена", show_alert=True)
        order.status = "rejected"
        await callback.message.edit_text(order.summary())
        await safe_cb_answer(callback, "❌ Заявка отклонена")

        with suppress(Exception):
            await bot.send_message(order.client_id, f"❌ Ваша заявка #{oid} отклонена банком.")
    except Exception as e:
        logger.error(f"cq_reject failed: {e}")
        await safe_cb_answer(callback, "⚠️ Ошибка", show_alert=True)

@router.callback_query(F.data.startswith("order:"))
async def cq_order(callback: CallbackQuery):
    try:
        oid = int(callback.data.split(":")[1])
        order = orders.get(oid)
        if not order:
            return await safe_cb_answer(callback, "❌ Заявка не найдена", show_alert=True)
        order.status = "order"
        await callback.message.edit_text(order.summary())
        await safe_cb_answer(callback, "📌 Сохранено как ордер")
    except Exception as e:
        logger.error(f"cq_order failed: {e}")
        await safe_cb_answer(callback, "⚠️ Ошибка", show_alert=True)

# ===================== WEBHOOK MGMT + WATCHDOG + SELF-PING =====================
_watchdog_task: Optional[asyncio.Task] = None
_self_ping_task: Optional[asyncio.Task] = None

async def set_webhook_safely(url: str):
    """Ставит вебхук с защитой от Flood Control и подробным логом."""
    try:
        with suppress(Exception):
            await bot.delete_webhook(drop_pending_updates=True)

        await bot.set_webhook(
            url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "callback_query"],
        )
        logger.info(f"Webhook set to {url}")
    except TelegramRetryAfter as e:
        delay = max(int(e.retry_after), 1)
        logger.warning(f"Flood control on set_webhook. Retry after {delay}s")
        await asyncio.sleep(delay)
        await bot.set_webhook(
            url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "callback_query"],
        )
        logger.info(f"Webhook set to {url} (after retry)")
    except TelegramBadRequest as e:
        logger.error(f"BadRequest on set_webhook: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error on set_webhook: {e}")
        raise

async def webhook_watchdog():
    """Каждые WATCHDOG_INTERVAL сек проверяет URL вебхука, при расхождении — переустанавливает."""
    desired = f"{WEBHOOK_BASE}{WEBHOOK_PATH}"
    while True:
        try:
            info = await bot.get_webhook_info()
            current = info.url or ""
            if current != desired:
                logger.warning(f"Watchdog: webhook mismatch (current='{current}', desired='{desired}'). Fixing...")
                with suppress(Exception):
                    await set_webhook_safely(desired)
            else:
                logger.info("Watchdog: webhook OK.")
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
        await asyncio.sleep(WATCHDOG_INTERVAL)

async def self_ping_loop():
    """Опциональный self-ping, чтобы Render не усыплял сервис (лучше держать включённым для Free-плана)."""
    if not SELF_PING_ENABLE:
        return
    url = f"{WEBHOOK_BASE}/"
    session_timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=session_timeout) as session:
        while True:
            try:
                async with session.get(url) as resp:
                    logger.info(f"Self-ping {url} -> {resp.status}")
            except Exception as e:
                logger.warning(f"Self-ping error: {e}")
            await asyncio.sleep(SELF_PING_INTERVAL)

# ===================== FASTAPI ROUTES =====================
@app.on_event("startup")
async def on_startup():
    try:
        # Команды бота (подсказки в интерфейсе Telegram)
        with suppress(Exception):
            await bot.set_my_commands([
                types.BotCommand(command="start", description="Запуск / выбор роли"),
                types.BotCommand(command="menu", description="Главное меню"),
                types.BotCommand(command="rate", description="Показать курсы"),
                types.BotCommand(command="cancel", description="Отмена текущего действия"),
                types.BotCommand(command="bank", description="Вход роли банк: /bank <пароль>"),
            ])

        # Проверка Redis
        with suppress(Exception):
            pong = await redis_conn.ping()
            if pong:
                logger.info("Redis connected OK.")

        # Вебхук
        desired = f"{WEBHOOK_BASE}{WEBHOOK_PATH}"
        await set_webhook_safely(desired)

        # Старт watchdog
        global _watchdog_task
        _watchdog_task = asyncio.create_task(webhook_watchdog())

        # Старт self-ping (если включён)
        global _self_ping_task
        if SELF_PING_ENABLE:
            _self_ping_task = asyncio.create_task(self_ping_loop())

        logger.info(f"Startup complete. Watchdog enabled (interval={WATCHDOG_INTERVAL}s).")
    except Exception as e:
        logger.error(f"Startup failed: {e}")

@app.on_event("shutdown")
async def on_shutdown():
    with suppress(Exception):
        if _watchdog_task:
            _watchdog_task.cancel()
    with suppress(Exception):
        if _self_ping_task:
            _self_ping_task.cancel()
    with suppress(Exception):
        await redis_conn.close()
    with suppress(Exception):
        await bot.session.close()
    logger.info("Shutdown complete.")

@app.get("/")
async def index():
    return {
        "status": "ok",
        "bot": "FXBankBot",
        "webhook": f"{WEBHOOK_BASE}{WEBHOOK_PATH}",
        "self_ping": SELF_PING_ENABLE,
    }

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    try:
        raw = await request.body()
        update = types.Update.model_validate_json(raw)
        # ВАЖНО: секрет передаём именованным аргументом, иначе третий позиционный — timeout (int)
        await dp.feed_webhook_update(bot, update, secret_token=WEBHOOK_SECRET)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"ok": False}
    return {"ok": True}

# ===================== ENTRY =====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
