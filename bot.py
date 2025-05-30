from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
import datetime

# ...

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Доход", callback_data="add_income")],
        [InlineKeyboardButton("📤 Расход", callback_data="add_expense")],
        [InlineKeyboardButton("📊 Баланс", callback_data="balance")]
    ])
    await update.message.reply_text("Выберите действие:", reply_markup=keyboard)

# Сохраняем состояние пользователя
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "add_income":
        context.user_data["action"] = "income"
        await query.edit_message_text("Введите сумму дохода:")
    elif query.data == "add_expense":
        context.user_data["action"] = "expense"
        await query.edit_message_text("Введите сумму расхода:")
    elif query.data == "balance":
        data = get_data()
        text = (
            f"💼 Баланс: {data.get('Баланс', '—')}\n"
            f"💳 Карта: {data.get('Карта', '—')}\n"
            f"💵 Наличные: {data.get('Наличные', '—')}"
        )
        await query.edit_message_text(text=text)

# Обработка ввода суммы
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get("action")
    if action not in ["income", "expense"]:
        return  # Игнорируем, если нет действия

    try:
        amount = float(update.message.text)
        now = datetime.datetime.now().strftime("%d.%m.%Y")

        client = get_gspread_client()
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        sheet.append_row([now, "Доход" if action == "income" else "Расход", str(amount)])

        await update.message.reply_text("✅ Данные успешно добавлены.")
        context.user_data.clear()
    except ValueError:
        await update.message.reply_text("⚠️ Введите число, например: `1200.50`")

# main() – подключаем хендлеры
def main():
    if not Telegram_Token or not GOOGLE_CREDENTIALS_B64:
        raise Exception("Переменные окружения отсутствуют")

    app = ApplicationBuilder().token(Telegram_Token).build()
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    logger.info("✅ Бот запущен")
    app.run_polling()
