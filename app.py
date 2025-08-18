import asyncio
import json
import logging
import os
import sys
from contextlib import suppress
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
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

WEBHOOK_PATH = "/webhook/secret"  # требование задачи
HEALTHCHECK_PATH = "/"

# Валюты
CCY = ("USD", "EUR", "PLN", "UAH")
TRADE_CCY = ("USD", "EUR", "PLN")  # клиентские операции против UAH

# ---- Ключи Redis ----
ROLE_KEY = "role:{user_id}"                    # "client" | "bank"
ORDER_KEY = "order:{user_id}:{order_id}"       # JSON order
USER_ORDERS_SET = "orders_by_user:{user_id}"   # set(order_id)
PENDING_ORDERS_SET = "orders:pending"          # set(order_id)
RATES_HASH_PAIRS = "rates_pairs"               # hash pair -> rate (ручные курсы банка)

# ---- Заглушка курса (в будущем — Bloomberg/LSEG) ----
# Базовый набор: USDUAH, EURUAH, PLNUAH, EURUSD, EURPLN, USDPLN
STUB_RATES: Dict[str, float] = {
    "USD/UAH": 41.25,
    "EUR/UAH": 45.10,
    "PLN/UAH": 10.60,
    "EUR/USD": 1.0920,
    "USD/PLN": 3.8760,   # из инверсии PLN/USD=0.2580
    "EUR/PLN": 4.2326,   # EUR/USD * USD/PLN
}

# -----------------------------
# FastAPI & Aiogram init
# -----------------------------
app = FastAPI(title="FX Bank Bot", version="1.2.0")

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
class ClientTradeSG(StatesGroup):
    # Покупка/продажа валюты против UAH
    enter_amount = State()
    choose_currency = State()
    enter_rate = State()
    confirm = State()


class ClientConvertSG(StatesGroup):
    # Конвертация валюта->валюта
    choose_from = State()
    choose_to = State()
    choose_amount_mode = State()  # sell_amount/buy_amount
    enter_amount = State()
    enter_rate = State()
    confirm = State()


class BankSetPairRateSG(StatesGroup):
    choose_pair_group = State()
    choose_pair = State()
    enter_rate = State()
    confirm = State()


# -----------------------------
# Utilities
# -----------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fmt_amount(ccy: str, amt: float) -> str:
    try:
        q = 2
        return f"{amt:.{q}f} {ccy}"
    except Exception:
        return f"{amt} {ccy}"


def pair(base: str, quote: str) -> str:
    return f"{base}/{quote}"


def inverse_pair(p: str) -> str:
    b, q = p.split("/")
    return f"{q}/{b}"


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
            InlineKeyboardButton(text="🟢 Купить (за UAH)", callback_data="client:buy"),
            InlineKeyboardButton(text="🔵 Продать (за UAH)", callback_data="client:sell"),
        ],
        [
            InlineKeyboardButton(text="🔁 Конвертация", callback_data="client:convert"),
            InlineKeyboardButton(text="📄 Мои заявки", callback_data="client:orders"),
        ],
        [
            InlineKeyboardButton(text="📊 Курсы", callback_data="common:rates"),
            InlineKeyboardButton(text="🔁 Сменить роль", callback_data="role:choose"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def make_bank_menu() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton(text="✏️ Установить курс пары", callback_data="bank:set_pair_rate"),
            InlineKeyboardButton(text="📥 Заявки клиентов", callback_data="bank:orders"),
        ],
        [
            InlineKeyboardButton(text="📊 Текущие курсы", callback_data="common:rates"),
            InlineKeyboardButton(text="🧹 Очистить pending", callback_data="bank:clear_orders"),
        ],
        [InlineKeyboardButton(text="🔁 Сменить роль", callback_data="role:choose")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def make_currency_keyboard(exclude: Optional[str] = None) -> InlineKeyboardMarkup:
    row = []
    for c in TRADE_CCY:
        if exclude and c == exclude:
            continue
        row.append(InlineKeyboardButton(text=c, callback_data=f"ccy:{c}"))
    return InlineKeyboardMarkup(inline_keyboard=[row, [InlineKeyboardButton(text="↩️ Назад", callback_data="common:back")]])


def make_amount_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Ввести сумму ПРОДАЖИ", callback_data="conv:mode:sell"),
            ],
            [
                InlineKeyboardButton(text="Ввести сумму ПОКУПКИ", callback_data="conv:mode:buy"),
            ],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="common:back")],
        ]
    )


def make_confirm_keyboard(ok_cb: str, cancel_cb: str = "common:cancel") -> InlineKeyboardMarkup:
    kb = [[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=ok_cb),
        InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_cb),
    ]]
    return InlineKeyboardMarkup(inline_keyboard=kb)


def bank_pair_groups_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Пары к UAH", callback_data="bank:pairgrp:uah"),
                InlineKeyboardButton(text="Кросс-пары", callback_data="bank:pairgrp:cross"),
            ],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="common:back")],
        ]
    )


def bank_pairs_keyboard(group: str) -> InlineKeyboardMarkup:
    if group == "uah":
        pairs = ["USD/UAH", "EUR/UAH", "PLN/UAH"]
    else:
        pairs = ["EUR/USD", "USD/PLN", "EUR/PLN"]
    rows = [[InlineKeyboardButton(text=p, callback_data=f"bank:pair:{p}") for p in pairs]]
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data="bank:set_pair_rate")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def safe_answer(callback: CallbackQuery, text: Optional[str] = None, show_alert: bool = False) -> None:
    """Безопасный ответ на callback; игнорируем 'query is too old'."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as e:
        msg = str(e)
        if "query is too old" in msg or "query ID is invalid" in msg:
            logger.debug("Ignoring old/invalid callback: %s", msg)
        else:
            logger.warning("callback.answer bad request: %s", msg)
    except Exception as e:
        logger.warning("callback.answer error: %s", e)


# -------- Работа с курсами --------
async def get_manual_rate(p: str) -> Optional[float]:
    try:
        val = await redis.hget(RATES_HASH_PAIRS, p)  # type: ignore
        return float(val) if val is not None else None
    except Exception as e:
        logger.error("get_manual_rate failed: %s", e)
        return None


def stub_rate(p: str) -> Optional[float]:
    if p in STUB_RATES:
        return STUB_RATES[p]
    inv = inverse_pair(p)
    if inv in STUB_RATES and STUB_RATES[inv] != 0:
        return 1.0 / STUB_RATES[inv]
    # Кросс через якорные валюты
    base, quote = p.split("/")
    anchors = ["UAH", "USD", "EUR", "PLN"]
    for a in anchors:
        if a == base or a == quote:
            continue
        x = STUB_RATES.get(f"{base}/{a}")
        y = STUB_RATES.get(f"{quote}/{a}")
        if x and y:
            return x / y
        # пробуем через инверсии
        xi = STUB_RATES.get(f"{a}/{base}")
        yi = STUB_RATES.get(f"{a}/{quote}")
        if xi and yi and xi != 0:
            return yi / xi
    return None


async def get_pair_rate(p: str) -> Tuple[Optional[float], str]:
    """
    Порядок приоритета:
    1) ручной курс банка (Redis)
    2) заглушка STUB_RATES
    3) None (если вычислить не удалось)
    """
    manual = await get_manual_rate(p)
    if manual is not None:
        return manual, "manual"
    s = stub_rate(p)
    if s is not None:
        return s, "stub"
    return None, "unknown"


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
                BotCommand(command="bank", description="Вход банка: /bank пароль"),
                BotCommand(command="cancel", description="Отмена действия"),
            ],
            scope=BotCommandScopeDefault(),
        )
        logger.info("Bot commands set.")
    except Exception as e:
        logger.error("set_my_commands failed: %s", e)


async def _desired_webhook_url() -> Optional[str]:
    if not WEBHOOK_BASE_URL:
        return None
    return f"{WEBHOOK_BASE_URL}{WEBHOOK_PATH}"


async def set_webhook(force: bool = False) -> None:
    """Idempotent setWebhook."""
    if not bot:
        logger.warning("Bot is None; skip set_webhook.")
        return
    desired = await _desired_webhook_url()
    if not desired:
        logger.warning("WEBHOOK_URL/RENDER_EXTERNAL_URL not set; skip webhook setup.")
        return

    try:
        info = await bot.get_webhook_info()
        current_url = (info.url or "").rstrip("/")
    except Exception as e:
        logger.warning("get_webhook_info failed: %s", e)
        current_url = ""

    if (current_url == desired.rstrip("/")) and not force:
        logger.info("Webhook already set to %s — skip update.", desired)
        return

    try:
        used_updates: List[str] = dp.resolve_used_update_types()
        await bot.set_webhook(
            url=desired,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=False,
            allowed_updates=used_updates,
        )
        logger.info("Webhook set to %s (allowed_updates=%s)", desired, used_updates)
    except TelegramRetryAfter as e:
        wait_s = int(getattr(e, "retry_after", 1) or 1)
        logger.warning("Rate limited on set_webhook, sleep %ss then retry once...", wait_s)
        await asyncio.sleep(wait_s)
        with suppress(Exception):
            used_updates = dp.resolve_used_update_types()
            await bot.set_webhook(
                url=desired,
                secret_token=WEBHOOK_SECRET,
                drop_pending_updates=False,
                allowed_updates=used_updates,
            )
            logger.info("Webhook set after retry to %s", desired)
    except TelegramBadRequest as e:
        logger.error("TelegramBadRequest on set_webhook: %s", e)
    except Exception as e:
        logger.exception("Failed to set webhook: %s", e)


async def watchdog_task():
    """Watchdog: проверяет/чинит вебхук только при рассинхроне."""
    if not ENABLE_WATCHDOG:
        return
    logger.info("Watchdog enabled (interval=%ss).", WATCHDOG_INTERVAL)
    while True:
        try:
            desired = await _desired_webhook_url()
            if not desired:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                continue
            info = None
            with suppress(Exception):
                info = await bot.get_webhook_info()
            current = (info.url if info else "") or ""
            if current.rstrip("/") != desired.rstrip("/"):
                logger.warning("Watchdog: webhook mismatch (current=%s, desired=%s). Fixing...", current, desired)
                await set_webhook(force=True)
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
            f"Выберите роль или введите команду: /bank пароль — для входа банка.",
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
            "/bank пароль — вход для банка\n"
            "/cancel — отменить текущее действие\n\n"
            "Операции:\n"
            "— Купить/Продать: валюта USD/EUR/PLN против UAH (клиент вводит сумму и СВОЙ курс).\n"
            "— Конвертация: валюта→валюта (клиент выбирает from/to, вводит сумму покупки ИЛИ продажи и СВОЙ курс).\n\n"
            "Курсы: пара BASE/QUOTE. Источник — ручной курс банка или заглушка (USDUAH, EURUAH, PLNUAH, EURUSD, EURPLN, USDPLN) с кросс-расчётом."
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
            "— Клиент: создать заявку (покупка/продажа/конвертация) с указанием СВОЕГО курса пары.\n"
            "— Банк: устанавливать курсы пар и управлять заявками.\n"
            "Курсы считаются как BASE/QUOTE (пример: USD/UAH=41.25).",
            reply_markup=make_role_keyboard(),
        )
        await safe_answer(callback)
    except Exception as e:
        logger.exception("cq_help failed: %s", e)


@common_router.callback_query(F.data == "role:choose")
async def cq_role_choose(callback: CallbackQuery, state: FSMContext):
    try:
        await state.clear()
        await callback.message.edit_text("Выберите роль:", reply_markup=make_role_keyboard())
        await safe_answer(callback)
    except Exception as e:
        logger.exception("cq_role_choose failed: %s", e)


@common_router.callback_query(F.data.startswith("role:"))
async def cq_role_set(callback: CallbackQuery):
    try:
        role = callback.data.split(":", 1)[1]
        if role not in ("client", "bank"):
            await safe_answer(callback, "Неизвестная роль.", show_alert=True)
            return
        await set_role(callback.from_user.id, role if role == "client" else "bank")
        if role == "bank":
            await callback.message.edit_text(
                "Роль установлена: 🏦 Банк.\n"
                "Если ещё не вводили пароль, выполните команду: /bank пароль\nВыберите действие:",
                reply_markup=make_bank_menu(),
            )
        else:
            await callback.message.edit_text("Роль установлена: 👤 Клиент.\nВыберите действие:", reply_markup=make_client_menu())
        await safe_answer(callback, "Роль изменена.")
    except Exception as e:
        logger.exception("cq_role_set failed: %s", e)


@common_router.callback_query(F.data == "common:rates")
async def cq_show_rates(callback: CallbackQuery):
    try:
        # Соберём список отображаемых пар
        pairs = ["USD/UAH", "EUR/UAH", "PLN/UAH", "EUR/USD", "USD/PLN", "EUR/PLN"]
        lines = []
        for p in pairs:
            r, src = await get_pair_rate(p)
            if r:
                lines.append(f"{p}: {hcode(f'{r:.4f}')} ({src})")
            else:
                lines.append(f"{p}: недоступно")
        await callback.message.edit_text(
            "Текущие курсы пар:\n" + "\n".join(lines),
            reply_markup=(make_bank_menu() if await get_role(callback.from_user.id) == "bank" else make_client_menu()),
        )
        await safe_answer(callback)
    except Exception as e:
        logger.exception("cq_show_rates failed: %s", e)


# -----------------------------
# Commands: bank login
# -----------------------------
@common_router.message(Command("bank"))
async def cmd_bank(message: Message):
    try:
        parts = (message.text or "").strip().split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("❌ Укажите пароль: /bank пароль")
            return
        if parts[1] == BANK_PASSWORD:
            await set_role(message.from_user.id, "bank")
            await message.answer("🏦 Успешный вход. Вы вошли как банк.", reply_markup=make_bank_menu())
        else:
            await message.answer("❌ Неверный пароль.")
    except Exception as e:
        logger.exception("cmd_bank failed: %s", e)
        await message.answer("Ошибка при входе банка.")
# ---------------------- FSM STATES ----------------------
class NewOrder(StatesGroup):
    choosing_type = State()
    entering_amount = State()
    entering_currency = State()
    entering_pair = State()  # только для конверсии
    entering_rate = State()
    confirming = State()


# ---------------------- CLIENT: новая заявка ----------------------
@router.message(F.text == "➕ Новая заявка")
async def new_order(message: Message, state: FSMContext):
    await state.set_state(NewOrder.choosing_type)
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Покупка"), KeyboardButton(text="Продажа")],
            [KeyboardButton(text="Конверсия")]
        ],
        resize_keyboard=True
    )
    await message.answer("Выберите тип операции:", reply_markup=kb)


@router.message(NewOrder.choosing_type)
async def choose_type(message: Message, state: FSMContext):
    op = message.text.lower()
    if op not in ["покупка", "продажа", "конверсия"]:
        return await message.answer("❌ Выберите из списка.")
    await state.update_data(operation=op)
    await state.set_state(NewOrder.entering_amount)
    await message.answer("Введите сумму:", reply_markup=types.ReplyKeyboardRemove())


@router.message(NewOrder.entering_amount)
async def enter_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        return await message.answer("❌ Введите число.")
    await state.update_data(amount=amount)

    data = await state.get_data()
    if data["operation"] == "конверсия":
        await state.set_state(NewOrder.entering_pair)
        await message.answer("Введите валютную пару (например: USD/EUR):")
    else:
        await state.set_state(NewOrder.entering_currency)
        await message.answer("Введите валюту (например: USD, EUR):")


@router.message(NewOrder.entering_currency)
async def enter_currency(message: Message, state: FSMContext):
    currency = message.text.upper()
    await state.update_data(currency=currency)
    await state.set_state(NewOrder.entering_rate)
    await message.answer("Введите курс (ваш желаемый):")


@router.message(NewOrder.entering_pair)
async def enter_pair(message: Message, state: FSMContext):
    pair = message.text.upper().replace(" ", "")
    if "/" not in pair:
        return await message.answer("❌ Формат: USD/EUR")
    await state.update_data(pair=pair)
    await state.set_state(NewOrder.entering_rate)
    await message.answer("Введите курс (ваш желаемый):")


@router.message(NewOrder.entering_rate)
async def enter_rate(message: Message, state: FSMContext):
    try:
        rate = float(message.text.replace(",", "."))
    except ValueError:
        return await message.answer("❌ Введите число.")
    await state.update_data(rate=rate)

    data = await state.get_data()
    if data["operation"] == "конверсия":
        base, quote = data["pair"].split("/")
        order = {
            "id": len(orders) + 1,
            "client": message.from_user.first_name,
            "operation": "конверсия",
            "amount": data["amount"],
            "pair": data["pair"],
            "rate": rate,
            "status": "new"
        }
        orders[order["id"]] = order
        await message.answer(
            f"✅ Конверсия создана\n"
            f"💱 {order['pair']} | {order['amount']}\n"
            f"📊 Курс клиента: {rate}",
            reply_markup=client_kb
        )
    else:
        order = {
            "id": len(orders) + 1,
            "client": message.from_user.first_name,
            "operation": data["operation"],
            "amount": data["amount"],
            "currency": data["currency"],
            "rate": rate,
            "status": "new"
        }
        orders[order["id"]] = order
        await message.answer(
            f"✅ Заявка создана\n"
            f"💱 {order['operation']} {order['amount']} {order['currency']}\n"
            f"📊 Курс клиента: {rate}",
            reply_markup=client_kb
        )

    await state.clear()

    # уведомим банк
    for uid, role in user_roles.items():
        if role == "bank":
            try:
                await bot.send_message(
                    uid,
                    f"🔔 Новая заявка #{order['id']}",
                    reply_markup=bank_order_kb(order["id"])
                )
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
        text = (
            f"📌 <b>Заявка #{order['id']}</b>\n"
            f"👤 {order['client']}\n"
            f"💱 {order['operation']}\n"
        )
        if order["operation"] == "конверсия":
            text += f"🔄 Пара: {order['pair']}, {order['amount']}\n"
        else:
            text += f"💵 {order['amount']} {order['currency']}\n"
        text += f"📊 Курс клиента: {order['rate']}\n📍 Статус: {order['status']}"
        await message.answer(text, reply_markup=bank_order_kb(order["id"]))


# ---------------------- INLINE CALLBACKS ----------------------
@router.callback_query(F.data.startswith("accept:"))
async def cb_accept(call: CallbackQuery):
    oid = int(call.data.split(":")[1])
    order = orders.get(oid)
    if not order:
        return await call.answer("Заявка не найдена", show_alert=True)
    order["status"] = "accepted"
    await call.message.edit_text(f"✅ Принято\n{order}")
    await call.answer("Заявка принята ✅")


@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: CallbackQuery):
    oid = int(call.data.split(":")[1])
    order = orders.get(oid)
    if not order:
        return await call.answer("Заявка не найдена", show_alert=True)
    order["status"] = "rejected"
    await call.message.edit_text(f"❌ Отклонено\n{order}")
    await call.answer("Заявка отклонена ❌")


# ---------------------- FastAPI + Webhook ----------------------
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"


@app.on_event("startup")
async def on_startup():
    logger.info("Startup FXBankBot...")
    url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}{WEBHOOK_PATH}"
    try:
        await bot.set_webhook(url)
        logger.info(f"Webhook set {url}")
    except Exception as e:
        logger.error(f"Ошибка вебхука: {e}")


@app.on_event("shutdown")
async def on_shutdown():
    with suppress(Exception):
        await bot.delete_webhook()
    logger.info("Shutdown complete.")


@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    update = await request.json()
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}


@app.get("/")
async def root():
    return {"status": "ok", "service": "FXBankBot"}
