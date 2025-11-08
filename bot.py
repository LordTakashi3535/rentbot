import os
import json
import base64
import logging
import gspread
import datetime
import re
import asyncio

from decimal import Decimal, ROUND_HALF_UP

DATE_FMT = "%d.%m.%Y %H:%M"  # как пишем в листы

def _parse_dt_safe(s: str):
    """Пытаемся распарсить 'ДД.ММ.ГГГГ ЧЧ:ММ' или 'ДД.ММ.ГГГГ'. Возвращаем datetime или None."""
    s = (s or "").strip()
    for fmt in (DATE_FMT, "%d.%m.%Y"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None

WORKSHOP_SHEET = "Мастерская"
WORKSHOP_HEADERS = ["ID", "Название", "VIN", "Создано"]

SERVICES_SHEET   = "Услуги"
SERVICES_HEADERS = ["ID", "CarID", "Название", "VIN", "Дата", "Сумма", "Описание"]


FREEZE_SHEET   = "Заморозка"
FREEZE_HEADERS = ["ID", "CarID", "Название", "VIN", "Дата", "Источник", "Сумма", "Описание"]
# индексы по именам будем искать безопасно

def _ensure_freeze_ws(client):
    ws = ensure_ws_with_headers(client, FREEZE_SHEET, FREEZE_HEADERS)
    # миграция: если нет "Источник", вставим колонку F
    try:
        header = ws.row_values(1)
        norm = [h.strip().lower() for h in header]
        if "источник" not in norm:
            # вставим новую колонку на позицию 6 (после "Дата")
            ws.insert_cols([["Источник"]], col=6)
            # убеждаемся что шапка корректная
            new_header = ws.row_values(1)
            # если шапка пустая/короче — обновим полностью
            if len(new_header) < len(FREEZE_HEADERS):
                ws.update("A1:H1", [FREEZE_HEADERS])
    except Exception as e:
        logger.error(f"freeze sheet migrate error: {e}")
    return ws

def _norm_source(s: str) -> str:
    s = (s or "").strip().lower()
    # любые варианты "нал", "наличка", "наличные"
    if s.startswith("нал") or "налич" in s:
        return "Наличные"
    # любые варианты "кар", "карта", возможно visa/master
    if s.startswith("кар") or "visa" in s or "master" in s:
        return "Карта"
    # если пришло "cash"/"card"
    if s in ("cash", "нал", "nal"):  # на всякий
        return "Наличные"
    if s in ("card", "kart", "karta"):
        return "Карта"
    return s.capitalize() if s else ""


def get_frozen_breakdown_for_car(client, car_id: str):
    ws = _ensure_freeze_ws(client)
    rows = ws.get_all_values()
    from decimal import Decimal
    if not rows:
        return {"card": Decimal("0"), "cash": Decimal("0"), "total": Decimal("0"), "count": 0}
    idx = _freeze_idx(rows[0])
    card = Decimal("0"); cash = Decimal("0"); cnt = 0
    for r in rows[1:]:
        if not r: 
            continue
        if idx["carid"] is None or idx["carid"] >= len(r):
            continue
        if (r[idx["carid"]] or "").strip() != car_id:
            continue
        amt = _to_amount(r[idx["amount"]]) if (idx["amount"] is not None and idx["amount"] < len(r)) else Decimal("0")
        src_raw = (r[idx["source"]] if (idx["source"] is not None and idx["source"] < len(r)) else "").strip()
        src = _norm_source(src_raw)  # ← НОРМАЛИЗАЦИЯ
        if src == "Карта":
            card += amt
        elif src == "Наличные":
            cash += amt
        else:
            # неизвестный/пустой источник — не считаем по источникам, но учитываем в total
            pass
        cnt += 1
    return {"card": card, "cash": cash, "total": card + cash, "count": cnt}

def _ensure_services_ws(client):
    return ensure_ws_with_headers(client, SERVICES_SHEET, SERVICES_HEADERS)

def get_services_recent_for_car(client, car_id: str, limit: int = 5):
    """
    Возвращает последние N услуг по машине (по порядку добавления).
    Формат элемента: (дата_str, Decimal сумма, описание_str)
    """
    ws = _ensure_services_ws(client)
    rows = ws.get_all_values()[1:]
    items = []
    for r in rows:
        if not r:
            continue
        if (r[1] or "").strip() != car_id:
            continue
        date_str = (r[4] if len(r) > 4 else "") or ""
        amount   = _to_amount(r[5] if len(r) > 5 else "0")
        desc     = (r[6] if len(r) > 6 else "") or "-"
        items.append((date_str, amount, desc))
    # последние по добавлению (мы дописываем в конец) → просто берём с конца
    items = items[-limit:][::-1]
    return items

def get_services_for_car(client, car_id: str):
    """
    Все услуги по машине (без даты).
    Возвращает список элементов (Decimal amount, str desc) в порядке добавления.
    """
    ws = _ensure_services_ws(client)
    rows = ws.get_all_values()[1:]
    items = []
    for r in rows:
        if not r:
            continue
        if (r[1] or "").strip() != car_id:
            continue
        amount = _to_amount(r[5] if len(r) > 5 else "0")
        desc   = (r[6] if len(r) > 6 else "") or "-"
        items.append((amount, desc))
    return items


def get_services_total_for_car(client, car_id: str) -> Decimal:
    ws = _ensure_services_ws(client)
    rows = ws.get_all_values()[1:]
    total = Decimal("0")
    for r in rows:
        if not r:
            continue
        if (r[1] or "").strip() == car_id:
            total += _to_amount(r[5] if len(r) > 5 else "0")
    return total

def _freeze_idx(header: list[str]) -> dict:
    # по именам, без регистра/пробелов; с fallback по длине
    norm = { (h or "").strip().lower(): i for i, h in enumerate(header) }
    def gi(key, default=None):
        return norm.get(key, default)

    amount_i = gi("сумма")
    desc_i   = gi("описание")
    src_i    = gi("источник")

    # если названий нет — падаем на позиционную схему
    if amount_i is None and len(header) >= 7:
        # старая схема: [ID,CarID,Название,VIN,Дата,Сумма,Описание]
        amount_i = 5
        desc_i   = 6 if len(header) > 6 else None
    if amount_i is None and len(header) >= 8:
        # новая схема: [ID,CarID,Название,VIN,Дата,Источник,Сумма,Описание]
        amount_i = 6
        desc_i   = 7 if len(header) > 7 else None

    return {
        "carid":  gi("carid", 1),
        "name":   gi("название", 2),
        "vin":    gi("vin", 3),
        "date":   gi("дата", 4),
        "source": src_i,          # может быть None (в старых строках)
        "amount": amount_i,
        "desc":   desc_i,
    }


def get_frozen_for_car(client, car_id: str) -> Decimal:
    ws = _ensure_freeze_ws(client)
    rows = ws.get_all_values()
    if not rows:
        return Decimal("0")
    idx = _freeze_idx(rows[0])
    total = Decimal("0")
    for r in rows[1:]:
        if not r: 
            continue
        if idx["carid"] is not None and idx["carid"] < len(r) and (r[idx["carid"]] or "").strip() == car_id:
            if idx["amount"] is not None and idx["amount"] < len(r):
                total += _to_amount(r[idx["amount"]])
    return total

def get_frozen_by_car(client):
    ws = _ensure_freeze_ws(client)
    rows = ws.get_all_values()
    if not rows:
        return [], Decimal("0")
    idx = _freeze_idx(rows[0])
    by = {}
    for r in rows[1:]:
        if not r:
            continue
        if idx["carid"] is None or idx["carid"] >= len(r): 
            continue
        car_id = (r[idx["carid"]] or "").strip()
        if not car_id:
            continue
        name = (r[idx["name"]] if idx["name"] is not None and idx["name"] < len(r) else "").strip() or "(без названия)"
        amt  = _to_amount(r[idx["amount"]]) if (idx["amount"] is not None and idx["amount"] < len(r)) else Decimal("0")
        by.setdefault(car_id, [name, Decimal("0")])
        by[car_id][1] += amt
    items = [(cid, nm, sm) for cid, (nm, sm) in by.items()]
    items.sort(key=lambda t: t[2], reverse=True)
    total = sum((sm for _, _, sm in items), Decimal("0"))
    return items, total

def get_frozen_totals(client):
    ws = _ensure_freeze_ws(client)
    rows = ws.get_all_values()
    if not rows:
        return {"card": Decimal("0"), "cash": Decimal("0"), "total": Decimal("0")}
    idx = _freeze_idx(rows[0])
    card = Decimal("0"); cash = Decimal("0")
    for r in rows[1:]:
        if not r:
            continue
        amt = _to_amount(r[idx["amount"]]) if (idx["amount"] is not None and idx["amount"] < len(r)) else Decimal("0")
        raw_src = (r[idx["source"]] if (idx["source"] is not None and idx["source"] < len(r)) else "").strip()
        src = _norm_source(raw_src)  # ← нормализуем перед сравнением
        if src == "Карта":
            card += amt
        elif src == "Наличные":
            cash += amt
        else:
            # старые/пустые значения источника игнорим в разрезе, но total посчитается из суммы card+cash
            pass
    return {"card": card, "cash": cash, "total": card + cash}

def ensure_ws_with_headers(client, sheet_name: str, headers: list[str]):
    ws = client.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)
    rows = ws.get_all_values()
    if not rows:
        # создаём шапку 1-й строкой
        last_col_letter = chr(64 + len(headers))  # D для 4-х колонок
        ws.update(f"A1:{last_col_letter}1", [headers])
    else:
        # мягко дополним недостающие заголовки
        header = rows[0]
        for i, h in enumerate(headers, start=1):
            cur = header[i-1].strip() if i-1 < len(header) else ""
            if not cur:
                ws.update_cell(1, i, h)
    return ws

def _safe_idx(header: list[str]) -> dict:
    return { (h or "").strip(): i for i, h in enumerate(header) }

def _get_row_by_id(ws, car_id: str):
    rows = ws.get_all_values()
    if not rows or len(rows) < 2:
        return None, None, None  # row, header, idx
    header = rows[0]
    for r in rows[1:]:
        if r and (r[0] or "").strip() == car_id:
            return r, header, _safe_idx(header)
    return None, header, _safe_idx(header)

# === Dynamic Categories & Records ===
from typing import Optional, List, Dict, Tuple, Union

INCOME_SHEET = "Доход"    # если назвал лист иначе — поменяй тут
EXPENSE_SHEET = "Расход"  # если назвал лист иначе — поменяй тут

def get_cats_ws(client):
    return client.open_by_key(SPREADSHEET_ID).worksheet("Категории")
    
def _parse_money(s: str) -> float:
    s = (s or "").strip().replace(",", ".")
    return float(s) if s else 0.0

def list_categories(kind: str):
    """Активные категории ('Доход'/'Расход') -> [{ID, Название, Порядок}]"""
    client = get_gspread_client()
    ws = get_cats_ws(client)
    rows = ws.get_all_values()
    if not rows or len(rows) < 2:
        return []
    header = rows[0]
    idx = {h.strip(): i for i, h in enumerate(header)}
    out = []
    for r in rows[1:]:
        if not r: 
            continue
        if not all(k in idx for k in ("ID","Тип","Название","Активна")):
            continue
        if any(idx[k] >= len(r) for k in ("ID","Тип","Название","Активна")):
            continue
        if r[idx["Тип"]].strip() != kind:
            continue
        if r[idx["Активна"]].strip() != "1":
            continue
        order = 0
        if "Порядок" in idx and idx["Порядок"] < len(r):
            s = (r[idx["Порядок"]] or "").strip()
            if s and s.lstrip("-").isdigit():
                order = int(s)
        out.append({"ID": r[idx["ID"]].strip(), "Название": r[idx["Название"]].strip(), "Порядок": order})
    out.sort(key=lambda x: (x["Порядок"], x["Название"].lower()))
    return out

def get_all_categories(kind: str):
    """ВСЕ категории данного типа ('Доход'/'Расход'), включая неактивные."""
    client = get_gspread_client()
    ws = get_cats_ws(client)
    rows = ws.get_all_values()
    if not rows or len(rows) < 2:
        return []
    header = rows[0]
    idx = {h.strip(): i for i, h in enumerate(header)}
    out = []
    for r in rows[1:]:
        if not r:
            continue
        if not all(k in idx for k in ("ID", "Тип", "Название", "Активна")):
            continue
        if any(idx[k] >= len(r) for k in ("ID","Тип","Название","Активна")):
            continue
        if r[idx["Тип"]].strip() != kind:
            continue
        out.append({
            "ID": r[idx["ID"]].strip(),
            "Название": r[idx["Название"]].strip(),
            "Активна": r[idx["Активна"]].strip(),
            "_row": r,  # на всякий
        })
    return out

def delete_category(cat_id: str) -> bool:
    """Удаляет строку категории по ID. Возвращает True/False."""
    client = get_gspread_client()
    ws = get_cats_ws(client)
    rows = ws.get_all_values()
    if not rows or len(rows) < 2:
        return False
    header = rows[0]
    try:
        id_idx = header.index("ID")
    except ValueError:
        return False
    for i, r in enumerate(rows[1:], start=2):
        if id_idx < len(r) and (r[id_idx] or "").strip() == cat_id.strip():
            ws.delete_rows(i)
            return True
    return False

def _aggregate_by_category(rows):
    by = {}
    for r in rows:
        cat = r[2] if len(r) > 2 and r[2].strip() else "—"
        # ❗ прячем переводы
        if cat.strip().lower() == "перевод":
            continue
        card = _to_amount(r[3] if len(r) > 3 else "")
        cash = _to_amount(r[4] if len(r) > 4 else "")
        by[cat] = by.get(cat, Decimal("0")) + (card + cash)
    return sorted(by.items(), key=lambda x: x[1], reverse=True)

def _sum_sheet_period(client, sheet_name: str, days: int, exclude_transfers: bool = False):
    """
    Возвращает (total_card, total_cash, rows_filtered)
    rows_filtered — строки, попавшие в диапазон по дате (последние N дней).
    Формат строк: [Дата, КатID, Кат, 💳, 💵, 📝]
    """
    ws = client.open_by_key(SPREADSHEET_ID).worksheet(sheet_name)
    rows = ws.get_all_values()[1:]
    now = datetime.datetime.now()
    start_date = now - datetime.timedelta(days=days)

    total_card = Decimal("0")
    total_cash = Decimal("0")
    filtered = []

    for r in rows:
        if not r:
            continue
        dt = _parse_dt_safe(r[0] if len(r) > 0 else "")
        if not dt or dt < start_date:
            continue

        # 🚫 пропускаем переводы в отчётах (категория == "Перевод")
        if exclude_transfers and (len(r) > 2) and (r[2] or "").strip().lower() == "перевод":
            continue

        card = _to_amount(r[3] if len(r) > 3 else "")
        cash = _to_amount(r[4] if len(r) > 4 else "")
        total_card += card
        total_cash += cash
        filtered.append(r)

    return total_card, total_cash, filtered



def _render_detail_line(r: list, is_income: bool) -> str:
    dt   = r[0] if len(r) > 0 else ""
    cat  = r[2] if len(r) > 2 else "-"
    card = r[3] if len(r) > 3 else ""
    cash = r[4] if len(r) > 4 else ""
    desc = r[5] if len(r) > 5 else "-"
    total = _to_amount(card) + _to_amount(cash)

    if is_income:
        return f"📅 {dt} | 🚗 {cat} | 🟢 {_fmt_amount(total)} (💳 {card or '0'} | 💵 {cash or '0'}) | 📝 {desc}"
    else:
        return f"📅 {dt} | 🚗 {cat} | 🔴 -{_fmt_amount(total)} (💳 {card or '0'} | 💵 {cash or '0'}) | 📝 {desc}"    

def get_category_name(cat_id: str) -> str:
    client = get_gspread_client()
    ws = get_cats_ws(client)
    rows = ws.get_all_values()
    if not rows: 
        return cat_id
    header = rows[0]
    idx = {h.strip(): i for i, h in enumerate(header)}
    if "ID" not in idx:
        return cat_id
    for r in rows[1:]:
        if idx["ID"] < len(r) and (r[idx["ID"]] or "").strip() == cat_id:
            if "Название" in idx and idx["Название"] < len(r):
                nm = (r[idx["Название"]] or "").strip()
                return nm or cat_id
            return cat_id
    return cat_id

def add_category(kind: str, name: str) -> str:
    """Создать категорию (Активна=1, Порядок=0). Возвращает cat_id."""
    client = get_gspread_client()
    ws = get_cats_ws(client)
    rows = ws.get_all_values()
    if not rows:
        ws.append_row(["ID","Тип","Название","Активна","Порядок"])
    cat_id = "cat_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    ws.append_row([cat_id, kind.strip(), name.strip(), "1", "0"])
    return cat_id

def ensure_default_category(kind: str) -> tuple[str, str]:
    """Гарантируем активную категорию 'Другое' для указанного типа."""
    cats = list_categories(kind)
    for c in cats:
        if c["Название"].strip().lower() == "другое":
            return c["ID"], c["Название"]
    cat_id = add_category(kind, "Другое")
    return cat_id, "Другое"

# ---- листы Доход/Расход со столбцами: Дата | КатегорияID | Категория | 💳 Карта | 💵 Наличные | 📝 Описание
INOUT_HEADERS = ["Дата", "КатегорияID", "Категория", "💳 Карта", "💵 Наличные", "📝 Описание"]

def ensure_sheet_headers(ws, headers: list[str]):
    rows = ws.get_all_values()
    if not rows:
        ws.append_row(headers)

def _fmt_amount(x): return f"{float(x):.2f}"

def append_income(category_id: str, category_name: str, card_amount: float, cash_amount: float, desc: str):
    client = get_gspread_client()
    ws = client.open_by_key(SPREADSHEET_ID).worksheet(INCOME_SHEET)
    ensure_sheet_headers(ws, INOUT_HEADERS)
    ws.append_row([
        datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
        category_id, category_name, _fmt_amount(card_amount), _fmt_amount(cash_amount), desc or "-",
    ])


def append_expense(category_id: str, category_name: str, card_amount: float, cash_amount: float, desc: str):
    client = get_gspread_client()
    ws = client.open_by_key(SPREADSHEET_ID).worksheet(EXPENSE_SHEET)
    ensure_sheet_headers(ws, INOUT_HEADERS)
    ws.append_row([
        datetime.datetime.now().strftime("%d.%m.%Y %H:%M"),
        category_id, category_name, _fmt_amount(card_amount), _fmt_amount(cash_amount), desc or "-",
    ])

def _parse_date_flex(s: str) -> Optional[datetime.date]:
    """Парсит 'ДД.ММ.ГГГГ' или 'ДД.ММ.ГГГГ ЧЧ:ММ'. Возвращает date или None."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None   

def _days_left_label(date_str: str) -> tuple[str, int | None]:
    """
    Возвращает метку 'осталось N дней' / 'сегодня' / 'просрочено N дней' и сам N (может быть <0),
    либо ('—', None) если даты нет, либо ('неверный формат', None) если не распарсили.
    """
    if not date_str:
        return "—", None
    d = _parse_date_flex(date_str)
    if not d:
        return "неверный формат", None
    today = datetime.date.today()
    delta = (d - today).days
    if delta > 0:
        return f"осталось {delta} дней", delta
    elif delta == 0:
        return "сегодня", 0
    else:
        return f"просрочено {abs(delta)} дней", delta

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

def _find_row_by_id(ws, car_id: str) -> int | None:
    """Вернёт индекс строки (2..N) по ID (первый столбец), иначе None."""
    rows = ws.get_all_values()
    if not rows:
        return None
    for i, r in enumerate(rows[1:], start=2):
        if r and r[0].strip() == car_id.strip():
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

# ---- KV в листе "Сводка": две колонки [Ключ | Значение] ----

def _summary_get(client, key: str, default: str = "") -> str:
    ws = client.open_by_key(SPREADSHEET_ID).worksheet("Сводка")
    rows = ws.get_all_values()
    for r in rows:
        if not r:
            continue
        if (r[0] or "").strip() == key:
            return (r[1] or "").strip() if len(r) > 1 else default
    return default

def _summary_set(client, key: str, value: str) -> None:
    ws = client.open_by_key(SPREADSHEET_ID).worksheet("Сводка")
    rows = ws.get_all_values()
    # попытка обновить существующую строку
    for i, r in enumerate(rows, start=1):
        if r and (r[0] or "").strip() == key:
            ws.update_cell(i, 2, value)  # кол. B = Значение
            return
    # иначе добавим строку
    ws.append_row([key, value], value_input_option="USER_ENTERED")

def get_initial_balance(client) -> Decimal:
    s = _summary_get(client, "INITIAL_BALANCE", "0")
    try:
        return _to_amount(s)
    except Exception:
        return Decimal("0")

def set_initial_balance(client, val: Decimal) -> None:
    _summary_set(client, "INITIAL_BALANCE", str(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)))



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
    - Доход/Расход: [Дата, КатID, Категория, 💳 D, 💵 E, 📝]
    - Наличные = SUM(Доход!E) - SUM(Расход!E)
    - Карта     = INITIAL_BALANCE + SUM(Доход!D) - SUM(Расход!D)
    - Баланс    = Карта + Наличные
    """
    income_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
    expense_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")

    income_rows = income_ws.get_all_values()[1:]
    expense_rows = expense_ws.get_all_values()[1:]

    income_card = Decimal("0")
    income_cash = Decimal("0")
    for r in income_rows:
        if len(r) > 3: income_card += _to_amount(r[3])
        if len(r) > 4: income_cash += _to_amount(r[4])

    expense_card = Decimal("0")
    expense_cash = Decimal("0")
    for r in expense_rows:
        if len(r) > 3: expense_card += _to_amount(r[3])
        if len(r) > 4: expense_cash += _to_amount(r[4])

    initial = get_initial_balance(client)

    cash  = income_cash - expense_cash
    card  = initial + income_card - expense_card
    total = card + cash

    return {"Баланс": total, "Карта": card, "Наличные": cash, "Начальная": initial}

def compute_summary(client):
    """
    Итоги по новому формату:
    - Доход = SUM(Доход!D:E)
    - Расход = SUM(Расход!D:E)
    - Наличные = SUM(Доход!E) - SUM(Расход!E)
    - Карта = INITIAL_BALANCE + SUM(Доход!D) - SUM(Расход!D)
    - Баланс = Карта + Наличные
    - Заработано (Чистая прибыль) = Доход - Расход
    """
    income_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
    expense_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")

    income_rows = income_ws.get_all_values()[1:]
    expense_rows = expense_ws.get_all_values()[1:]

    income_card = Decimal("0")
    income_cash = Decimal("0")
    for r in income_rows:
        if len(r) > 3: income_card += _to_amount(r[3])  # 💳
        if len(r) > 4: income_cash += _to_amount(r[4])  # 💵

    expense_card = Decimal("0")
    expense_cash = Decimal("0")
    for r in expense_rows:
        if len(r) > 3: expense_card += _to_amount(r[3])  # 💳
        if len(r) > 4: expense_cash += _to_amount(r[4])  # 💵

    income_total  = income_card + income_cash
    expense_total = expense_card + expense_cash

    # Динамическая начальная сумма, если функция есть; иначе — константа.
    try:
        initial = get_initial_balance(client)  # динамический вариант (из "Сводка")
    except NameError:
        try:
            initial = INITIAL_BALANCE          # старый вариант (константа в коде)
        except NameError:
            initial = Decimal("0")

    cash    = income_cash - expense_cash
    card    = initial + income_card - expense_card
    balance = card + cash

    earned  = income_total - expense_total  # <-- ЧИСТАЯ ПРИБЫЛЬ

    return {
        "Начальная": initial,
        "Доход": income_total,
        "Расход": expense_total,
        "Наличные": cash,
        "Карта": card,
        "Баланс": balance,
        "Заработано": earned,  # теперь это Доход − Расход
    } 

# Статичная клавиатура с кнопкой "Меню" под полем ввода
def persistent_menu_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[["Меню"]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

async def _show_categories_view(query, kind: str):
    cats = list_categories(kind)
    if not cats:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
        ])
        await query.edit_message_text(f"Нет категорий для {kind.lower()}а.", reply_markup=kb)
        return

    cbp = "income_cat" if kind == "Доход" else "expense_cat"
    buttons = [[InlineKeyboardButton(c["Название"], callback_data=f"{cbp}|{c['ID']}")] for c in cats]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu")])

    await query.edit_message_text(
        f"{'📥' if kind=='Доход' else '📤'} Категории {kind.lower()}:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# Показываем меню (inline кнопки) и добавляем кнопку "Меню" под полем ввода
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    inline_keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("📊 Баланс", callback_data="balance")],
    [InlineKeyboardButton("📥 Доход", callback_data="income"),
     InlineKeyboardButton("📤 Расход", callback_data="expense")],
    [InlineKeyboardButton("🔁 Перевод", callback_data="transfer"),
     InlineKeyboardButton("🚗 Автомобили", callback_data="cars")],
    [InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
     InlineKeyboardButton("🧰 Автомастерская", callback_data="workshop")],
    [InlineKeyboardButton("📈 Отчёт 7 дней", callback_data="report_7"),
     InlineKeyboardButton("📊 Отчёт 30 дней", callback_data="report_30")],
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

def back_or_cancel_keyboard(back_cb: str):
    """Клавиатура с кнопками 'Назад' и 'Отмена'."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад",  callback_data=back_cb)],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])    

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel" or data == "menu":
        context.user_data.clear()
        await menu_command(update, context)
        return

    elif data.startswith("workshop_services:"):
        # формат: workshop_services:<car_id>:<page>
        parts = data.split(":", 2)
        if len(parts) == 2:
            car_id = parts[1]
            page = 0
        else:
            car_id = parts[1]
            try:
                page = int(parts[2].replace("page", ""))
            except Exception:
                page = 0

        try:
            client = get_gspread_client()
            # подтянем имя авто для заголовка
            ws = ensure_ws_with_headers(client, WORKSHOP_SHEET, WORKSHOP_HEADERS)
            rows = ws.get_all_values()
            header = rows[0] if rows else []
            idx = {h.strip(): i for i, h in enumerate(header)}

            name = "(без названия)"
            for r in rows[1:]:
                if r and (r[0] or "").strip() == car_id:
                    name = (r[idx.get("Название", 1)] if len(r) > 1 else "") or "(без названия)"
                    break

            services = get_services_for_car(client, car_id)
            total = len(services)
            page_size = 20
            if total == 0:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Назад к машине", callback_data=f"workshop_view:{car_id}")],
                    [InlineKeyboardButton("⬅️ К списку", callback_data="workshop")],
                ])
                await query.edit_message_text(
                    f"🧰 *{name}*\n\nУслуги не найдены.",
                    reply_markup=kb, parse_mode="Markdown"
                )
                return

            max_page = (total - 1) // page_size
            page = max(0, min(page, max_page))
            start = page * page_size
            end = start + page_size
            chunk = services[start:end]

            lines = []
            for amt, desc in chunk:
                tail = f" — {desc}" if desc and desc != "-" else ""
                lines.append(f"• {_fmt_amount(amt)}{tail}")

            pager_line = f"Страница {page + 1}/{max_page + 1} • всего услуг: {total}"
            text = (
                f"🧰 *{name}*\n"
                f"📜 Все услуги\n\n" +
                "\n".join(lines) + "\n\n" + pager_line
            )

            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"workshop_services:{car_id}:page{page-1}"))
            if page < max_page:
                nav.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"workshop_services:{car_id}:page{page+1}"))

            kb = InlineKeyboardMarkup([
                nav] if nav else [[InlineKeyboardButton("• 1/1 •", callback_data=f"workshop_services:{car_id}:page0")],
                [InlineKeyboardButton("⬅️ К машине", callback_data=f"workshop_view:{car_id}")],
                [InlineKeyboardButton("⬅️ К списку", callback_data="workshop")],
            ])
            await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"workshop_services error: {e}")
            await query.message.reply_text("⚠️ Не удалось показать услуги.")
        return    

    elif data == "workshop":
        try:
            client = get_gspread_client()
            ws = ensure_ws_with_headers(client, WORKSHOP_SHEET, WORKSHOP_HEADERS)
            rows = ws.get_all_values()[1:]  # пропускаем шапку

            if not rows:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Добавить машину", callback_data="workshop_add")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
                ])
                await query.edit_message_text(
                    "🧰 *Автомастерская*\n\nСписок пуст.",
                    reply_markup=kb,
                    parse_mode="Markdown"
                )
                return

            # кнопки по машинам
            buttons = []
            for r in rows:
                if not r:
                    continue
                car_id = (r[0] or "").strip()
                name   = (r[1] or "").strip() or "(без названия)"
                buttons.append([InlineKeyboardButton(name, callback_data=f"workshop_view:{car_id}")])

            buttons.append([InlineKeyboardButton("➕ Добавить машину", callback_data="workshop_add")])
            buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu")])

            await query.edit_message_text(
                "🧰 *Автомастерская* — выберите машину:",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"workshop list error: {e}")
            await query.message.reply_text("⚠️ Не удалось открыть Автомастерскую.")
        return 

    elif data == "settings":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗂 Настройки категорий", callback_data="cat_settings")],
            [InlineKeyboardButton("💼 Настройки баланса", callback_data="balance_settings")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
        ])
        await query.edit_message_text("⚙️ Настройки:", reply_markup=kb)
        return

    elif data == "balance_settings":
        try:
            client = get_gspread_client()
            init = get_initial_balance(client)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Изменить начальную сумму", callback_data="balance_init_edit")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="settings")],
            ])
            await query.edit_message_text(
                f"💼 Настройки баланса\n\n"
                f"🏁 Текущая начальная сумма: {_fmt_amount(init)}",
                reply_markup=kb
            )
        except Exception as e:
            logger.error(f"balance_settings error: {e}")
            await query.edit_message_text("⚠️ Не удалось загрузить настройки баланса.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="settings")]]))
        return

    elif data == "balance_init_edit":
        # запускаем ввод значения
        context.user_data.clear()
        context.user_data["action"] = "balance_init_edit"
        context.user_data["return_cb"] = "balance_settings"
        await query.edit_message_text(
            "Введите новую начальную сумму (например 20000.00):",
            reply_markup=back_or_cancel_keyboard("balance_settings")
        )
        return

    elif data == "cat_settings":
        # Выбор типа категорий
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Доход", callback_data="cat_settings_kind|Доход")],
            [InlineKeyboardButton("📤 Расход", callback_data="cat_settings_kind|Расход")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="settings")],
        ])
        await query.edit_message_text("🗂 Что настраиваем?", reply_markup=kb)
        return

    elif data.startswith("cat_settings_kind|"):
        kind = data.split("|", 1)[1]  # 'Доход' или 'Расход'
        # список всех категорий (включая неактивные)
        cats = get_all_categories(kind)
        rows = []
        for c in cats:
            # Кнопка удаления для каждой категории
            rows.append([InlineKeyboardButton(f"🗑 {c['Название']}", callback_data=f"cat_del|{c['ID']}|{kind}")])

        # Кнопки "добавить" и "назад"
        rows.append([InlineKeyboardButton(f"➕ Добавить категорию для {kind.lower()}а", callback_data=f"cat_add|{kind}")])
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="cat_settings")])

        await query.edit_message_text(
            f"🗂 Категории ({kind}):\nНажми на 🗑 чтобы удалить, или ➕ чтобы добавить.",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    elif data.startswith("cat_del|"):
        # Формат: cat_del|<cat_id>|<kind>
        _, cat_id, kind = data.split("|", 2)
        # имя категории для красоты (если не найдём — покажем ID)
        try:
            cat_name = get_category_name(cat_id) or cat_id
        except Exception:
            cat_name = cat_id

        back_cb = f"cat_settings_kind|{kind}"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, удалить", callback_data=f"cat_del_yes|{cat_id}|{kind}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=back_cb)],
        ])
        await query.edit_message_text(
            f"Удалить категорию «{cat_name}»?",
            reply_markup=kb
        )
        return

    elif data.startswith("cat_del_yes|"):
        # Формат: cat_del_yes|<cat_id>|<kind>
        _, cat_id, kind = data.split("|", 2)
        ok = False
        try:
            ok = delete_category(cat_id)  # или deactivate_category(cat_id) — если выберешь мягкое отключение
        except Exception as e:
            logger.error(f"delete_category error: {e}")

        # Пересобираем список категорий
        cats = get_all_categories(kind)
        rows = []
        for c in cats:
            rows.append([InlineKeyboardButton(f"🗑 {c['Название']}", callback_data=f"cat_del|{c['ID']}|{kind}")])
        rows.append([InlineKeyboardButton(f"➕ Добавить категорию для {kind.lower()}а", callback_data=f"cat_add|{kind}")])
        rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="cat_settings")])

        msg = "✅ Категория удалена." if ok else "⚠️ Не удалось удалить категорию."
        await query.edit_message_text(
            f"{msg}\n\n🗂 Категории ({kind}):",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    elif data == "income":
        cats = list_categories("Доход")
        if not cats:
            # тихо ставим дефолт «Другое» и идём сразу к выбору источника
            cat_id, cat_name = ensure_default_category("Доход")
            context.user_data.clear()
            context.user_data["action"] = "income"
            context.user_data["category_id"] = cat_id
            context.user_data["category"] = cat_name
            context.user_data["step"] = "source"  # сначала источник
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Карта",    callback_data="source_card")],
                [InlineKeyboardButton("💵 Наличные", callback_data="source_cash")],
                [InlineKeyboardButton("❌ Отмена",   callback_data="cancel")],
            ])
            await query.edit_message_text("Выберите источник:", reply_markup=kb)
            return
        await _show_categories_view(query, "Доход")
        return

    elif data == "expense":
        cats = list_categories("Расход")
        if not cats:
            cat_id, cat_name = ensure_default_category("Расход")
            context.user_data.clear()
            context.user_data["action"] = "expense"
            context.user_data["category_id"] = cat_id
            context.user_data["category"] = cat_name
            context.user_data["step"] = "source"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Карта",    callback_data="source_card")],
                [InlineKeyboardButton("💵 Наличные", callback_data="source_cash")],
                [InlineKeyboardButton("❌ Отмена",   callback_data="cancel")],
            ])
            await query.edit_message_text("Выберите источник:", reply_markup=kb)
            return
        await _show_categories_view(query, "Расход")
        return

    elif data == "workshop":
        # Список машин в Автомастерской
        try:
            client = get_gspread_client()
            ws = ensure_ws_with_headers(client, WORKSHOP_SHEET, WORKSHOP_HEADERS)
            rows = ws.get_all_values()[1:]  # пропускаем шапку

            if not rows:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Добавить машину", callback_data="workshop_add")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
                ])
                await query.edit_message_text("🧰 *Автомастерская*\n\nСписок пуст.", reply_markup=kb, parse_mode="Markdown")
                return

            buttons = []
            for r in rows:
                if not r:
                    continue
                car_id = (r[0] or "").strip()
                name   = (r[1] or "").strip() or "(без названия)"
                buttons.append([InlineKeyboardButton(name, callback_data=f"workshop_view:{car_id}")])

            buttons.append([InlineKeyboardButton("➕ Добавить машину", callback_data="workshop_add")])
            buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="menu")])

            await query.edit_message_text("🧰 *Автомастерская* — выберите машину:",
                                          reply_markup=InlineKeyboardMarkup(buttons),
                                          parse_mode="Markdown")
        except Exception as e:
            logger.error(f"workshop list error: {e}")
            await query.message.reply_text("⚠️ Не удалось открыть Автомастерскую.")
        return

    elif data == "workshop_add":
        # Мастер добавления: шаг 1 — название
        context.user_data.clear()
        context.user_data["action"] = "workshop_add"
        context.user_data["step"] = "ws_add_name"
        await query.edit_message_text(
            "🧰 Добавление машины\n\nВведите *название* машины (например: Passat B7):",
            reply_markup=back_or_cancel_keyboard("workshop"),
            parse_mode="Markdown"
        )
        return

    elif data.startswith("workshop_view:"):
        car_id = data.split(":", 1)[1]
        try:
            client = get_gspread_client()
            ws = ensure_ws_with_headers(client, WORKSHOP_SHEET, WORKSHOP_HEADERS)

            row, header, idx = _get_row_by_id(ws, car_id)
            if row is None:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="workshop")]])
                await query.edit_message_text("🚫 Машина не найдена в листе «Мастерская».", reply_markup=kb)
                return

            def g(col_name: str, default=""):
                i = idx.get(col_name, None)
                return (row[i].strip() if (i is not None and i < len(row) and row[i]) else default)

            name = g("Название", "(без названия)")
            vin  = g("VIN", "—")

            frozen         = get_frozen_for_car(client, car_id)
            services_total = get_services_total_for_car(client, car_id)

            all_services   = get_services_for_car(client, car_id)
            services_count = len(all_services)

            recent = get_services_recent_for_car(client, car_id, limit=5)
            if recent:
                lines = []
                for _dt, amt, desc in recent:  # дату не выводим
                    tail = f" — {desc}" if desc and desc != "-" else ""
                    lines.append(f"• {_fmt_amount(amt)}{tail}")
                services_list_block = "Последние услуги:\n" + "\n".join(lines) + "\n"
            else:
                services_list_block = ""

            text = (
                f"🧰 *{name}*\n"
                f"🔑 VIN: `{vin}`\n"
                f"🧊 Запчастей (заморожено): {_fmt_amount(frozen)}\n"
                f"🛠️ Услуг на сумму: {_fmt_amount(services_total)}\n"
                f"{services_list_block}\n"
                f"Что делаем?"
            )

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🧾 Купить запчасти", callback_data=f"workshop_buy_parts:{car_id}")],
                [InlineKeyboardButton("🛠️ Добавить услугу", callback_data=f"workshop_add_service:{car_id}")],
                [InlineKeyboardButton(f"📜 Все услуги ({services_count})", callback_data=f"workshop_services:{car_id}:page0")],
                [InlineKeyboardButton("✅ Завершить ремонт", callback_data=f"workshop_finish:{car_id}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="workshop")],
            ])
            await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")

        except Exception as e:
            logger.error(f"workshop_view error: {e}")
            await query.message.reply_text("⚠️ Не удалось открыть карточку машины.")
        return

    elif data.startswith("workshop_buy_parts:"):
        car_id = data.split(":", 1)[1]
        try:
            client = get_gspread_client()
            ws = ensure_ws_with_headers(client, WORKSHOP_SHEET, WORKSHOP_HEADERS)
            rows = ws.get_all_values()
            header = rows[0]
            idx = {h.strip(): i for i, h in enumerate(header)}
            row = None
            for r in rows[1:]:
                if r and (r[0] or "").strip() == car_id:
                    row = r
                    break
            if not row:
                await query.edit_message_text(
                    "🚫 Машина не найдена.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="workshop")]])
                )
                return

            car_name = (row[idx.get("Название", 1)] if len(row) > 1 else "") or "(без названия)"
            car_vin  = (row[idx.get("VIN", 2)] if len(row) > 2 else "") or "—"
        except Exception as e:
            logger.error(f"workshop_buy_parts fetch car error: {e}")
            await query.message.reply_text("⚠️ Не удалось открыть машину.")
            return

        # ⬇️ Вот эти строки ДОЛЖНЫ быть внутри этого elif (с тем же отступом, что и выше)
        context.user_data.clear()
        context.user_data["action"]   = "ws_buy"
        context.user_data["car_id"]   = car_id
        context.user_data["car_name"] = car_name
        context.user_data["car_vin"]  = car_vin
        context.user_data["step"]     = "ws_buy_amount"

        await query.edit_message_text(
            f"🧾 *Покупка запчастей* для *{car_name}*\n🔑 VIN: `{car_vin}`\n\nВведите сумму:",
            reply_markup=back_or_cancel_keyboard(f"workshop_view:{car_id}"),
            parse_mode="Markdown"
        )
        return

    elif data.startswith("workshop_finish:"):
        car_id = data.split(":", 1)[1]
        try:
            client = get_gspread_client()

            # 0) Проверки базовых вещей
            assert WORKSHOP_SHEET, "WORKSHOP_SHEET пуст"
            assert WORKSHOP_HEADERS and isinstance(WORKSHOP_HEADERS, list), "WORKSHOP_HEADERS пустой"
            # эти функции должны существовать:
            assert callable(ensure_ws_with_headers), "нет ensure_ws_with_headers"
            assert callable(_get_row_by_id), "нет _get_row_by_id"
            assert callable(get_frozen_for_car), "нет get_frozen_for_car"
            assert callable(get_services_total_for_car), "нет get_services_total_for_car"

            ws = ensure_ws_with_headers(client, WORKSHOP_SHEET, WORKSHOP_HEADERS)

            row, header, idx = _get_row_by_id(ws, car_id)
            if row is None:
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="workshop")]])
                await query.edit_message_text("🚫 Машина не найдена в листе «Мастерская».", reply_markup=kb)
                return

            def g(col_name: str, default=""):
                i = idx.get(col_name, None)
                return (row[i].strip() if (i is not None and i < len(row) and row[i]) else default)

            car_name = g("Название", "(без названия)")
            car_vin  = g("VIN", "—")

            # 1) Вот тут чаще всего падают: нет Decimal в этом модуле/скоупе или нет функций
            from decimal import Decimal  # на случай, если импорт выше не сработал в этом файле
            frozen_total   = get_frozen_for_car(client, car_id)
            services_total = get_services_total_for_car(client, car_id)

            if frozen_total == Decimal("0") and services_total == Decimal("0"):
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад к машине", callback_data=f"workshop_view:{car_id}")]])
                await query.edit_message_text(
                    f"Для *{car_name}* нет замороженных сумм и услуг.\nНечего завершать.",
                    reply_markup=kb, parse_mode="Markdown"
                )
                return

            # 2) Сохраняем контекст мастера
            context.user_data.clear()
            context.user_data["action"]         = "ws_finish"
            context.user_data["car_id"]         = car_id
            context.user_data["car_name"]       = car_name
            context.user_data["car_vin"]        = car_vin
            context.user_data["frozen_total"]   = frozen_total
            context.user_data["services_total"] = services_total
            context.user_data["step"]           = "ws_finish_src_frozen"

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 На карту",    callback_data=f"ws_finish_src_frozen:card:{car_id}")],
                [InlineKeyboardButton("💵 В наличные", callback_data=f"ws_finish_src_frozen:cash:{car_id}")],
                [InlineKeyboardButton("❌ Отмена",     callback_data=f"workshop_view:{car_id}")],
            ])
            await query.edit_message_text(
                f"✅ Завершение ремонта для *{car_name}*\n"
                f"🧊 Заморожено: {_fmt_amount(frozen_total)}\n"
                f"🛠️ Услуг: {_fmt_amount(services_total)}\n\n"
                f"Куда вернуть *замороженные* деньги?",
                reply_markup=kb, parse_mode="Markdown"
            )

        except Exception as e:
            # Диагностика — увидим конкретную причину
            err = f"workshop_finish start error: {type(e).__name__}: {e}"
            logger.error(err)
            try:
                await query.message.reply_text(f"⚠️ {err}")
            except Exception:
                pass
            await query.message.reply_text("⚠️ Не удалось начать завершение ремонта.")
        return

    elif data.startswith("ws_finish_src_frozen:"):
        # формат: ws_finish_src_frozen:card|cash:<car_id>
        try:
            parts = data.split(":", 2)
            src = parts[1]                # "card" или "cash"
            car_id = parts[2]
            dest_frozen = "Карта" if src == "card" else "Наличные"

            # сохраняем выбор и двигаем мастер дальше
            context.user_data["action"] = "ws_finish"
            context.user_data["step"] = "ws_finish_src_income"
            context.user_data["dest_frozen"] = dest_frozen

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 На карту",    callback_data=f"ws_finish_src_income:card:{car_id}")],
                [InlineKeyboardButton("💵 В наличные", callback_data=f"ws_finish_src_income:cash:{car_id}")],
                [InlineKeyboardButton("⬅️ Назад",      callback_data=f"workshop_finish:{car_id}")],
            ])
            await query.edit_message_text(
                "Куда зачислить *доход по услугам*?", reply_markup=kb, parse_mode="Markdown"
            )
        except Exception as e:
            await query.message.reply_text(f"⚠️ ws_finish_src_frozen error: {e}")
        return

    elif data.startswith("ws_finish_src_income:"):
        # формат: ws_finish_src_income:card|cash:<car_id>
        try:
            parts = data.split(":", 2)
            src = parts[1]                # "card" или "cash"
            car_id = parts[2]
            dest_income = "Карта" if src == "card" else "Наличные"

            context.user_data["action"] = "ws_finish"
            context.user_data["step"] = "ws_finish_confirm"
            context.user_data["dest_income"] = dest_income

            from decimal import Decimal
            car_name = context.user_data.get("car_name", "")
            fz = context.user_data.get("frozen_total", Decimal("0"))
            sv = context.user_data.get("services_total", Decimal("0"))

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Подтвердить", callback_data=f"ws_finish_apply:{car_id}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data=f"workshop_finish:{car_id}")],
            ])
            await query.edit_message_text(
                f"*Итог для {car_name}:*\n"
                f"🧊 Разморозить: {_fmt_amount(fz)} → {context.user_data.get('dest_frozen')}\n"
                f"🛠️ Доход по услугам: {_fmt_amount(sv)} → {dest_income}\n\n"
                f"Подтверждаете?",
                reply_markup=kb, parse_mode="Markdown"
            )
        except Exception as e:
            await query.message.reply_text(f"⚠️ ws_finish_src_income error: {e}")
        return   

    elif data.startswith("ws_finish_apply:"):
        car_id = data.split(":", 1)[1]
        try:
            from decimal import Decimal
            client = get_gspread_client()

            car_name = context.user_data.get("car_name", "(без названия)")
            dest_frozen = context.user_data.get("dest_frozen")
            dest_income = context.user_data.get("dest_income")
            services_total = context.user_data.get("services_total", Decimal("0"))

            # нужно явно выбрать оба кошелька
            if dest_frozen not in ("Карта", "Наличные") or dest_income not in ("Карта", "Наличные"):
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Назад", callback_data=f"workshop_finish:{car_id}")]
                ])
                await query.edit_message_text(
                    "Не выбран кошелёк для заморозки и/или дохода. Укажи оба.",
                    reply_markup=kb
                )
                return

            # helper для нормализации источника
            def _ws_norm_source(raw: str) -> str:
                s = (raw or "").strip()
                s = s.replace("💳", "").replace("💵", "")
                s = s.strip().lower()
                if s.startswith("кар"):
                    return "Карта"
                if s.startswith("нал"):
                    return "Наличные"
                if s in ("card",):
                    return "Карта"
                if s in ("cash",):
                    return "Наличные"
                return ""

            # ===== 1. Считаем, откуда реально была заморозка, и удаляем эти строки =====
            ws_data = client.open_by_key(SPREADSHEET_ID).worksheet("Мастерская_Данные")
            data_rows = ws_data.get_all_values()

            frozen_from_card = Decimal("0")
            frozen_from_cash = Decimal("0")
            rows_to_delete = []

            for i, r in enumerate(data_rows[1:], start=2):
                if not r:
                    continue
                typ = (r[0] or "").strip()
                cid = (r[2] or "").strip() if len(r) > 2 else ""
                if typ != "Заморозка" or cid != car_id:
                    continue

                src = _ws_norm_source(r[5] if len(r) > 5 else "")
                raw_amt = (r[7] if len(r) > 7 else "").replace(" ", "").replace("\u00a0", "")
                amt = Decimal("0")
                if raw_amt:
                    amt = Decimal(raw_amt.replace(",", ".")).quantize(Decimal("0.01"))

                if src == "Карта":
                    frozen_from_card += amt
                elif src == "Наличные":
                    frozen_from_cash += amt
                else:
                    # если не распознали — отнесём к наличке, чаще так
                    frozen_from_cash += amt

                rows_to_delete.append(i)

            # удалить все строки заморозки по этой машине
            for i in sorted(rows_to_delete, reverse=True):
                ws_data.delete_rows(i)

            # готовим листы и время
            expense_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")
            income_ws  = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
            now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

            # ===== 2. если источник ≠ куда вернуть — делаем перевод =====
            def _append_transfer(amount: Decimal, direction: str):
                if amount <= 0:
                    return
                q = str(amount.quantize(Decimal("0.01")))
                try:
                    cat_id, cat_name = ensure_category_by_name("Доход", "Перевод")
                except NameError:
                    cat_id, cat_name = "", "Перевод"

                exp = [now, cat_id, cat_name, "", "", f"Перевод заморозки: {car_name}"]
                inc = [now, cat_id, cat_name, "", "", f"Перевод заморозки: {car_name}"]

                if direction == "card_to_cash":
                    exp[3] = q  # списали с карты
                    inc[4] = q  # положили в нал
                else:  # cash_to_card
                    exp[4] = q  # списали с нал
                    inc[3] = q  # положили на карту

                expense_ws.append_row(exp, value_input_option="USER_ENTERED", table_range="A:F")
                income_ws.append_row(inc,  value_input_option="USER_ENTERED", table_range="A:F")

            # карта <- нал, если вернуть на карту, а было налом
            if dest_frozen == "Карта" and frozen_from_cash > 0:
                _append_transfer(frozen_from_cash, "cash_to_card")

            # нал <- карта, если вернуть налом, а было с карты
            if dest_frozen == "Наличные" and frozen_from_card > 0:
                _append_transfer(frozen_from_card, "card_to_cash")

            # ===== 3. Доход по услугам =====
            if services_total > 0:
                try:
                    cat_id_inc, cat_name_inc = ensure_category_by_name("Доход", "Ремонт")
                except NameError:
                    cat_id_inc, cat_name_inc = "", "Ремонт"

                inc_row = [now, cat_id_inc, cat_name_inc, "", "", f"Ремонт: {car_name}"]
                q = str(services_total.quantize(Decimal("0.01")))
                if dest_income == "Карта":
                    inc_row[3] = q
                else:
                    inc_row[4] = q
                income_ws.append_row(inc_row, value_input_option="USER_ENTERED")

            # ===== 4. Удаляем машину из основного листа "Мастерская" =====
            try:
                ws_cars = client.open_by_key(SPREADSHEET_ID).worksheet(WORKSHOP_SHEET)
                car_rows = ws_cars.get_all_values()
                for i, r in enumerate(car_rows[1:], start=2):
                    if r and (r[0] or "").strip() == car_id:
                        ws_cars.delete_rows(i)
                        break
            except Exception as e:
                logger.warning(f"Не удалось удалить машину из Мастерская после завершения: {e}")

            # ===== 5. Сообщение =====
            lines = [f"✅ Ремонт завершён: {car_name}"]
            if frozen_from_card or frozen_from_cash:
                lines.append(f"🧊 Заморожено возвращено → {dest_frozen}")
            if services_total > 0:
                lines.append(f"🛠️ Доход по услугам: {services_total} → {dest_income}")
            try:
                await context.bot.send_message(chat_id=REMINDER_CHAT_ID, text="\n".join(lines))
            except Exception:
                pass

            context.user_data.clear()
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К списку", callback_data="workshop")]])
            await query.edit_message_text("✅ Ремонт завершён. Машина убрана.", reply_markup=kb)
        except Exception as e:
            err = f"ws_finish_apply error: {type(e).__name__}: {e}"
            logger.error(err)
            try:
                await query.message.reply_text(f"⚠️ {err}")
            except Exception:
                pass
            await query.message.reply_text("❌ Не удалось завершить ремонт.")
        return
        
    elif data.startswith("ws_buy_src:"):
        # формат: ws_buy_src:card:<car_id>  или ws_buy_src:cash:<car_id>
        _, src, car_id = data.split(":", 2)
        source = "Карта" if src == "card" else "Наличные"
        # сохраняем выбор и просим описание
        context.user_data["action"] = "ws_buy"
        context.user_data["step"] = "ws_buy_desc"
        context.user_data["source"] = source
        context.user_data["ws_freeze_source"] = source  # ← дублируем для совместимости
        # car_id/имя/vin/amount уже лежат в user_data из предыдущих шагов
        await query.edit_message_text(
            "Добавьте описание (что купили) — можно одним словом:",
            reply_markup=back_or_cancel_keyboard(f"workshop_view:{car_id}")
        )
        return 

    elif data.startswith("income_cat|"):
        cat_id = data.split("|", 1)[1]
        cat_name = get_category_name(cat_id)
        context.user_data.clear()
        context.user_data["action"] = "income"
        context.user_data["category_id"] = cat_id
        context.user_data["category"] = cat_name
        context.user_data["step"] = "source"  # сначала источник
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Карта",    callback_data="source_card")],
            [InlineKeyboardButton("💵 Наличные", callback_data="source_cash")],
            [InlineKeyboardButton("❌ Отмена",   callback_data="cancel")],
        ])
        await query.edit_message_text("Выберите источник:", reply_markup=kb)
        return

    elif data.startswith("expense_cat|"):
        cat_id = data.split("|", 1)[1]
        cat_name = get_category_name(cat_id)
        context.user_data.clear()
        context.user_data["action"] = "expense"
        context.user_data["category_id"] = cat_id
        context.user_data["category"] = cat_name
        context.user_data["step"] = "source"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Карта",    callback_data="source_card")],
            [InlineKeyboardButton("💵 Наличные", callback_data="source_cash")],
            [InlineKeyboardButton("❌ Отмена",   callback_data="cancel")],
        ])
        await query.edit_message_text("Выберите источник:", reply_markup=kb)
        return

    elif data.startswith("cat_add|"):
        kind = data.split("|", 1)[1]  # "Доход" или "Расход"
        context.user_data.clear()
        context.user_data["action"] = "cat_add"
        context.user_data["kind"] = kind
        # куда вернуться «Назад»: в список категорий этого типа
        context.user_data["return_cb"] = f"cat_settings_kind|{kind}"
        await query.edit_message_text(
            f"Введите название новой категории для {kind.lower()}:",
            reply_markup=back_or_cancel_keyboard(context.user_data["return_cb"])
        )
        return

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

    elif data.startswith("car_extend:"):
        car_id = data.split(":", 1)[1]

        client = get_gspread_client()
        ws = client.open_by_key(SPREADSHEET_ID).worksheet("Автомобили")

        row_idx = _find_row_by_id(ws, car_id)
        if not row_idx:
            await query.edit_message_text("❌ Машина не найдена.")
            return

        rows = ws.get_all_values()
        header = rows[0]
        idx = {h.strip(): i for i, h in enumerate(header)}
        name_col = idx.get("Название")
        car_name = rows[row_idx-1][name_col].strip() if name_col is not None else car_id

        context.user_data["action"] = "extend_contract"
        context.user_data["car_id"] = car_id
        context.user_data["car_name"] = car_name

        await query.edit_message_text(
            f"Введите новую дату окончания договора для *{car_name}* (например 20.11.2025):",
            parse_mode="Markdown"
        )
        return

    elif data.startswith("editcar_select|"):
        name = data.split("|", 1)[1]
        context.user_data["edit_car_name"] = name

        try:
            client = get_gspread_client()
            ws = client.open_by_key(SPREADSHEET_ID).worksheet("Автомобили")

            row_idx = _find_row_by_name(ws, name)
            if not row_idx:
                await query.edit_message_text(
                    "🚫 Автомобиль не найден.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="cars_edit")]])
                )
                return

            header = ws.row_values(1)
            row    = ws.row_values(row_idx)

            def get_col(label: str) -> str:
                return row[header.index(label)].strip() if label in header and header.index(label) < len(row) else ""

            car_id       = get_col("ID")  # нужен для надёжных апдейтов
            vin          = get_col("VIN")
            plate        = get_col("Номер")
            driver       = get_col("Водитель") or "—"
            driver_phone = get_col("Телефон водителя") or "—"
            contract     = get_col("Договор до")
            contract_fmt = _format_date_with_days(contract) if contract else "—"

            text = (
                f"🚘 *{name}*\n"
                f"🔑 _VIN:_ `{vin}`\n"
                f"🔖 _Номер:_ `{plate}`\n"
                f"👤 _Водитель:_ {driver}\n"
                f"📞 _Телефон:_ {driver_phone}\n"
                f"📃 _Договор:_ {contract_fmt}\n\n"
                "Что редактировать?"
            )

            # если есть хотя бы одно поле водителя — показываем «Сменить» + «Продлить», иначе «Добавить»
            has_driver = (driver != "—") or (driver_phone != "—") or bool(contract)

            if has_driver:
                driver_rows = [
                    [InlineKeyboardButton("⏩ Продлить договор", callback_data=f"car_extend:{car_id}")],  # продление по ID оставляем
                    [InlineKeyboardButton("🔁 Сменить водителя", callback_data="editcar_driver_menu")],  # БЕЗ параметров
                ]
            else:
                driver_rows = [
                    [InlineKeyboardButton("👤 Добавить водителя", callback_data="editcar_driver")],       # БЕЗ параметров
                ]

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🛡️ Страховка", callback_data="editcar_field|insurance")],
                [InlineKeyboardButton("🧰 Техосмотр",   callback_data="editcar_field|tech")],
                *driver_rows,
                [InlineKeyboardButton("🗑 Удалить машину", callback_data="editcar_delete_confirm")],      # БЕЗ параметров
                [InlineKeyboardButton("⬅️ Назад", callback_data="cars_edit")],
            ])
            await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"editcar_select fetch error: {e}")
            await query.message.reply_text("⚠️ Не удалось загрузить данные авто.")
        return

    elif data == "editcar_driver_menu":
        # показываем меню действий с текущим водителем
        name = context.user_data.get("edit_car_name", "")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 Сменить водителя", callback_data="editcar_driver_change")],
            [InlineKeyboardButton("🗑 Удалить водителя", callback_data="editcar_driver_delete_confirm")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"editcar_select|{name}")],
        ])
        await query.edit_message_text(f"🚘 {name}\nЧто сделать с водителем?", reply_markup=kb)
        return

    elif data == "editcar_driver_change":
        # запускаем тот же мастер (имя → телефон → дата)
        name = context.user_data.get("edit_car_name", "")
        context.user_data["action"] = "edit_car"
        context.user_data["step"] = "edit_driver_name"
        await query.edit_message_text(
            f"🚘 {name}\nВведите имя нового водителя:", reply_markup=cancel_keyboard()
        )
        return

    elif data == "editcar_driver_delete_confirm":
        name = context.user_data.get("edit_car_name", "")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, удалить водителя", callback_data="editcar_driver_delete_yes")],
            [InlineKeyboardButton("⬅️ Отмена", callback_data=f"editcar_select|{name}")],
        ])
        await query.edit_message_text(f"Удалить водителя у «{name}»? Будут очищены имя, телефон и дата договора.", reply_markup=kb)
        return

    elif data == "editcar_driver_delete_yes":
        try:
            client = get_gspread_client()
            ws = client.open_by_key(SPREADSHEET_ID).worksheet("Автомобили")
            name = context.user_data.get("edit_car_name", "")
            row_idx = _find_row_by_name(ws, name)
            if not row_idx:
                await query.edit_message_text("🚫 Автомобиль не найден.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="cars_edit")]]))
                return

            col_driver        = _ensure_column(ws, "Водитель")
            col_driver_phone  = _ensure_column(ws, "Телефон водителя")
            col_contract_till = _ensure_column(ws, "Договор до")

            ws.update_cell(row_idx, col_driver,        "")
            ws.update_cell(row_idx, col_driver_phone,  "")
            ws.update_cell(row_idx, col_contract_till, "")

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ К редактированию", callback_data="cars_edit")],
                [InlineKeyboardButton("⬅️ К списку", callback_data="cars")],
            ])
            await query.edit_message_text("✅ Водитель удалён (имя, телефон, договор очищены).", reply_markup=kb)
        except Exception as e:
            logger.error(f"delete driver error: {e}")
            await query.message.reply_text("⚠️ Не удалось удалить водителя.")
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

    elif data == "editcar_driver":
        # старт мастера добавления водителя
        name = context.user_data.get("edit_car_name", "")
        context.user_data["action"] = "edit_car"
        context.user_data["step"] = "edit_driver_name"
        await query.edit_message_text(
            f"🚘 {name}\nВведите имя водителя:",
            reply_markup=cancel_keyboard()
        )
        return     

    elif data == "source_card":
        context.user_data["source"] = "Карта"
        context.user_data["step"] = "amount"  # теперь просим сумму
        await query.edit_message_text("Введите сумму:")
        return

    elif data == "source_cash":
        context.user_data["source"] = "Наличные"
        context.user_data["step"] = "amount"
        await query.edit_message_text("Введите сумму:")
        return

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
                    driver = g(r, "Водитель") or "—"
                    driver_phone = g(r, "Телефон водителя") or "—"
                    contract_str = _format_date_with_days(g(r, "Договор до"))  # 12.11.2025 (30 дней)

                    card = (
                        f"🚘 *{name}*\n"
                        f"🔑 _VIN:_ `{vin}`\n"
                        f"🔖 _Номер:_ `{plate}`\n"
                        f"🛡️ _Страховка:_ {_format_date_with_days(g(r, 'Страховка до'))}\n"
                        f"🧰 _Техосмотр:_ {_format_date_with_days(g(r, 'ТО до'))}\n"
                        f"👤 _Водитель:_ {driver}\n"
                        f"📞 _Телефон:_ {driver_phone}\n"
                        f"📃 _Договор:_ {contract_str}"
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

    elif data == "balance":
        try:
            client = get_gspread_client()

            # суммарная заморозка по машинам + раздельно по источникам
            items, frozen_total = get_frozen_by_car(client)
            fz = get_frozen_totals(client)  # {'card','cash','total'}

            s = compute_summary(client)

            # доступно с учётом заморозки
            avail_card = s["Карта"] - fz["card"]
            avail_cash = s["Наличные"] - fz["cash"]
            avail_total = avail_card + avail_cash

            # Блок «заморожено по машинам»
            frozen_lines = [f"🧊 {name}: {_fmt_amount(sm)}" for _, name, sm in items]
            frozen_block = ""
            if frozen_lines:
                frozen_block = "Заморожено запчасти:\n" + "\n".join(frozen_lines) + "\n\n"

            text = (
                f"{frozen_block}"
                f"🏁 Начальная сумма: {_fmt_amount(s['Начальная'])}\n"
                f"💰 Доход: {_fmt_amount(s['Доход'])}\n"
                f"💸 Расход: {_fmt_amount(s['Расход'])}\n"
                f"\n"
                f"💼 Баланс (по учёту): {_fmt_amount(s['Баланс'])}\n"
                f"  ├─ 💳 Карта: {_fmt_amount(s['Карта'])}\n"
                f"  └─ 💵 Наличные: {_fmt_amount(s['Наличные'])}\n"
                f"\n"
                f"🧊 Заморожено всего: {_fmt_amount(fz['total'])} "
                f"(💳 {_fmt_amount(fz['card'])} | 💵 {_fmt_amount(fz['cash'])})\n"
                f"✅ Доступно сейчас: *_{_fmt_amount(avail_total)}_*\n"
                f"  ├─ 💳 Карта (доступно): {_fmt_amount(avail_card)}\n"
                f"  └─ 💵 Наличные (доступно): {_fmt_amount(avail_cash)}"
            )

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("📥 Доход", callback_data="income"),
                        InlineKeyboardButton("📤 Расход", callback_data="expense"),
                    ],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
                ]
            )
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Ошибка баланса: {e}")
            await query.message.reply_text("⚠️ Не удалось получить баланс.")
        return

    elif data.startswith("workshop_add_service:"):
        car_id = data.split(":", 1)[1]
        try:
            client = get_gspread_client()
            ws = ensure_ws_with_headers(client, WORKSHOP_SHEET, WORKSHOP_HEADERS)
            rows = ws.get_all_values()
            header = rows[0]
            idx = {h.strip(): i for i, h in enumerate(header)}
            row = None
            for r in rows[1:]:
                if r and (r[0] or "").strip() == car_id:
                    row = r
                    break
            if not row:
                await query.edit_message_text(
                    "🚫 Машина не найдена.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="workshop")]])
                )
                return
            car_name = (row[idx.get("Название", 1)] if len(row) > 1 else "") or "(без названия)"
            car_vin  = (row[idx.get("VIN", 2)] if len(row) > 2 else "") or "—"
        except Exception as e:
            logger.error(f"workshop_add_service fetch car error: {e}")
            await query.message.reply_text("⚠️ Не удалось открыть машину.")
            return

        context.user_data.clear()
        context.user_data["action"]   = "ws_service"
        context.user_data["car_id"]   = car_id
        context.user_data["car_name"] = car_name
        context.user_data["car_vin"]  = car_vin
        context.user_data["step"]     = "ws_service_amount"

        await query.edit_message_text(
            f"🛠️ *Добавить услугу* для *{car_name}*\n🔑 VIN: `{car_vin}`\n\nВведите стоимость:",
            reply_markup=back_or_cancel_keyboard(f"workshop_view:{car_id}"),
            parse_mode="Markdown"
        )
        return 

    elif data in ["report_7", "report_30"]:
        days = 7 if data == "report_7" else 30
        try:
            client = get_gspread_client()

            in_card, in_cash, _ = _sum_sheet_period(client, "Доход", days, exclude_transfers=True)
            ex_card, ex_cash, _ = _sum_sheet_period(client, "Расход", days, exclude_transfers=True)


            income_total  = in_card + in_cash
            expense_total = ex_card + ex_cash
            net_income    = income_total - expense_total

            text = (
                f"📅 Отчёт за {days} дней:\n\n"
                f"📥 Доход:  {_fmt_amount(income_total)}  (💳 {_fmt_amount(in_card)} | 💵 {_fmt_amount(in_cash)})\n"
                f"📤 Расход: {_fmt_amount(expense_total)} (💳 {_fmt_amount(ex_card)} | 💵 {_fmt_amount(ex_cash)})\n"
                f"— — — — — — — — —\n"
                f"💼 Итог: *{_fmt_amount(net_income)}*"
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📋 Подробности", callback_data=f"report_{days}_details_page0")],
                    [InlineKeyboardButton("🏷 По категориям", callback_data=f"report_{days}_bycat")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
                ]
            )
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Ошибка получения отчёта: {e}")
            await query.message.reply_text("⚠️ Не удалось загрузить отчёт.")
        return

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
            is_income = (detail_type == "income")
            sheet_name = "Доход" if is_income else "Расход"

            _, _, filtered = _sum_sheet_period(client, sheet_name, days, exclude_transfers=True)


            page_size = 10
            total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
            page = max(0, min(page, total_pages - 1))
            page_rows = filtered[page * page_size : (page + 1) * page_size]

            lines = [_render_detail_line(r, is_income) for r in page_rows]
            text = f"📋 Подробности ({'Доход' if is_income else 'Расход'}) за {days} дней:\n\n"
            text += "\n".join(lines) if lines else "Данные не найдены."

            buttons = []
            if page > 0:
                buttons.append(
                    InlineKeyboardButton("⬅️ Предыдущая", callback_data=f"report_{days}_details_{detail_type}_page{page-1}")
                )
            if page < total_pages - 1:
                buttons.append(
                    InlineKeyboardButton("➡️ Следующая", callback_data=f"report_{days}_details_{detail_type}_page{page+1}")
                )

            keyboard = InlineKeyboardMarkup(
                [
                    buttons if buttons else [InlineKeyboardButton("• 1/1 •", callback_data=f"report_{days}_details_page0")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data=f"report_{days}_details_page0")],
                    [InlineKeyboardButton("🏠 Меню", callback_data="menu")],
                ]
            )
            await query.edit_message_text(text, reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Ошибка загрузки подробностей отчёта: {e}")
            await query.message.reply_text("⚠️ Не удалось загрузить подробности отчёта.")
        return

        # ... тут идут другие ветки внутри handle_button ...

    elif re.match(r"report_(7|30)_bycat$", data):
        m = re.match(r"report_(7|30)_bycat$", data)
        days = int(m.group(1))
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Доход по категориям",  callback_data=f"report_{days}_bycat_income_page0")],
            [InlineKeyboardButton("📤 Расход по категориям", callback_data=f"report_{days}_bycat_expense_page0")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"report_{days}")],
        ])
        await query.edit_message_text(f"🏷 По категориям за {days} дней:", reply_markup=kb)
        return

    elif re.match(r"report_(7|30)_bycat_(income|expense)_page(\d+)$", data):
        m = re.match(r"report_(7|30)_bycat_(income|expense)_page(\d+)$", data)
        days = int(m.group(1))
        kind = m.group(2)  # 'income' | 'expense'
        page = int(m.group(3))

        try:
            client = get_gspread_client()
            sheet_name = "Доход" if kind == "income" else "Расход"
            _, _, filtered = _sum_sheet_period(client, sheet_name, days, exclude_transfers=True)
            items = _aggregate_by_category(filtered)

            # пагинация
            page_size = 15
            total_pages = max(1, (len(items) + page_size - 1) // page_size)
            page = max(0, min(page, total_pages - 1))
            slice_items = items[page * page_size : (page + 1) * page_size]

            is_income = (kind == "income")
            hdr_icon = "📥" if is_income else "📤"
            line_icon = "🟢" if is_income else "🔴"
            sign = "" if is_income else "-"

            if slice_items:
                start_idx = page * page_size + 1
                lines = [
                    f"{i}. {cat} — {line_icon} {sign}{_fmt_amount(amt)}"
                    for i, (cat, amt) in enumerate(slice_items, start=start_idx)
                ]
                body = "\n".join(lines)
            else:
                body = "Нет данных за период."

            from decimal import Decimal
            total_sum = sum((v for _, v in items), Decimal("0"))
            total_line = f"Итого: {line_icon} {sign}{_fmt_amount(total_sum)}"

            text = f"{hdr_icon} По категориям за {days} дней:\n\n{body}\n\n{total_line}"

            # навигация
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("⬅️ Предыдущая", callback_data=f"report_{days}_bycat_{kind}_page{page-1}"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("➡️ Следующая", callback_data=f"report_{days}_bycat_{kind}_page{page+1}"))

            kb_rows = []
            if nav:
                kb_rows.append(nav)
            kb_rows.append([InlineKeyboardButton("🔁 Выбрать тип", callback_data=f"report_{days}_bycat")])
            kb_rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"report_{days}")])

            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb_rows))
        except Exception as e:
            logger.error(f"Ошибка отчёта по категориям: {e}")
            await query.message.reply_text("⚠️ Не удалось построить отчёт по категориям.")
        return
    
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

    # --- DEBUG: короткий лог шагов (можно потом убрать) ---
    try:
        logger.info(f"[TEXT] action={context.user_data.get('action')} step={context.user_data.get('step')} text={(update.message.text or '').strip()!r}")
    except Exception:
        pass

    # --- КОРОТКОЕ ШОССЕ для доход/расход: amount -> description ---
    action = context.user_data.get("action")
    step   = context.user_data.get("step")
    text   = (update.message.text or "").strip()

    from decimal import Decimal, ROUND_HALF_UP
    import datetime
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    def _to_amount(s: str) -> Decimal:
        s = (s or "").strip().replace(",", ".")
        return Decimal(s)

    async def _ask_source(update, context):
        context.user_data["step"] = "source"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Карта",    callback_data="source_card")],
            [InlineKeyboardButton("💵 Наличные", callback_data="source_cash")],
            [InlineKeyboardButton("❌ Отмена",   callback_data="cancel")],
        ])
        await update.message.reply_text("Выберите источник:", reply_markup=kb)

    # --- Редактирование начальной суммы баланса ---
    if context.user_data.get("action") == "balance_init_edit":
        return_cb = context.user_data.get("return_cb", "balance_settings")
        txt = (update.message.text or "").strip()
        try:
            val = _to_amount(txt)
            if val < 0:
                raise ValueError("negative")
        except Exception:
            await update.message.reply_text(
                "❌ Введите корректное неотрицательное число (пример: 15000.00).",
                reply_markup=back_or_cancel_keyboard(return_cb)
            )
            return
        try:
            client = get_gspread_client()
            set_initial_balance(client, val)
            context.user_data.clear()

            # покажем текущий баланс после изменения
            live = compute_summary(client)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="balance_settings")],
                [InlineKeyboardButton("🏠 Меню",  callback_data="menu")],
            ])
            await update.message.reply_text(
                "✅ Начальная сумма обновлена.\n\n"
                f"🏁 Начальная: {_fmt_amount(live['Начальная'])}\n"
                f"💼 Баланс:    {_fmt_amount(live['Баланс'])}\n"
                f"💳 Карта:     {_fmt_amount(live['Карта'])}\n"
                f"💵 Наличные:  {_fmt_amount(live['Наличные'])}",
                reply_markup=kb
            )
        except Exception as e:
            logger.error(f"set_initial_balance error: {e}")
            await update.message.reply_text(
                "⚠️ Не удалось сохранить начальную сумму.",
                reply_markup=back_or_cancel_keyboard(return_cb)
            )
        return
    # === Автомастерская: добавление услуги ===
    if context.user_data.get("action") == "ws_service":
        step = context.user_data.get("step")
        txt  = (update.message.text or "").strip()

        if step == "ws_service_amount":
            try:
                amount = _to_amount(txt)
                if amount <= 0:
                    raise ValueError
            except Exception:
                await update.message.reply_text(
                    "⚠️ Введите положительное число (например: 350.00)",
                    reply_markup=back_or_cancel_keyboard(f"workshop_view:{context.user_data.get('car_id','')}")
                )
                return
            context.user_data["amount"] = amount
            context.user_data["step"] = "ws_service_desc"
            await update.message.reply_text(
                "Добавьте описание услуги (например: СТО, шиномонтаж, эвакуатор):",
                reply_markup=back_or_cancel_keyboard(f"workshop_view:{context.user_data.get('car_id','')}")
            )
            return

        if step == "ws_service_desc":
            desc = txt or "-"
            try:
                client = get_gspread_client()
                ws = _ensure_services_ws(client)

                car_id   = context.user_data.get("car_id")
                car_name = context.user_data.get("car_name") or "(без названия)"
                car_vin  = context.user_data.get("car_vin") or "—"
                amount   = context.user_data.get("amount", Decimal("0"))
                now      = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
                rec_id   = datetime.datetime.now().strftime("sv_%Y%m%d_%H%M%S")

                ws.append_row(
                    [rec_id, car_id, car_name, car_vin, now, str(amount.quantize(Decimal("0.01"))), desc],
                    value_input_option="USER_ENTERED"
                )

                total_services = get_services_total_for_car(client, car_id)

                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Назад к машине", callback_data=f"workshop_view:{car_id}")],
                    [InlineKeyboardButton("🛠️ Добавить ещё услугу", callback_data=f"workshop_add_service:{car_id}")],
                    [InlineKeyboardButton("⬅️ К списку", callback_data="workshop")],
                ])
                context.user_data.clear()
                await update.message.reply_text(
                    f"✅ Услуга добавлена: {_fmt_amount(amount)}\n"
                    f"🛠️ Итого по услугам: {_fmt_amount(total_services)}",
                    parse_mode="Markdown",
                    reply_markup=kb
                )
            except Exception as e:
                logger.error(f"ws_service save error: {e}")
                await update.message.reply_text(
                    "❌ Не удалось сохранить услугу.",
                    reply_markup=back_or_cancel_keyboard(f"workshop_view:{context.user_data.get('car_id','')}")
                )
            return
  
    # === Автомастерская: добавление машины ===
    if context.user_data.get("action") == "workshop_add":
        step = context.user_data.get("step")
        txt = (update.message.text or "").strip()

        # 1) Название
        if step == "ws_add_name":
            if not txt:
                await update.message.reply_text(
                    "⚠️ Название не может быть пустым. Введите ещё раз:",
                    reply_markup=back_or_cancel_keyboard("workshop")
                )
                return
            context.user_data["ws_name"] = txt
            context.user_data["step"] = "ws_add_vin"
            await update.message.reply_text(
                "Введите VIN (или '-' если не хотите указывать):",
                reply_markup=back_or_cancel_keyboard("workshop")
            )
            return

        # 2) VIN (или '-'), запись в лист
        if step == "ws_add_vin":
            vin = txt.upper().replace(" ", "")
            if vin == "-":
                vin = ""
            else:
                bad = set("IOQ")
                if vin and (len(vin) != 17 or any(ch in bad for ch in vin)):
                    await update.message.reply_text(
                        "⚠️ VIN должен быть 17 символов и без I/O/Q. Либо пришлите '-' чтобы пропустить.",
                        reply_markup=back_or_cancel_keyboard("workshop")
                    )
                    return
            try:
                client = get_gspread_client()
                ws = ensure_ws_with_headers(client, WORKSHOP_SHEET, WORKSHOP_HEADERS)

                new_id = datetime.datetime.now().strftime("ws_%Y%m%d_%H%M%S")
                now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
                name = context.user_data.get("ws_name") or "(без названия)"

                ws.append_row([new_id, name, vin, now], value_input_option="USER_ENTERED")

                context.user_data.clear()
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("➡️ Открыть карточку", callback_data=f"workshop_view:{new_id}")],
                    [InlineKeyboardButton("⬅️ К списку", callback_data="workshop")],
                ])
                pretty_vin = vin if vin else "—"
                await update.message.reply_text(
                    f"✅ Машина добавлена:\n"
                    f"Название: *{name}*\n"
                    f"VIN: `{pretty_vin}`",
                    parse_mode="Markdown",
                    reply_markup=kb
                )
            except Exception as e:
                logger.error(f"workshop_add save error: {e}")
                await update.message.reply_text("❌ Не удалось сохранить машину.", reply_markup=back_or_cancel_keyboard("workshop"))
            return
        # === Автомастерская: покупка запчастей (заморозка) ===
    # === Автомастерская: покупка запчастей (заморозка) ===
    if context.user_data.get("action") == "ws_buy":
        step = context.user_data.get("step")
        txt  = (update.message.text or "").strip()

        if step == "ws_buy_amount":
            try:
                amount = _to_amount(txt)
                if amount <= 0:
                    raise ValueError
            except Exception:
                await update.message.reply_text(
                    "⚠️ Введите положительное число (пример: 250.00)",
                    reply_markup=back_or_cancel_keyboard(f"workshop_view:{context.user_data.get('car_id','')}")
                )
                return
            context.user_data["amount"] = amount
            context.user_data["step"] = "ws_buy_source"
            car_id = context.user_data.get("car_id","")
            # спрашиваем источник кнопками
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Карта",    callback_data=f"ws_buy_src:card:{car_id}")],
                [InlineKeyboardButton("💵 Наличные", callback_data=f"ws_buy_src:cash:{car_id}")],
                [InlineKeyboardButton("❌ Отмена",   callback_data=f"workshop_view:{car_id}")],
            ])
            await update.message.reply_text("Где замораживаем деньги?", reply_markup=kb)
            return

        if step == "ws_buy_desc":
            desc = txt or "-"
            try:
                client = get_gspread_client()
                ws = _ensure_freeze_ws(client)
                now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

                car_id   = context.user_data.get("car_id")
                car_name = context.user_data.get("car_name") or "(без названия)"
                car_vin  = context.user_data.get("car_vin") or "—"
                amount   = context.user_data.get("amount", Decimal("0"))
                raw_source = context.user_data.get("source", "")  # "Карта" / "Наличные" / возможные варианты
                source = _norm_source(raw_source) or "Карта"      # подстрахуемся дефолтом
                rec_id = datetime.datetime.now().strftime("fz_%Y%m%d_%H%M%S")

                ws.append_row(
                    [rec_id, car_id, car_name, car_vin, now, source, str(amount.quantize(Decimal("0.01"))), desc],
                    value_input_option="USER_ENTERED"
                )
                # сумма заморозки по машине
                frozen = get_frozen_for_car(client, car_id)

                # 🔔 Уведомление в группу
                try:
                    src_emoji = "💳" if source == "Карта" else "💵"
                    desc_q = f" — {desc}" if desc and desc != "-" else ""
                    group_msg = (
                        f"🧊 Заморозка запчастей: {src_emoji} +{_fmt_amount(amount)} на *{car_name}*{desc_q}\n"
                        f"Итого по машине: {_fmt_amount(frozen)}"
                    )
                    await context.bot.send_message(
                        chat_id=REMINDER_CHAT_ID,
                        text=group_msg,
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"freeze group notify error: {e}")

                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Назад к машине", callback_data=f"workshop_view:{car_id}")],
                    [InlineKeyboardButton("🧾 Купить ещё", callback_data=f"workshop_buy_parts:{car_id}")],
                    [InlineKeyboardButton("⬅️ К списку", callback_data="workshop")],
                ])
                context.user_data.clear()
                await update.message.reply_text(
                    f"✅ Заморожено {_fmt_amount(amount)} для *{car_name}*.\n"
                    f"🧊 Итого по машине: {_fmt_amount(frozen)}",
                    parse_mode="Markdown",
                    reply_markup=kb
                )
            except Exception as e:
                logger.error(f"ws_buy save error: {e}")
                await update.message.reply_text(
                    "❌ Не удалось сохранить покупку.",
                    reply_markup=back_or_cancel_keyboard(f"workshop_view:{context.user_data.get('car_id','')}")
                )
            return

    # ====== ШАГ ВВОДА СУММЫ ======
    if step == "amount":
        try:
            amount = _to_amount(text)
            if amount <= 0:
                raise ValueError("Сумма должна быть положительной")

            context.user_data["amount"] = amount

            # ---- МГНОВЕННЫЙ ПЕРЕВОД (без описания) ----
            if action == "transfer":
                description = ""
                direction = context.user_data.get("direction")  # "card_to_cash" | "cash_to_card"

                try:
                    client = get_gspread_client()
                    income_ws  = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
                    expense_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")

                    now = datetime.datetime.now().strftime("%d.%m.%Y %H:%М")
                    income_row  = [now, "", "Перевод", "", "", description]
                    expense_row = [now, "", "Перевод", "", "", description]

                    q = str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

                    if direction == "card_to_cash":
                        expense_row[3] = q  # 💳 D
                        income_row[4]  = q  # 💵 E
                        arrow = "💳 → 💵"
                    else:
                        expense_row[4] = q  # 💵 E
                        income_row[3]  = q  # 💳 D
                        arrow = "💵 → 💳"

                    expense_ws.append_row(expense_row, value_input_option="USER_ENTERED", table_range="A:F")
                    income_ws.append_row(income_row,  value_input_option="USER_ENTERED", table_range="A:F")

                    live = compute_balance(client)

                    text_msg = (
                        f"✅ Перевод выполнен:\n"
                        f"{arrow}  {amount}\n"
                        f"\n📊 Баланс:\n"
                        f"💼 {_fmt_amount(live['Баланс'])}\n"
                        f"💳 {_fmt_amount(live['Карта'])}\n"
                        f"💵 {_fmt_amount(live['Наличные'])}"
                    )
                    kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("📥 Доход",  callback_data="income"),
                        InlineKeyboardButton("📤 Расход", callback_data="expense")],
                        [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
                    ])
                    context.user_data.clear()
                    await update.message.reply_text(text_msg, reply_markup=kb, parse_mode="Markdown")

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

            # ---- если не перевод → идём дальше ----
            context.user_data["step"] = "description"
            await update.message.reply_text("Добавьте описание (или '-' если без описания):")

        except Exception:
            await update.message.reply_text("⚠️ Введите положительное число (пример: 1200.50)")
        return

    # ====== ШАГ ВВОДА ОПИСАНИЯ ======
    if step == "description":
        description = text or "-"
        now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

        amount   = context.user_data.get("amount")
        source   = (context.user_data.get("source") or "").strip()
        cat_id   = context.user_data.get("category_id")
        cat_name = context.user_data.get("category")

        # защита: если нет источника/суммы — вернём пользователя на нужный шаг
        if source not in ("Карта", "Наличные"):
            await _ask_source(update, context)
            return
        if amount is None:
            context.user_data["step"] = "amount"
            await update.message.reply_text("Введите сумму:")
            return

        # если категории нет — тихо ставим «Другое» нужного типа
        try:
            if not cat_id or not cat_name:
                if action == "income":
                    cat_id, cat_name = ensure_default_category("Доход")
                else:
                    cat_id, cat_name = ensure_default_category("Расход")
        except Exception as e:
            logger.error(f"ensure_default_category error: {e}")
            cat_id, cat_name = "", "Другое"

        # запись в лист
        try:
            client = get_gspread_client()
            ws_name = "Доход" if action == "income" else "Расход"
            ws = client.open_by_key(SPREADSHEET_ID).worksheet(ws_name)

            # строка нового формата:
            # [Дата, КатегорияID, Категория, 💳 Карта, 💵 Наличные, 📝 Описание]
            row = [now, cat_id, cat_name, "", "", description]
            q   = str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
            if source == "Карта":
                row[3] = q
            else:
                row[4] = q

            # Чтобы не упираться в пустой лист — можно без table_range,
            # но если хочешь, оставь A:F
            ws.append_row(row, value_input_option="USER_ENTERED")

            # баланс
            live = compute_balance(client)

            header = "✅ Добавлено в *Доход*:" if action == "income" else "✅ Добавлено в *Расход*:"
            money  = f"💰 {amount} ({source})" if action == "income" else f"💸 -{amount} ({source})"
            text_msg = (
                f"{header}\n"
                f"📅 {now}\n"
                f"🏷 {cat_name}\n"
                f"{money}\n"
                f"📝 {description}"
                f"\n\n📊 Баланс:\n"
                f"💼 {_fmt_amount(live['Баланс'])}\n"
                f"💳 {_fmt_amount(live['Карта'])}\n"
                f"💵 {_fmt_amount(live['Наличные'])}"
            )

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Доход",  callback_data="income"),
                InlineKeyboardButton("📤 Расход", callback_data="expense")],
                [InlineKeyboardButton("⬅️ Назад",  callback_data="menu")],
            ])
            context.user_data.clear()
            await update.message.reply_text(text_msg, reply_markup=kb, parse_mode="Markdown")

            # короткое сообщение в канал (не критично, можно убрать)
            try:
                source_emoji = "💳" if source == "Карта" else "💵"
                sign = "+" if action == "income" else "-"
                group_msg = (
                    f"{'📥 Доход' if action=='income' else '📤 Расход'}: "
                    f"{source_emoji} {sign}{_fmt_amount(amount)} — {cat_name}"
                    + (f' “{description}”' if description and description != "-" else "")
                    + "\n"
                    f"Баланс: 💳 {_fmt_amount(live['Карта'])} | 💵 {_fmt_amount(live['Наличные'])}"
                )
                await context.bot.send_message(chat_id=REMINDER_CHAT_ID, text=group_msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"send group error: {e}")

        except Exception as e:
            logger.error(f"WRITE ERROR: {e}")
            await update.message.reply_text("⚠️ Ошибка записи в таблицу.")
        return

    # --- Добавление категории из UI ---
    if context.user_data.get("action") == "cat_add":
        kind = context.user_data.get("kind")
        return_cb = context.user_data.get("return_cb", "cat_settings")
        name = (update.message.text or "").strip()

        if not name:
            await update.message.reply_text(
                "❌ Название не может быть пустым. Введите ещё раз:",
                reply_markup=back_or_cancel_keyboard(return_cb)
            )
            return
        try:
            add_category(kind, name)
            # после успеха показываем кнопки назад/меню
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад к списку", callback_data=return_cb)],
                [InlineKeyboardButton("🏠 Меню", callback_data="menu")],
            ])
            context.user_data.clear()
            await update.message.reply_text(f"✅ Категория добавлена: *{name}*", parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            logger.error(f"cat_add error: {e}")
            await update.message.reply_text(
                "⚠️ Не удалось добавить категорию. Проверь лист 'Категории'.",
                reply_markup=back_or_cancel_keyboard(return_cb)
            )
        return

    # --- ДОХОД: карта -> наличные -> описание -> запись ---
    if context.user_data.get("flow") == "income" and context.user_data.get("step") == "amount_card":
        try:
            card_amt = _parse_money(update.message.text)
        except Exception:
            await update.message.reply_text("❌ Введите число для суммы по *карте* (например 123.45).", parse_mode="Markdown")
            return
        context.user_data["card_amt"] = card_amt
        context.user_data["step"] = "amount_cash"
        await update.message.reply_text("Введите сумму *наличными* (0 если нет):", parse_mode="Markdown")
        return

    if context.user_data.get("flow") == "income" and context.user_data.get("step") == "amount_cash":
        try:
            cash_amt = _parse_money(update.message.text)
        except Exception:
            await update.message.reply_text("❌ Введите число для суммы *наличными* (например 50).", parse_mode="Markdown")
            return
        context.user_data["cash_amt"] = cash_amt
        context.user_data["step"] = "desc"
        await update.message.reply_text("Добавьте описание (или '-' если без описания):")
        return

    if context.user_data.get("flow") == "income" and context.user_data.get("step") == "desc":
        desc = (update.message.text or "").strip() or "-"
        cat_id = context.user_data.get("category_id")
        cat_nm = context.user_data.get("category")
        card_amt = float(context.user_data.get("card_amt", 0.0))
        cash_amt = float(context.user_data.get("cash_amt", 0.0))
        try:
            append_income(cat_id, cat_nm, card_amt, cash_amt, desc)
            total = card_amt + cash_amt
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Ещё доход", callback_data="income")],
                [InlineKeyboardButton("⬅️ В меню",    callback_data="menu")],
            ])
            msg = (
                f"✅ Доход добавлен:\n"
                f"Категория: *{cat_nm}*\n"
                f"💳 Карта: {card_amt:.2f}\n"
                f"💵 Наличные: {cash_amt:.2f}\n"
                f"Итого: *{total:.2f}*\n"
                f"📝 {desc}"
            )
            await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            logger.error(f"append_income error: {e}")
            await update.message.reply_text("⚠️ Не удалось сохранить доход. Проверь лист 'Доход'.")
        context.user_data.clear()
        return

    # --- РАСХОД: карта -> наличные -> описание -> запись ---
    if context.user_data.get("flow") == "expense" and context.user_data.get("step") == "amount_card":
        try:
            card_amt = _parse_money(update.message.text)
        except Exception:
            await update.message.reply_text("❌ Введите число для суммы по *карте* (например 99.99).", parse_mode="Markdown")
            return
        context.user_data["card_amt"] = card_amt
        context.user_data["step"] = "amount_cash"
        await update.message.reply_text("Введите сумму *наличными* (0 если нет):", parse_mode="Markdown")
        return

    if context.user_data.get("flow") == "expense" and context.user_data.get("step") == "amount_cash":
        try:
            cash_amt = _parse_money(update.message.text)
        except Exception:
            await update.message.reply_text("❌ Введите число для суммы *наличными* (например 15).", parse_mode="Markdown")
            return
        context.user_data["cash_amt"] = cash_amt
        context.user_data["step"] = "desc"
        await update.message.reply_text("Добавьте описание (или '-' если без описания):")
        return

    if context.user_data.get("flow") == "expense" and context.user_data.get("step") == "desc":
        desc = (update.message.text or "").strip() or "-"
        cat_id = context.user_data.get("category_id")
        cat_nm = context.user_data.get("category")
        card_amt = float(context.user_data.get("card_amt", 0.0))
        cash_amt = float(context.user_data.get("cash_amt", 0.0))
        try:
            append_expense(cat_id, cat_nm, card_amt, cash_amt, desc)
            total = card_amt + cash_amt
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Ещё расход", callback_data="expense")],
                [InlineKeyboardButton("⬅️ В меню",     callback_data="menu")],
            ])
            msg = (
                f"✅ Расход добавлен:\n"
                f"Категория: *{cat_nm}*\n"
                f"💳 Карта: {card_amt:.2f}\n"
                f"💵 Наличные: {cash_amt:.2f}\n"
                f"Итого: *{total:.2f}*\n"
                f"📝 {desc}"
            )
            await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            logger.error(f"append_expense error: {e}")
            await update.message.reply_text("⚠️ Не удалось сохранить расход. Проверь лист 'Расход'.")
        context.user_data.clear()
        return

    # --- Продление договора: ожидание даты ---
    if context.user_data.get("action") == "extend_contract":
        car_id = context.user_data.get("car_id")
        car_name = context.user_data.get("car_name", car_id)
        new_date = (update.message.text or "").strip()

        client = get_gspread_client()
        ws = client.open_by_key(SPREADSHEET_ID).worksheet("Автомобили")

        row_idx = _find_row_by_id(ws, car_id)
        if not row_idx:
            await update.message.reply_text("❌ Машина не найдена.")
            context.user_data.clear()
            return

        rows = ws.get_all_values()
        header = rows[0]
        idx = {h.strip(): i for i, h in enumerate(header)}
        col_contract = idx.get("Договор до")
        if col_contract is None:
            await update.message.reply_text("❌ В таблице нет колонки «Договор до».")
            context.user_data.clear()
            return

        ws.update_cell(row_idx, col_contract + 1, new_date)  # gspread 1-based

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Назад в Автомобили", callback_data="cars")],
            [InlineKeyboardButton("✏️ Редактировать другой авто", callback_data="cars_edit")],
        ])

        await update.message.reply_text(
            f"✅ Договор по *{car_name}* продлён до {new_date}.",
            parse_mode="Markdown",
            reply_markup=kb
        )
        context.user_data.clear()
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

    # === Редактирование авто: добавление водителя ===
    if context.user_data.get("action") == "edit_car":
        step = context.user_data.get("step")
        car_name = context.user_data.get("edit_car_name", "")
        txt = (update.message.text or "").strip()

        # 3.1 Имя водителя
        if step == "edit_driver_name":
            if not txt:
                await update.message.reply_text("⚠️ Введите имя водителя.")
                return
            context.user_data["driver_name"] = txt
            context.user_data["step"] = "edit_driver_phone"
            await update.message.reply_text("Введите номер телефона водителя (например: +48 600 000 000):",
                                            reply_markup=cancel_keyboard())
            return

        # 3.2 Телефон водителя
        if step == "edit_driver_phone":
            phone = txt
            # мягкая валидация (опционально, можно упростить)
            if len(phone) < 6:
                await update.message.reply_text("⚠️ Слишком короткий телефон. Попробуйте снова.")
                return
            context.user_data["driver_phone"] = phone
            context.user_data["step"] = "edit_driver_contract"
            await update.message.reply_text("Введите дату окончания договора (ДД.ММ.ГГГГ):",
                                            reply_markup=cancel_keyboard())
            return

        if step == "edit_driver_contract":
            try:
                datetime.datetime.strptime(txt, "%d.%m.%Y")
            except ValueError:
                await update.message.reply_text("⚠️ Формат даты: ДД.ММ.ГГГГ")
                return

            try:
                client = get_gspread_client()
                ws = client.open_by_key(SPREADSHEET_ID).worksheet("Автомобили")
                row_idx = _find_row_by_name(ws, car_name)
                if not row_idx:
                    await update.message.reply_text("🚫 Автомобиль не найден.")
                    return

                # гарантируем колонки
                col_driver        = _ensure_column(ws, "Водитель")
                col_driver_phone  = _ensure_column(ws, "Телефон водителя")
                col_contract_till = _ensure_column(ws, "Договор до")

                # Сохраним локально ПРЕЖДЕ чем чистить user_data
                driver_name  = context.user_data.get("driver_name", "")
                driver_phone = context.user_data.get("driver_phone", "")
                contract_till = txt

                # Запись в таблицу
                ws.update_cell(row_idx, col_driver,        driver_name)
                ws.update_cell(row_idx, col_driver_phone,  driver_phone)
                ws.update_cell(row_idx, col_contract_till, contract_till)

                # Очистка состояния
                context.user_data.pop("action", None)
                context.user_data.pop("step", None)
                context.user_data.pop("driver_name", None)
                context.user_data.pop("driver_phone", None)

                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ К редактированию", callback_data="cars_edit")],
                    [InlineKeyboardButton("⬅️ К списку", callback_data="cars")],
                ])
                pretty = _format_date_with_days(contract_till)
                await update.message.reply_text(
                    "✅ Водитель добавлен:\n"
                    f"👤 {driver_name}\n"
                    f"📞 {driver_phone}\n"
                    f"📃 Договор: {pretty}",
                    reply_markup=kb,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"edit driver error: {e}")
                await update.message.reply_text("⚠️ Не удалось обновить данные водителя.")
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
                    income_ws  = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
                    expense_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")

                    # Формат новых листов:
                    # [Дата, КатегорияID, Категория, 💳 Карта, 💵 Наличные, 📝 Описание]
                    now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
                    income_row  = [now, "", "Перевод", "", "", description]
                    expense_row = [now, "", "Перевод", "", "", description]

                    q = str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

                    if direction == "card_to_cash":
                        # расход по карте, доход наличными
                        expense_row[3] = q  # 💳 Карта (4-я колонка -> индекс 3)
                        income_row[4]  = q  # 💵 Наличные (5-я колонка -> индекс 4)
                        arrow = "💳 → 💵"
                    else:
                        # расход наличными, доход по карте
                        expense_row[4] = q  # 💵 Наличные
                        income_row[3]  = q  # 💳 Карта
                        arrow = "💵 → 💳"

                    expense_ws.append_row(expense_row, value_input_option="USER_ENTERED", table_range="A:F")
                    income_ws.append_row(income_row,  value_input_option="USER_ENTERED", table_range="A:F")

                    # Баланс
                    live = compute_balance(client)

                    text_msg = (
                        f"✅ Перевод выполнен:\n"
                        f"{arrow}  {amount}\n"
                        f"\n📊 Баланс:\n"
                        f"💼 {_fmt_amount(live['Баланс'])}\n"
                        f"💳 {_fmt_amount(live['Карта'])}\n"
                        f"💵 {_fmt_amount(live['Наличные'])}"
                    )
                    kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("📥 Доход",  callback_data="income"),
                        InlineKeyboardButton("📤 Расход", callback_data="expense")],
                        [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
                    ])
                    context.user_data.clear()
                    await update.message.reply_text(text_msg, reply_markup=kb, parse_mode="Markdown")

                    # Сообщение в канал
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

            # Источник уже выбран → сразу переходим к описанию
            context.user_data["step"] = "description"
            await update.message.reply_text("Добавьте описание (или '-' если без описания):")

        except Exception:
            await update.message.reply_text("⚠️ Введите положительное число (пример: 1200.50)")
        return


    # -------- Шаг описания (ТОЛЬКО для доход/расход) --------
    if step == "description":
        description = text or "-"
        now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

        amount   = context.user_data.get("amount")
        source   = context.user_data.get("source", "").strip()
        cat_id   = context.user_data.get("category_id")
        cat_name = context.user_data.get("category")

        # ✅ Защита: если почему-то не выбрали источник — вернём пользователя на выбор
        if source not in ("Карта", "Наличные"):
            context.user_data["step"] = "source"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Карта",    callback_data="source_card")],
                [InlineKeyboardButton("💵 Наличные", callback_data="source_cash")],
                [InlineKeyboardButton("❌ Отмена",   callback_data="cancel")],
            ])
            await update.message.reply_text("Выберите источник:", reply_markup=kb)
            return

        # ✅ Защита: сумма должна быть
        if amount is None:
            context.user_data["step"] = "amount"
            await update.message.reply_text("Введите сумму:")
            return

        # Если категория не выбрана — используем дефолт «Другое»
        try:
            if not cat_id or not cat_name:
                if action == "income":
                    cat_id, cat_name = ensure_default_category("Доход")
                else:
                    cat_id, cat_name = ensure_default_category("Расход")
        except Exception as e:
            logger.error(f"ensure_default_category error: {e}")
            cat_id, cat_name = "", "Другое"

        try:
            client = get_gspread_client()

            # Новая строка: [Дата, КатегорияID, Категория, 💳 Карта, 💵 Наличные, 📝 Описание]
            row = [now, cat_id, cat_name, "", "", description]
            q   = str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

            if source == "Карта":
                row[3] = q  # 💳 Карта
            else:
                row[4] = q  # 💵 Наличные

            if action == "income":
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Доход")
                sheet.append_row(row, value_input_option="USER_ENTERED", table_range="A:F")
                money_line = f"💰 {amount} ({source})"
                text_msg = (
                    f"✅ Добавлено в *Доход*:\n"
                    f"📅 {now}\n"
                    f"🏷 {cat_name}\n"
                    f"{money_line}\n"
                    f"📝 {description}"
                )
            else:
                sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Расход")
                sheet.append_row(row, value_input_option="USER_ENTERED", table_range="A:F")
                money_line = f"💸 -{amount} ({source})"
                text_msg = (
                    f"✅ Добавлено в *Расход*:\n"
                    f"📅 {now}\n"
                    f"{money_line}\n"
                    f"🏷 {cat_name}\n"
                    f"📝 {description}"
                )

            # Баланс
            live = compute_balance(client)
            text_msg += (
                f"\n\n📊 Баланс:\n"
                f"💼 {_fmt_amount(live['Баланс'])}\n"
                f"💳 {_fmt_amount(live['Карта'])}\n"
                f"💵 {_fmt_amount(live['Наличные'])}"
            )

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Доход",  callback_data="income"),
                InlineKeyboardButton("📤 Расход", callback_data="expense")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu")],
            ])
            context.user_data.clear()
            await update.message.reply_text(text_msg, reply_markup=kb, parse_mode="Markdown")

            # Короткое сообщение в канал
            try:
                source_emoji = "💳" if source == "Карта" else "💵"
                desc_q = f' “{description}”' if description and description != "-" else ""
                if action == "income":
                    group_msg = (
                        f"📥 Доход: {source_emoji} +{_fmt_amount(amount)} — {cat_name}{desc_q}\n"
                        f"Баланс: 💳 {_fmt_amount(live['Карта'])} | 💵 {_fmt_amount(live['Наличные'])}"
                    )
                else:
                    group_msg = (
                        f"📤 Расход: {source_emoji} -{_fmt_amount(amount)} — {cat_name}{desc_q}\n"
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
    """
    Раз в сутки пробегает лист 'Автомобили' и шлёт напоминания по страховке и тех.осмотру.
    Требуемые заголовки: 'Название', 'Страховка до', 'ТО до'.
    Если заголовков нет — создадим автоматически.
    """
    REMIND_BEFORE_DAYS = 7  # оповещать за N дней

    while True:
        try:
            client = get_gspread_client()
            wb = client.open_by_key(SPREADSHEET_ID)
            ws = wb.worksheet("Автомобили")

            # гарантируем наличие нужных колонок
            header = ws.row_values(1)
            if not header:
                header = []
            # обеспечим колонки (вернёт индекс 1-based)
            col_idx_name = _ensure_column(ws, "Название")
            col_idx_ins  = _ensure_column(ws, "Страховка до")
            col_idx_tech = _ensure_column(ws, "ТО до")
            col_idx_contract = _ensure_column(ws, "Договор до")

            # берём все строки
            rows = ws.get_all_values()
            body = rows[1:] if len(rows) > 1 else []

            today = datetime.date.today()

            for r in body:
                name = r[col_idx_name - 1].strip() if len(r) >= col_idx_name else ""
                ins  = r[col_idx_ins  - 1].strip() if len(r) >= col_idx_ins  else ""
                tech = r[col_idx_tech - 1].strip() if len(r) >= col_idx_tech else ""
                contract = r[col_idx_contract - 1].strip() if len(r) >= col_idx_contract else ""

                # --- страховка ---
                if ins:
                    label, days = _days_left_label(ins)
                    if days is not None:
                        # отправляем, если просрочено / сегодня / в пределах окна
                        if days < 0:
                            msg = f"🚨 Страховка на *{name}* просрочена! ({ins}, {label})."
                            await app.bot.send_message(chat_id=REMINDER_CHAT_ID, text=msg, parse_mode="Markdown")
                        elif days == 0:
                            msg = f"⏰ Сегодня истекает страховка на *{name}* ({ins})."
                            await app.bot.send_message(chat_id=REMINDER_CHAT_ID, text=msg, parse_mode="Markdown")
                        elif days <= REMIND_BEFORE_DAYS:
                            msg = f"⏰ Через {days} дней истекает страховка на *{name}* ({ins})."
                            await app.bot.send_message(chat_id=REMINDER_CHAT_ID, text=msg, parse_mode="Markdown")

                # --- техосмотр ---
                if tech:
                    label, days = _days_left_label(tech)
                    if days is not None:
                        if days < 0:
                            msg = f"🚨 Техосмотр на *{name}* просрочен! ({tech}, {label})."
                            await app.bot.send_message(chat_id=REMINDER_CHAT_ID, text=msg, parse_mode="Markdown")
                        elif days == 0:
                            msg = f"⏰ Сегодня истекает техосмотр на *{name}* ({tech})."
                            await app.bot.send_message(chat_id=REMINDER_CHAT_ID, text=msg, parse_mode="Markdown")
                        elif days <= REMIND_BEFORE_DAYS:
                            msg = f"⏰ Через {days} дней истекает техосмотр на *{name}* ({tech})."
                            await app.bot.send_message(chat_id=REMINDER_CHAT_ID, text=msg, parse_mode="Markdown")

                if contract:
                    label, days = _days_left_label(contract)
                    if days is not None:
                        if days < 0:
                            msg = (
                                f"📃🤝 *Договор аренды* по *{name}* истёк!\n"
                                f"⏱ Был до: {contract} ({label})."
                            )
                            await app.bot.send_message(chat_id=REMINDER_CHAT_ID, text=msg, parse_mode="Markdown")
                        elif days == 0:
                            msg = (
                                f"📃🤝 Сегодня истекает *договор аренды* по *{name}*.\n"
                                f"⏱ Дата: {contract}."
                            )
                            await app.bot.send_message(chat_id=REMINDER_CHAT_ID, text=msg, parse_mode="Markdown")
                        elif days <= REMIND_BEFORE_DAYS:
                            msg = (
                                f"📃🤝 Через {days} дней истекает *договор аренды* по *{name}*.\n"
                                f"⏱ До: {contract}."
                            )
                            await app.bot.send_message(chat_id=REMINDER_CHAT_ID, text=msg, parse_mode="Markdown")
     
        except Exception as e:
            logger.error(f"Ошибка при проверке напоминаний: {e}")

        # спим 24 часа (можно уменьшить до 6–12, если хочешь чаще)
        await asyncio.sleep(86400)

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
