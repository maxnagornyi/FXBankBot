import asyncio
import hashlib
import logging
import os
import ssl
from decimal import Decimal, InvalidOperation
from typing import Optional, Literal

from fastapi import FastAPI, Request, Response, HTTPException
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.types import Message, Update, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage, DefaultKeyBuilder
from aiogram.client.default import DefaultBotProperties
from redis.asyncio import Redis

# ------------------------
# Logging
# ------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("FXBankBot")

# ------------------------
# Env variables
# ------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Environment variable BOT_TOKEN is required.")

WEBHOOK_BASE = os.getenv("WEBHOOK_URL")  # e.g. https://your-app.onrender.com
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "fxbankbot-secret")
REDIS_URL = os.getenv("REDIS_URL")       # e.g. rediss://default:pass@host:6379/0
PORT = int(os.getenv("PORT", "10000"))
HOST = "0.0.0.0"

WEBHOOK_PATH = f"/webhook/{hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:18]}"

# ------------------------
# FastAPI app
# ------------------------
app = FastAPI(title="FXBankBot")

# ------------------------
# Aiogram core (late init in startup)
# ------------------------
bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None
router = Router()

Mode = Literal["webhook", "polling"]
app.state.mode: Optional[Mode] = None
app.state.polling_task: Optional[asyncio.Task] = None

# ------------------------
# Keyboards
# ------------------------
KB_MAIN = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Покупка"), KeyboardButton(text="Продажа")],
        [KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите операцию",
)

# ------------------------
# FSM States
# ------------------------
class DealFSM(StatesGroup):
    client_name = State()
    operation = State()
    buy_currency = State()
    buy_budget_uah = State()
    sell_currency = State()
    sell_amount_cur = State()
    confirm = State()

# ------------------------
# Utils
# ------------------------
async def try_build_storage() -> object:
    """Создаём RedisStorage (Upstash) с TLS. Fallback на MemoryStorage."""
    if not REDIS_URL:
        logger.info("REDIS_URL not set, using MemoryStorage.")
        return MemoryStorage()
    try:
        conn_kwargs = {
            "encoding": "utf-8",
            "decode_responses": True,
            "socket_connect_timeout": 5,
            "socket_timeout": 5,
            "health_check_interval": 30,
            "retry_on_timeout": True,
        }
        if REDIS_URL.startswith("rediss://") and os.getenv("REDIS_SSL_NO_VERIFY") == "1":
            conn_kwargs["ssl_cert_reqs"] = ssl.CERT_NONE  # не рекомендуется, только если CA ломается

        redis = Redis.from_url(REDIS_URL, **conn_kwargs)
        await redis.ping()
        logger.info("Connected to Redis, using RedisStorage.")
        return RedisStorage(
            redis=redis,
            key_builder=DefaultKeyBuilder(with_bot_id=True, prefix="fxbank"),
        )
    except Exception as e:
        logger.warning(f"Redis недоступен: {e} — переключаюсь на память")
        return MemoryStorage()

def parse_decimal(value: str) -> Decimal:
    value = value.strip().replace(" ", "").replace(",", ".")
    return Decimal(value)

async def start_polling_task() -> None:
    if not (bot and dp):
        raise RuntimeError("Bot/Dispatcher is not initialized")
    if app.state.polling_task and not app.state.polling_task.done():
        return
    async def _run():
        try:
            await dp.start_polling(bot)  # без allowed_updates — принимаем всё
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Polling crashed:")
    app.state.polling_task = asyncio.create_task(_run())

async def stop_polling_task() -> None:
    task = app.state.polling_task
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    app.state.polling_task = None

async def switch_mode(new_mode: Mode) -> None:
    assert bot and dp
    if new_mode == "webhook":
        await stop_polling_task()
        full_url = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH
        await bot.set_webhook(
            url=full_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,  # allowed_updates убраны
        )
        app.state.mode = "webhook"
        logger.info(f"Switched to WEBHOOK mode: {full_url}")
    else:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        await start_polling_task()
        app.state.mode = "polling"
        logger.info("Switched to POLLING mode.")

# ------------------------
# Handlers
# ------------------------
@router.message(CommandStart())
@router.message(Command("start"))
@router.message(StateFilter("*"), F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    """Ловим /start в любом состоянии и даже как обычный текст."""
    await state.clear()
    await state.set_state(DealFSM.client_name)
    await message.answer(
        "Привет! Я FXBankBot.\n\n"
        "Давай оформим заявку. Сначала укажи <b>название клиента</b>.",
        parse_mode=ParseMode.HTML,
    )

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Текущая заявка отменена. Чтобы начать заново — отправьте /start.")

@router.message(Command("restart"))
async def cmd_restart(message: Message, state: FSMContext):
    """Очистка состояния пользователя и перезапуск режима (webhook/polling)."""
    await state.clear()
    info = ["Состояние пользователя очищено."]
    try:
        if WEBHOOK_BASE:
            await switch_mode("webhook")
            full_url = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH
            info.append(f"Вебхук переустановлен: <code>{full_url}</code>")
        else:
            await switch_mode("polling")
            info.append("Включён long polling (WEBHOOK_URL не задан).")
        await message.answer("Перезапуск:\n" + "\n".join(info), parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.answer(f"Перезапуск частичный: {e!r}")

@router.message(Command("status"))
async def cmd_status(message: Message):
    try:
        storage_type = type(dp.storage).__name__
    except Exception:
        storage_type = "unknown"
    redis_status = "memory"
    try:
        if hasattr(dp.storage, "redis"):
            ok = await dp.storage.redis.ping()
            redis_status = "ok" if ok else "fail"
    except Exception as e:
        redis_status = f"fail: {e!r}"
    mode = getattr(app.state, "mode", None)
    await message.answer(
        f"🔎 Mode: <b>{mode}</b>\nStorage: <b>{storage_type}</b>\nRedis ping: <b>{redis_status}</b>",
        parse_mode="HTML",
    )

@router.message(DealFSM.client_name, F.text)
async def ask_operation(message: Message, state: FSMContext):
    client = message.text.strip()
    if not client:
        await message.answer("Название клиента не может быть пустым.")
        return
    await state.update_data(client_name=client)
    await state.set_state(DealFSM.operation)
    await message.answer(
        f"Клиент: <b>{client}</b>\nВыберите операцию:",
        reply_markup=KB_MAIN,
        parse_mode=ParseMode.HTML,
    )

@router.message(DealFSM.operation, F.text.lower().in_(("покупка", "купить")))
async def op_buy(message: Message, state: FSMContext):
    await state.update_data(operation="buy")
    await state.set_state(DealFSM.buy_currency)
    await message.answer(
        "Покупка валюты.\nУкажите, <b>какую валюту покупаем</b> (например: USD, EUR, GBP).",
        parse_mode=ParseMode.HTML,
    )

@router.message(DealFSM.operation, F.text.lower().in_(("продажа", "продать")))
async def op_sell(message: Message, state: FSMContext):
    await state.update_data(operation="sell")
    await state.set_state(DealFSM.sell_currency)
    await message.answer(
        "Продажа валюты.\nУкажите, <b>какую валюту продаём</b> (например: USD, EUR, GBP).",
        parse_mode=ParseMode.HTML,
    )

@router.message(DealFSM.operation)
async def op_unknown(message: Message):
    await message.answer("Выберите: «Покупка» или «Продажа».")

# ---- BUY FLOW ----
@router.message(DealFSM.buy_currency, F.text)
async def buy_currency(message: Message, state: FSMContext):
    cur = message.text.strip().upper()
    if len(cur) not in (3, 4):
        await message.answer("Некорректный код валюты. Пример: USD, EUR.")
        return
    await state.update_data(buy_currency=cur)
    await state.set_state(DealFSM.buy_budget_uah)
    await message.answer(
        "Укажите <b>бюджет в UAH</b>, который готовы потратить (например: 100000 или 100000,50).",
        parse_mode=ParseMode.HTML,
    )

@router.message(DealFSM.buy_budget_uah, F.text)
async def buy_budget(message: Message, state: FSMContext):
    try:
        budget = parse_decimal(message.text)
        if budget <= 0:
            raise InvalidOperation
    except Exception:
        await message.answer("Не получилось распознать сумму. Введите число, например 150000,50.")
        return
    data = await state.get_data()
    client = data.get("client_name")
    currency = data.get("buy_currency")
    await state.clear()
    await message.answer(
        "✅ <b>Заявка сохранена</b>\n\n"
        f"Клиент: <b>{client}</b>\n"
        f"Операция: <b>Покупка</b>\n"
        f"Валюта: <b>{currency}</b>\n"
        f"Бюджет: <b>{budget} UAH</b>\n\n"
        "Чтобы оформить новую заявку — /start.",
        parse_mode=ParseMode.HTML,
    )

# ---- SELL FLOW ----
@router.message(DealFSM.sell_currency, F.text)
async def sell_currency(message: Message, state: FSMContext):
    cur = message.text.strip().upper()
    if len(cur) not in (3, 4):
        await message.answer("Некорректный код валюты. Пример: USD, EUR.")
        return
    await state.update_data(sell_currency=cur)
    await state.set_state(DealFSM.sell_amount_cur)
    await message.answer(
        f"Укажите <b>сумму {cur}</b>, которую хотите продать.",
        parse_mode=ParseMode.HTML,
    )

@router.message(DealFSM.sell_amount_cur, F.text)
async def sell_amount(message: Message, state: FSMContext):
    data = await state.get_data()
    currency = data.get("sell_currency") or "XXX"
    try:
        amount = parse_decimal(message.text)
        if amount <= 0:
            raise InvalidOperation
    except Exception:
        await message.answer(f"Введите число в {currency}, например 2500,75.")
        return
    client = data.get("client_name")
    await state.clear()
    await message.answer(
        "✅ <b>Заявка сохранена</b>\n\n"
        f"Клиент: <b>{client}</b>\n"
        f"Операция: <b>Продажа</b>\n"
        f"Валюта: <b>{currency}</b>\n"
        f"Сумма: <b>{amount} {currency}</b>\n\n"
        "Чтобы оформить новую заявку — /start.",
        parse_mode=ParseMode.HTML,
    )

# ------------------------
# Error logger (на всякий случай)
# ------------------------
@router.errors()
async def errors_handler(event, error):
    logger.exception("Unhandled error: %r", error)

# ------------------------
# FastAPI endpoints
# ------------------------
class Health(BaseModel):
    status: str = "ok"
    mode: Optional[str] = None

@app.get("/", response_model=Health)
async def healthcheck():
    return Health(status="ok", mode=app.state.mode)

@app.head("/")
async def healthcheck_head():
    return Response(status_code=200)

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid secret token")
    raw = await request.json()
    update = Update.model_validate(raw, context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response(status_code=200)

# ------------------------
# Startup / Shutdown
# ------------------------
@app.on_event("startup")
async def on_startup():
    global bot, dp
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = await try_build_storage()
    dp = Dispatcher(storage=storage)
    dp.include_router(router)

    if WEBHOOK_BASE:
        full_url = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH
        try:
            await bot.set_webhook(
                url=full_url,
                secret_token=WEBHOOK_SECRET,
                drop_pending_updates=True,  # allowed_updates убраны
            )
            app.state.mode = "webhook"
            logger.info(f"Webhook set to {full_url}")
        except Exception:
            logger.exception("Failed to set webhook at startup, switching to polling:")
            await switch_mode("polling")
    else:
        await switch_mode("polling")

@app.on_event("shutdown")
async def on_shutdown():
    try:
        if app.state.mode == "polling":
            await stop_polling_task()
        else:
            try:
                await bot.delete_webhook(drop_pending_updates=False)
            except Exception:
                pass
        await bot.session.close()
    except Exception:
        pass
    logger.info("Shutdown complete.")

# ------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
