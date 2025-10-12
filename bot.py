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
        [InlineKeyboardButton("💱 Перевод", callback_data="transfer")],  # 👈 добавлено
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

    if data in ["cancel", "menu"]:
        context.user_data.clear()
        await menu_command(update, context)
        return

    client = get_gspread_client()
    sheet_summary = client.open_by_key(SPREADSHEET_ID).worksheet("Сводка")

    # Функция для получения актуальных значений карты и налички
    def get_current_balance():
        rows = sheet_summary.get_all_values()
        data = {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2}
        try:
            card = float(data.get("Карта", "0").replace(",", "."))
        except ValueError:
            card = 0.0
        try:
            cash = float(data.get("Наличные", "0").replace(",", "."))
        except ValueError:
            cash = 0.0
        return card, cash

    # Функция для обновления сводки
    def update_summary(card, cash):
        rows = sheet_summary.get_all_values()
        for i, row in enumerate(rows):
            if row[0].strip().lower() == "карта":
                sheet_summary.update_cell(i + 1, 2, str(card))
            elif row[0].strip().lower() == "наличные":
                sheet_summary.update_cell(i + 1, 2, str(cash))
            elif row[0].strip().lower() == "баланс":
                sheet_summary.update_cell(i + 1, 2, str(card + cash))

    # Добавление дохода
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
        category_map = {"cat_franky": "Franky", "cat_fraiz": "Fraiz", "cat_other": "Другое"}
        context.user_data["action"] = "income"
        context.user_data["category"] = category_map[data]
        context.user_data["step"] = "amount"
        await query.edit_message_text("Введите сумму дохода:", reply_markup=cancel_keyboard())

    # Добавление расхода
    elif data == "add_expense":
        context.user_data.clear()
        context.user_data["action"] = "expense"
        context.user_data["step"] = "amount"
        await query.edit_message_text("Введите сумму расхода:", reply_markup=cancel_keyboard())

    # Выбор источника
    elif data in ["source_card", "source_cash"]:
        context.user_data["source"] = "Карта" if data == "source_card" else "Наличные"
        context.user_data["step"] = "description"
        await query.edit_message_text("Введите описание:")

    # Перевод между картой и наличкой
    elif data == "transfer":
        context.user_data.clear()
        context.user_data["action"] = "transfer"
        context.user_data["step"] = "amount"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 → 💵 С карты на наличку", callback_data="transfer_card_to_cash")],
            [InlineKeyboardButton("💵 → 💳 С налички на карту", callback_data="transfer_cash_to_card")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
        ])
        await query.edit_message_text("Выберите направление перевода:", reply_markup=keyboard)

    elif data in ["transfer_card_to_cash", "transfer_cash_to_card"]:
        context.user_data["direction"] = "card_to_cash" if data == "transfer_card_to_cash" else "cash_to_card"
        context.user_data["action"] = "transfer"
        context.user_data["step"] = "amount"
        await query.edit_message_text("Введите сумму перевода:", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]
        ))

    # Обновление баланса после любой операции
    if "action" in context.user_data and context.user_data["action"] in ["income", "expense", "transfer"] and context.user_data.get("step") == "completed":
        card, cash = get_current_balance()
        update_summary(card, cash)

    # ------------------- СТРАХОВКИ -------------------
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
        return

    # ------------------- ТЕХОСМОТР -------------------
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
        return

    # ------------------- РЕДАКТИРОВАНИЕ -------------------
    elif data in ["edit_insurance", "edit_tech"]:
        context.user_data["edit_type"] = "insurance" if data == "edit_insurance" else "tech"
        await query.edit_message_text(
            "Введите название машины и дату через тире (Пример: Toyota - 01.09.2025)",
            reply_markup=cancel_keyboard()
        )
        return

    # ------------------- БАЛАНС -------------------
    elif data == "balance":
        try:
            data_dict = get_data()
            text = (
                f"💼 Баланс: {data_dict.get('Баланс', '—')}\n"
                f"💳 Карта: {data_dict.get('Карта', '—')}\n"
                f"💵 Наличные: {data_dict.get('Наличные', '—')}"
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
        return

    # ------------------- ОТЧЕТЫ -------------------

    elif data in ["report_7", "report_30"]:
        days = 7 if data == "report_7" else 30
        try:
            client = get_gspread_client()
            now = datetime.datetime.now()
            start_date = now - datetime.timedelta(days=days)
    
            def get_sum_and_details(sheet_name, is_income):
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)
                rows = sheet.get_all_values()[1:]
                total = 0.0
                for row in rows:
                    try:
                        date_str = row[0].strip()
                        try:
                            dt = datetime.datetime.strptime(date_str, "%d.%m.%Y %H:%M")
                        except ValueError:
                            dt = datetime.datetime.strptime(date_str, "%d.%m.%Y")
                        if dt >= start_date:
                            if is_income:
                                card = row[2] if len(row) > 2 else ""
                                cash = row[3] if len(row) > 3 else ""
                            else:
                                card = row[1] if len(row) > 1 else ""
                                cash = row[2] if len(row) > 2 else ""
    
                            amount_str = card or cash or "0"
                            amount_str = amount_str.replace(" ", "").replace(",", ".")
                            amount = float(amount_str) if amount_str else 0
                            total += amount
                    except Exception as e:
                        logger.warning(f"Ошибка строки: {row} — {e}")
                        continue
                return total
    
            income_total = get_sum_and_details("Доход", True)
            expense_total = get_sum_and_details("Расход", False)
            net_income = income_total - expense_total
    
            text = (
                f"📅 Отчёт за {days} дней:\n\n"
                f"📥 Доход: {income_total:,.2f}\n"
                f"📤 Расход: {expense_total:,.2f}\n"
                f"💰 Чистый доход: {net_income:,.2f}"
            )
    
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Подробности", callback_data=f"report_{days}_details_page0")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
            ])
    
            await query.edit_message_text(text, reply_markup=keyboard)
    
        except Exception as e:
            logger.error(f"Ошибка получения отчёта: {e}")
            await query.message.reply_text("⚠️ Не удалось загрузить отчёт.")
            
    elif re.match(r"report_(7|30)_details_page(\d+)", data):
        m = re.match(r"report_(7|30)_details_page(\d+)", data)
        days = int(m.group(1))
        page = int(m.group(2))
    
        # Можно сохранить, если используешь context.user_data, для удобства
        context.user_data["report_days"] = days
        context.user_data["report_page"] = page
    
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Доходы", callback_data=f"report_{days}_details_income_page{page}")],
            [InlineKeyboardButton("📤 Расходы", callback_data=f"report_{days}_details_expense_page{page}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"report_{days}")]
        ])

        await query.edit_message_text("Выберите подробности:", reply_markup=keyboard)
    
    elif re.match(r"report_(7|30)_details_(income|expense)_page(\d+)", data):
        m = re.match(r"report_(7|30)_details_(income|expense)_page(\d+)", data)
        days, detail_type, page = int(m.group(1)), m.group(2), int(m.group(3))
    
        try:
            client = get_gspread_client()
            now = datetime.datetime.now()
            start_date = now - datetime.timedelta(days=days)
            sheet_name = "Доход" if detail_type == "income" else "Расход"
            sheet = client.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)
            rows = sheet.get_all_values()[1:]
    
            filtered = []
            for row in rows:
                try:
                    dt = datetime.datetime.strptime(row[0].strip(), "%d.%m.%Y %H:%M")
                except ValueError:
                    dt = datetime.datetime.strptime(row[0].strip(), "%d.%m.%Y")
                if dt >= start_date:
                    filtered.append(row)
    
            page_size = 10
            total_pages = (len(filtered) + page_size - 1) // page_size
            page = max(0, min(page, total_pages - 1))
            page_rows = filtered[page*page_size:(page+1)*page_size]
    
            lines = []
            for r in page_rows:
                date = r[0]
            
                if detail_type == "income":
                    category = r[1] if len(r) > 1 else "-"
                    card = r[2] if len(r) > 2 else ""
                    cash = r[3] if len(r) > 3 else ""
                    desc = r[4] if len(r) > 4 else "-"
            
                    # Определяем сумму и источник
                    if card:
                        amount = card
                        source_emoji = "💳"
                    elif cash:
                        amount = cash
                        source_emoji = "💵"
                    else:
                        amount = "0"
                        source_emoji = ""
            
                    amount = amount.replace(" ", "").replace(",", ".")
            
                    # Иконка категории
                    category_icon = "🛠️" if category.strip().lower() == "другое" else "🚗"
            
                    lines.append(f"📅 {date} | {category_icon} {category} | 🟢 {source_emoji} {amount} | 📝 {desc}")
            
                else:
                    card = r[1] if len(r) > 1 else ""
                    cash = r[2] if len(r) > 2 else ""
                    desc = r[3] if len(r) > 3 else "-"
            
                    # Определяем сумму и источник
                    if card:
                        amount = card
                        source_emoji = "💳"
                    elif cash:
                        amount = cash
                        source_emoji = "💵"
                    else:
                        amount = "0"
                        source_emoji = ""
            
                    amount = amount.replace(" ", "").replace(",", ".")
            
                    lines.append(f"📅 {date} | 🔴 {source_emoji} -{amount} | 📝 {desc}")
            
            text = f"📋 Подробности ({'Доходов' if detail_type == 'income' else 'Расходов'}) за {days} дней:\n\n"
            text += "\n".join(lines) if lines else "Данные не найдены."

            buttons = []
            if page > 0:
                buttons.append(InlineKeyboardButton("⬅️ Предыдущая", callback_data=f"report_{days}_details_{detail_type}_page{page-1}"))
            if page < total_pages - 1:
                buttons.append(InlineKeyboardButton("➡️ Следующая", callback_data=f"report_{days}_details_{detail_type}_page{page+1}"))
    
            keyboard = InlineKeyboardMarkup([
                buttons,
                [InlineKeyboardButton("⬅️ Назад", callback_data=f"report_{days}_details_page0")],
                [InlineKeyboardButton("⬅️ Главное меню", callback_data="menu")]
            ])
    
            await query.edit_message_text(text, reply_markup=keyboard)
    
        except Exception as e:
            logger.error(f"Ошибка загрузки подробностей отчёта: {e}")
            await query.message.reply_text("⚠️ Не удалось загрузить подробности отчёта.")



# Обработчик нажатия на кнопку "Меню" с клавиатуры — не отправляем текст, просто открываем меню
async def on_menu_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await menu_command(update, context)


async def handle_amount_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Отмена
    if text.lower() == "отмена":
        context.user_data.clear()
        await update.message.reply_text("❌ Отменено.")
        return await menu_command(update, context)

    action = context.user_data.get("action")
    step = context.user_data.get("step")

    if not action or not step:
        return

    client = get_gspread_client()

    # ----------------- ПЕРЕВОД -----------------
    if action == "transfer" and step == "amount":
        clean_text = text.replace(" ", "").replace(",", ".")
        try:
            amount = float(clean_text)
            if amount <= 0:
                await update.message.reply_text("⚠️ Введите положительное число (пример: 500.00)")
                return

            direction = context.user_data["direction"]

            sheet_summary = client.open_by_key(SPREADSHEET_ID).worksheet("Сводка")
            rows_summary = sheet_summary.get_all_values()
            data_summary = {row[0].strip(): row[1].strip() for row in rows_summary if len(row) >= 2}

            # Безопасно конвертируем
            try:
                card = float(data_summary.get("Карта", "0").replace(",", "."))
            except ValueError:
                card = 0.0
            try:
                cash = float(data_summary.get("Наличные", "0").replace(",", "."))
            except ValueError:
                cash = 0.0

            # Логика перевода
            if direction == "card_to_cash":
                if card < amount:
                    await update.message.reply_text("⚠️ Недостаточно средств на карте.")
                    return
                card -= amount
                cash += amount
                direction_text = "💳 → 💵 Перевод с карты на наличку"
            else:
                if cash < amount:
                    await update.message.reply_text("⚠️ Недостаточно наличных.")
                    return
                cash -= amount
                card += amount
                direction_text = "💵 → 💳 Перевод с налички на карту"

            # Обновляем Сводку
            for i, row in enumerate(rows_summary):
                key = row[0].strip().lower()
                if key == "карта":
                    sheet_summary.update_cell(i + 1, 2, str(card))
                elif key == "наличные":
                    sheet_summary.update_cell(i + 1, 2, str(cash))
                elif key == "баланс":
                    sheet_summary.update_cell(i + 1, 2, str(card + cash))

            now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
            text_out = (
                f"💱 *Перевод средств*\n"
                f"📅 {now}\n"
                f"{direction_text}\n"
                f"💰 Сумма: {amount:,.2f}\n\n"
                f"📊 *Текущий баланс:*\n"
                f"💳 Карта: {card:,.2f}\n"
                f"💵 Наличные: {cash:,.2f}\n"
                f"💼 Общий: {card + cash:,.2f}"
            )

            context.user_data.clear()
            await update.message.reply_text(text_out, reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Назад", callback_data="menu")]]
            ), parse_mode="Markdown")

            # Отправка в канал
            try:
                await context.bot.send_message(chat_id=REMINDER_CHAT_ID, text=text_out, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Ошибка отправки в канал: {e}")

        except ValueError:
            await update.message.reply_text("⚠️ Введите положительное число (пример: 500.00)")
        except Exception as e:
            logger.error(f"Ошибка перевода: {e}")
            await update.message.reply_text("❌ Ошибка при переводе средств.")
        return

    # ----------------- ДОХОД / РАСХОД -----------------
    if step == "amount":
        try:
            amount = float(text.replace(" ", "").replace(",", "."))
            if amount <= 0:
                await update.message.reply_text("⚠️ Введите положительное число (пример: 1200.50)")
                return
            context.user_data["amount"] = amount
            context.user_data["step"] = "source"

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Карта", callback_data="source_card")],
                [InlineKeyboardButton("💵 Наличные", callback_data="source_cash")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
            ])
            await update.message.reply_text("Выберите источник:", reply_markup=keyboard)
            return
        except ValueError:
            await update.message.reply_text("⚠️ Введите положительное число (пример: 1200.50)")
            return

    elif step == "description":
        description = text
        now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
        amount = context.user_data.get("amount")
        category = context.user_data.get("category", "-")
        source = context.user_data.get("source", "-")

        try:
            if action == "income":
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
                row = [now, category, "", "", description]
                if source == "Карта":
                    row[2] = amount
                else:
                    row[3] = amount
                sheet.append_row(row)
                msg_prefix = "✅ Добавлено в *Доход*"
            else:
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")
                row = [now, "", "", description]
                if source == "Карта":
                    row[1] = amount
                else:
                    row[2] = amount
                sheet.append_row(row)
                msg_prefix = "✅ Добавлено в *Расход*"

            # ----------------- ОБНОВЛЕНИЕ СВОДКИ -----------------
            sheet_income = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
            sheet_expense = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")

            def sum_column(rows, col_index):
                total = 0
                for r in rows:
                    try:
                        total += float(str(r[col_index]).replace(",", "."))
                    except (ValueError, IndexError):
                        continue
                return total

            rows_income = sheet_income.get_all_values()[1:]
            rows_expense = sheet_expense.get_all_values()[1:]

            card_total = sum_column(rows_income, 2) - sum_column(rows_expense, 1)
            cash_total = sum_column(rows_income, 3) - sum_column(rows_expense, 2)
            balance_total = card_total + cash_total

            sheet_summary = client.open_by_key(SPREADSHEET_ID).worksheet("Сводка")
            for i, row in enumerate(sheet_summary.get_all_values()):
                key = row[0].strip().lower()
                if key == "карта":
                    sheet_summary.update_cell(i + 1, 2, str(card_total))
                elif key == "наличные":
                    sheet_summary.update_cell(i + 1, 2, str(cash_total))
                elif key == "баланс":
                    sheet_summary.update_cell(i + 1, 2, str(balance_total))

            # ----------------- ВЫВОД -----------------
            text_out = (
                f"{msg_prefix}:\n📅 {now}\n"
                f"🏷 {category}\n💰 {amount} ({source})\n📝 {description}\n\n"
                f"📊 Баланс:\n💼 {balance_total}\n💳 {card_total}\n💵 {cash_total}"
            )

            context.user_data.clear()
            await update.message.reply_text(text_out, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Доход", callback_data="add_income"),
                 InlineKeyboardButton("📤 Расход", callback_data="add_expense")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")]
            ]), parse_mode="Markdown")

            # Отправляем в канал
            try:
                await context.bot.send_message(chat_id=REMINDER_CHAT_ID, text=text_out, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Ошибка отправки в канал: {e}")

        except Exception as e:
            logger.error(f"Ошибка записи доход/расход: {e}")
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
