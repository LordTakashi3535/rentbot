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

# Получение данных из таблицы (например, для баланса)
def get_data():
    try:
        client = get_gspread_client()
        sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Сводка")
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
        [InlineKeyboardButton("📤 Расход", callback_data="add_expense")],
        [InlineKeyboardButton("🛡 Страховки", callback_data="insurance")],
        [InlineKeyboardButton("🧰 Тех.Осмотры", callback_data="tech")]
    ])
    await update.message.reply_text("Выберите действие:", reply_markup=keyboard)

# Кнопка отмены
def cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])

# Обработка нажатий кнопок
async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "cancel":
        context.user_data.clear()
        await query.edit_message_text("Действие отменено. Возврат в меню.")
        return await menu_command(update, context)

    if data == "menu":
        context.user_data.clear()
        return await menu_command(update, context)

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
        
    elif data == "insurance":
        try:
            sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("Страховки")
            rows = sheet.get_all_values()[1:]  # пропустить заголовок
            if not rows:
                await query.edit_message_text("🚗 Страховки не найдены.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu")]]))
                return
            text = "🚗 *Страховки:*\n"
            for row in rows:
                if len(row) >= 2:
                    text += f"• `{row[0]}` до `{row[1]}`\n"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Изменить", callback_data="edit_insurance")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
            ])
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка получения страховок: {e}")
            await query.message.reply_text("⚠️ Не удалось получить данные по страховкам.")

    elif data == "tech":
        try:
            sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("Техосмотры")
            rows = sheet.get_all_values()[1:]
            if not rows:
                await query.edit_message_text("🛠 Техосмотры не найдены.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu")]]))
                return
            text = "🛠 *Тех. Осмотры:*\n"
            for row in rows:
                if len(row) >= 2:
                    text += f"• `{row[0]}` до `{row[1]}`\n"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Изменить", callback_data="edit_tech")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
            ])
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка получения техосмотров: {e}")
            await query.message.reply_text("⚠️ Не удалось получить данные по техосмотрам.")

    elif data == "edit_insurance":
        context.user_data["edit_type"] = "insurance"
        await query.edit_message_text("Введите название машины и новую дату через тире (например: Toyota - 01.09.2025)", reply_markup=cancel_keyboard())

    elif data == "edit_tech":
        context.user_data["edit_type"] = "tech"
        await query.edit_message_text("Введите название машины и новую дату через тире (например: BMW - 15.10.2025)", reply_markup=cancel_keyboard())
            
    elif data == "balance":
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
                [InlineKeyboardButton("📤 Расход", callback_data="add_expense")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
            ])
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Ошибка при выводе баланса: {e}")
            await query.message.reply_text("⚠️ Не удалось получить баланс.")

# Обработка ввода суммы, описания и редактирования дат
async def handle_amount_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "edit_type" in context.user_data:
        edit_type = context.user_data.pop("edit_type")
        text = update.message.text.strip()
        try:
            name, new_date = map(str.strip, text.split("-", 1))
            sheet_name = "Страховки" if edit_type == "insurance" else "Техосмотры"
            sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet(sheet_name)
            rows = sheet.get_all_values()

            for i, row in enumerate(rows):
                if row and row[0].lower() == name.lower():
                    sheet.update_cell(i + 1, 2, new_date)
                    await update.message.reply_text(f"✅ Дата обновлена:\n{name} — {new_date}")
                    return
            await update.message.reply_text("🚫 Машина не найдена.")
        except Exception as e:
            logger.error(f"Ошибка при обновлении даты: {e}")
            await update.message.reply_text("❌ Ошибка обновления. Формат: Название - Дата")
        return

    # Если пользователь нажал отмену (через кнопку)
    if update.message.text.strip().lower() == "отмена":
        context.user_data.clear()
        await update.message.reply_text("Действие отменено.")
        return await menu_command(update, context)

    step = context.user_data.get("step")
    action = context.user_data.get("action")

    if not action or not step:
        # Нет активного действия, игнорируем
        return

    user_message = update.message
    text = user_message.text.strip()

    if step == "amount":
        try:
            amount = float(text.replace(",", "."))
            if amount <= 0:
                raise ValueError("Сумма должна быть положительной")
            context.user_data["amount"] = amount
            context.user_data["step"] = "description"

            await user_message.delete()  # Удаляем сообщение с суммой
            await user_message.chat.send_message("Введите описание:", reply_markup=cancel_keyboard())
        except ValueError:
            await user_message.reply_text("⚠️ Введите корректную положительную сумму, например: 1500.00")

    elif step == "description":
        description = text
        now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
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
            else:
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")
                sheet.append_row([now, amount, description])
                reply_text = (
                    f"✅ Добавлено в *Расход*:\n\n"
                    f"📅 Дата: `{now}`\n"
                    f"💸 Сумма: `-{amount}`\n"
                    f"📝 Описание: `{description}`"
                )

            # Баланс из "Сводка"
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

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Доход", callback_data="add_income")],
                [InlineKeyboardButton("📤 Расход", callback_data="add_expense")],
                [InlineKeyboardButton("⬅️ В меню", callback_data="menu")]
            ])

            await user_message.delete()  # Удаляем сообщение с описанием
            await user_message.chat.send_message(reply_text, parse_mode="Markdown", reply_markup=keyboard)
            context.user_data.clear()

        except Exception as e:
            logger.error(f"Ошибка записи в таблицу: {e}")
            await user_message.reply_text("❌ Ошибка при записи данных. Попробуйте позже.")

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
