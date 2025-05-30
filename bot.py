import os
import json
import base64
import logging
import asyncio
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("Telegram_Token")
GOOGLE_CREDENTIALS_B64 = os.getenv("GOOGLE_CREDENTIALS_B64")
SPREADSHEET_ID = "1qjVJZUqm1hT5IkrASq-_iL9cc4wDl8fdjvd7KDMWL-U"

def get_gspread_client():
    if not GOOGLE_CREDENTIALS_B64:
        raise Exception("Переменная GOOGLE_CREDENTIALS_B64 не найдена")

    creds_json = base64.b64decode(GOOGLE_CREDENTIALS_B64).decode("utf-8")
    creds_dict = json.loads(creds_json)

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)

def fetch_menu_data():
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        data = sheet.get_all_records()
        # Предположим, что данные в таблице в формате:
        # [{"Параметр": "Начальная сумма", "Значение": "10000"}, ...]
        # Или просто возьмём нужные значения по названиям столбцов и строк.

        # Для примера — возвращаем строку с параметрами:
        # Здесь нужно подогнать под структуру твоей таблицы

        start_sum = data[0].get("Начальная сумма", "0")
        earned = data[0].get("Заработано", "0")
        income = data[0].get("Доход", "0")
        expense = data[0].get("Расход", "0")
        balance = data[0].get("Баланс", "0")
        card = data[0].get("Карта", "0")
        cash = data[0].get("Наличные", "0")

        menu_text = (
            f"📊 *Статус Финансов:*\n"
            f"Начальная сумма: {start_sum}\n"
            f"Заработано: {earned}\n"
            f"Доход: {income}\n"
            f"Расход: {expense}\n"
            f"Баланс: {balance}\n"
            f"Карта: {card}\n"
            f"Наличные: {cash}\n"
        )
        return menu_text
    except Exception as e:
        logger.error(f"Ошибка Google Sheets: {e}")
        return "Ошибка при загрузке данных из Google Sheets."

# Команда для старта меню
async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    menu_text = fetch_menu_data()

    # Отправляем сообщение с меню и сохраняем ID сообщения
    sent_message = await update.message.reply_text(menu_text, parse_mode="Markdown")
    message_id = sent_message.message_id

    # Сохраняем chat_id и message_id для обновления (можно в памяти, или базе)
    context.chat_data["menu_message_id"] = message_id

    # Запускаем цикл обновления меню (каждые 60 сек)
    async def update_loop():
        while True:
            await asyncio.sleep(60)
            new_text = fetch_menu_data()
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=new_text,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Ошибка обновления меню: {e}")
                # Можно прекратить обновлять если сообщение удалено
                break

    # Запускаем фоновую задачу
    context.application.create_task(update_loop())

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

def main():
    if not TELEGRAM_TOKEN:
        raise Exception("Переменная Telegram_Token не найдена")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("test", test_command))
    app.add_handler(CommandHandler("menu", start_menu))  # команда для запуска меню

    print("🤖 Бот запущен.")
    app.run_polling()

if __name__ == "__main__":
    main()
