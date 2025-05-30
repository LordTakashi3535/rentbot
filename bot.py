import os
import json
import base64
import logging
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Настройка логов
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Получение переменных окружения
TELEGRAM_TOKEN = os.getenv("Telegram_Token")
GOOGLE_CREDENTIALS_B64 = os.getenv("GOOGLE_CREDENTIALS_B64")

# ID таблицы
SPREADSHEET_ID = "1qjVJZUqm1hT5IkrASq-_iL9cc4wDl8fdjvd7KDMWL-U"

# Авторизация Google Sheets
def get_gspread_client():
    if not GOOGLE_CREDENTIALS_B64:
        raise Exception("Не найдена переменная GOOGLE_CREDENTIALS_B64")
    
    creds_json = base64.b64decode(GOOGLE_CREDENTIALS_B64).decode("utf-8")
    creds_dict = json.loads(creds_json)

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

# Обработчик команды /test
async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1  # Первый лист
        data = sheet.get_all_values()

        # Преобразуем в текст (ограничим вывод до 10 строк для читаемости)
        preview = "\n".join([", ".join(row) for row in data[:10]]) or "Нет данных"
        await update.message.reply_text(f"✅ Таблица найдена! Данные:\n\n{preview}")
    except Exception as e:
        logger.exception("Ошибка при подключении к таблице")
        await update.message.reply_text(f"❌ Ошибка при доступе к таблице:\n{str(e)}")

# Основной запуск бота
async def main():
    if not TELEGRAM_TOKEN:
        raise Exception("Telegram_Token не задан в переменных окружения.")
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("test", test_command))

    print("🤖 Бот запущен.")
    await app.run_polling()

if __name__ == "__main__":
    import asyncio

    try:
        asyncio.get_event_loop().run_until_complete(main())
    except RuntimeError:
        # Если уже есть запущенный loop (например, в Render), просто запускаем main как таск
        loop = asyncio.get_event_loop()
        loop.create_task(main())
        loop.run_forever()
