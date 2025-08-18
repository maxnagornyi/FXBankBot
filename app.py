import asyncio
import json
import logging
import os
import sys
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.redis import RedisStorage, DefaultKeyBuilder
from aiogram.types import (
    Update,
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    BotCommandScopeDefault,
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.markdown import hbold, hcode

# -------- Redis (async) ----------
try:
    # redis>=5
    from redis.asyncio import Redis
except Exception:  # pragma: no cover
    from aioredis import from_url as Redis  # type: ignore

# -----------------------------
# Logging setup (robust)
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("fxbank_bot")

# -----------------------------
# Environment & constants
# -----------------------------
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not BOT_TOKEN:
    logger.error("BOT_TOKEN is not set! Please add it to Render Environment.")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "fxbank-secret")
STRICT_HEADER = os.getenv("STRICT_HEADER", "false").lower() == "true"
ASYNC_UPDATES = os.getenv("ASYNC_UPDATES", "true").lower() == "true"
ENABLE_WATCHDOG = os.getenv("ENABLE_WATCHDOG", "false").lower() == "true"
WATCHDOG_INTERVAL = int(os.getenv("WEBHOOK_WATCHDOG_INTERVAL", "120"))

BANK_PASSWORD = os.getenv("BANK_PASSWORD", "12345")

WEBHOOK_PATH = "/webhook/secret"  # требование задачи — фиксированный путь
HEALTHCHECK_PATH = "/"

SUPPORTED_CCY = ("USD", "EUR", "PLN")

# Redis keys/templates
ROLE_KEY = "role:{user_id}"                   # "client" | "bank"
ORDER_KEY = "order:{user_id}:{order_id}"      # JSON order
USER_ORDERS_SET = "orders_by_user:{user_id}"  # set(order_id)
PENDING_ORDERS_SET = "orders:pending"         # set(order_id)
RATES_HASH = "rates"                          # hash ccy->rate

# -----------------------------
# FastAPI & Aiogram init
# -----------------------------
app = FastAPI(title="FX Bank Bot", version="1.1.0")

# Redis connection (Upstash rediss:// ok)
try:
    redis: Redis = Redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)  # type: ignore
except Exception as e:
    logger.exception("Redis.from_url failed: %s", e)
    raise

storage = RedisStorage(redis=redis, key_builder=DefaultKeyBuilder(prefix="fsm"))
bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
) if BOT_TOKEN else None
dp = Dispatcher(storage=storage)

# Routers
common_router = Router(name="common")
client_router = Router(name="client")
bank_router = Router(name="bank")
dp.include_router(common_router)
dp.include_router(client_router)
dp.include_router(bank_router)

# -----------------------------
# FSM States
# -----------------------------
class ClientOrderSG(StatesGroup):
    enter_amount = State()
    choose_currency = State()
    confirm = State()


class BankSetRateSG(StatesGroup):
    choose_currency = State()
    enter_rate = State()
    confirm = State()


# -----------------------------
# Utilities
# -----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_role(user_id: int) -> str:
    try:
        role = await redis.get(ROLE_KEY.format(user_id=user_id))
        return role or "client"
    except Exception as e:
        logger.error("get_role failed: %s", e)
        return "client"


async def set_role(user_id: int, role: str) -> None:
    try:
        await redis.set(ROLE_KEY.format(user_id=user_id), role)
    except Exception as e:
        logger.error("set_role failed: %s", e)


def make_role_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="👤 Я клиент", callback_data="role:client"),
            InlineKeyboardButton(text="🏦 Я банк", callback_data="role:bank"),
        ],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="common:help")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def make_client_menu() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="🟢 Купить", callback_data="client:buy"),
            InlineKeyboardButton(text="🔵 Продать", callback_data="client:sell"),
        ],
        [
            InlineKeyboardButton(text="📄 Мои заявки", callback_data="client:orders"),
            InlineKeyboardButton(text="📊 Курсы", callback_data="common:rates"),
        ],
        [InlineKeyboardButton(text="🔁 Сменить роль", callback_data="role:choose")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def make_bank_menu() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="✏️ Установить курс", callback_data="bank:set_rate"),
            InlineKeyboardButton(text="📥 Заявки клиентов", callback_data="bank:orders"),
        ],
        [
            InlineKeyboardButton(text="📊 Текущие курсы", callback_data="common:rates"),
            InlineKeyboardButton(text="🧹 Очистить pending", callback_data="bank:clear_orders"),
        ],
        [InlineKeyboardButton(text="🔁 Сменить роль", callback_data="role:choose")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def make_currency_keyboard() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text=ccy, callback_data=f"ccy:{ccy}") for ccy in SUPPORTED_CCY]
    return InlineKeyboardMarkup(inline_keyboard=[row, [InlineKeyboardButton(text="↩️ Назад", callback_data="common:back")]])


def make_confirm_keyboard(ok_cb: str, cancel_cb: str = "common:cancel") -> InlineKeyboardMarkup:
    kb = [[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=ok_cb),
        InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_cb),
    ]]
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def set_commands() -> None:
    if not bot:
        return
    try:
        await bot.set_my_commands(
            commands=[
                BotCommand(command="start", description="Запуск / выбор роли"),
                BotCommand(command="help", description="Помощь"),
                BotCommand(command="menu", description="Показать меню"),
                BotCommand(command="role", description="Сменить роль"),
                BotCommand(command="bank", description="Вход банка: /bank <пароль>"),
                BotCommand(command="cancel", description="Отмена действия"),
            ],
            scope=BotCommandScopeDefault(),
        )
        logger.info("Bot commands set.")
    except Exception as e:
        logger.error("set_my_commands failed: %s", e)


async def set_webhook() -> None:
    """Configure Telegram webhook using WEBHOOK_URL/RENDER_EXTERNAL_URL and fixed path."""
    if not bot:
        logger.warning("Bot is None; skip set_webhook.")
        return
    if not WEBHOOK_BASE_URL:
        logger.warning("WEBHOOK_URL/RENDER_EXTERNAL_URL not set; skip webhook setup.")
        return
    url = f"{WEBHOOK_BASE_URL}{WEBHOOK_PATH}"
    try:
        used_updates: List[str] = dp.resolve_used_update_types()
        await bot.set_webhook(
            url=url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
            allowed_updates=used_updates,
        )
        logger.info("Webhook set to %s (allowed_updates=%s)", url, used_updates)
    except TelegramBadRequest as e:
        logger.error("TelegramBadRequest on set_webhook: %s", e)
    except Exception as e:
        logger.exception("Failed to set webhook: %s", e)


async def watchdog_task():
    """Optional watchdog: periodically ensure webhook is set."""
    if not ENABLE_WATCHDOG:
        return
    logger.info("Watchdog enabled (interval=%ss).", WATCHDOG_INTERVAL)
    while True:
        try:
            await set_webhook()
        except Exception as e:
            logger.warning("Watchdog error: %s", e)
        await asyncio.sleep(WATCHDOG_INTERVAL)

# -----------------------------
# Common handlers
# -----------------------------
@common_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    try:
        await state.clear()
        await message.answer(
            f"Привет, {hbold(message.from_user.full_name)}!\n"
            f"Этот бот помогает клиентам и банкам обмениваться заявками по валютному рынку.\n\n"
            f"Выберите роль или введите /bank <пароль> для входа банка:",
            reply_markup=make_role_keyboard(),
        )
    except Exception as e:
        logger.exception("cmd_start failed: %s", e)
        await message.answer("Произошла ошибка при /start.")


@common_router.message(Command("help"))
async def cmd_help(message: Message):
    try:
        txt = (
            "ℹ️ Помощь\n\n"
            "/start — запустить бота и выбрать роль\n"
            "/menu — показать актуальное меню\n"
            "/role — сменить роль (клиент/банк)\n"
            "/bank <пароль> — вход для банка\n"
            "/cancel — отменить текущее действие\n\n"
            "Поддерживаемые валюты: " + ", ".join(SUPPORTED_CCY)
        )
        await message.answer(txt)
    except Exception as e:
        logger.exception("cmd_help failed: %s", e)
        await message.answer("Ошибка при показе помощи.")


@common_router.message(Command("menu"))
async def cmd_menu(message: Message):
    try:
        role = await get_role(message.from_user.id)
        if role == "bank":
            await message.answer("🏦 Меню банка:", reply_markup=make_bank_menu())
        else:
            await message.answer("👤 Меню клиента:", reply_markup=make_client_menu())
    except Exception as e:
        logger.exception("cmd_menu failed: %s", e)
        await message.answer("Ошибка при показе меню.")


@common_router.message(Command("role"))
async def cmd_role(message: Message):
    try:
        await message.answer("Выберите роль:", reply_markup=make_role_keyboard())
    except Exception as e:
        logger.exception("cmd_role failed: %s", e)
        await message.answer("Ошибка при выборе роли.")


@common_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    try:
        await state.clear()
        role = await get_role(message.from_user.id)
        kb = make_bank_menu() if role == "bank" else make_client_menu()
        await message.answer("Действие отменено.", reply_markup=kb)
    except Exception as e:
        logger.exception("cmd_cancel failed: %s", e)
        await message.answer("Не удалось отменить действие.")


@common_router.callback_query(F.data == "common:help")
async def cq_help(callback: CallbackQuery):
    try:
        await callback.message.edit_text(
            "Справка по боту:\n"
            "— Клиент: создать заявку на покупку/продажу валюты.\n"
            "— Банк: устанавливать курс и управлять заявками.\n"
            "Команды доступны через меню.",
            reply_markup=make_role_keyboard(),
        )
        await callback.answer()
    except Exception as e:
        logger.exception("cq_help failed: %s", e)
        await callback.answer("Ошибка.", show_alert=True)


@common_router.callback_query(F.data == "role:choose")
async def cq_role_choose(callback: CallbackQuery, state: FSMContext):
    try:
        await state.clear()
        await callback.message.edit_text("Выберите роль:", reply_markup=make_role_keyboard())
        await callback.answer()
    except Exception as e:
        logger.exception("cq_role_choose failed: %s", e)
        await callback.answer("Ошибка.", show_alert=True)


@common_router.callback_query(F.data.startswith("role:"))
async def cq_role_set(callback: CallbackQuery):
    try:
        role = callback.data.split(":", 1)[1]
        if role not in ("client", "bank"):
            await callback.answer("Неизвестная роль.", show_alert=True)
            return
        # Для роли банк — дополнительная подсказка про /bank <пароль>
        await set_role(callback.from_user.id, role if role == "client" else "bank")
        if role == "bank":
            await callback.message.edit_text(
                "Роль установлена: 🏦 Банк.\n"
                "Если ещё не вводили пароль, выполните команду: /bank <пароль>\nВыберите действие:",
                reply_markup=make_bank_menu(),
            )
        else:
            await callback.message.edit_text("Роль установлена: 👤 Клиент.\nВыберите действие:", reply_markup=make_client_menu())
        await callback.answer("Роль изменена.")
    except Exception as e:
        logger.exception("cq_role_set failed: %s", e)
        await callback.answer("Не удалось установить роль.", show_alert=True)


@common_router.callback_query(F.data == "common:rates")
async def cq_show_rates(callback: CallbackQuery):
    try:
        try:
            rates: Dict[str, str] = await redis.hgetall(RATES_HASH)  # type: ignore
        except Exception as re:
            logger.error("Redis hgetall rates failed: %s", re)
            rates = {}
        if not rates:
            txt = "Пока не установлены курсы. Банки могут установить курс через меню."
        else:
            lines = [f"{ccy}: {hcode(rates[ccy])}" for ccy in sorted(rates.keys())]
            txt = "Текущие курсы (устанавливает банк):\n" + "\n".join(lines)
        await callback.message.edit_text(
            txt,
            reply_markup=(make_bank_menu() if await get_role(callback.from_user.id) == "bank" else make_client_menu()),
        )
        await callback.answer()
    except Exception as e:
        logger.exception("cq_show_rates failed: %s", e)
        await callback.answer("Ошибка при получении курсов.", show_alert=True)


@common_router.callback_query(F.data == "common:cancel")
async def cq_common_cancel(callback: CallbackQuery, state: FSMContext):
    try:
        await state.clear()
        role = await get_role(callback.from_user.id)
        kb = make_bank_menu() if role == "bank" else make_client_menu()
        await callback.message.edit_text("Отменено. Возврат в меню.", reply_markup=kb)
        await callback.answer()
    except Exception as e:
        logger.exception("cq_common_cancel failed: %s", e)
        await callback.answer("Ошибка.", show_alert=True)


@common_router.callback_query(F.data == "common:back")
async def cq_common_back(callback: CallbackQuery, state: FSMContext):
    try:
        await state.clear()
        role = await get_role(callback.from_user.id)
        kb = make_bank_menu() if role == "bank" else make_client_menu()
        await callback.message.edit_text("Назад в меню.", reply_markup=kb)
        await callback.answer()
    except Exception as e:
        logger.exception("cq_common_back failed: %s", e)
        await callback.answer("Ошибка.", show_alert=True)


# -----------------------------
# Commands: bank login
# -----------------------------
@common_router.message(Command("bank"))
async def cmd_bank(message: Message):
    try:
        parts = (message.text or "").strip().split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("❌ Укажите пароль: /bank <пароль>")
            return
        if parts[1] == BANK_PASSWORD:
            await set_role(message.from_user.id, "bank")
            await message.answer("🏦 Успешный вход. Вы вошли как банк.", reply_markup=make_bank_menu())
        else:
            await message.answer("❌ Неверный пароль.")
    except Exception as e:
        logger.exception("cmd_bank failed: %s", e)
        await message.answer("Ошибка при входе банка.")
# -----------------------------
# Client handlers
# -----------------------------
@client_router.callback_query(F.data == "client:buy")
async def cq_client_buy(callback: CallbackQuery, state: FSMContext):
    try:
        await state.set_state(ClientOrderSG.enter_amount)
        await state.update_data(action="buy")
        await callback.message.edit_text(
            "🟢 Покупка валюты.\nВведите сумму (например, 1000.50):",
            reply_markup=make_confirm_keyboard(ok_cb="noop", cancel_cb="common:cancel"),
        )
        await callback.answer()
    except Exception as e:
        logger.exception("cq_client_buy failed: %s", e)
        await callback.answer("Ошибка при начале заявки.", show_alert=True)


@client_router.callback_query(F.data == "client:sell")
async def cq_client_sell(callback: CallbackQuery, state: FSMContext):
    try:
        await state.set_state(ClientOrderSG.enter_amount)
        await state.update_data(action="sell")
        await callback.message.edit_text(
            "🔵 Продажа валюты.\nВведите сумму (например, 500):",
            reply_markup=make_confirm_keyboard(ok_cb="noop", cancel_cb="common:cancel"),
        )
        await callback.answer()
    except Exception as e:
        logger.exception("cq_client_sell failed: %s", e)
        await callback.answer("Ошибка при начале заявки.", show_alert=True)


@client_router.message(ClientOrderSG.enter_amount)
async def msg_client_enter_amount(message: Message, state: FSMContext):
    try:
        text = (message.text or "").replace(",", ".").strip()
        amount = float(text)
        if amount <= 0:
            raise ValueError("Amount must be positive")
        await state.update_data(amount=amount)
        await state.set_state(ClientOrderSG.choose_currency)
        await message.answer(
            f"Сумма: {hcode(amount)}\nВыберите валюту:",
            reply_markup=make_currency_keyboard(),
        )
    except ValueError:
        await message.answer("Некорректная сумма. Введите число, например 1200.75")
    except Exception as e:
        logger.exception("msg_client_enter_amount failed: %s", e)
        await message.answer("Ошибка при обработке суммы. /cancel для отмены.")


@client_router.callback_query(ClientOrderSG.choose_currency, F.data.startswith("ccy:"))
async def cq_client_choose_currency(callback: CallbackQuery, state: FSMContext):
    try:
        ccy = callback.data.split(":", 1)[1]
        if ccy not in SUPPORTED_CCY:
            await callback.answer("Неподдерживаемая валюта.", show_alert=True)
            return
        data = await state.get_data()
        action = data.get("action", "buy")
        amount = data.get("amount", 0)
        await state.update_data(currency=ccy)
        await state.set_state(ClientOrderSG.confirm)

        rate_val = None
        try:
            rate_str = await redis.hget(RATES_HASH, ccy)  # type: ignore
            if rate_str is not None:
                rate_val = float(rate_str)
        except Exception as re:
            logger.error("Redis hget rate failed: %s", re)

        summary_lines = [
            f"Действие: {hbold('Покупка' if action == 'buy' else 'Продажа')}",
            f"Сумма: {hcode(amount)}",
            f"Валюта: {hcode(ccy)}",
        ]
        if rate_val:
            summary_lines.append(f"Ориентир. курс: {hcode(rate_val)}")

        await callback.message.edit_text(
            "Проверьте заявку:\n" + "\n".join(summary_lines),
            reply_markup=make_confirm_keyboard(ok_cb="client:confirm", cancel_cb="common:cancel"),
        )
        await callback.answer()
    except Exception as e:
        logger.exception("cq_client_choose_currency failed: %s", e)
        await callback.answer("Ошибка при выборе валюты.", show_alert=True)


@client_router.callback_query(ClientOrderSG.confirm, F.data == "client:confirm")
async def cq_client_confirm(callback: CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        action = data.get("action")
        amount = data.get("amount")
        currency = data.get("currency")
        if not all([action, amount, currency]):
            await callback.answer("Данные заявки неполные. Начните заново.", show_alert=True)
            await state.clear()
            return

        order_id = f"{callback.from_user.id}-{int(datetime.now().timestamp())}"
        order = {
            "order_id": order_id,
            "user_id": callback.from_user.id,
            "username": callback.from_user.username,
            "full_name": callback.from_user.full_name,
            "action": action,
            "amount": float(amount),
            "currency": currency,
            "status": "pending",
            "created_at": now_iso(),
        }

        # Save to Redis
        try:
            pipe = redis.pipeline()
            pipe.set(ORDER_KEY.format(user_id=callback.from_user.id, order_id=order_id), json.dumps(order))
            pipe.sadd(USER_ORDERS_SET.format(user_id=callback.from_user.id), order_id)
            pipe.sadd(PENDING_ORDERS_SET, order_id)
            await pipe.execute()
        except Exception as re:
            logger.exception("Failed to save order: %s", re)
            await callback.answer("Не удалось сохранить заявку (ошибка БД).", show_alert=True)
            return

        await state.clear()
        await callback.message.edit_text(
            f"✅ Заявка создана! ID: {hcode(order_id)}\n"
            f"Действие: {hbold('Покупка' if action == 'buy' else 'Продажа')}\n"
            f"Сумма: {hcode(amount)} {hcode(currency)}\n"
            f"Статус: {hcode('pending')}\n\n"
            "Ожидайте ответа банка.",
            reply_markup=make_client_menu(),
        )
        await callback.answer("Создано.")
    except Exception as e:
        logger.exception("cq_client_confirm failed: %s", e)
        await callback.answer("Ошибка при подтверждении заявки.", show_alert=True)


@client_router.callback_query(F.data == "client:orders")
async def cq_client_orders(callback: CallbackQuery):
    try:
        try:
            order_ids = await redis.smembers(USER_ORDERS_SET.format(user_id=callback.from_user.id))  # type: ignore
        except Exception as re:
            logger.error("Redis smembers user orders failed: %s", re)
            order_ids = set()

        if not order_ids:
            await callback.message.edit_text("У вас пока нет заявок.", reply_markup=make_client_menu())
            await callback.answer()
            return

        orders: List[Dict[str, Any]] = []
        for oid in sorted(order_ids, reverse=True):
            try:
                raw = await redis.get(ORDER_KEY.format(user_id=callback.from_user.id, order_id=oid))  # type: ignore
                if raw:
                    orders.append(json.loads(raw))
            except Exception as re:
                logger.error("Get order failed: %s", re)

        orders = sorted(orders, key=lambda x: x.get("created_at", ""), reverse=True)[:10]
        lines = []
        for o in orders:
            lines.append(
                f"• {hcode(o['order_id'])}: {o['action']} {o['amount']} {o['currency']} — {hbold(o['status'])}"
            )
        await callback.message.edit_text("Ваши последние заявки:\n" + "\n".join(lines), reply_markup=make_client_menu())
        await callback.answer()
    except Exception as e:
        logger.exception("cq_client_orders failed: %s", e)
        await callback.answer("Ошибка при получении заявок.", show_alert=True)


# -----------------------------
# Bank handlers
# -----------------------------
@bank_router.callback_query(F.data == "bank:set_rate")
async def cq_bank_set_rate(callback: CallbackQuery, state: FSMContext):
    try:
        if await get_role(callback.from_user.id) != "bank":
            await callback.answer("Эта функция доступна роли 'банк'.", show_alert=True)
            return
        await state.set_state(BankSetRateSG.choose_currency)
        await callback.message.edit_text("Выберите валюту для установки курса:", reply_markup=make_currency_keyboard())
        await callback.answer()
    except Exception as e:
        logger.exception("cq_bank_set_rate failed: %s", e)
        await callback.answer("Ошибка.", show_alert=True)


@bank_router.callback_query(BankSetRateSG.choose_currency, F.data.startswith("ccy:"))
async def cq_bank_choose_currency(callback: CallbackQuery, state: FSMContext):
    try:
        ccy = callback.data.split(":", 1)[1]
        if ccy not in SUPPORTED_CCY:
            await callback.answer("Неподдерживаемая валюта.", show_alert=True)
            return
        await state.update_data(currency=ccy)
        await state.set_state(BankSetRateSG.enter_rate)
        await callback.message.edit_text(
            f"Валюта: {hcode(ccy)}\nВведите курс (число):",
            reply_markup=make_confirm_keyboard(ok_cb="noop", cancel_cb="common:cancel"),
        )
        await callback.answer()
    except Exception as e:
        logger.exception("cq_bank_choose_currency failed: %s", e)
        await callback.answer("Ошибка.", show_alert=True)


@bank_router.message(BankSetRateSG.enter_rate)
async def msg_bank_enter_rate(message: Message, state: FSMContext):
    try:
        text = (message.text or "").replace(",", ".").strip()
        rate = float(text)
        if rate <= 0:
            raise ValueError("rate <= 0")
        await state.update_data(rate=rate)
        data = await state.get_data()
        ccy = data.get("currency")
        await state.set_state(BankSetRateSG.confirm)
        await message.answer(
            f"Установить курс {hcode(ccy)} = {hcode(rate)} ?",
            reply_markup=make_confirm_keyboard(ok_cb="bank:rate_confirm", cancel_cb="common:cancel"),
        )
    except ValueError:
        await message.answer("Некорректный курс. Введите положительное число.")
    except Exception as e:
        logger.exception("msg_bank_enter_rate failed: %s", e)
        await message.answer("Ошибка при обработке курса. /cancel для отмены.")


@bank_router.callback_query(BankSetRateSG.confirm, F.data == "bank:rate_confirm")
async def cq_bank_rate_confirm(callback: CallbackQuery, state: FSMContext):
    try:
        if await get_role(callback.from_user.id) != "bank":
            await callback.answer("Доступ запрещён (только для банка).", show_alert=True)
            return
        data = await state.get_data()
        ccy = data.get("currency")
        rate = data.get("rate")
        if not (ccy and rate):
            await callback.answer("Данные неполные.", show_alert=True)
            return
        try:
            await redis.hset(RATES_HASH, ccy, str(rate))  # type: ignore
        except Exception as re:
            logger.exception("Failed to set rate in Redis: %s", re)
            await callback.answer("Ошибка сохранения курса.", show_alert=True)
            return
        await state.clear()
        await callback.message.edit_text(
            f"✅ Курс обновлён: {hcode(ccy)} = {hcode(rate)}", reply_markup=make_bank_menu()
        )
        await callback.answer("Сохранено.")
    except Exception as e:
        logger.exception("cq_bank_rate_confirm failed: %s", e)
        await callback.answer("Ошибка.", show_alert=True)


@bank_router.callback_query(F.data == "bank:orders")
async def cq_bank_orders(callback: CallbackQuery):
    try:
        if await get_role(callback.from_user.id) != "bank":
            await callback.answer("Доступ запрещён (только для банка).", show_alert=True)
            return
        try:
            order_ids = await redis.smembers(PENDING_ORDERS_SET)  # type: ignore
        except Exception as re:
            logger.error("Redis smembers pending orders failed: %s", re)
            order_ids = set()

        if not order_ids:
            await callback.message.edit_text("Пока нет заявок в статусе pending.", reply_markup=make_bank_menu())
            await callback.answer()
            return

        orders: List[Dict[str, Any]] = []
        for oid in sorted(order_ids, reverse=True):
            try:
                user_id_str = oid.split("-", 1)[0]
                raw = await redis.get(ORDER_KEY.format(user_id=user_id_str, order_id=oid))  # type: ignore
                if raw:
                    orders.append(json.loads(raw))
            except Exception as re:
                logger.error("Redis get pending order failed: %s", re)

        if not orders:
            await callback.message.edit_text("Не удалось загрузить заявки.", reply_markup=make_bank_menu())
            await callback.answer()
            return

        lines = []
        for o in sorted(orders, key=lambda x: x.get("created_at", ""), reverse=True)[:15]:
            uname = ("@" + o["username"]) if o.get("username") else o.get("full_name", o["user_id"])
            lines.append(
                f"• {hcode(o['order_id'])} | {o['action']} {o['amount']} {o['currency']} | от {uname} | {hbold(o['status'])}"
            )

        kb_rows = []
        for o in orders[:5]:
            kb_rows.append(
                [
                    InlineKeyboardButton(text=f"✅ Принять {o['order_id']}", callback_data=f"bank:accept:{o['order_id']}"),
                    InlineKeyboardButton(text=f"❌ Отклонить {o['order_id']}", callback_data=f"bank:reject:{o['order_id']}"),
                ]
            )
        kb_rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="role:choose")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

        await callback.message.edit_text("Заявки pending:\n" + "\n".join(lines), reply_markup=kb)
        await callback.answer()
    except Exception as e:
        logger.exception("cq_bank_orders failed: %s", e)
        await callback.answer("Ошибка при получении заявок.", show_alert=True)


@bank_router.callback_query(F.data.startswith("bank:accept:"))
async def cq_bank_accept(callback: CallbackQuery):
    try:
        if await get_role(callback.from_user.id) != "bank":
            await callback.answer("Доступ запрещён.", show_alert=True)
            return
        order_id = callback.data.split(":", 2)[2]
        user_id_str = order_id.split("-", 1)[0]
        key = ORDER_KEY.format(user_id=user_id_str, order_id=order_id)
        raw = await redis.get(key)  # type: ignore
        if not raw:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return
        order = json.loads(raw)
        order["status"] = "accepted"
        order["updated_at"] = now_iso()

        pipe = redis.pipeline()
        pipe.set(key, json.dumps(order))
        pipe.srem(PENDING_ORDERS_SET, order_id)
        await pipe.execute()

        await callback.answer("Заявка принята.")
        try:
            if bot:
                text = (
                    f"✅ Ваша заявка {hcode(order_id)} принята банком.\n"
                    f"{order['action']} {order['amount']} {order['currency']}"
                )
                await bot.send_message(chat_id=int(user_id_str), text=text)
        except Exception as ne:
            logger.error("Notify client failed: %s", ne)

        await cq_bank_orders(callback)
    except Exception as e:
        logger.exception("cq_bank_accept failed: %s", e)
        await callback.answer("Ошибка при принятии заявки.", show_alert=True)


@bank_router.callback_query(F.data.startswith("bank:reject:"))
async def cq_bank_reject(callback: CallbackQuery):
    try:
        if await get_role(callback.from_user.id) != "bank":
            await callback.answer("Доступ запрещён.", show_alert=True)
            return
        order_id = callback.data.split(":", 2)[2]
        user_id_str = order_id.split("-", 1)[0]
        key = ORDER_KEY.format(user_id=user_id_str, order_id=order_id)
        raw = await redis.get(key)  # type: ignore
        if not raw:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return
        order = json.loads(raw)
        order["status"] = "rejected"
        order["updated_at"] = now_iso()

        pipe = redis.pipeline()
        pipe.set(key, json.dumps(order))
        pipe.srem(PENDING_ORDERS_SET, order_id)
        await pipe.execute()

        await callback.answer("Заявка отклонена.")
        try:
            if bot:
                text = (
                    f"❌ Ваша заявка {hcode(order_id)} отклонена банком.\n"
                    f"{order['action']} {order['amount']} {order['currency']}"
                )
                await bot.send_message(chat_id=int(user_id_str), text=text)
        except Exception as ne:
            logger.error("Notify client failed: %s", ne)

        await cq_bank_orders(callback)
    except Exception as e:
        logger.exception("cq_bank_reject failed: %s", e)
        await callback.answer("Ошибка при отклонении заявки.", show_alert=True)


@bank_router.callback_query(F.data == "bank:clear_orders")
async def cq_bank_clear_orders(callback: CallbackQuery):
    try:
        if await get_role(callback.from_user.id) != "bank":
            await callback.answer("Доступ запрещён.", show_alert=True)
            return
        try:
            pending = await redis.smembers(PENDING_ORDERS_SET)  # type: ignore
            if pending:
                await redis.delete(PENDING_ORDERS_SET)  # type: ignore
        except Exception as re:
            logger.error("Failed to clear pending set: %s", re)
        await callback.message.edit_text("Очередь pending очищена.", reply_markup=make_bank_menu())
        await callback.answer("Готово.")
    except Exception as e:
        logger.exception("cq_bank_clear_orders failed: %s", e)
        await callback.answer("Ошибка.", show_alert=True)


# -----------------------------
# FastAPI endpoints (healthcheck & webhook)
# -----------------------------
@app.get(HEALTHCHECK_PATH)
async def healthcheck():
    try:
        redis_ok = True
        try:
            pong = await redis.ping()  # type: ignore
            redis_ok = bool(pong)
        except Exception as re:
            logger.warning("Redis ping failed on healthcheck: %s", re)
            redis_ok = False

        return JSONResponse(
            {
                "status": "ok",
                "time": datetime.utcnow().isoformat() + "Z",
                "redis": "ok" if redis_ok else "error",
                "webhook_path": WEBHOOK_PATH,
                "strict_header": STRICT_HEADER,
                "async_updates": ASYNC_UPDATES,
            }
        )
    except Exception as e:
        logger.exception("Healthcheck error: %s", e)
        return JSONResponse({"status": "error"}, status_code=500)


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    # Optional header validation
    try:
        if STRICT_HEADER:
            secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
                logger.warning("Invalid secret token header on webhook.")
                raise HTTPException(status_code=403, detail="Forbidden")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Secret token validation error: %s", e)
        raise HTTPException(status_code=400, detail="Bad Request")

    if not bot:
        logger.error("BOT_TOKEN is missing; webhook cannot process updates.")
        raise HTTPException(status_code=500, detail="Bot not configured")

    try:
        data = await request.json()
    except Exception as e:
        logger.error("Invalid JSON in webhook: %s", e)
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        update = Update.model_validate(data)
    except Exception as e:
        logger.error("Update validate failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid update payload")

    try:
        if ASYNC_UPDATES:
            asyncio.create_task(dp.feed_update(bot, update))
        else:
            await dp.feed_update(bot, update)
    except Exception as e:
        logger.exception("dp.feed_update failed: %s", e)
    return PlainTextResponse("OK")


# -----------------------------
# Startup / Shutdown hooks
# -----------------------------
@app.on_event("startup")
async def on_startup():
    try:
        logger.info("Starting up application...")
        await set_commands()
        await set_webhook()
        if ENABLE_WATCHDOG:
            asyncio.create_task(watchdog_task())
        logger.info("Startup complete.")
    except Exception as e:
        logger.exception("Startup failed: %s", e)


@app.on_event("shutdown")
async def on_shutdown():
    try:
        logger.info("Shutting down...")
        if bot:
            with suppress(Exception):
                await bot.delete_webhook()
            with suppress(Exception):
                await bot.session.close()
        with suppress(Exception):
            await redis.close()  # type: ignore
        logger.info("Shutdown complete.")
    except Exception as e:
        logger.exception("Shutdown failed: %s", e)


# -----------------------------
# Local dev entrypoint
# -----------------------------
if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run("app:app", host=host, port=port, reload=bool(os.getenv("RELOAD", "0") == "1"))
