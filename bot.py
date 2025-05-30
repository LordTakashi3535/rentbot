import os
import json
import base64
import logging
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
)

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Переменные окружения
Telegram_Token = os.getenv("Telegram_Token")
GOOGLE_CREDENTIALS_B64 = os.getenv("GOOGLE_CREDENTIALS_B64")
SPREADSHEET_ID = "1qjVJZUqm1hT5IkrASq-_iL9cc4wDl8fdjvd7KDMWL-U"

# Авторизация Google Sheets
def get_gspread_client():
    creds_json = base64.b64decode(GOOGLE_CREDENTIALS_B64).decode("utf-8")
    creds_dict = json.loads(creds_json)
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

# Получение данных из таблицы
def get_data():
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        rows = sheet.get_all_values()
        return {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2}
    except Exception as e:
        logger.error(f"Ошибка получения данных: {e}")
        return {}

# Команда /menu
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Баланс", callback_data="balance")],
        [InlineKeyboardButton("Карта", callback_data="card")],
        [InlineKeyboardButton("Наличные", callback_data="cash")]
    ])
    await update.message.reply_text("Выберите пункт отчёта:", reply_markup=keyboard)

# Обработка нажатий кнопок
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = get_data()

    if query.data == "balance":
        text = f"💼 Баланс: {data.get('Баланс', '—')}"
    elif query.data == "card":
        text = f"💳 Карта: {data.get('Карта', '—')}"
    elif query.data == "cash":
        text = f"💵 Наличные: {data.get('Наличные', '—')}"
    else:
        text = "Неизвестная команда."

    await query.edit_message_text(text=text)

# Основная функция
def main():
    if not Telegram_Token or not GOOGLE_CREDENTIALS_B64:
        raise Exception("Переменные окружения отсутствуют")

    app = ApplicationBuilder().token(Telegram_Token).build()
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(handle_button))

    logger.info("✅ Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
