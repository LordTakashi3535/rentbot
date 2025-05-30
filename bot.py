import os
import json
import base64
import logging
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Переменные окружения
TELEGRAM_TOKEN = os.getenv("Telegram_Token")
GOOGLE_CREDENTIALS_B64 = os.getenv("GOOGLE_CREDENTIALS_B64")

SPREADSHEET_ID = "1qjVJZUqm1hT5IkrASq-_iL9cc4wDl8fdjvd7KDMWL-U"

def get_gspread_client():
    if not GOOGLE_CREDENTIALS_B64:
        raise Exception("Переменная GOOGLE_CREDENTIALS_B64 не найдена")

    creds_json = base64.b64decode(GOOGLE_CREDENTIALS_B64).decode("utf-8")
    creds_dict = json.loads(creds_json)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client

# Команда /test для проверки таблицы
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

# Команда /menu - выводит статичное меню с данными из таблицы
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1

        # Предположим, что заголовки в первой строке, а данные — во второй
        headers = sheet.row_values(1)
        values = sheet.row_values(2)

        # Создаем словарь из заголовков и значений
        data = dict(zip(headers, values))

        # Формируем сообщение меню
        menu_text = (
            f"📊 Статистика:\n"
            f"Начальная сумма: {data.get('начальная сумма', '—')}\n"
            f"Заработано: {data.get('заработано', '—')}\n"
            f"Доход: {data.get('доход', '—')}\n"
            f"Расход: {data.get('расход', '—')}\n"
            f"Баланс карта: {data.get('баланс карта', '—')}\n"
            f"Наличные: {data.get('наличные', '—')}"
        )

        await update.message.reply_text(menu_text)
    except Exception as e:
        logger.exception("Ошибка при получении меню из Google Sheets")
        await update.message.reply_text(f"❌ Ошибка при получении меню:\n{e}")

def main():
    if not TELEGRAM_TOKEN:
        raise Exception("Переменная Telegram_Token не найдена")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("test", test_command))
    app.add_handler(CommandHandler("menu", menu_command))

    print("🤖 Бот запущен.")
    app.run_polling()

if __name__ == "__main__":
    main()
