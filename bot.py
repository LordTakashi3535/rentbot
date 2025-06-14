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
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")  # <- Новый чат ID
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


# остальная часть handle_button не изменилась
# пропущу для краткости — оставить как у тебя

async def on_menu_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await menu_command(update, context)


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
                await update.message.reply_text("❌ Некорректный формат даты. Используйте дд.мм.гггг")
                return
            sheet_name = "Страховки" if edit_type == "insurance" else "ТехОсмотры"
            sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet(sheet_name)
            rows = sheet.get_all_values()

            for i, row in enumerate(rows):
                if row and row[0].lower() == name.lower():
                    sheet.update_cell(i + 1, 2, new_date)
                    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu")]])
                    await update.message.reply_text(f"✅ Дата обновлена:\n{name} — {new_date}", reply_markup=keyboard)
                    return
            await update.message.reply_text("🚫 Машина не найдена.")
        except Exception as e:
            logger.error(f"Ошибка при обновлении: {e}")
            await update.message.reply_text("❌ Ошибка обновления.")
        return

    action = context.user_data.get("action")
    step = context.user_data.get("step")

    if not action or not step:
        return

    if step == "amount":
        try:
            amount = float(text.replace(",", "."))
            if amount <= 0:
                raise ValueError("Сумма должна быть положительной")
            context.user_data["amount"] = amount
            context.user_data["step"] = "source"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Карта", callback_data="source_card")],
                [InlineKeyboardButton("💵 Наличные", callback_data="source_cash")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
            ])
            await update.message.reply_text("Выберите источник:", reply_markup=keyboard)
        except ValueError:
            await update.message.reply_text("⚠️ Введите положительное число (пример: 1200.50)")

    elif step == "description":
        description = text
        now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
        amount = context.user_data.get("amount")
        category = context.user_data.get("category", "-")
        source = context.user_data.get("source", "-")

        try:
            client = get_gspread_client()

            if action == "income":
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
                row = [now, category, "", "", description]
                if source == "Карта":
                    row[2] = amount
                else:
                    row[3] = amount
                sheet.append_row(row)
                text_out = f"✅ Добавлено в *Доход*:\n📅 {now}\n🏷 {category}\n💰 {amount} ({source})\n📝 {description}"
            else:
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")
                row = [now, "", "", description]
                if source == "Карта":
                    row[1] = amount
                else:
                    row[2] = amount
                sheet.append_row(row)
                text_out = f"✅ Добавлено в *Расход*:\n📅 {now}\n💸 -{amount} ({source})\n📝 {description}"

            summary = get_data()
            text_out += f"\n\n📊 Баланс:\n💼 {summary.get('Баланс', '—')}\n💳 {summary.get('Карта', '—')}\n💵 {summary.get('Наличные', '—')}"

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Доход", callback_data="add_income"),
                 InlineKeyboardButton("📤 Расход", callback_data="add_expense")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
            ])

            context.user_data.clear()
            await update.message.reply_text(text_out, reply_markup=keyboard, parse_mode="Markdown")

            # 👇 Отправка в группу
            if GROUP_CHAT_ID:
                try:
                    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text_out, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Ошибка отправки в группу: {e}")

        except Exception as e:
            logger.error(f"Ошибка записи: {e}")
            await update.message.reply_text("⚠️ Ошибка записи в таблицу.")


def main():
    if not Telegram_Token or not GOOGLE_CREDENTIALS_B64:
        raise Exception("❌ Не заданы переменные окружения")

    app = ApplicationBuilder().token(Telegram_Token).build()

    app.add_handler(CommandHandler("start", menu_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^Меню$"), on_menu_button_pressed))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_amount_description))

    logger.info("✅ Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
