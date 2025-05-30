import os
import json
import base64
import logging
import gspread
import asyncio
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, Bot
from telegram.constants import ParseMode
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
    creds_json = base64.b64decode(GOOGLE_CREDENTIALS_B64).decode("utf-8")
    creds_dict = json.loads(creds_json)
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

# Получить текст меню
def get_menu_text():
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        rows = sheet.get_all_values()
        data = {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2}

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
    except Exception as e:
        logger.exception("Ошибка при получении данных меню")
        return "❌ Ошибка при загрузке данных из таблицы."

# Обновление сообщения с меню каждые 5 секунд
async def auto_update_menu(bot: Bot, chat_id: int, message_id: int):
    while True:
        try:
            text = get_menu_text()
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.warning(f"Ошибка при обновлении меню: {e}")
        await asyncio.sleep(5)

# Команда /start запускает меню и автообновление
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = get_menu_text()
    message = await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    chat_id = message.chat_id
    message_id = message.message_id

    asyncio.create_task(auto_update_menu(context.bot, chat_id, message_id))

# Старт бота
def main():
    if not TELEGRAM_TOKEN:
        raise Exception("Переменная Telegram_Token не найдена")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))

    print("🤖 Бот запущен.")
    app.run_polling()

if __name__ == "__main__":
    main()
