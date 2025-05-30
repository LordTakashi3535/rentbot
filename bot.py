import os
import json
import base64
import logging
import gspread
import asyncio
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

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
        logger.error(f"Ошибка при получении данных: {e}")
        return {}

def build_main_keyboard():
    keyboard = [["Финансовый отчёт"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def build_report_text(data):
    return (
        f"📊 *Финансовый отчёт:*\n"
        f"🔹 Начальная сумма: {data.get('Начальная сумма', '—')}\n"
        f"💰 Заработано: {data.get('Заработано', '—')}\n"
        f"📈 Доход: {data.get('Доход', '—')}\n"
        f"📉 Расход: {data.get('Расход', '—')}\n"
        f"💼 Баланс: {data.get('Баланс', '—')}\n"
        f"💳 Карта: {data.get('Карта', '—')}\n"
        f"💵 Наличные: {data.get('Наличные', '—')}"
    )

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = build_main_keyboard()
    await update.message.reply_text(
        "Меню:",
        reply_markup=keyboard
    )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "Финансовый отчёт":
        data = get_data()
        report = build_report_text(data)
        await update.message.reply_text(report, parse_mode="Markdown")
    else:
        await update.message.reply_text("Пожалуйста, выберите пункт меню.")

def main():
    if not TELEGRAM_TOKEN:
        raise Exception("Переменная Telegram_Token не найдена")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), message_handler))

    print("🤖 Бот запущен.")
    app.run_polling()

if __name__ == "__main__":
    main()
