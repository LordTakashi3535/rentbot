import os
import json
import base64
import logging
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("Telegram_Token")
GOOGLE_CREDENTIALS_B64 = os.getenv("GOOGLE_CREDENTIALS_B64")
SPREADSHEET_ID = "1qjVJZUqm1hT5IkrASq-_iL9cc4wDl8fdjvd7KDMWL-U"

def get_gspread_client():
    creds_json = base64.b64decode(GOOGLE_CREDENTIALS_B64).decode("utf-8")
    creds_dict = json.loads(creds_json)
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

def get_data():
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        rows = sheet.get_all_values()
        data = {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2}
        return data
    except Exception as e:
        logger.error(f"Ошибка при получении данных из Google Sheets: {e}")
        return {}

def build_inline_keyboard():
    keyboard = [[InlineKeyboardButton("💼 Баланс", callback_data="get_balance")]]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Нажми кнопку ниже, чтобы получить баланс:",
        reply_markup=build_inline_keyboard()
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "get_balance":
        data = get_data()
        balance = data.get("Баланс", "Данные отсутствуют")
        await query.edit_message_text(text=f"💼 Баланс: {balance}")

def main():
    if not TELEGRAM_TOKEN:
        raise Exception("Переменная Telegram_Token не найдена")
    if not GOOGLE_CREDENTIALS_B64:
        raise Exception("Переменная GOOGLE_CREDENTIALS_B64 не найдена")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("🤖 Бот запущен.")
    app.run_polling()

if __name__ == "__main__":
    main()
