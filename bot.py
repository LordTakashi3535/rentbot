import os
import json
import base64
import logging
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Переменные окружения
TELEGRAM_TOKEN = os.getenv("Telegram_Token")
GOOGLE_CREDENTIALS_B64 = os.getenv("GOOGLE_CREDENTIALS_B64")

SPREADSHEET_ID = "1qjVJZUqm1hT5IkrASq-_iL9cc4wDl8fdjvd7KDMWL-U"

# Авторизация Google Sheets
def get_gspread_client():
    if not GOOGLE_CREDENTIALS_B64:
        raise Exception("Переменная GOOGLE_CREDENTIALS_B64 не найдена")

    creds_json = base64.b64decode(GOOGLE_CREDENTIALS_B64).decode("utf-8")
    creds_dict = json.loads(creds_json)

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

# Команда /test
async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        data = sheet.get_all_values()
        preview = "\n".join([", ".join(row) for row in data[:10]]) or "Нет данных"
        await update.message.reply_text(f"✅ Таблица подключена. Данные:\n{preview}")
    except Exception as e:
        logger.exception("Ошибка Google Sheets")
        await update.message.reply_text(f"❌ Ошибка:\n{e}")

# 👇 Здесь мы просто запускаем Application без asyncio.run()
def main():
    if not TELEGRAM_TOKEN:
        raise Exception("Переменная Telegram_Token не найдена")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("test", test_command))

    print("🤖 Бот запущен.")
    app.run_polling()

if __name__ == "__main__":
    main()

