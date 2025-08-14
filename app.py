import os
import random
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from dotenv import load_dotenv

# ====================== ENV & LOGGING ======================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
if not BOT_TOKEN or not WEBHOOK_URL:
    raise ValueError("❌ BOT_TOKEN или WEBHOOK_URL не найдены в окружении!")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ====================== In-memory storage (MVP) ======================
requests_db = []         # [{id, operation, currency1, currency2, amount, rate, client_name, status}]
user_roles = {}          # {user_id: "client"|"bank"}
client_map = {}          # {request_id: client_user_id}
counter_offers = {}      # {request_id: counter_rate}

# ====================== FSM ======================
class RequestForm(StatesGroup):
    client_name = State()   # 1) имя клиента (первый шаг)
    operation = State()     # 2) операция
    currency1 = State()     # 3) валюта покупки/продажи/первая (зависит от operation)
    currency2 = State()     # 4) (только для Convert)
    amount = State()        # 5) сумма
    rate = State()          # 6) курс
    confirm = State()       # 7) подтверждение

class CounterForm(StatesGroup):
    new_rate = State()

class UpdateRateForm(StatesGroup):
    update_rate = State()

# ====================== Mock rates ======================
def get_mock_rates():
    return {
        "USD/UAH": round(random.uniform(39.8, 40.3), 2),
        "EUR/UAH": round(random.uniform(43.0, 44.0), 2),
        "PLN/UAH": round(random.uniform(9.5, 9.9), 2),
        "EUR/USD": round(random.uniform(1.05, 1.10), 2),
    }

# ====================== Keyboards ======================
role_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="👤 Клиент", callback_data="role_client")],
    [InlineKeyboardButton(text="🏦 Банк", callback_data="role_bank")],
])

operation_kb = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="💰 Sell", callback_data="Sell")],
    [InlineKeyboardButton(text="💵 Buy", callback_data="Buy")],
    [InlineKeyboardButton(text="🔄 Convert", callback_data="Convert")],
])

def currency_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="USD", callback_data="USD")],
        [InlineKeyboardButton(text="EUR", callback_data="EUR")],
        [InlineKeyboardButton(text="PLN", callback_data="PLN")],
    ])

client_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Новая заявка"), KeyboardButton(text="📊 Курс (/rate)")],
    ],
    resize_keyboard=True,
)

# ====================== Helpers ======================
def render_request(r: dict) -> str:
    status_icon = "⏳" if r["status"] == "pending" else "✅" if r["status"] == "approved" else "❌" if r["status"] == "rejected" else "💬"
    return (
        f"📌 Заявка #{r['id']} | {status_icon} {r['status'].upper()}\n"
        f"💼 {r['operation']} {r['currency1']}/{r['currency2']} @ {r['rate']}\n"
        f"💵 Сумма: {r['amount']}\n"
        f"👤 Клиент: {r['client_name']}"
    )

async def notify_client(req_id: int, text: str, buttons: InlineKeyboardMarkup | None = None):
    user_id = client_map.get(req_id)
    if not user_id:
        return
    try:
        await bot.send_message(user_id, text, reply_markup=buttons)
    except Exception as e:
        logging.warning("Failed to notify client %s for req %s: %s", user_id, req_id, e)

# ====================== BASIC HANDLERS ======================
@dp.message(Command("start"))
async def start_cmd(message: Message):
    await message.answer("👋 Привет! Выберите роль:", reply_markup=role_kb)

@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "Доступные действия:\n"
        "• /start — выбрать роль (Клиент/Банк)\n"
        "• /rate — посмотреть mock‑курсы\n"
        "• Клиент: «➕ Новая заявка»\n"
        "• Банк: /list — список заявок"
    )

@dp.callback_query(F.data.startswith("role_"))
async def set_role(callback: CallbackQuery):
    role = callback.data.split("_", 1)[1]
    user_roles[callback.from_user.id] = role
    if role == "client":
        await callback.message.answer("✅ Роль установлена: Клиент.\n📋 Меню:", reply_markup=client_menu)
    else:
        await callback.message.answer("✅ Роль установлена: Банк.\nИспользуйте /list для просмотра заявок.")
    await callback.answer()

# ====================== CLIENT FLOW ======================
@dp.message(F.text == "➕ Новая заявка")
async def new_request(message: Message, state: FSMContext):
    if user_roles.get(message.from_user.id) != "client":
        return await message.answer("⛔ Только для клиентов. Нажмите /start и выберите роль.")
    # 1) ПЕРВЫЙ ВОПРОС — имя клиента
    await message.answer("Введите имя клиента:")
    await state.set_state(RequestForm.client_name)

@dp.message(RequestForm.client_name)
async def step_client_name(message: Message, state: FSMContext):
    await state.update_data(client_name=message.text.strip())
    # 2) операция
    await message.answer("Выберите операцию:", reply_markup=operation_kb)
    await state.set_state(RequestForm.operation)

@dp.callback_query(RequestForm.operation)
async def step_operation(callback: CallbackQuery, state: FSMContext):
    op = callback.data
    await state.update_data(operation=op)
    if op == "Sell":
        await callback.message.answer("Введите валюту продажи:", reply_markup=currency_kb())
    elif op == "Buy":
        await callback.message.answer("Введите валюту покупки:", reply_markup=currency_kb())
    else:  # Convert
        await callback.message.answer("Выберите первую валюту:", reply_markup=currency_kb())
    await state.set_state(RequestForm.currency1)
    await callback.answer()

@dp.callback_query(RequestForm.currency1)
async def step_currency1(callback: CallbackQuery, state: FSMContext):
    await state.update_data(currency1=callback.data)
    data = await state.get_data()
    op = data["operation"]
    if op == "Convert":
        await callback.message.answer("Выберите вторую валюту:", reply_markup=currency_kb())
        await state.set_state(RequestForm.currency2)
    else:
        await callback.message.answer("Введите сумму (например: 0.5 mio):")
        await state.set_state(RequestForm.amount)
    await callback.answer()

@dp.callback_query(RequestForm.currency2)
async def step_currency2(callback: CallbackQuery, state: FSMContext):
    await state.update_data(currency2=callback.data)
    await callback.message.answer("Введите сумму (например: 0.5 mio):")
    await state.set_state(RequestForm.amount)
    await callback.answer()

@dp.message(RequestForm.amount)
async def step_amount(message: Message, state: FSMContext):
    amount = message.text.strip()
    await state.update_data(amount=amount)

    # Рекомендованный курс по паре
    rates = get_mock_rates()
    data = await state.get_data()
    cur1 = data["currency1"]
    cur2 = data.get("currency2", "UAH")
    pair = f"{cur1}/{cur2}"
    recommended_rate = rates.get(pair, "нет данных")

    await message.answer(f"💡 Рекомендованный курс: {recommended_rate}\nТеперь введите ваш курс:")
    await state.set_state(RequestForm.rate)

@dp.message(RequestForm.rate)
async def step_rate(message: Message, state: FSMContext):
    try:
        rate = float(message.text.replace(",", "."))
    except ValueError:
        return await message.answer("❌ Неверный формат. Введите число.")
    await state.update_data(rate=rate)

    data = await state.get_data()
    op, cur1, cur2 = data["operation"], data["currency1"], data.get("currency2", "UAH")
    amount, client = data["amount"], data["client_name"]

    text = (
        "🔍 Подтвердите заявку:\n"
        f"• Клиент: {client}\n"
        f"• Операция: {op}\n"
        f"• Валюта: {cur1}/{cur2}\n"
        f"• Сумма: {amount}\n"
        f"• Курс: {rate}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
    ])
    await message.answer(text, reply_markup=kb)
    await state.set_state(RequestForm.confirm)

@dp.callback_query(RequestForm.confirm)
async def step_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.data == "confirm":
        data = await state.get_data()
        req_id = len(requests_db) + 1
        # Для Buy/Sell вторая валюта по умолчанию UAH
        currency2 = data.get("currency2", "UAH")
        requests_db.append({
            "id": req_id,
            "operation": data["operation"],
            "currency1": data["currency1"],
            "currency2": currency2,
            "amount": data["amount"],
            "rate": data["rate"],
            "client_name": data["client_name"],
            "status": "pending",
        })
        client_map[req_id] = callback.from_user.id
        await callback.message.answer(f"✅ Заявка #{req_id} отправлена банку!")
    else:
        await callback.message.answer("❌ Заявка отменена.")
    await state.clear()
    await callback.answer()

# ====================== /rate ======================
@dp.message(F.text == "📊 Курс (/rate)")
@dp.message(Command("rate"))
async def show_rates(message: Message):
    rates = get_mock_rates()
    text = "\n".join([f"• {k}: {v}" for k, v in rates.items()])
    await message.answer(f"📊 *Текущие курсы:*\n{text}", parse_mode="Markdown")

# ====================== BANK FLOW ======================
@dp.message(Command("list"))
async def list_requests(message: Message):
    if user_roles.get(message.from_user.id) != "bank":
        return await message.answer("⛔ Только для банка. Нажмите /start и выберите роль.")
    if not requests_db:
        return await message.answer("📭 Нет заявок.")
    for r in requests_db:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{r['id']}")],
            [InlineKeyboardButton(text="❌ Reject", callback_data=f"reject_{r['id']}")],
            [InlineKeyboardButton(text="💬 Counter", callback_data=f"counter_{r['id']}")],
        ])
        await message.answer(render_request(r), reply_markup=kb)

@dp.callback_query(F.data.startswith(("approve_", "reject_", "counter_")))
async def bank_actions(callback: CallbackQuery, state: FSMContext):
    action, req_id_str = callback.data.split("_", 1)
    req_id = int(req_id_str)
    for r in requests_db:
        if r["id"] == req_id:
            if action == "approve":
                r["status"] = "approved"
                await callback.message.edit_text(f"✅ Заявка #{req_id} подтверждена!")
                await notify_client(req_id, f"✅ Ваша заявка #{req_id} одобрена банком!")
            elif action == "reject":
                r["status"] = "rejected"
                await callback.message.edit_text(f"❌ Заявка #{req_id} отклонена!")
                await notify_client(req_id, f"❌ Ваша заявка #{req_id} отклонена банком.")
            elif action == "counter":
                await state.update_data(req_id=req_id)
                await callback.message.answer("Введите новый курс:")
                await state.set_state(CounterForm.new_rate)
            break
    await callback.answer()

@dp.message(CounterForm.new_rate)
async def enter_counter_rate(message: Message, state: FSMContext):
    try:
        new_rate = float(message.text.replace(",", "."))
    except ValueError:
        return await message.answer("❌ Неверный формат. Введите число.")
    data = await state.get_data()
    req_id = data["req_id"]
    for r in requests_db:
        if r["id"] == req_id:
            r["status"] = "counter"
            counter_offers[req_id] = new_rate
            await message.answer(f"💬 Новый оффер по заявке #{req_id}: {new_rate}")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Принять", callback_data=f"accept_counter_{req_id}")],
                [InlineKeyboardButton(text="✏ Изменить", callback_data=f"change_rate_{req_id}")],
            ])
            await notify_client(req_id, f"💬 Банк предложил новый курс по заявке #{req_id}: {new_rate}", buttons=kb)
    await state.clear()

@dp.callback_query(F.data.startswith(("accept_counter_", "change_rate_")))
async def handle_client_counter_response(callback: CallbackQuery, state: FSMContext):
    action, _, id_str = callback.data.split("_", 2)
    req_id = int(id_str)
    for r in requests_db:
        if r["id"] == req_id:
            if action == "accept":
                # применяем курс банка
                if req_id in counter_offers:
                    r["rate"] = counter_offers[req_id]
                r["status"] = "pending"
                await callback.message.answer(f"✅ Вы приняли курс {r['rate']} по заявке #{req_id}. Заявка отправлена банку.")
            elif action == "change":
                await state.update_data(req_id=req_id)
                await callback.message.answer("Введите новый курс:")
                await state.set_state(UpdateRateForm.update_rate)
            break
    await callback.answer()

@dp.message(UpdateRateForm.update_rate)
async def update_client_rate(message: Message, state: FSMContext):
    try:
        new_rate = float(message.text.replace(",", "."))
    except ValueError:
        return await message.answer("❌ Неверный формат. Введите число.")
    data = await state.get_data()
    req_id = data["req_id"]
    for r in requests_db:
        if r["id"] == req_id:
            r["rate"] = new_rate
            r["status"] = "pending"
            await message.answer(f"✏ Новый курс {new_rate} отправлен банку по заявке #{req_id}.")
    await state.clear()

# ====================== FALLBACK ======================
@dp.message()
async def fallback(message: Message):
    await message.answer("Не понял 🤔\nОтправьте /start и выберите роль, или /help.")

# ====================== WEBHOOK SERVER ======================
async def on_startup(app: web.Application):
    logging.info("Setting webhook to %s", WEBHOOK_URL)
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)

async def on_shutdown(app: web.Application):
    logging.info("Deleting webhook")
    await bot.delete_webhook()

async def handle(request: web.Request):
    try:
        data = await request.json()
        logging.info("Incoming update keys: %s", list(data.keys()))
        update = types.Update.model_validate(data)  # надёжный парсинг для aiogram 3
        await dp.feed_update(bot, update)
        return web.Response(text="ok")
    except Exception as e:
        logging.exception("Error handling update: %s", e)
        return web.Response(status=500, text="error")

async def health(request: web.Request):
    return web.Response(text="ok")

async def webhook_info(request: web.Request):
    return web.Response(text="webhook endpoint (use POST)", content_type="text/plain")

app = web.Application()
app.router.add_get("/", health)                     # healthcheck
app.router.add_get(f"/{BOT_TOKEN}", webhook_info)   # опционально для удобной проверки
app.router.add_post(f"/{BOT_TOKEN}", handle)        # сам вебхук
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

