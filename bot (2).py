    import os
    import json
    import base64
    import logging
    import datetime
    import re
    import asyncio
    from typing import Dict, List, Tuple, Optional

    import gspread
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

    # -----------------------------
    # Logging & Constants
    # -----------------------------
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    TELEGRAM_TOKEN: Optional[str] = os.getenv("Telegram_Token")
    GOOGLE_CREDENTIALS_B64: Optional[str] = os.getenv("GOOGLE_CREDENTIALS_B64")

    # Chat where reminders are posted
    REMINDER_CHAT_ID: int = -1002522776417

    # Google Sheets
    SPREADSHEET_ID: str = "1qjVJZUqm1hT5IkrASq-_iL9cc4wDl8fdjvd7KDMWL-U"

    # -----------------------------
    # Google Sheets helpers
    # -----------------------------
    def get_gspread_client() -> gspread.Client:
        """
        Build an authorized gspread client using base64-encoded service account JSON
        from GOOGLE_CREDENTIALS_B64 env var.
        """
        if not GOOGLE_CREDENTIALS_B64:
            raise RuntimeError("GOOGLE_CREDENTIALS_B64 is not set")

        creds_json = base64.b64decode(GOOGLE_CREDENTIALS_B64).decode("utf-8")
        creds_dict = json.loads(creds_json)
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        return gspread.authorize(creds)

    def get_data() -> Dict[str, str]:
        """
        Read key-value pairs from the 'Сводка' sheet and return as a dict.
        """
        try:
            client = get_gspread_client()
            sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Сводка")
            rows = sheet.get_all_values()
            return {row[0].strip(): row[1].strip() for row in rows if len(row) >= 2}
        except Exception as e:
            logger.error(f"Ошибка получения данных: {e}")
            return {}

    # -----------------------------
    # Keyboards
    # -----------------------------
    def persistent_menu_keyboard() -> ReplyKeyboardMarkup:
        """Static reply keyboard with 'Меню' button under the input field."""
        return ReplyKeyboardMarkup(
            keyboard=[["Меню"]],
            resize_keyboard=True,
            one_time_keyboard=False,
        )

    def cancel_keyboard() -> InlineKeyboardMarkup:
        """Single inline 'Отмена' button."""
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]
        )

    # -----------------------------
    # Commands & Handlers
    # -----------------------------
    async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Show main inline menu and also add 'Меню' keyboard below the input.
        Works for both /menu command and when invoked from callbacks.
        """
        inline_keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📊 Баланс", callback_data="balance")],
                [
                    InlineKeyboardButton("📥 Доход", callback_data="add_income"),
                    InlineKeyboardButton("📤 Расход", callback_data="add_expense"),
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
            # Just show the reply keyboard as a separate message (empty text)
            await update.message.reply_text("", reply_markup=reply_kb)
        elif update.callback_query:
            await update.callback_query.edit_message_text(
                "Выберите действие:", reply_markup=inline_keyboard
            )
            await update.callback_query.message.reply_text("", reply_markup=reply_kb)

    async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handle all inline button callbacks (callback_data).
        """
        query = update.callback_query
        await query.answer()
        data = query.data

        # Universal cancel & back to menu
        if data in {"cancel", "menu"}:
            context.user_data.clear()
            await menu_command(update, context)
            return

        # ---- Add Income (category selection then amount) ----
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
            return

        if data in {"cat_franky", "cat_fraiz", "cat_other"}:
            category_map = {
                "cat_franky": "Franky",
                "cat_fraiz": "Fraiz",
                "cat_other": "Другое",
            }
            context.user_data["action"] = "income"
            context.user_data["category"] = category_map[data]
            context.user_data["step"] = "amount"
            await query.edit_message_text(
                "Введите сумму дохода:", reply_markup=cancel_keyboard()
            )
            return

        # ---- Add Expense ----
        if data == "add_expense":
            context.user_data.clear()
            context.user_data["action"] = "expense"
            context.user_data["step"] = "amount"
            await query.edit_message_text(
                "Введите сумму расхода:", reply_markup=cancel_keyboard()
            )
            return

        # ---- Source selection (card/cash) ----
        if data == "source_card":
            context.user_data["source"] = "Карта"
            context.user_data["step"] = "description"
            await query.edit_message_text("Введите описание:")
            return

        if data == "source_cash":
            context.user_data["source"] = "Наличные"
            context.user_data["step"] = "description"
            await query.edit_message_text("Введите описание:")
            return

        # ---- Insurance list ----
        if data == "insurance":
            try:
                sheet = get_gspread_client().open_by_key(SPREADSHEET_ID).worksheet("Страховки")
                rows = sheet.get_all_values()[1:]  # skip header
                if not rows:
                    await query.edit_message_text(
                        "🚗 Страховки не найдены.",
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("⬅️ Назад", callback_data="menu")]]
                        ),
                    )
                    return

                text = "🚗 Страховки:
"
                today = datetime.datetime.now().date()
                for i, row in enumerate(rows):
                    name = row[0] if len(row) > 0 else ""
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
                    text += f"{i+1}. {name} до {date_str or '—'} ({days_left})
"

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
            return

        # ---- Tech inspections list ----
        if data == "tech":
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

                text = "🧰 Тех.Осмотры:
"
                today = datetime.datetime.now().date()
                for i, row in enumerate(rows):
                    name = row[0] if len(row) > 0 else ""
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
                    text += f"{i+1}. {name} до {date_str or '—'} ({days_left})
"

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
            return

        # ---- Edit single date (insurance/tech) ----
        if data == "edit_insurance":
            context.user_data["edit_type"] = "insurance"
            await query.edit_message_text(
                "Введите название машины и дату через тире (Пример: Toyota - 01.09.2025)",
                reply_markup=cancel_keyboard(),
            )
            return

        if data == "edit_tech":
            context.user_data["edit_type"] = "tech"
            await query.edit_message_text(
                "Введите название машины и дату через тире (Пример: BMW - 15.10.2025)",
                reply_markup=cancel_keyboard(),
            )
            return

        # ---- Balance ----
        if data == "balance":
            try:
                summary = get_data()
                text = (
                    f"💼 Баланс: {summary.get('Баланс', '—')}
"
                    f"💳 Карта: {summary.get('Карта', '—')}
"
                    f"💵 Наличные: {summary.get('Наличные', '—')}"
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
            return

        # ---- Reports (7/30) ----
        if data in {"report_7", "report_30"}:
            days = 7 if data == "report_7" else 30
            try:
                client = get_gspread_client()
                now = datetime.datetime.now()
                start_date = now - datetime.timedelta(days=days)

                def get_sum(sheet_name: str, is_income: bool) -> float:
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
                                amount_str = (card or cash or "0").replace(" ", "").replace(",", ".")
                                amount = float(amount_str) if amount_str else 0.0
                                total += amount
                        except Exception as e:
                            logger.warning(f"Ошибка строки: {row} — {e}")
                            continue
                    return total

                income_total = get_sum("Доход", True)
                expense_total = get_sum("Расход", False)
                net_income = income_total - expense_total

                text = (
                    f"📅 Отчёт за {days} дней:

"
                    f"📥 Доход: {income_total:,.2f}
"
                    f"📤 Расход: {expense_total:,.2f}
"
                    f"💰 Чистый доход: {net_income:,.2f}"
                )

                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "📋 Подробности",
                                callback_data=f"report_{days}_details_page0",
                            )
                        ],
                        [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
                    ]
                )
                await query.edit_message_text(text, reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Ошибка получения отчёта: {e}")
                await query.message.reply_text("⚠️ Не удалось загрузить отчёт.")
            return

        # ---- Report details navigation ----
        if re.match(r"report_(7|30)_details_page(\d+)", data):
            m = re.match(r"report_(7|30)_details_page(\d+)", data)
            days = int(m.group(1))
            page = int(m.group(2))
            context.user_data["report_days"] = days
            context.user_data["report_page"] = page

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📥 Доходы", callback_data=f"report_{days}_details_income_page{page}"
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "📤 Расходы",
                            callback_data=f"report_{days}_details_expense_page{page}",
                        )
                    ],
                    [InlineKeyboardButton("⬅️ Назад", callback_data=f"report_{days}")],
                ]
            )
            await query.edit_message_text("Выберите подробности:", reply_markup=keyboard)
            return

        if re.match(r"report_(7|30)_details_(income|expense)_page(\d+)", data):
            m = re.match(r"report_(7|30)_details_(income|expense)_page(\d+)", data)
            days = int(m.group(1))
            detail_type = m.group(2)
            page = int(m.group(3))

            try:
                client = get_gspread_client()
                now = datetime.datetime.now()
                start_date = now - datetime.timedelta(days=days)

                sheet_name = "Доход" if detail_type == "income" else "Расход"
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)
                rows = sheet.get_all_values()[1:]
                filtered: List[List[str]] = []
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
                page = max(0, min(page, total_pages - 1)) if total_pages > 0 else 0
                page_rows = filtered[page * page_size : (page + 1) * page_size]

                lines: List[str] = []
                for r in page_rows:
                    date = r[0] if len(r) > 0 else "-"
                    if detail_type == "income":
                        category = r[1] if len(r) > 1 else "-"
                        card = r[2] if len(r) > 2 else ""
                        cash = r[3] if len(r) > 3 else ""
                        desc = r[4] if len(r) > 4 else "-"
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
                        category_icon = "🛠️" if category.strip().lower() == "другое" else "🚗"
                        lines.append(
                            f"📅 {date} | {category_icon} {category} | 🟢 {source_emoji} {amount} | 📝 {desc}"
                        )
                    else:
                        card = r[1] if len(r) > 1 else ""
                        cash = r[2] if len(r) > 2 else ""
                        desc = r[3] if len(r) > 3 else "-"
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

                header = "Доходов" if detail_type == "income" else "Расходов"
                text = f"📋 Подробности ({header}) за {days} дней:

"
                text += "
".join(lines) if lines else "Данные не найдены."

                buttons: List[InlineKeyboardButton] = []
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
                        buttons if buttons else [InlineKeyboardButton("—", callback_data="menu")],
                        [InlineKeyboardButton("⬅️ Назад", callback_data=f"report_{days}_details_page0")],
                        [InlineKeyboardButton("⬅️ Главное меню", callback_data="menu")],
                    ]
                )
                await query.edit_message_text(text, reply_markup=keyboard)
            except Exception as e:
                logger.error(f"Ошибка загрузки подробностей отчёта: {e}")
                await query.message.reply_text("⚠️ Не удалось загрузить подробности отчёта.")
            return

    async def on_menu_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handle 'Меню' from the persistent reply keyboard. Open main menu.
        """
        await menu_command(update, context)

    async def handle_amount_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handle user text input during multi-step flows:
        - Amount input
        - Description input
        - Edit insurance/tech dates
        """
        if not update.message:
            return

        text = update.message.text.strip()

        # Allow user to cancel by typing 'отмена'
        if text.lower() == "отмена":
            context.user_data.clear()
            await update.message.reply_text("❌ Отменено.")
            await menu_command(update, context)
            return

        # Process edit date flow
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

                # Find row by name (case-insensitive) and update date in column B (2)
                for i, row in enumerate(rows, start=1):
                    if row and row[0].strip().lower() == name.lower():
                        sheet.update_cell(i, 2, new_date)
                        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="menu")]])
                        await update.message.reply_text(
                            f"✅ Дата обновлена:\n{name} — {new_date}",
                            reply_markup=keyboard,
                        )
                        return

                await update.message.reply_text("🚫 Машина не найдена.")
            except Exception as e:
                logger.error(f"Ошибка при обновлении: {e}")
                await update.message.reply_text("❌ Ошибка обновления.")
            return

        # Multi-step add income/expense flow
        action = context.user_data.get("action")
        step = context.user_data.get("step")
        if not action or not step:
            return

        if step == "amount":
            try:
                amount = float(text.replace(",", ".").replace(" ", ""))
                if amount <= 0:
                    raise ValueError("Сумма должна быть положительной")
                context.user_data["amount"] = amount
                context.user_data["step"] = "source"
                keyboard = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("💳 Карта", callback_data="source_card")],
                        [InlineKeyboardButton("💵 Наличные", callback_data="source_cash")],
                        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
                    ]
                )
                await update.message.reply_text("Выберите источник:", reply_markup=keyboard)
            except ValueError:
                await update.message.reply_text("⚠️ Введите положительное число (пример: 1200.50)")
            return

        if step == "description":
            description = text
            now_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
            amount = context.user_data.get("amount")
            category = context.user_data.get("category", "-")
            source = context.user_data.get("source", "-")

            try:
                client = get_gspread_client()

                if action == "income":
                    sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
                    row = [now_str, category, "", "", description]  # C=card, D=cash
                    if source == "Карта":
                        row[2] = amount  # C
                    else:
                        row[3] = amount  # D
                    sheet.append_row(row)

                    text_msg = (
                        f"✅ Добавлено в *Доход*:\n"
                        f"📅 {now_str}\n"
                        f"🏷 {category}\n"
                        f"💰 {amount} ({source})\n"
                        f"📝 {description}"
                    )
                else:
                    sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")
                    row = [now_str, "", "", description]  # B=card, C=cash
                    if source == "Карта":
                        row[1] = amount  # B
                    else:
                        row[2] = amount  # C
                    sheet.append_row(row)

                    text_msg = (
                        f"✅ Добавлено в *Расход*:\n"
                        f"📅 {now_str}\n"
                        f"💸 -{amount} ({source})\n"
                        f"📝 {description}"
                    )

                summary = get_data()
                text_msg += (
                    f"\n\n📊 Баланс:\n"
                    f"💼 {summary.get('Баланс', '—')}\n"
                    f"💳 {summary.get('Карта', '—')}\n"
                    f"💵 {summary.get('Наличные', '—')}"
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

                context.user_data.clear()
                await update.message.reply_text(text_msg, reply_markup=keyboard, parse_mode="Markdown")

                # Duplicate message to group
                try:
                    await context.bot.send_message(
                        chat_id=REMINDER_CHAT_ID, text=text_msg, parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Ошибка отправки в группу: {e}")

            except Exception as e:
                logger.error(f"Ошибка записи: {e}")
                await update.message.reply_text("⚠️ Ошибка записи в таблицу.")

    # -----------------------------
    # Reminders background task
    # -----------------------------
    async def check_reminders(app) -> None:
        """
        Periodically (every 24h) check 'Страховки' and 'ТехОсмотры' for items
        whose deadlines are within 7 days or overdue, and post notifications
        to REMINDER_CHAT_ID.
        """
        while True:
            try:
                client = get_gspread_client()
                now = datetime.datetime.now().date()
                remind_before_days = 7

                def collect(sheet_name: str) -> List[Tuple[str, datetime.date, int]]:
                    sheet = client.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)
                    rows = sheet.get_all_values()[1:]  # skip header
                    reminders: List[Tuple[str, datetime.date, int]] = []
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

                insurance_reminders = collect("Страховки")
                tech_reminders = collect("ТехОсмотры")

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

            await asyncio.sleep(86400)  # 24h

    async def on_startup(app) -> None:
        asyncio.create_task(check_reminders(app))

    def main() -> None:
        if not TELEGRAM_TOKEN:
            raise RuntimeError("Telegram_Token is not set")

        application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

        # Commands & callbacks
        application.add_handler(CommandHandler("menu", menu_command))
        application.add_handler(CallbackQueryHandler(handle_button))
        application.add_handler(MessageHandler(filters.Regex(r"^(Меню)$"), on_menu_button_pressed))

        # Text handler for amounts/descriptions and edit flows
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount_description))

        # Start background reminders after the bot is initialized
        application.post_init = on_startup

        # Run bot
        application.run_polling()

    if __name__ == "__main__":
        main()
