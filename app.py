@router.callback_query(F.data.startswith("accept:"))
async def cb_accept(call: CallbackQuery):
    try:
        if user_roles.get(call.from_user.id) != "bank":
            return await call.answer("Нет доступа", show_alert=True)
        oid = int(call.data.split(":")[1])
        o = orders.get(oid)
        if not o:
            return await call.answer("Заявка не найдена", show_alert=True)
        o.status = "accepted"
        await call.message.edit_text(o.summary())
        await call.answer("Заявка принята ✅")
        with suppress(Exception):
            await bot.send_message(o.client_id, f"✅ Ваша заявка #{o.id} принята банком.")
    except Exception as e:
        logger.error(f"cb_accept failed: {e}")
        with suppress(Exception):
            await call.answer("Ошибка обработки", show_alert=True)

@router.callback_query(F.data.startswith("reject:"))
async def cb_reject(call: CallbackQuery):
    try:
        if user_roles.get(call.from_user.id) != "bank":
            return await call.answer("Нет доступа", show_alert=True)
        oid = int(call.data.split(":")[1])
        o = orders.get(oid)
        if not o:
            return await call.answer("Заявка не найдена", show_alert=True)
        o.status = "rejected"
        await call.message.edit_text(o.summary())
        await call.answer("Заявка отклонена ❌")
        with suppress(Exception):
            await bot.send_message(o.client_id, f"❌ Ваша заявка #{o.id} отклонена банком.")
    except Exception as e:
        logger.error(f"cb_reject failed: {e}")
        with suppress(Exception):
            await call.answer("Ошибка обработки", show_alert=True)

@router.callback_query(F.data.startswith("order:"))
async def cb_order(call: CallbackQuery):
    try:
        if user_roles.get(call.from_user.id) != "bank":
            return await call.answer("Нет доступа", show_alert=True)
        oid = int(call.data.split(":")[1])
        o = orders.get(oid)
        if not o:
            return await call.answer("Заявка не найдена", show_alert=True)
        o.status = "order"
        await call.message.edit_text(o.summary())
        await call.answer("Заявка сохранена как ордер 📌")
        with suppress(Exception):
            await bot.send_message(o.client_id, f"📌 Ваша заявка #{o.id} сохранена как ордер.")
    except Exception as e:
        logger.error(f"cb_order failed: {e}")
        with suppress(Exception):
            await call.answer("Ошибка обработки", show_alert=True)

# ---------------------- FALLBACK ----------------------
@router.message()
async def fallback_handler(message: Message, state: FSMContext):
    try:
        cur_state = await state.get_state()
        if cur_state:
            await message.answer(
                f"Сейчас я жду данные для состояния <b>{cur_state}</b>.\n"
                f"Если хотите выйти, используйте /cancel."
            )
        else:
            await message.answer("Не понимаю сообщение. Используйте меню или команду /start.", reply_markup=main_kb())
    except Exception as e:
        logger.error(f"fallback failed: {e}")

# ---------------------- FASTAPI ----------------------
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    logger.info("Starting up application...")
    if WEBHOOK_BASE:
        url = f"{WEBHOOK_BASE}{WEBHOOK_PATH}"
        try:
            await bot.set_webhook(url, allowed_updates=["message", "callback_query"], secret_token=WEBHOOK_SECRET)
            logger.info(f"Webhook set to {url}")
        except Exception as e:
            logger.error(f"Failed to set webhook: {e}")
    else:
        logger.warning("WEBHOOK_BASE is empty — webhook is not set (local run).")
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
        await dp.feed_webhook_update(bot, update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"webhook failed: {e}")
        return {"ok": False}

@app.get("/")
async def health():
    return {"status": "ok", "service": "FXBankBot", "webhook_path": WEBHOOK_PATH}

# ---------------------- LOCAL RUN ----------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
