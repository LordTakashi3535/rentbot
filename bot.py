import os
import json
import base64
import logging
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters
)

# 🔧 Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 🔐 Получение переменных окружения
Telegram_Token = os.getenv("Telegram_Token")
GOOGLE_CREDENTIALS_B64 = os.getenv("GOOGLE_CREDENTIALS_B64")
SPREADSHEET_ID = "1qjVJZUqm1hT5IkrASq-_iL9cc4wDl8fdjvd7KDMWL-U"

# 🔐 Авторизация Google Sheets
def get_gspread_client():
    creds_json = base64.b64decode(GOOGLE_CREDENTIALS_B64).decode("utf-8")
    creds_dict = json.loads(creds_json)
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

# 📊 Получение данных из таблицы
def get_data():
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        rows = sheet.get_all_values()
        return {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2}
    except Exception as e:
        logger.error(f"Ошибка получения данных: {e}")
        return {}

# 🎛 Кнопки
def build_keyboard():
    return ReplyKeyboardMarkup(
        [["Баланс", "Карта", "Наличные"]],
        resize_keyboard=True
    )

# 📍 Команда /menu
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выберите нужный пункт:", reply_markup=build_keyboard())

# 📬 Обработка кнопок
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    data = get_data()

    if text == "Баланс":
        await update.message.reply_text(f"💼 Баланс: {data.get('Баланс', '—')}")
    elif text == "Карта":
        await update.message.reply_text(f"💳 Карта: {data.get('Карта', '—')}")
    elif text == "Наличные":
        await update.message.reply_text(f"💵 Наличные: {data.get('Наличные', '—')}")
    else:
        await update.message.reply_text("Пожалуйста, выберите кнопку из меню.")

# 🧠 Основная функция
def main():
    if not Telegram_Token or not GOOGLE_CREDENTIALS_B64:
        raise Exception("Переменные окружения отсутствуют")

    app = ApplicationBuilder().token(Telegram_Token).build()
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    logger.info("✅ Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
