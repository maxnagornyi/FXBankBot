import os
import logging
from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.filters import Command
from fastapi import FastAPI, Request
import uvicorn

# -------------------- ЛОГИ --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("fxbank_bot")

# -------------------- НАСТРОЙКИ --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "fxbank-secret")
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://fxbankbot.onrender.com")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

WEBHOOK_URL = f"{WEBAPP_URL}{WEBHOOK_PATH}"

# -------------------- FSM --------------------
class ClientFSM(StatesGroup):
    client_name = State()
    operation = State()
    currency_from = State()
    currency_to = State()
    amount = State()
    rate = State()
    confirm = State()

# -------------------- РЕСУРСЫ --------------------
router = Router()
storage = RedisStorage.from_url(REDIS_URL)
bot = Bot(token=BOT_TOKEN, default=None)
dp = Dispatcher(storage=storage)
dp.include_router(router)

# Заглушка курсов
RATES = {
    "USDUAH": 41.2,
    "EURUAH": 45.5,
    "PLNUAH": 10.1,
    "EURUSD": 1.1,
    "EURPLN": 4.3,
    "USDPLN": 3.9,
}

# -------------------- ХЭНДЛЕРЫ --------------------
@router.message(Command("start"))
async def start_cmd(message: Message, state: FSMContext):
    await state.set_state(ClientFSM.client_name)
    await message.answer("Введите название клиента:")

@router.message(ClientFSM.client_name)
async def get_client_name(message: Message, state: FSMContext):
    await state.update_data(client_name=message.text)
    await state.set_state(ClientFSM.operation)
    await message.answer("Выберите операцию: покупка / продажа / конверсия")

@router.message(ClientFSM.operation)
async def choose_operation(message: Message, state: FSMContext):
    op = message.text.lower()
    if op not in ["покупка", "продажа", "конверсия"]:
        await message.answer("Нужно выбрать: покупка / продажа / конверсия")
        return
    await state.update_data(operation=op)
    await state.set_state(ClientFSM.currency_from)
    await message.answer("Введите валюту продажи (например USD):")

@router.message(ClientFSM.currency_from)
async def get_currency_from(message: Message, state: FSMContext):
    await state.update_data(currency_from=message.text.upper())
    data = await state.get_data()
    if data["operation"] == "конверсия":
        await state.set_state(ClientFSM.currency_to)
        await message.answer("Введите валюту покупки (например EUR):")
    else:
        await state.set_state(ClientFSM.amount)
        await message.answer("Введите сумму в валюте:")

@router.message(ClientFSM.currency_to)
async def get_currency_to(message: Message, state: FSMContext):
    await state.update_data(currency_to=message.text.upper())
    await state.set_state(ClientFSM.amount)
    await message.answer("Введите сумму:")

@router.message(ClientFSM.amount)
async def get_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введите число!")
        return
    await state.update_data(amount=amount)
    await state.set_state(ClientFSM.rate)
    await message.answer("Введите курс сделки (или оставьте пустым для автоподстановки):")

@router.message(ClientFSM.rate)
async def get_rate(message: Message, state: FSMContext):
    data = await state.get_data()
    rate = None
    if message.text.strip():
        try:
            rate = float(message.text.replace(",", "."))
        except ValueError:
            await message.answer("Курс должен быть числом")
            return
    else:
        pair = f"{data['currency_from']}{data.get('currency_to','UAH')}"
        rate = RATES.get(pair, 1.0)
    await state.update_data(rate=rate)
    await state.set_state(ClientFSM.confirm)

    text = (
        f"Клиент: {data['client_name']}\n"
        f"Операция: {data['operation']}\n"
        f"Валюта продажи: {data['currency_from']}\n"
        f"Валюта покупки: {data.get('currency_to','UAH')}\n"
        f"Сумма: {data['amount']}\n"
        f"Курс: {rate}\n\n"
        f"Подтвердите сделку?"
    )
    await message.answer(text)

@router.message(ClientFSM.confirm)
async def confirm_deal(message: Message, state: FSMContext):
    if message.text.lower() not in ["да", "нет"]:
        await message.answer("Введите 'да' или 'нет'")
        return
    if message.text.lower() == "да":
        data = await state.get_data()
        await message.answer("✅ Сделка сохранена!")
        logger.info(f"Сделка сохранена: {data}")
    else:
        await message.answer("❌ Сделка отменена.")
    await state.clear()

@router.message(Command("rate"))
async def show_rates(message: Message):
    text = "📊 Курсы валют:\n"
    for pair, rate in RATES.items():
        text += f"{pair}: {rate}\n"
    await message.answer(text)

# -------------------- FASTAPI --------------------
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    logger.info("RedisStorage initialized.")
    await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()
    logger.info("Webhook удалён")

@app.post(WEBHOOK_PATH)
async def webhook_handler(request: Request):
    update = await request.json()
    await dp.feed_webhook_update(bot, update, WEBHOOK_SECRET)
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "FXBankBot работает 🚀"}

# -------------------- ЗАПУСК --------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
