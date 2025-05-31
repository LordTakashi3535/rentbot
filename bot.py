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
    BotCommand,
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


# Показываем меню (inline кнопки)
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

    elif data == "insurance":
        try:
            sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("Страховки")
            rows = sheet.get_all_values()[1:]
            if not rows:
                await query.edit_message_text("🚗 Страховки не найдены.", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
                ]))
                return
            text = "🚗 Страховки:\n"
            for i, row in enumerate(rows):
                text += f"{i+1}. {row[0]} до {row[1] if len(row) > 1 else '—'}\n"

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Изменить", callback_data="edit_insurance")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
            ])
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Ошибка страховок: {e}")
            await query.message.reply_text("⚠️ Не удалось получить данные по страховкам.")

    elif data == "tech":
        try:
            sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("ТехОсмотры")
            rows = sheet.get_all_values()[1:]
            if not rows:
                await query.edit_message_text("🧰 Тех.Осмотры не найдены.", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
                ]))
                return
            text = "🧰 Тех.Осмотры:\n"
            for i, row in enumerate(rows):
                text += f"{i+1}. {row[0]} до {row[1] if len(row) > 1 else '—'}\n"

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Изменить", callback_data="edit_tech")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
            ])
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Ошибка тех.осмотров: {e}")
            await query.message.reply_text("⚠️ Не удалось получить данные по тех.осмотрам.")

    elif data == "edit_insurance":
        context.user_data["edit_type"] = "insurance"
        await query.edit_message_text("Введите название машины и дату через тире (Пример: Toyota - 01.09.2025)", reply_markup=cancel_keyboard())

    elif data == "edit_tech":
        context.user_data["edit_type"] = "tech"
        await query.edit_message_text("Введите название машины и дату через тире (Пример: BMW - 15.10.2025)", reply_markup=cancel_keyboard())


async def handle_amount_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text.lower() == "отмена":
        context.user_data.clear()
        await update.message.reply_text("❌ Действие отменено.", reply_markup=cancel_keyboard())
        return

    edit_type = context.user_data.get("edit_type")
    if edit_type:
        try:
            name, new_date = text.split(" - ")
            new_date = new_date.strip()

            if edit_type == "insurance":
                sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("Страховки")
            else:
                sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("ТехОсмотры")

            rows = sheet.get_all_values()
            name_found = False

            for i, row in enumerate(rows):
                if row[0].lower() == name.lower():
                    name_found = True
                    sheet.update_cell(i+1, 2, new_date)
                    await update.message.reply_text(f"✅ Дата для '{name}' обновлена на {new_date}.")
                    break

            if not name_found:
                await update.message.reply_text(f"⚠️ '{name}' не найдено.")
        except ValueError:
            await update.message.reply_text("❌ Неверный формат. Попробуйте еще раз (Пример: Toyota - 01.09.2025)")

        context.user_data.clear()
        return

    action = context.user_data.get("action")

    if action == "income" or action == "expense":
        try:
            amount = float(text)
            category = context.user_data.get("category")
            sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("Сводка")

            if action == "income":
                data = get_data()
                current_balance = float(data.get("Баланс", 0))
                new_balance = current_balance + amount
                sheet.update_cell(2, 2, new_balance)
                await update.message.reply_text(f"✅ Добавлено {amount} к доходам в категорию {category}. Новый баланс: {new_balance}.")

            elif action == "expense":
                data = get_data()
                current_balance = float(data.get("Баланс", 0))
                new_balance = current_balance - amount
                sheet.update_cell(2, 2, new_balance)
                await update.message.reply_text(f"✅ Расход {amount} списан. Новый баланс: {new_balance}.")

        except ValueError:
            await update.message.reply_text("❌ Пожалуйста, введите число.")

        context.user_data.clear()
        return


async def set_bot_commands(app):
    commands = [
        BotCommand("start", "Запустить бота"),
        BotCommand("menu", "Показать меню"),
        BotCommand("balance", "Показать баланс"),
    ]
    await app.set_my_commands(commands)


async def main():
    if not Telegram_Token or not GOOGLE_CREDENTIALS_B64:
        raise Exception("❌ Не заданы переменные окружения")

    app = ApplicationBuilder().token(Telegram_Token).build()

    app.add_handler(CommandHandler("start", menu_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_amount_description))

    await set_bot_commands(app)

    logger.info("✅ Бот запущен")
    await app.run_polling()  # Здесь запускаем polling, это инициирует цикл событий


if __name__ == "__main__":
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
