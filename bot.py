import os
import json
import base64
import logging
import gspread
import datetime
import re
import asyncio

from oauth2client.service_account import ServiceAccountCredentials
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
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
REMINDER_CHAT_ID = -1002522776417
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


# Статичная клавиатура с кнопкой "Меню" под полем ввода
def persistent_menu_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[["Меню"]],
        resize_keyboard=True,
        one_time_keyboard=False
    )


# Показываем меню (inline кнопки) и добавляем кнопку "Меню" под полем ввода
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inline_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Баланс", callback_data="balance")],
        [InlineKeyboardButton("📥 Доход", callback_data="add_income"),
         InlineKeyboardButton("📤 Расход", callback_data="add_expense")],
        [InlineKeyboardButton("🛡 Страховки", callback_data="insurance"),
         InlineKeyboardButton("🧰 Тех.Осмотры", callback_data="tech")],
        [InlineKeyboardButton("📈 Отчёт 7 дней", callback_data="report_7"),
         InlineKeyboardButton("📊 Отчёт 30 дней", callback_data="report_30")]
    ])

    reply_kb = persistent_menu_keyboard()

    if update.message:
        await update.message.reply_text("Выберите действие:", reply_markup=inline_keyboard)
        # Просто клавиатура без дополнительного текста
        await update.message.reply_text("", reply_markup=reply_kb)
    elif update.callback_query:
        await update.callback_query.edit_message_text("Выберите действие:", reply_markup=inline_keyboard)
        await update.callback_query.message.reply_text("", reply_markup=reply_kb)


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
            if not rows:
                await query.edit_message_text("🚗 Страховки не найдены.", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
                ]))
                return
            text = "🚗 Страховки:\n"
            today = datetime.datetime.now().date()
            for i, row in enumerate(rows):
                name = row[0]
                date_str = row[1] if len(row) > 1 else None
                days_left = "—"
                if date_str:
                    try:
                        deadline = datetime.datetime.strptime(date_str, "%d.%m.%Y").date()
                        delta = (deadline - today).days
                        if delta > 0:
                            days_left = f"осталось {delta} дней"
                        elif delta == 0:
                            days_left = "сегодня"
                        else:
                            days_left = f"просрочено на {abs(delta)} дней"
                    except ValueError:
                        days_left = "неверный формат даты"
                text += f"{i+1}. {name} до {date_str or '—'} ({days_left})\n"

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
            today = datetime.datetime.now().date()
            for i, row in enumerate(rows):
                name = row[0]
                date_str = row[1] if len(row) > 1 else None
                days_left = "—"
                if date_str:
                    try:
                        deadline = datetime.datetime.strptime(date_str, "%d.%m.%Y").date()
                        delta = (deadline - today).days
                        if delta > 0:
                            days_left = f"осталось {delta} дней"
                        elif delta == 0:
                            days_left = "сегодня"
                        else:
                            days_left = f"просрочено на {abs(delta)} дней"
                    except ValueError:
                        days_left = "неверный формат даты"
                text += f"{i+1}. {name} до {date_str or '—'} ({days_left})\n"


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
            
    elif data.startswith("report_7_details") or data.startswith("report_30_details"):
        days = 7 if data.startswith("report_7") else 30
        match = re.search(r"_page_(\d+)", data)
        page = int(match.group(1)) if match else 0
        per_page = 10

        try:
            client = get_gspread_client()
            now = datetime.datetime.now()
            start_date = now - datetime.timedelta(days=days)

            def get_details(worksheet_name, is_income):
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet(worksheet_name)
                rows = sheet.get_all_values()[1:]
                details = []
                for row in rows:
                    try:
                        date_str = row[0].strip()
                        try:
                            dt = datetime.datetime.strptime(date_str, "%d.%m.%Y %H:%M")
                        except ValueError:
                            dt = datetime.datetime.strptime(date_str, "%d.%m.%Y")
                        if dt >= start_date:
                            if is_income:
                                category = row[1] if len(row) > 1 else "-"
                                card = row[2] if len(row) > 2 else ""
                                cash = row[3] if len(row) > 3 else ""
                                description = row[4] if len(row) > 4 else "-"
                            else:
                                card = row[1] if len(row) > 1 else ""
                                cash = row[2] if len(row) > 2 else ""
                                description = row[3] if len(row) > 3 else "-"
                                category = "—"

                            source = "Карта" if card else "Наличные" if cash else "-"
                            amount = card or cash or "0"
                            details.append(f"{'📥' if is_income else '📤'} {dt.strftime('%d.%m %H:%M')} • {amount} ({source})\n📝 {description}")
                    except Exception as e:
                        logger.warning(f"Ошибка строки: {row} — {e}")
                        continue
                return details

            income_details = get_details("Доход", True)
            expense_details = get_details("Расход", False)

            all_details = sorted(income_details + expense_details)  # по дате
            total_pages = (len(all_details) - 1) // per_page + 1
            current_details = all_details[page * per_page: (page + 1) * per_page]

            if not current_details:
                await query.edit_message_text(f"📋 Нет данных за последние {days} дней.")
                return

            text = f"📋 Подробности за *{days} дней* (стр. {page + 1}/{total_pages}):\n\n" + "\n\n".join(current_details)

            # Кнопки навигации
            nav_buttons = []
            if page > 0:
                nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"report_{days}_details_page_{page - 1}"))
            if page < total_pages - 1:
                nav_buttons.append(InlineKeyboardButton("➡️ Вперёд", callback_data=f"report_{days}_details_page_{page + 1}"))

            keyboard = InlineKeyboardMarkup([
                nav_buttons,
                [InlineKeyboardButton("🔙 Меню", callback_data="menu")]
            ])

            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Ошибка получения подробностей: {e}")
            await query.message.reply_text("⚠️ Не удалось загрузить подробности.")

# Обработчик нажатия на кнопку "Меню" с клавиатуры — не отправляем текст, просто открываем меню
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

                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
                    ])

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
                row = [now, category, "", "", description]  # C и D будут позже

                if source == "Карта":
                    row[2] = amount  # C
                else:
                    row[3] = amount  # D

                sheet.append_row(row)

                text = f"✅ Добавлено в *Доход*:\n📅 {now}\n🏷 {category}\n💰 {amount} ({source})\n📝 {description}"

            else:
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")
                row = [now, "", "", description]  # B и C

                if source == "Карта":
                    row[1] = amount  # B
                else:
                    row[2] = amount  # C

                sheet.append_row(row)

                text = f"✅ Добавлено в *Расход*:\n📅 {now}\n💸 -{amount} ({source})\n📝 {description}"

            summary = get_data()
            text += f"\n\n📊 Баланс:\n💼 {summary.get('Баланс', '—')}\n💳 {summary.get('Карта', '—')}\n💵 {summary.get('Наличные', '—')}"

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Доход", callback_data="add_income"),
                 InlineKeyboardButton("📤 Расход", callback_data="add_expense")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
            ])

            context.user_data.clear()

            await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

            try:
                # Отправляем сообщение в группу (дублируем)
                await context.bot.send_message(chat_id=-1002522776417, text=text, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Ошибка отправки в группу: {e}")

        except Exception as e:
            logger.error(f"Ошибка записи: {e}")
            await update.message.reply_text("⚠️ Ошибка записи в таблицу.")

async def check_reminders(app):
    while True:
        try:
            client = get_gspread_client()
            now = datetime.datetime.now().date()
            remind_before_days = 7

            def check_sheet(sheet_name):
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)
                rows = sheet.get_all_values()[1:]  # пропускаем заголовок
                reminders = []
                for row in rows:
                    if len(row) < 2:
                        continue
                    car = row[0].strip()
                    date_str = row[1].strip()
                    try:
                        try:
                            dt = datetime.datetime.strptime(date_str, "%d.%m.%Y %H:%M").date()
                        except ValueError:
                            dt = datetime.datetime.strptime(date_str, "%d.%m.%Y").date()
                    except Exception:
                        continue

                    days_left = (dt - now).days
                    if days_left <= remind_before_days:
                        reminders.append((car, dt, days_left))
                return reminders

            insurance_reminders = check_sheet("Страховки")
            tech_reminders = check_sheet("ТехОсмотры")

            for car, dt, days_left in insurance_reminders:
                if days_left < 0:
                    text = f"🚨 Страховка на *{car}* просрочена! Срочно оплатите и обновите дату."
                else:
                    text = f"⏰ Через {days_left} дней заканчивается страховка на *{car}* ({dt.strftime('%d.%m.%Y')})."

                await app.bot.send_message(chat_id=REMINDER_CHAT_ID, text=text, parse_mode="Markdown")

            for car, dt, days_left in tech_reminders:
                if days_left < 0:
                    text = f"🚨 Тех.осмотр на *{car}* просрочен! Срочно пройдите тех.осмотр и обновите дату."
                else:
                    text = f"⏰ Через {days_left} дней заканчивается тех.осмотр на *{car}* ({dt.strftime('%d.%m.%Y')})."

                await app.bot.send_message(chat_id=REMINDER_CHAT_ID, text=text, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Ошибка при проверке напоминаний: {e}")

        await asyncio.sleep(86400)  # Ждем 24 часа


async def on_startup(app):
    asyncio.create_task(check_reminders(app))


def main():
    application = ApplicationBuilder().token(Telegram_Token).build()
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(MessageHandler(filters.Regex("^(Меню)$"), on_menu_button_pressed))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount_description))

    application.post_init = on_startup

    application.run_polling()

if __name__ == "__main__":
    main()
