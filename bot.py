import os
import json
import base64
import logging
import gspread
import datetime
import re

from oauth2client.service_account import ServiceAccountCredentials
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

Telegram_Token = os.getenv("Telegram_Token")
GOOGLE_CREDENTIALS_B64 = os.getenv("GOOGLE_CREDENTIALS_B64")
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")
SPREADSHEET_ID = "1qjVJZUqm1hT5IkrASq-_iL9cc4wDl8fdjvd7KDMWL-U"


def get_gspread_client():
    creds_json = base64.b64decode(GOOGLE_CREDENTIALS_B64).decode("utf-8")
    creds_dict = json.loads(creds_json)
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    return gspread.authorize(creds)


def get_data():
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Сводка")
        rows = sheet.get_all_values()
        return {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2}
    except Exception as e:
        logger.error(f"Ошибка получения данных: {e}")
        return {}


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inline_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Баланс", callback_data="balance")],
        [InlineKeyboardButton("📥 Доход", callback_data="add_income"),
         InlineKeyboardButton("📤 Расход", callback_data="add_expense")],
        [InlineKeyboardButton("🛡 Страховки", callback_data="insurance"),
         InlineKeyboardButton("🧰 Тех.Осмотры", callback_data="tech")]
    ])

    if update.message:
        await update.message.reply_text("Выберите действие:", reply_markup=inline_keyboard)
    elif update.callback_query:
        await update.callback_query.edit_message_text("Выберите действие:", reply_markup=inline_keyboard)


def cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel" or data == "menu":
        context.user_data.clear()
        await menu_command(update, context)
        return

    if data == "add_income":
        context.user_data.clear()
        context.user_data["action"] = "income_category"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Franky", callback_data="cat_franky")],
            [InlineKeyboardButton("Fraiz", callback_data="cat_fraiz")],
            [InlineKeyboardButton("Другое", callback_data="cat_other")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
        ])
        await query.edit_message_text("Выберите категорию дохода:", reply_markup=keyboard)

    elif data in ["cat_franky", "cat_fraiz", "cat_other"]:
        category_map = {
            "cat_franky": "Franky",
            "cat_fraiz": "Fraiz",
            "cat_other": "Другое"
        }
        context.user_data["action"] = "income"
        context.user_data["category"] = category_map[data]
        context.user_data["step"] = "amount"
        await query.edit_message_text("Введите сумму дохода:", reply_markup=cancel_keyboard())

    elif data == "add_expense":
        context.user_data.clear()
        context.user_data["action"] = "expense"
        context.user_data["step"] = "amount"
        await query.edit_message_text("Введите сумму расхода:", reply_markup=cancel_keyboard())

    elif data == "source_card":
        context.user_data["source"] = "Карта"
        context.user_data["step"] = "description"
        await query.edit_message_text("Введите описание:")
    elif data == "source_cash":
        context.user_data["source"] = "Наличные"
        context.user_data["step"] = "description"
        await query.edit_message_text("Введите описание:")

    elif data == "insurance":
        try:
            sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("Страховки")
            rows = sheet.get_all_values()[1:]
            text = "🚗 Страховки:\n" + "\n".join(
                f"{i+1}. {row[0]} до {row[1] if len(row) > 1 else '—'}" for i, row in enumerate(rows)
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Изменить", callback_data="edit_insurance")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
            ])
            await query.edit_message_text(text or "🚗 Нет данных.", reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Ошибка страховок: {e}")
            await query.message.reply_text("⚠️ Не удалось получить данные по страховкам.")

    elif data == "tech":
        try:
            sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("ТехОсмотры")
            rows = sheet.get_all_values()[1:]
            text = "🧰 Тех.Осмотры:\n" + "\n".join(
                f"{i+1}. {row[0]} до {row[1] if len(row) > 1 else '—'}" for i, row in enumerate(rows)
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Изменить", callback_data="edit_tech")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
            ])
            await query.edit_message_text(text or "🧰 Нет данных.", reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Ошибка тех.осмотров: {e}")
            await query.message.reply_text("⚠️ Не удалось получить данные по тех.осмотрам.")

    elif data == "edit_insurance":
        context.user_data["edit_type"] = "insurance"
        await query.edit_message_text("Введите: Машина - Дата (Пример: Toyota - 01.09.2025)", reply_markup=cancel_keyboard())

    elif data == "edit_tech":
        context.user_data["edit_type"] = "tech"
        await query.edit_message_text("Введите: Машина - Дата (Пример: BMW - 15.10.2025)", reply_markup=cancel_keyboard())

    elif data == "balance":
        try:
            data = get_data()
            text = (
                f"💼 Баланс: {data.get('Баланс', '—')}\n"
                f"💳 Карта: {data.get('Карта', '—')}\n"
                f"💵 Наличные: {data.get('Наличные', '—')}"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Доход", callback_data="add_income"),
                 InlineKeyboardButton("📤 Расход", callback_data="add_expense")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
            ])
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Ошибка баланса: {e}")
            await query.message.reply_text("⚠️ Не удалось получить баланс.")


async def handle_amount_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text.lower() == "отмена":
        context.user_data.clear()
        await update.message.reply_text("❌ Отменено.")
        return await menu_command(update, context)

    if "edit_type" in context.user_data:
        edit_type = context.user_data.pop("edit_type")
        try:
            name, new_date = map(str.strip, text.split("-", 1))
            if not re.match(r"^\d{2}\.\d{2}\.\d{4}$", new_date):
                return await update.message.reply_text("❌ Формат даты: дд.мм.гггг")

            sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet(
                "Страховки" if edit_type == "insurance" else "ТехОсмотры"
            )
            cells = sheet.findall(name)
            if not cells:
                return await update.message.reply_text("❌ Машина не найдена.")
            for cell in cells:
                sheet.update_cell(cell.row, 2, new_date)
            await update.message.reply_text("✅ Обновлено.")
        except Exception as e:
            logger.error(f"Ошибка обновления {edit_type}: {e}")
            await update.message.reply_text("❌ Ошибка при обновлении.")
        return await menu_command(update, context)

    if context.user_data.get("action") == "income" and context.user_data.get("step") == "amount":
        try:
            amount = float(text.replace(",", "."))
            context.user_data["amount"] = amount
            context.user_data["step"] = "source"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Карта", callback_data="source_card"),
                 InlineKeyboardButton("Наличные", callback_data="source_cash")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
            ])
            await update.message.reply_text("Выберите источник:", reply_markup=keyboard)
        except ValueError:
            await update.message.reply_text("❌ Введите корректную сумму (число).")
        return

    if context.user_data.get("action") == "income" and context.user_data.get("step") == "description":
        context.user_data["description"] = text
        await save_transaction(update, context, "Доход")
        context.user_data.clear()
        return await menu_command(update, context)

    if context.user_data.get("action") == "expense" and context.user_data.get("step") == "amount":
        try:
            amount = float(text.replace(",", "."))
            context.user_data["amount"] = amount
            context.user_data["step"] = "description"
            await update.message.reply_text("Введите описание:")
        except ValueError:
            await update.message.reply_text("❌ Введите корректную сумму (число).")
        return

    if context.user_data.get("action") == "expense" and context.user_data.get("step") == "description":
        context.user_data["description"] = text
        await save_transaction(update, context, "Расход")
        context.user_data.clear()
        return await menu_command(update, context)


async def save_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE, trans_type: str):
    amount = context.user_data.get("amount")
    description = context.user_data.get("description", "")
    category = context.user_data.get("category", trans_type)
    source = context.user_data.get("source", "")

    date_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

    try:
        sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("ДоходыРасходы")
        sheet.append_row([date_str, trans_type, category, amount, source, description])

        msg = (
            f"💰 {trans_type} записан:\n"
            f"Категория: {category}\n"
            f"Сумма: {amount}\n"
            f"Источник: {source}\n"
            f"Описание: {description}\n"
            f"Дата: {date_str}"
        )
        await update.message.reply_text(msg)
        if GROUP_CHAT_ID:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=msg)

    except Exception as e:
        logger.error(f"Ошибка записи транзакции: {e}")
        await update.message.reply_text("❌ Ошибка записи в Google Sheets.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Используйте /menu для работы с ботом.")


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Неизвестная команда. Используйте /menu.")


def main():
    application = ApplicationBuilder().token(Telegram_Token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_amount_description))
    application.add_handler(MessageHandler(filters.COMMAND, unknown))

    application.run_polling()


if __name__ == "__main__":
    main()
