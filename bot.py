import os
import json
import base64
import logging
import gspread
import datetime
import re
import asyncio

def _ensure_column(ws, header_name: str) -> int:
    """Вернёт индекс колонки по заголовку. Если нет — создаст новую справа и вернёт её индекс."""
    header = ws.row_values(1)
    if header_name in header:
        return header.index(header_name) + 1
    col = len(header) + 1
    ws.update_cell(1, col, header_name)
    return col

def _find_row_by_name(ws, name: str, name_header: str = "Название") -> int | None:
    """Вернёт индекс строки (2..N) по названию авто, иначе None."""
    rows = ws.get_all_values()
    if not rows:
        return None
    header = rows[0]
    try:
        name_idx = header.index(name_header)
    except ValueError:
        return None
    for i, r in enumerate(rows[1:], start=2):
        if name_idx < len(r) and r[name_idx].strip() == name.strip():
            return i
    return None
def _format_date_with_days(date_str: str) -> str:
    """
    "ДД.ММ.ГГГГ" или "ДД.ММ.ГГГГ ЧЧ:ММ" -> "ДД.ММ.ГГГГ (N дней)"
    Пусто -> "—", ошибки -> "неверный формат".
    """
    if not date_str:
        return "—"
    s = date_str.strip()
    try:
        try:
            dt = datetime.datetime.strptime(s, "%d.%m.%Y %H:%M")
        except ValueError:
            dt = datetime.datetime.strptime(s, "%d.%m.%Y")
        d = dt.date()
        today = datetime.date.today()
        delta = (d - today).days
        if delta > 0:
            tail = f"({delta} дней)"
        elif delta == 0:
            tail = "(сегодня)"
        else:
            tail = f"(просрочено {abs(delta)} дней)"
        return f"{d.strftime('%d.%m.%Y')} {tail}"
    except Exception:
        return "неверный формат"


from decimal import Decimal, ROUND_HALF_UP

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

INITIAL_BALANCE = Decimal("21263.99")  # 🏁 Начальная сумма


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


def _to_amount(val):
    s = str(val) if val is not None else "0"
    s = s.replace(" ", "").replace(",", ".")
    try:
        return Decimal(s)
    except Exception:
        return Decimal("0")


def _fmt_amount(val):
    if not isinstance(val, Decimal):
        val = _to_amount(val)
    # format with thousands sep and 2 decimals
    return format(val.quantize(Decimal("0.01")), ",.2f")


def compute_balance(client):
    """
    Формулы точно как в листе 'Сводка':
    - Наличные = СУММ('Доход'!D) - СУММ('Расход'!C)
    - Карта = INITIAL_BALANCE + СУММ('Доход'!C) - СУММ('Расход'!B)
    - Баланс = Карта + Наличные
    """
    income_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
    expense_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")

    income_rows = income_ws.get_all_values()[1:]
    expense_rows = expense_ws.get_all_values()[1:]

    income_card = Decimal("0")
    income_cash = Decimal("0")
    for r in income_rows:
        if len(r) > 2:
            income_card += _to_amount(r[2])
        if len(r) > 3:
            income_cash += _to_amount(r[3])

    expense_card = Decimal("0")
    expense_cash = Decimal("0")
    for r in expense_rows:
        if len(r) > 1:
            expense_card += _to_amount(r[1])
        if len(r) > 2:
            expense_cash += _to_amount(r[2])

    # Формулы как в листе 'Сводка'
    cash_bal_display = income_cash - expense_cash  # = SUM(Доход!D) - SUM(Расход!C)
    card_bal_display = INITIAL_BALANCE + income_card - expense_card  # = INITIAL + SUM(Доход!C) - SUM(Расход!B)
    total_bal = card_bal_display + cash_bal_display  # = Карта + Наличные

    return {
        "Баланс": total_bal,
        "Карта": card_bal_display,
        "Наличные": cash_bal_display,
    }

def compute_summary(client):
    """
    Возвращает полный набор показателей как на листе 'Сводка':
    - Начальная сумма (INITIAL_BALANCE)
    - Доход = SUM(Доход!C:D)
    - Расход = SUM(Расход!B:C)
    - Наличные = SUM(Доход!D) - SUM(Расход!C)
    - Карта = INITIAL_BALANCE + SUM(Доход!C) - SUM(Расход!B)
    - Баланс = Карта + Наличные
    - Заработано = Доход - Начальная сумма
    """
    income_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
    expense_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")

    income_rows = income_ws.get_all_values()[1:]
    expense_rows = expense_ws.get_all_values()[1:]

    income_card = Decimal("0")
    income_cash = Decimal("0")
    for r in income_rows:
        if len(r) > 2:
            income_card += _to_amount(r[2])
        if len(r) > 3:
            income_cash += _to_amount(r[3])

    expense_card = Decimal("0")
    expense_cash = Decimal("0")
    for r in expense_rows:
        if len(r) > 1:
            expense_card += _to_amount(r[1])
        if len(r) > 2:
            expense_cash += _to_amount(r[2])

    income_total = income_card + income_cash
    expense_total = expense_card + expense_cash

    cash = income_cash - expense_cash
    card = INITIAL_BALANCE + income_card - expense_card
    balance = card + cash
    earned = income_total - INITIAL_BALANCE

    return {
        "Начальная": INITIAL_BALANCE,
        "Доход": income_total,
        "Расход": expense_total,
        "Наличные": cash,
        "Карта": card,
        "Баланс": balance,
        "Заработано": earned,
    }
# Статичная клавиатура с кнопкой "Меню" под полем ввода
def persistent_menu_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[["Меню"]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


# Показываем меню (inline кнопки) и добавляем кнопку "Меню" под полем ввода
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inline_keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Баланс", callback_data="balance")],
            [
                InlineKeyboardButton("📥 Доход", callback_data="add_income"),
                InlineKeyboardButton("📤 Расход", callback_data="add_expense"),
            ],
            [
                InlineKeyboardButton("🔁 Перевод", callback_data="transfer"),
                InlineKeyboardButton("🚗 Автомобили", callback_data="cars")
            ],
            [
                InlineKeyboardButton("🛡 Страховки", callback_data="insurance"),
                InlineKeyboardButton("🧰 Тех.Осмотры", callback_data="tech"),
            ],
            [
                InlineKeyboardButton("📈 Отчёт 7 дней", callback_data="report_7"),
                InlineKeyboardButton("📊 Отчёт 30 дней", callback_data="report_30"),
            ],
        ]
    )
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
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Franky", callback_data="cat_franky")],
                [InlineKeyboardButton("Fraiz", callback_data="cat_fraiz")],
                [InlineKeyboardButton("Другое", callback_data="cat_other")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
            ]
        )
        await query.edit_message_text("Выберите категорию дохода:", reply_markup=keyboard)

    elif data == "cars_edit":
        # список всех машин по названию
        try:
            client = get_gspread_client()
            ws = client.open_by_key(SPREADSHEET_ID).worksheet("Автомобили")
            rows = ws.get_all_values()
            if not rows or len(rows) < 2:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="cars")]])
                await query.edit_message_text("Список пуст.", reply_markup=kb)
                return

            header, body = rows[0], rows[1:]
            try:
                name_idx = header.index("Название")
            except ValueError:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="cars")]])
                await query.edit_message_text("Не найдена колонка «Название».", reply_markup=kb)
                return

            # Кнопки по именам
            btns = []
            for r in body:
                if name_idx < len(r) and r[name_idx].strip():
                    name = r[name_idx].strip()
                    btns.append([InlineKeyboardButton(name, callback_data=f"editcar_select|{name}")])

            btns.append([InlineKeyboardButton("⬅️ Назад", callback_data="cars")])
            await query.edit_message_text("Выберите автомобиль для редактирования:", reply_markup=InlineKeyboardMarkup(btns))
        except Exception as e:
            logger.error(f"cars_edit error: {e}")
            await query.message.reply_text("⚠️ Не удалось загрузить список.")
        return

    elif data.startswith("editcar_select|"):
        # выбрали конкретную машину — показываем, что редактировать
        name = data.split("|", 1)[1]
        context.user_data["edit_car_name"] = name
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🛡️ Страховка", callback_data="editcar_field|insurance")],
            [InlineKeyboardButton("🧰 Техосмотр", callback_data="editcar_field|tech")],
            [InlineKeyboardButton("🗑 Удалить машину", callback_data="editcar_delete_confirm")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="cars_edit")],
        ])
        await query.edit_message_text(f"🚘 {name}\nЧто редактировать?", reply_markup=kb)
        return

    elif data.startswith("editcar_field|"):
        field = data.split("|", 1)[1]   # insurance | tech
        context.user_data["action"] = "edit_car"
        context.user_data["step"] = f"edit_{field}"
        prompt = "Введите дату страховки (ДД.ММ.ГГГГ):" if field == "insurance" else "Введите дату техосмотра (ДД.ММ.ГГГГ):"
        await query.edit_message_text(prompt, reply_markup=cancel_keyboard())
        return

    elif data == "editcar_delete_confirm":
        name = context.user_data.get("edit_car_name", "-")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, удалить", callback_data="editcar_delete_yes")],
            [InlineKeyboardButton("⬅️ Отмена", callback_data="cars_edit")],
        ])
        await query.edit_message_text(f"Удалить «{name}» безвозвратно?", reply_markup=kb)
        return

    elif data == "editcar_delete_yes":
        try:
            client = get_gspread_client()
            ws = client.open_by_key(SPREADSHEET_ID).worksheet("Автомобили")
            row_idx = _find_row_by_name(ws, context.user_data.get("edit_car_name", ""))
            if not row_idx:
                await query.edit_message_text("Авто не найдено.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="cars_edit")]]))
                return
            ws.delete_rows(row_idx)
            context.user_data.pop("edit_car_name", None)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К списку", callback_data="cars")]])
            await query.edit_message_text("✅ Машина удалена.", reply_markup=kb)
        except Exception as e:
            logger.error(f"delete car error: {e}")
            await query.message.reply_text("⚠️ Не удалось удалить.")
        return    

    elif data in ["cat_franky", "cat_fraiz", "cat_other"]:
        category_map = {
            "cat_franky": "Franky",
            "cat_fraiz": "Fraiz",
            "cat_other": "Другое",
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

    elif data == "transfer":
        # Start transfer flow: ask direction
        context.user_data.clear()
        context.user_data["action"] = "transfer"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 → 💵 С карты в наличные", callback_data="transfer_card_to_cash")],
            [InlineKeyboardButton("💵 → 💳 С наличных на карту", callback_data="transfer_cash_to_card")],
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
        ])
        await query.edit_message_text("Выберите направление перевода:", reply_markup=kb)

    elif data in ["transfer_card_to_cash", "transfer_cash_to_card"]:
        context.user_data.clear()
        context.user_data["action"] = "transfer"
        context.user_data["direction"] = "card_to_cash" if data == "transfer_card_to_cash" else "cash_to_card"
        context.user_data["step"] = "amount"
        await query.edit_message_text("Введите сумму перевода:", reply_markup=cancel_keyboard())

    elif data == "cars":
        try:
            client = get_gspread_client()
            ws = client.open_by_key(SPREADSHEET_ID).worksheet("Автомобили")
            rows = ws.get_all_values()

            if not rows or len(rows) < 2:
                text = "🚗 *Автомобили:*\n\nСписок пуст."
            else:
                header = rows[0]
                body = rows[1:]

                # Индексируем колонки по заголовкам
                idx = {name.strip(): i for i, name in enumerate(header)}
                def g(row, key):
                    i = idx.get(key)
                    return row[i].strip() if (i is not None and i < len(row)) else ""

                cards = []
                for r in body:
                    name  = g(r, "Название") or "-"
                    vin   = g(r, "VIN") or "-"
                    plate = g(r, "Номер") or "-"

                    ins_left  = _format_date_with_days(g(r, "Страховка до"))
                    tech_left = _format_date_with_days(g(r, "ТО до"))

                    card = (
                        f"🚘 *{name}*\n"
                        f"🔑 _VIN:_ `{vin}`\n"
                        f"🔖 _Номер:_ `{plate}`\n"
                        f"🛡️ _Страховка:_ {ins_left}\n"
                        f"🧰 _Техосмотр:_ {tech_left}"
                    )
                    cards.append(card)

                separator = "─" * 35  # ← длина линии (поменяй на сколько хочешь)
                text = "🚗 *Автомобили:*\n\n" + f"\n{separator}\n".join(cards)


            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Создать автомобиль", callback_data="create_car")],
                [InlineKeyboardButton("✏️ Редактировать", callback_data="cars_edit")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
            ])
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Ошибка списка авто: {e}")
            await query.message.reply_text("⚠️ Не удалось загрузить список «Автомобили».")


    elif data == "create_car":
        # старт мастера создания авто
        context.user_data.clear()
        context.user_data["action"] = "create_car"
        context.user_data["step"] = "car_name"
        try:
            await query.edit_message_text(
                "Введите *название авто* (например: Mazda 3):",
                reply_markup=cancel_keyboard(),
                parse_mode="Markdown",
            )
        except Exception as e:
            # если редактирование нельзя – отправим обычным сообщением
            logger.error(f"create_car edit failed: {e}")
            await query.message.reply_text(
                "Введите *название авто* (например: Mazda 3):",
                reply_markup=cancel_keyboard(),
                parse_mode="Markdown",
            )
        return

    elif data == "insurance":
        try:
            sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("Страховки")
            rows = sheet.get_all_values()[1:]
            if not rows:
                await query.edit_message_text(
                    "🚗 Страховки не найдены.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Назад", callback_data="menu")]]
                    ),
                )
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

            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✏️ Изменить", callback_data="edit_insurance")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
                ]
            )
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Ошибка страховок: {e}")
            await query.message.reply_text("⚠️ Не удалось получить данные по страховкам.")

    elif data == "tech":
        try:
            sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("ТехОсмотры")
            rows = sheet.get_all_values()[1:]
            if not rows:
                await query.edit_message_text(
                    "🧰 Тех.Осмотры не найдены.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⬅️ Назад", callback_data="menu")]]
                    ),
                )
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

            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✏️ Изменить", callback_data="edit_tech")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
                ]
            )
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Ошибка тех.осмотров: {e}")
            await query.message.reply_text("⚠️ Не удалось получить данные по тех.осмотрам.")

    elif data == "edit_insurance":
        context.user_data["edit_type"] = "insurance"
        await query.edit_message_text(
            "Введите название машины и дату через тире (Пример: Toyota - 01.09.2025)",
            reply_markup=cancel_keyboard(),
        )

    elif data == "edit_tech":
        context.user_data["edit_type"] = "tech"
        await query.edit_message_text(
            "Введите название машины и дату через тире (Пример: BMW - 15.10.2025)",
            reply_markup=cancel_keyboard(),
        )

    elif data == "balance":
        try:
            client = get_gspread_client()
            s = compute_summary(client)
            text = (
                f"🏁 Начальная сумма: {_fmt_amount(s['Начальная'])}\n"
                f"💼 Заработано: {_fmt_amount(s['Заработано'])}\n"
                f"💰 Доход: {_fmt_amount(s['Доход'])}\n"
                f"💸 Расход: {_fmt_amount(s['Расход'])}\n"
                f"\n"
                f"💼 Баланс: {_fmt_amount(s['Баланс'])}\n"
                f"💳 Карта: {_fmt_amount(s['Карта'])}\n"
                f"💵 Наличные: {_fmt_amount(s['Наличные'])}"
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("📥 Доход", callback_data="add_income"),
                        InlineKeyboardButton("📤 Расход", callback_data="add_expense"),
                    ],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
                ]
            )
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Ошибка баланса: {e}")
            await query.message.reply_text("⚠️ Не удалось получить баланс.")

    # В handle_button добавим обработку новых callback_data
    elif data in ["report_7", "report_30"]:
        days = 7 if data == "report_7" else 30
        try:
            client = get_gspread_client()
            now = datetime.datetime.now()
            start_date = now - datetime.timedelta(days=days)

            def get_sum_and_details(sheet_name, is_income):
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)
                rows = sheet.get_all_values()[1:]
                total = Decimal('0.0')
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
                            amount = _to_amount(amount_str)
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
                f"📥 Доход: {_fmt_amount(income_total)}\n"
                f"📤 Расход: {_fmt_amount(expense_total)}\n"
                f"💰 Чистый доход: {_fmt_amount(net_income)}"
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📋 Подробности", callback_data=f"report_{days}_details_page0")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
                ]
            )
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

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📥 Доходы", callback_data=f"report_{days}_details_income_page{page}")],
                [InlineKeyboardButton("📤 Расходы", callback_data=f"report_{days}_details_expense_page{page}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data=f"report_{days}")],
            ]
        )
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
                    try:
                        dt = datetime.datetime.strptime(row[0].strip(), "%d.%m.%Y %H:%M")
                    except ValueError:
                        dt = datetime.datetime.strptime(row[0].strip(), "%d.%m.%Y")
                    if dt >= start_date:
                        filtered.append(row)
                except Exception:
                    continue

            page_size = 10
            total_pages = (len(filtered) + page_size - 1) // page_size
            page = max(0, min(page, total_pages - 1))
            page_rows = filtered[page * page_size : (page + 1) * page_size]

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
                    amount = _fmt_amount(amount)
                    # Иконка категории
                    category_icon = "🛠️" if category.strip().lower() == "другое" else "🚗"
                    lines.append(
                        f"📅 {date} | {category_icon} {category} | 🟢 {source_emoji} {amount} | 📝 {desc}"
                    )
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
                    amount = _fmt_amount(amount)
                    lines.append(f"📅 {date} | 🔴 {source_emoji} -{amount} | 📝 {desc}")

            text = (
                f"📋 Подробности ({'Доходов' if detail_type == 'income' else 'Расходов'}) за {days} дней:\n\n"
            )
            text += "\n".join(lines) if lines else "Данные не найдены."

            buttons = []
            if page > 0:
                buttons.append(
                    InlineKeyboardButton(
                        "⬅️ Предыдущая",
                        callback_data=f"report_{days}_details_{detail_type}_page{page-1}",
                    )
                )
            if page < total_pages - 1:
                buttons.append(
                    InlineKeyboardButton(
                        "➡️ Следующая",
                        callback_data=f"report_{days}_details_{detail_type}_page{page+1}",
                    )
                )

            keyboard = InlineKeyboardMarkup(
                [
                    buttons,
                    [InlineKeyboardButton("⬅️ Назад", callback_data=f"report_{days}_details_page0")],
                    [InlineKeyboardButton("⬅️ Главное меню", callback_data="menu")],
                ]
            )
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Ошибка загрузки подробностей отчёта: {e}")
            await query.message.reply_text("⚠️ Не удалось загрузить подробности отчёта.")


# Обработчик нажатия на кнопку "Меню" с клавиатуры — не отправляем текст, просто открываем меню
async def on_menu_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await menu_command(update, context)


async def handle_amount_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # -------- Отмена --------
    if text.lower() == "отмена":
        context.user_data.clear()
        await update.message.reply_text("❌ Отменено.")
        await menu_command(update, context)
        return

    if context.user_data.get("action") == "edit_car":
        step = context.user_data.get("step")
        name = context.user_data.get("edit_car_name", "")
        date_txt = (update.message.text or "").strip()

        if step in ("edit_insurance", "edit_tech"):
            # простая валидация даты
            try:
                try:
                    d = datetime.datetime.strptime(date_txt, "%d.%m.%Y")
                except ValueError:
                    await update.message.reply_text("⚠️ Формат даты: ДД.ММ.ГГГГ")
                    return

                client = get_gspread_client()
                ws = client.open_by_key(SPREADSHEET_ID).worksheet("Автомобили")

                row_idx = _find_row_by_name(ws, name)
                if not row_idx:
                    await update.message.reply_text("🚫 Автомобиль не найден.")
                    return

                header = ws.row_values(1)
                col_name = "Страховка до" if step == "edit_insurance" else "ТО до"
                col_idx = header.index(col_name) + 1 if col_name in header else _ensure_column(ws, col_name)

                ws.update_cell(row_idx, col_idx, date_txt)

                context.user_data.pop("action", None)
                context.user_data.pop("step", None)

                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ К редактированию", callback_data="cars_edit")],
                    [InlineKeyboardButton("⬅️ К списку", callback_data="cars")],
                ])
                await update.message.reply_text(f"✅ Обновлено: {col_name} = {date_txt} для «{name}».", reply_markup=kb)
            except Exception as e:
                logger.error(f"edit insurance/tech error: {e}")
                await update.message.reply_text("⚠️ Не удалось обновить.")
            return
    # -------- Режим редактирования дат (страховки/ТО) --------
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
                    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu")]])
                    await update.message.reply_text(f"✅ Дата обновлена:\n{name} — {new_date}", reply_markup=kb)
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

    # -------- Шаг ввода суммы --------
    if step == "amount":
        try:
            amount = _to_amount(text)
            if amount <= 0:
                raise ValueError("Сумма должна быть положительной")

            context.user_data["amount"] = amount

            # ---- МГНОВЕННЫЙ ПЕРЕВОД (без описания) ----
            if action == "transfer":
                description = ""
                direction = context.user_data.get("direction")

                try:
                    client = get_gspread_client()
                    income_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
                    expense_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")

                    now = datetime.datetime.now().strftime("%d.%m.%Y %H:%М")
                    # Доход: [date, category, card(C), cash(D), desc]
                    income_row = [now, "Перевод", "", "", description]
                    # Расход: [date, card(B), cash(C), desc]
                    expense_row = [now, "", "", description]

                    q = str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

                    if direction == "card_to_cash":
                        # расход по карте (B), доход в наличные (D)
                        expense_row[1] = q  # B
                        income_row[3] = q   # D
                        arrow = "💳 → 💵"
                    else:
                        # расход по наличным (C), доход на карту (C)
                        expense_row[2] = q  # C
                        income_row[2] = q   # C
                        arrow = "💵 → 💳"

                    # Запись
                    expense_ws.append_row(expense_row, value_input_option="USER_ENTERED", table_range="A:D")
                    income_ws.append_row(income_row, value_input_option="USER_ENTERED", table_range="A:E")

                    # Баланс
                    live = compute_balance(client)

                    # Тебе — подробное сообщение
                    text_msg = (
                        f"✅ Перевод выполнен:\n"
                        f"{arrow}  {amount}\n"
                        f"\n📊 Баланс:\n"
                        f"💼 {_fmt_amount(live['Баланс'])}\n"
                        f"💳 {_fmt_amount(live['Карта'])}\n"
                        f"💵 {_fmt_amount(live['Наличные'])}"
                    )
                    kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("📥 Доход", callback_data="add_income"),
                         InlineKeyboardButton("📤 Расход", callback_data="add_expense")],
                        [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
                    ])
                    context.user_data.clear()
                    await update.message.reply_text(text_msg, reply_markup=kb, parse_mode="Markdown")

                    # В канал — компактно, без описания
                    try:
                        group_msg = (
                            f"🔁 Перевод: {arrow} {_fmt_amount(amount)}\n"
                            f"Баланс: 💳 {_fmt_amount(live['Карта'])} | 💵 {_fmt_amount(live['Наличные'])}"
                        )
                        await context.bot.send_message(chat_id=REMINDER_CHAT_ID, text=group_msg, parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"Ошибка отправки в группу: {e}")

                except Exception as e:
                    logger.error(f"Ошибка перевода: {e}")
                    await update.message.reply_text("⚠️ Не удалось выполнить перевод.")
                return

            # ---- ДОХОД/РАСХОД: перейти к выбору источника ----
            context.user_data["step"] = "source"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Карта", callback_data="source_card")],
                [InlineKeyboardButton("💵 Наличные", callback_data="source_cash")],
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
            ])
            await update.message.reply_text("Выберите источник:", reply_markup=keyboard)

        except Exception:
            await update.message.reply_text("⚠️ Введите положительное число (пример: 1200.50)")
        return

    # -------- Шаг описания (ТОЛЬКО для доход/расход) --------
    if step == "description":
        description = text or ""
        now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

        amount = context.user_data.get("amount")
        category = context.user_data.get("category", "-")
        source = context.user_data.get("source", "-")

        try:
            client = get_gspread_client()

            if action == "income":
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
                row = [now, category, "", "", description]
                q = str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                if source == "Карта":
                    row[2] = q  # C
                else:
                    row[3] = q  # D
                sheet.append_row(row, value_input_option="USER_ENTERED", table_range="A:E")

                text_msg = (
                    f"✅ Добавлено в *Доход*:\n"
                    f"📅 {now}\n"
                    f"🏷 {category}\n"
                    f"💰 {amount} ({source})\n"
                    f"📝 {description or '-'}"
                )
            else:
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")
                row = [now, "", "", description]
                q = str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
                if source == "Карта":
                    row[1] = q  # B
                else:
                    row[2] = q  # C
                sheet.append_row(row, value_input_option="USER_ENTERED", table_range="A:D")

                text_msg = (
                    f"✅ Добавлено в *Расход*:\n"
                    f"📅 {now}\n"
                    f"💸 -{amount} ({source})\n"
                    f"📝 {description or '-'}"
                )

            # Живой баланс
            live = compute_balance(client)
            text_msg += (
                f"\n\n📊 Баланс:\n"
                f"💼 {_fmt_amount(live['Баланс'])}\n"
                f"💳 {_fmt_amount(live['Карта'])}\n"
                f"💵 {_fmt_amount(live['Наличные'])}"
            )

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Доход", callback_data="add_income"),
                 InlineKeyboardButton("📤 Расход", callback_data="add_expense")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
            ])
            context.user_data.clear()
            await update.message.reply_text(text_msg, reply_markup=kb, parse_mode="Markdown")

            # ---- Компактные сообщения в канал ----
            try:
                source_emoji = "💳" if source == "Карта" else "💵"
                desc_q = f' “{description}”' if description else ""
                if action == "income":
                    # Доход — с категорией и описанием
                    group_msg = (
                        f"📥 Доход: {source_emoji} +{_fmt_amount(amount)} — {category}{desc_q}\n"
                        f"Баланс: 💳 {_fmt_amount(live['Карта'])} | 💵 {_fmt_amount(live['Наличные'])}"
                    )
                else:
                    # Расход — тоже с категорией (если есть) и описанием
                    group_msg = (
                        f"📤 Расход: {source_emoji} -{_fmt_amount(amount)}" +
                        (f" — {category}" if category and category != "-" else "") +
                        (desc_q) + "\n" +
                        f"Баланс: 💳 {_fmt_amount(live['Карта'])} | 💵 {_fmt_amount(live['Наличные'])}"
                    )
                await context.bot.send_message(chat_id=REMINDER_CHAT_ID, text=group_msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Ошибка отправки в группу: {e}")

        except Exception as e:
            logger.error(f"Ошибка записи: {e}")
            await update.message.reply_text("⚠️ Ошибка записи в таблицу.")
        return
    # ===== СОЗДАНИЕ АВТО =====
    if context.user_data.get("action") == "create_car":
        step = context.user_data.get("step")

        # 1) Название
        if step == "car_name":
            name = (text or "").strip()
            if not name:
                await update.message.reply_text("⚠️ Введите название, например: Mazda 3")
                return
            context.user_data["car_name"] = name
            context.user_data["step"] = "car_vin"
            await update.message.reply_text("Введите *VIN* (17 символов, латиница+цифры):", parse_mode="Markdown")
            return

        # 2) VIN
        if step == "car_vin":
            vin = (text or "").strip().upper().replace(" ", "")
            # Базовая валидация (длина 17 и без I/O/Q)
            bad = set("IOQ")
            if len(vin) != 17 or any(ch in bad for ch in vin):
                await update.message.reply_text("⚠️ VIN должен быть 17 символов, без I/O/Q. Попробуйте снова.")
                return
            context.user_data["car_vin"] = vin
            context.user_data["step"] = "car_plate"
            await update.message.reply_text("Введите *госномер* (как в техпаспорте):", parse_mode="Markdown")
            return

        # 3) Номер (госномер) -> запись в таблицу
        if step == "car_plate":
            plate = (text or "").strip().upper()
            if not plate:
                await update.message.reply_text("⚠️ Введите госномер.")
                return
            context.user_data["car_plate"] = plate

            # Записываем в Google Sheets
            try:
                client = get_gspread_client()
                ws = client.open_by_key(SPREADSHEET_ID).worksheet("Автомобили")

                new_id = datetime.datetime.now().strftime("car_%Y%m%d_%H%M%S")
                now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

                row = [
                    new_id,                          # A: ID
                    context.user_data["car_name"],   # B: Название
                    context.user_data["car_vin"],    # C: VIN
                    context.user_data["car_plate"],  # D: Номер
                    now,                             # E: Дата создания
                ]
                ws.append_row(row, value_input_option="USER_ENTERED", table_range="A:E")

                # Ответ пользователю
                msg = (
                    "✅ Авто создано:\n"
                    f"ID: {new_id}\n"
                    f"Название: {context.user_data['car_name']}\n"
                    f"VIN: {context.user_data['car_vin']}\n"
                    f"Номер: {context.user_data['car_plate']}\n"
                    f"Дата: {now}"
                )
                context.user_data.clear()
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ К списку автомобилей", callback_data="cars")],
                    [InlineKeyboardButton("⬅️ Главное меню", callback_data="menu")],
                ])
                await update.message.reply_text(msg, reply_markup=kb)

            except Exception as e:
                logger.error(f"Ошибка создания авто: {e}")
                await update.message.reply_text("⚠️ Не удалось создать автомобиль. Проверьте лист «Автомобили».")
            return    

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
