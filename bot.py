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
    MessageHandler,
    filters,
)
import datetime

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
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
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
        [InlineKeyboardButton("📊 Баланс", callback_data="balance")],
        [InlineKeyboardButton("📥 Доход", callback_data="add_income")],
        [InlineKeyboardButton("📤 Расход", callback_data="add_expense")]
    ])
    await update.message.reply_text("Выберите действие:", reply_markup=keyboard)

# Обработка нажатий кнопок
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "add_income":
        context.user_data.clear()
        context.user_data["action"] = "income_category"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Franky", callback_data="cat_franky")],
            [InlineKeyboardButton("Fraiz", callback_data="cat_fraiz")],
            [InlineKeyboardButton("Другое", callback_data="cat_other")]
        ])
        await query.edit_message_text("Выберите категорию дохода:", reply_markup=keyboard)

    elif query.data in ["cat_franky", "cat_fraiz", "cat_other"]:
        category_map = {
            "cat_franky": "Franky",
            "cat_fraiz": "Fraiz",
            "cat_other": "Другое"
        }
        context.user_data["action"] = "income"
        context.user_data["category"] = category_map[query.data]
        context.user_data["step"] = "amount"
        await query.edit_message_text("Введите сумму дохода:")

    elif query.data == "add_expense":
        context.user_data.clear()
        context.user_data["action"] = "expense"
        context.user_data["step"] = "amount"
        await query.edit_message_text("Введите сумму расхода:")

    elif query.data == "balance":
        try:
            data = get_data()
            text = (
                f"💼 Баланс: {data.get('Баланс', '—')}\n"
                f"💳 Карта: {data.get('Карта', '—')}\n"
                f"💵 Наличные: {data.get('Наличные', '—')}"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Баланс", callback_data="balance")],
                [InlineKeyboardButton("📥 Доход", callback_data="add_income")],
                [InlineKeyboardButton("📤 Расход", callback_data="add_expense")]
            ])
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Ошибка при выводе баланса: {e}")
            await query.message.reply_text("⚠️ Не удалось получить баланс.")

# Обработка ввода суммы и описания
async def handle_amount_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("step")
    action = context.user_data.get("action")

    if not action or not step:
        return  # Пропустить, если нет процесса

    text = update.message.text.strip()

    if step == "amount":
        try:
            amount = float(text.replace(",", "."))
            context.user_data["amount"] = amount
            context.user_data["step"] = "description"
            await update.message.reply_text("Введите описание:")
        except ValueError:
            await update.message.reply_text("⚠️ Введите корректную сумму, например: 1500.00")

    elif step == "description":
        description = text
        now = datetime.datetime.now().strftime("%d.%m.%Y")
        amount = context.user_data.get("amount")
        category = context.user_data.get("category", "-")

        try:
            client = get_gspread_client()
            if action == "income":
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
                sheet.append_row([now, category, amount, description])
                reply_text = (
                    f"✅ Добавлено в *Доход*:\n\n"
                    f"📅 Дата: `{now}`\n"
                    f"🏷 Категория: `{category}`\n"
                    f"💰 Сумма: `{amount}`\n"
                    f"📝 Описание: `{description}`"
                )
            else:  # expense
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")
                sheet.append_row([now, amount, description])
                reply_text = (
                    f"✅ Добавлено в *Расход*:\n\n"
                    f"📅 Дата: `{now}`\n"
                    f"💸 Сумма: `-{amount}`\n"
                    f"📝 Описание: `{description}`"
                )

            # Получаем данные баланса из листа "Сводка"
            summary_sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Сводка")
            summary_data = summary_sheet.get_all_values()
            summary_dict = {row[0].strip(): row[1].strip() for row in summary_data if len(row) >= 2}

            balance_text = (
                f"\n\n📊 Текущий баланс:\n"
                f"💼 Баланс: {summary_dict.get('Баланс', '—')}\n"
                f"💳 Карта: {summary_dict.get('Карта', '—')}\n"
                f"💵 Наличные: {summary_dict.get('Наличные', '—')}"
            )

            reply_text += balance_text

            # Кнопки "Доход" и "Расход"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Доход", callback_data="add_income")],
                [InlineKeyboardButton("📤 Расход", callback_data="add_expense")]
            ])

            await update.message.reply_text(reply_text, parse_mode="Markdown", reply_markup=keyboard)
            context.user_data.clear()

        except Exception as e:
            logger.error(f"Ошибка записи в таблицу: {e}")
            await update.message.reply_text("❌ Ошибка при записи данных. Попробуйте позже.")

# Основная функция
def main():
    if not Telegram_Token or not GOOGLE_CREDENTIALS_B64:
        raise Exception("Переменные окружения отсутствуют")

    app = ApplicationBuilder().token(Telegram_Token).build()
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_amount_description))

    logger.info("✅ Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
