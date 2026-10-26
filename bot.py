import asyncio
import html
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 590568180))
MASTER_USERNAME = os.getenv("MASTER_USERNAME", "LizaSalmi")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@linails_kazan")

# Часовой пояс Казани / Москвы (UTC+3), чтобы бот не зависел от времени сервера
TZ = timezone(timedelta(hours=3))

def get_now() -> datetime:
    """Возвращает текущее время по Казани (МСК) без tzinfo для удобного сравнения"""
    return datetime.now(TZ).replace(tzinfo=None)

# Безопасный путь к базе данных для хостинга
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "nails_schedule.db")
# =============================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

MONTHS_RU = {
    r"\bянвар[ьяюе]?\b": (1, "ЯНВАРЬ"),
    r"\bфеврал[ьяюе]?\b": (2, "ФЕВРАЛЬ"),
    r"\bмарт[ауе]?\b": (3, "МАРТ"),
    r"\bапрел[ьяюе]?\b": (4, "АПРЕЛЬ"),
    r"\bма[йяюе]\b": (5, "МАЙ"),
    r"\bиюн[ьяюе]?\b": (6, "ИЮНЬ"),
    r"\bиюл[ьяюе]?\b": (7, "ИЮЛЬ"),
    r"\bавгуст[ауе]?\b": (8, "АВГУСТ"),
    r"\bсентябр[ьяюе]?\b": (9, "СЕНТЯБРЬ"),
    r"\bоктябр[ьяюе]?\b": (10, "ОКТЯБРЬ"),
    r"\bноябр[ьяюе]?\b": (11, "НОЯБРЬ"),
    r"\bдекабр[ьяюе]?\b": (12, "ДЕКАБРЬ")
}

MONTH_NAMES_BY_NUM = {
    1: "ЯНВАРЬ", 2: "ФЕВРАЛЬ", 3: "МАРТ", 4: "АПРЕЛЬ", 5: "МАЙ", 6: "ИЮНЬ",
    7: "ИЮЛЬ", 8: "АВГУСТ", 9: "СЕНТЯБРЬ", 10: "ОКТЯБРЬ", 11: "НОЯБРЬ", 12: "ДЕКАБРЬ"
}

SERVICES = {
    "mani": "💅 Маникюр",
    "pedi": "🦶 Педикюр",
    "combo": "✨ Маникюр + Педикюр"
}

# --- СОСТОЯНИЯ (FSM) ---
class BookingState(StatesGroup):
    service = State()
    date = State()
    time = State()
    name = State()
    phone = State()

class AdminState(StatesGroup):
    waiting_schedule = State()

# --- ПОСТОЯННЫЕ КЛАВИАТУРЫ ---
def get_client_reply_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="💅 Записаться")
    kb.button(text="🔍 Проверить запись")
    kb.button(text="🔥 Мне срочно вне времени")
    kb.adjust(2, 1)
    return kb.as_markup(resize_keyboard=True)

def get_admin_reply_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="📋 Активные записи клиентов")
    kb.button(text="📅 Обновить расписание")
    kb.button(text="📅 Мое расписание")
    kb.adjust(2, 1)
    return kb.as_markup(resize_keyboard=True)

def get_main_reply_kb(user_id: int):
    return get_admin_reply_kb() if user_id == ADMIN_ID else get_client_reply_kb()

def get_services_inline_kb():
    kb = InlineKeyboardBuilder()
    for key, title in SERVICES.items():
        kb.button(text=title, callback_data=f"srv:{key}")
    kb.button(text="🏠 Домой", callback_data="go_home")
    kb.adjust(1)
    return kb.as_markup()

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("PRAGMA table_info(slots)")
    columns = [row[1] for row in c.fetchall()]
    if columns and "date_str" not in columns:
        c.execute("DROP TABLE slots")
        columns = []

    c.execute("""
        CREATE TABLE IF NOT EXISTS slots (
            date_str TEXT,
            time_str TEXT,
            is_booked INTEGER DEFAULT 0,
            client_name TEXT,
            client_phone TEXT,
            client_username TEXT,
            client_tg_id INTEGER,
            service TEXT,
            service_key TEXT,
            reminded INTEGER DEFAULT 0,
            confirmed INTEGER DEFAULT 0,
            reminded_at TEXT,
            thanks_sent INTEGER DEFAULT 0,
            last_client_name TEXT,
            last_client_phone TEXT,
            last_client_username TEXT,
            last_client_tg_id INTEGER,
            last_service TEXT,
            last_service_key TEXT,
            PRIMARY KEY (date_str, time_str)
        )
    """)

    # Безопасная миграция: добавляем колонку thanks_sent, если база уже была создана ранее
    c.execute("PRAGMA table_info(slots)")
    current_cols = [row[1] for row in c.fetchall()]
    if "thanks_sent" not in current_cols:
        c.execute("ALTER TABLE slots ADD COLUMN thanks_sent INTEGER DEFAULT 0")

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            tg_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            saved_name TEXT,
            saved_phone TEXT
        )
    """)
    conn.commit()
    conn.close()

def register_user(user: types.User):
    if user.id == ADMIN_ID:
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (tg_id, username, first_name) VALUES (?, ?, ?)
        ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name
    """, (user.id, user.username or "", user.first_name or ""))
    conn.commit()
    conn.close()

def save_client_profile(tg_id: int, name: str, phone: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET saved_name = ?, saved_phone = ? WHERE tg_id = ?", (name, phone, tg_id))
    conn.commit()
    conn.close()

def get_client_profile(tg_id: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT saved_name, saved_phone FROM users WHERE tg_id = ?", (tg_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_all_users() -> list:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT tg_id FROM users")
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows

def sync_free_slots_db(parsed_schedule: dict) -> int:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    added = 0
    for date_str, times in parsed_schedule.items():
        for t in times:
            c.execute("SELECT is_booked FROM slots WHERE date_str = ? AND time_str = ?", (date_str, t))
            row = c.fetchone()
            if row is None:
                c.execute("INSERT INTO slots (date_str, time_str, is_booked) VALUES (?, ?, 0)", (date_str, t))
                added += 1
    conn.commit()
    conn.close()
    return added

def get_free_slots_by_date():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    now = get_now()
    today_str = now.strftime("%Y-%m-%d")
    current_time_str = now.strftime("%H:%M")

    c.execute(
        "SELECT date_str, time_str FROM slots WHERE is_booked = 0 AND date_str >= ? ORDER BY date_str, time_str",
        (today_str,)
    )
    rows = c.fetchall()
    conn.close()

    schedule = {}
    for d, t in rows:
        # Не показываем клиентам уже прошедшие часы сегодняшнего дня
        if d == today_str and t <= current_time_str:
            continue
        schedule.setdefault(d, []).append(t)
    return schedule

def get_available_times_for_service(free_times: list, service_key: str) -> list:
    std_times = [t for t in free_times if t in ("12:00", "14:00", "16:00")]
    if service_key in ("mani", "pedi"):
        return std_times
    available = []
    if "12:00" in std_times and "14:00" in std_times:
        available.append("12:00")
    if "14:00" in std_times and "16:00" in std_times:
        available.append("14:00")
    if "16:00" in std_times:
        available.append("16:00")
    return available

def cancel_booking_db(date_str: str, time_str: str, save_backup: bool = False):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT client_name, client_phone, client_username, client_tg_id, service, service_key 
        FROM slots WHERE date_str = ? AND time_str = ?
    """, (date_str, time_str))
    row = c.fetchone()
    if not row:
        conn.close()
        return None

    name, phone, username, tg_id, service, service_key = row
    slots_to_free = [time_str]
    if service_key == "combo":
        if time_str == "12:00":
            slots_to_free.append("14:00")
        elif time_str == "14:00":
            slots_to_free.append("16:00")

    for t in slots_to_free:
        if save_backup:
            c.execute("""
                UPDATE slots SET 
                    is_booked = 0,
                    last_client_name = client_name, last_client_phone = client_phone,
                    last_client_username = client_username, last_client_tg_id = client_tg_id,
                    last_service = service, last_service_key = service_key,
                    client_name = NULL, client_phone = NULL, client_username = NULL,
                    client_tg_id = NULL, service = NULL, service_key = NULL,
                    reminded = 0, confirmed = 0, reminded_at = NULL, thanks_sent = 0
                WHERE date_str = ? AND time_str = ?
            """, (date_str, t))
        else:
            c.execute("""
                UPDATE slots SET 
                    is_booked = 0, client_name = NULL, client_phone = NULL,
                    client_username = NULL, client_tg_id = NULL, service = NULL,
                    service_key = NULL, reminded = 0, confirmed = 0, reminded_at = NULL, thanks_sent = 0
                WHERE date_str = ? AND time_str = ?
            """, (date_str, t))

    if time_str not in ("12:00", "14:00", "16:00") and not save_backup:
        c.execute("DELETE FROM slots WHERE date_str = ? AND time_str = ?", (date_str, time_str))

    conn.commit()
    conn.close()
    return row

def restore_booking_db(date_str: str, time_str: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT is_booked, last_client_name, last_service_key 
        FROM slots WHERE date_str = ? AND time_str = ?
    """, (date_str, time_str))
    row = c.fetchone()
    if not row or row[0] == 1 or not row[1]:
        conn.close()
        return False

    _, _, service_key = row
    slots_to_restore = [time_str]
    if service_key == "combo":
        if time_str == "12:00":
            slots_to_restore.append("14:00")
        elif time_str == "14:00":
            slots_to_restore.append("16:00")

    for t in slots_to_restore:
        c.execute("SELECT is_booked FROM slots WHERE date_str = ? AND time_str = ?", (date_str, t))
        r = c.fetchone()
        if not r or r[0] == 1:
            conn.close()
            return False

    for t in slots_to_restore:
        c.execute("""
            UPDATE slots SET 
                is_booked = 1,
                client_name = last_client_name, client_phone = last_client_phone,
                client_username = last_client_username, client_tg_id = last_client_tg_id,
                service = last_service, service_key = last_service_key,
                confirmed = 1, reminded = 1, thanks_sent = 0
            WHERE date_str = ? AND time_str = ?
        """, (date_str, t))

    conn.commit()
    conn.close()
    return True

def book_slot_db(date_str: str, time_str: str, service_key: str, name: str, phone: str, username: str, tg_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    slots_to_book = [time_str]
    if service_key == "combo":
        if time_str == "12:00":
            slots_to_book.append("14:00")
        elif time_str == "14:00":
            slots_to_book.append("16:00")

    for t in slots_to_book:
        c.execute("SELECT is_booked FROM slots WHERE date_str = ? AND time_str = ?", (date_str, t))
        row = c.fetchone()
        if not row or row[0] == 1:
            conn.close()
            return False

    service_title = SERVICES[service_key]
    for i, t in enumerate(slots_to_book):
        label = service_title if i == 0 else f"↳ Продолжение комплекса (с {time_str})"
        c.execute("""
            UPDATE slots 
            SET is_booked = 1, client_name = ?, client_phone = ?, client_username = ?, 
                client_tg_id = ?, service = ?, service_key = ?, reminded = 0, confirmed = 0, reminded_at = NULL, thanks_sent = 0
            WHERE date_str = ? AND time_str = ?
        """, (name, phone, username, tg_id, label, service_key, date_str, t))

    conn.commit()
    conn.close()
    return True

def add_manual_booking_db(date_str: str, time_str: str, name: str, service_desc: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO slots 
        (date_str, time_str, is_booked, client_name, client_phone, client_username, client_tg_id, service, service_key, reminded, confirmed, thanks_sent)
        VALUES (?, ?, 1, ?, 'Вне записи (ЛС)', 'нет', 0, ?, 'custom', 1, 1, 1)
    """, (date_str, time_str, name, f"🔥 {service_desc}"))
    conn.commit()
    conn.close()

# --- ПАРСЕРЫ ---
def parse_master_schedule(text: str) -> tuple[dict, str]:
    now = get_now()
    year = now.year
    month = now.month
    month_name = MONTH_NAMES_BY_NUM.get(month, "ОКТЯБРЬ")

    text_lower = text.lower()
    for m_key, (m_num, m_title) in MONTHS_RU.items():
        if re.search(m_key, text_lower):
            month = m_num
            month_name = m_title
            if month < now.month and (now.month - month) > 6:
                year += 1
            break

    schedule = {}
    current_date = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        times_in_line = re.findall(r"\b(12:00|14:00|16:00)\b", line)
        line_without_times = re.sub(r"\b(12:00|14:00|16:00)\b", "", line)

        m_dot = re.match(r"^\s*(\d{1,2})\.(\d{1,2})\b", line_without_times)
        m_day = re.match(r"^\s*(\d{1,2})\s*(?:пн|вт|ср|чт|пт|сб|вс)?\b", line_without_times, re.IGNORECASE)

        if m_dot:
            day_num, month_num = int(m_dot.group(1)), int(m_dot.group(2))
            month_name = MONTH_NAMES_BY_NUM.get(month_num, month_name)
            try:
                current_date = datetime(year, month_num, day_num).strftime("%Y-%m-%d")
            except ValueError:
                current_date = None
        elif m_day and not line.startswith(("12:", "14:", "16:")):
            day_num = int(m_day.group(1))
            if 1 <= day_num <= 31:
                try:
                    current_date = datetime(year, month, day_num).strftime("%Y-%m-%d")
                except ValueError:
                    current_date = None

        if times_in_line and current_date:
            schedule.setdefault(current_date, [])
            for t in times_in_line:
                if t not in schedule[current_date]:
                    schedule[current_date].append(t)

    return schedule, month_name

def parse_manual_urgent_booking(text: str):
    """
    Умный парсер заметки мастера. Понимает любое время и дату:
    - "катя в 11 30 починка" -> сегодня в 11:30
    - "Катя 18.10 11 15 починка ногтя" -> 18.10 в 11:15
    - "Алина маникюр в 15 18.10" -> 18.10 в 15:00
    """
    if "\n" in text.strip() or ";" in text:
        return None

    now = get_now()
    working = text.strip()
    date_str = None

    # 1. Ищем дату ДД.ММ или ДД/ММ
    for m in re.finditer(r"\b(\d{1,2})[./](\d{1,2})\b", working):
        d_cand, m_cand = int(m.group(1)), int(m.group(2))
        before = working[:m.start()].strip().lower()
        if 1 <= d_cand <= 31 and 1 <= m_cand <= 12 and not re.search(r"\bв$", before):
            year = now.year
            if m_cand < now.month and (now.month - m_cand) > 6:
                year += 1
            try:
                date_str = datetime(year, m_cand, d_cand).strftime("%Y-%m-%d")
                working = working[:m.start()] + " " + working[m.end():]
                break
            except ValueError:
                pass

    # 2. Проверяем слова "сегодня", "завтра", "послезавтра"
    if not date_str:
        if re.search(r"\bпослезавтра\b", working, flags=re.IGNORECASE):
            date_str = (now + timedelta(days=2)).strftime("%Y-%m-%d")
            working = re.sub(r"\bпослезавтра\b", " ", working, flags=re.IGNORECASE)
        elif re.search(r"\bзавтра\b", working, flags=re.IGNORECASE):
            date_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            working = re.sub(r"\bзавтра\b", " ", working, flags=re.IGNORECASE)
        elif re.search(r"\bсегодня\b", working, flags=re.IGNORECASE):
            date_str = now.strftime("%Y-%m-%d")
            working = re.sub(r"\bсегодня\b", " ", working, flags=re.IGNORECASE)

    # 3. Проверяем формат "18 октября"
    if not date_str:
        for m_regex, (m_num, _) in MONTHS_RU.items():
            pattern = r"\b(\d{1,2})\s+" + m_regex[2:]
            m_month = re.search(pattern, working, flags=re.IGNORECASE)
            if m_month:
                d_cand = int(m_month.group(1))
                year = now.year
                if m_num < now.month and (now.month - m_num) > 6:
                    year += 1
                try:
                    date_str = datetime(year, m_num, d_cand).strftime("%Y-%m-%d")
                    working = working[:m_month.start()] + " " + working[m_month.end():]
                    break
                except ValueError:
                    pass

    # 4. Ищем время
    time_str = None
    m_time = re.search(
        r"(?:^|\s)в\s*([01]?\d|2[0-3])(?:[:.\-\s]\s*([0-5]\d))?\b",
        working,
        flags=re.IGNORECASE
    )
    if m_time:
        hh = int(m_time.group(1))
        mm = int(m_time.group(2)) if m_time.group(2) else 0
        time_str = f"{hh:02d}:{mm:02d}"
        working = working[:m_time.start()] + " " + working[m_time.end():]
    else:
        m_time2 = re.search(r"\b([01]?\d|2[0-3])[:.\-\s]([0-5]\d)\b", working)
        if m_time2:
            hh = int(m_time2.group(1))
            mm = int(m_time2.group(2))
            time_str = f"{hh:02d}:{mm:02d}"
            working = working[:m_time2.start()] + " " + working[m_time2.end():]

    if not time_str:
        return None

    # 5. Если дата не указана, ищем число дня или ставим сегодня
    if not date_str:
        m_day_only = re.search(r"\b(\d{1,2})(?:\s*числа)?\b", working, flags=re.IGNORECASE)
        if m_day_only and 1 <= int(m_day_only.group(1)) <= 31:
            d_cand = int(m_day_only.group(1))
            try:
                date_str = datetime(now.year, now.month, d_cand).strftime("%Y-%m-%d")
                working = working[:m_day_only.start()] + " " + working[m_day_only.end():]
            except ValueError:
                date_str = now.strftime("%Y-%m-%d")
        else:
            date_str = now.strftime("%Y-%m-%d")

    # 6. Очищаем оставшийся текст для имени клиента и заметки
    cleaned = re.sub(r"\b(в|на)\b", " ", working, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")

    if not cleaned:
        name = "Клиент"
        service_desc = "Запись вне времени"
    else:
        parts = cleaned.split(" ", 1)
        name = parts[0].capitalize()
        service_desc = parts[1] if len(parts) > 1 else "Запись вне времени"

    return date_str, time_str, name, service_desc

def format_date_human(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    wd = WEEKDAYS[dt.weekday()]
    return f"{dt.day} {wd} ({dt.strftime('%d.%m')})"

def generate_channel_post(month_name: str) -> str:
    schedule = get_free_slots_by_date()
    lines = [
        f"Девочки появились новые окошки на {month_name} успевайте записаться ✨\n",
        "Где свободно 2 времени подряд , можно записаться на комплекс, либо по отдельности маникюр /педикюр 🧚🏼‍♂️\n"
    ]
    for date_str, times in schedule.items():
        std_times = [t for t in times if t in ("12:00", "14:00", "16:00")]
        if not std_times:
            continue
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        wd = WEEKDAYS[dt.weekday()]
        lines.append(f"{dt.day} {wd}\n  {';'.join(std_times)}")
    return "\n".join(lines)

# ================= НАВИГАЦИЯ =================

@dp.message(Command("start", "admin"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    register_user(message.from_user)

    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "👩‍💻 <b>Панель мастера активна!</b>\n\n"
            "• Нажмите <b>«📋 Активные записи клиентов»</b> для просмотра и управления бронями.\n"
            "• Нажмите <b>«📅 Обновить расписание»</b> для публикации окошек.\n"
            "• Нажмите <b>«📅 Мое расписание»</b> для обзора всех занятых и свободных часов.\n"
            "• Чтобы добавить запись/заметку на любое время, просто напишите боту:\n"
            "<code>катя в 11 30 починка</code> (запишет на сегодня)\n"
            "или <code>Катя 28.10 11 15 починка ногтя</code>",
            reply_markup=get_admin_reply_kb(),
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "Здравствуйте☀️\nДобро пожаловать в бот онлайн-записи <b>Linails</b> 💅🏼💓\n\n"
            "Выберите нужное действие на кнопках внизу экрана 👇",
            reply_markup=get_client_reply_kb(),
            parse_mode="HTML"
        )

@dp.callback_query(F.data == "go_home")
async def cb_go_home(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer(
        "🏠 Вы вернулись на главный экран. Выберите нужное действие на кнопках внизу 👇",
        reply_markup=get_main_reply_kb(call.from_user.id)
    )
    await call.answer()

# ================= КНОПКИ КЛИЕНТА =================

@dp.message(F.text == "💅 Записаться")
async def btn_book(message: types.Message, state: FSMContext):
    await state.clear()
    register_user(message.from_user)
    await message.answer("Выберите процедуру:", reply_markup=get_services_inline_kb())

@dp.callback_query(F.data == "open_booking")
async def cb_open_booking(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Выберите процедуру:", reply_markup=get_services_inline_kb())
    await call.answer()

@dp.message(F.text == "🔍 Проверить запись")
async def btn_check_booking(message: types.Message, state: FSMContext):
    await state.clear()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today_str = get_now().strftime("%Y-%m-%d")
    c.execute("""
        SELECT date_str, time_str, service, confirmed 
        FROM slots 
        WHERE is_booked = 1 AND client_tg_id = ? AND service NOT LIKE '↳%' AND date_str >= ?
        ORDER BY date_str, time_str
    """, (message.from_user.id, today_str))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await message.answer(
            "У вас еще нет активных записей 🌸\n\nВыберите процедуру для записи:",
            reply_markup=get_services_inline_kb()
        )
        return

    for date_str, time_str, service_title, confirmed in rows:
        status_str = "✅ Подтверждена" if confirmed else "⏳ Ожидает визита"
        
        kb = InlineKeyboardBuilder()
        kb.button(text="🔄 Перенести", callback_data=f"resched:{date_str}:{time_str}")
        kb.button(text="❌ Отменить", callback_data=f"cancel_booking:{date_str}:{time_str}")
        kb.adjust(2)

        await message.answer(
            f"📋 <b>Ваша запись:</b>\n"
            f"💅 Процедура: {html.escape(service_title or '')}\n"
            f"📅 День: {format_date_human(date_str)}\n"
            f"🕒 Время: {time_str}\n"
            f"📍 Адрес: Ибрагимова 71, 2-ой подъезд, 1-ый этаж, 23-я квартира\n"
            f"Статус: {status_str}",
            reply_markup=kb.as_markup(),
            parse_mode="HTML"
        )

    home_kb = InlineKeyboardBuilder()
    home_kb.button(text="🏠 Домой", callback_data="go_home")
    await message.answer("Выберите действие или вернитесь на главный экран:", reply_markup=home_kb.as_markup())

@dp.callback_query(F.data.startswith("cancel_booking:"))
async def cb_cancel_booking_direct(call: types.CallbackQuery, state: FSMContext):
    _, date_str, time_str = call.data.split(":", 2)
    old_info = cancel_booking_db(date_str, time_str, save_backup=False)
    await state.clear()

    if old_info:
        name, phone, username, client_tg_id, service_title, _ = old_info
        if call.from_user.id != ADMIN_ID:
            tg_tag = f"@{username}" if username and username != "нет" else "скрыт"
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"❌ <b>Клиент отменил запись!</b>\n"
                    f"Окошко снова свободно:\n"
                    f"👤 {html.escape(name or '')} ({tg_tag}) | {html.escape(phone or '')}\n"
                    f"📅 Отменено: {format_date_human(date_str)} в {time_str} ({html.escape(service_title or '')})",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        elif client_tg_id and client_tg_id > 0:
            # Если отменил сам мастер через админку — вежливо уведомляем клиента
            try:
                await bot.send_message(
                    client_tg_id,
                    f"ℹ️ Ваша запись на <b>{format_date_human(date_str)} в {time_str}</b> была отменена мастером.",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    await call.message.edit_text(
        f"❌ Запись на <b>{format_date_human(date_str)} в {time_str}</b> успешно удалена/отменена.",
        parse_mode="HTML"
    )
    await call.message.answer("Главное меню:", reply_markup=get_main_reply_kb(call.from_user.id))
    await call.answer()

@dp.message(F.text == "🔄 Перенести запись")
async def btn_reschedule_menu(message: types.Message, state: FSMContext):
    await state.clear()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today_str = get_now().strftime("%Y-%m-%d")
    c.execute("""
        SELECT date_str, time_str, service 
        FROM slots 
        WHERE is_booked = 1 AND client_tg_id = ? AND service NOT LIKE '↳%' AND date_str >= ?
        ORDER BY date_str, time_str LIMIT 1
    """, (message.from_user.id, today_str))
    booking = c.fetchone()
    conn.close()

    if not booking:
        await message.answer(
            "У вас еще нет активных записей для переноса 🌸\n\nВыберите процедуру для записи:",
            reply_markup=get_services_inline_kb()
        )
        return

    date_str, time_str, service_title = booking
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да, удалить старую и выбрать новую", callback_data=f"resched:{date_str}:{time_str}")
    kb.button(text="🏠 Домой", callback_data="go_home")
    kb.adjust(1)

    await message.answer(
        f"У вас есть запись на <b>{format_date_human(date_str)} в {time_str}</b> ({html.escape(service_title or '')}).\n\n"
        f"При переносе текущее окошко освободится, и вы сразу выберете новую дату и время. Перенести?",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("resched:"))
async def cb_reschedule_confirm(call: types.CallbackQuery, state: FSMContext):
    _, date_str, time_str = call.data.split(":", 2)
    old_info = cancel_booking_db(date_str, time_str, save_backup=False)
    await state.clear()

    if old_info:
        name, phone, username, _, service_title, _ = old_info
        tg_tag = f"@{username}" if username and username != "нет" else "скрыт"
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🔄 <b>Клиент переносит запись!</b>\n"
                f"Старая запись отменена:\n"
                f"👤 {html.escape(name or '')} ({tg_tag}) | {html.escape(phone or '')}\n"
                f"📅 Было: {format_date_human(date_str)} в {time_str} ({html.escape(service_title or '')})",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await call.answer("Старая запись отменена. Выберите новое время:")
    await call.message.answer(
        "♻️ Старая запись успешно отменена, окошко снова свободно!\n\nВыберите процедуру для новой записи:",
        reply_markup=get_services_inline_kb()
    )

@dp.message(F.text == "🔥 Мне срочно вне времени")
async def btn_urgent(message: types.Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="💌 Написать мастеру в ЛС", url=f"https://t.me/{MASTER_USERNAME.lstrip('@')}")
    kb.button(text="🏠 Домой", callback_data="go_home")
    kb.adjust(1)

    await message.answer(
        "Если вам нужно попасть на процедуру срочно или вне основного расписания — "
        "напишите мастеру в личные сообщения, и мы подберем для вас индивидуальное время 🤍",
        reply_markup=kb.as_markup()
    )

# ================= ПРОЦЕСС ЗАПИСИ КЛИЕНТА =================

@dp.callback_query(F.data.startswith("srv:"))
async def select_service(call: types.CallbackQuery, state: FSMContext):
    service_key = call.data.split(":")[1]
    await state.update_data(service=service_key)

    schedule = get_free_slots_by_date()
    kb = InlineKeyboardBuilder()
    has_dates = False

    for date_str, free_times in schedule.items():
        avail_times = get_available_times_for_service(free_times, service_key)
        if avail_times:
            has_dates = True
            kb.button(text=format_date_human(date_str), callback_data=f"date:{date_str}")

    kb.button(text="⬅️ Назад", callback_data="open_booking")
    kb.button(text="🏠 Домой", callback_data="go_home")
    kb.adjust(2)

    if not has_dates:
        await call.message.edit_text(
            "К сожалению, для этой процедуры сейчас нет свободных окошек 😔\n"
            "Попробуйте выбрать другую услугу или нажмите «🔥 Мне срочно вне времени».",
            reply_markup=kb.as_markup()
        )
        return

    await state.set_state(BookingState.date)
    await call.message.edit_text(
        f"Вы выбрали: <b>{SERVICES[service_key]}</b>\n\nВыберите удобный день:",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("date:"))
async def select_date(call: types.CallbackQuery, state: FSMContext):
    date_str = call.data.split(":")[1]
    data = await state.get_data()
    service_key = data.get("service", "mani")
    await state.update_data(date=date_str)

    schedule = get_free_slots_by_date()
    free_times = schedule.get(date_str, [])
    avail_times = get_available_times_for_service(free_times, service_key)

    kb = InlineKeyboardBuilder()
    for t in avail_times:
        kb.button(text=f"🕒 {t}", callback_data=f"time:{t}")
    kb.button(text="⬅️ Назад", callback_data=f"srv:{service_key}")
    kb.button(text="🏠 Домой", callback_data="go_home")
    kb.adjust(3, 2)

    await state.set_state(BookingState.time)
    await call.message.edit_text(
        f"Процедура: <b>{SERVICES[service_key]}</b>\nДень: <b>{format_date_human(date_str)}</b>\n\nВыберите время:",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("time:"))
async def select_time(call: types.CallbackQuery, state: FSMContext):
    time_str = call.data.split(":", 1)[1]
    await state.update_data(time=time_str)
    data = await state.get_data()

    user = call.from_user
    profile = get_client_profile(user.id)

    if profile and profile[0] and profile[1]:
        saved_name, saved_phone = profile
        username = user.username if user.username else "нет"

        success = book_slot_db(
            date_str=data["date"],
            time_str=data["time"],
            service_key=data["service"],
            name=saved_name,
            phone=saved_phone,
            username=username,
            tg_id=user.id
        )
        await state.clear()

        if not success:
            await call.message.edit_text(
                "Ой! Это окошко только что заняли 😔 Пожалуйста, выберите другое время.",
                reply_markup=get_services_inline_kb()
            )
            return

        human_date = format_date_human(data["date"])
        service_title = SERVICES[data["service"]]

        await call.message.edit_text(
            f"Здравствуйте☀️\n"
            f"✅ Вы успешно записаны!\n"
            f"💅 Процедура: {service_title}\n"
            f"📅 День: {human_date}\n"
            f"🕒 Время: {time_str}\n"
            f"Адрес : Ибрагимова 71, 2-ой подъезд, 1-ый этаж ,23-я квартира\n"
            f"До встречи 🤍",
            reply_markup=None
        )
        await call.message.answer("Главное меню:", reply_markup=get_main_reply_kb(user.id))

        tg_tag = f"@{username}" if username != "нет" else "скрыт"
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🔔 <b>Новая запись (по сохраненным данным)!</b>\n\n"
                f"👤 Имя: <b>{html.escape(saved_name)}</b>\n"
                f"🔗 Ник: {tg_tag}\n"
                f"📞 Телефон: <code>{html.escape(saved_phone)}</code>\n"
                f"💅 Процедура: {service_title}\n"
                f"📅 Когда: {human_date} в <b>{time_str}</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    await state.set_state(BookingState.name)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=f"date:{data['date']}")
    kb.button(text="🏠 Домой", callback_data="go_home")
    kb.adjust(2)

    await call.message.edit_text(
        f"Выбрано: <b>{format_date_human(data['date'])} в {time_str}</b>\n\n"
        f"Это ваша первая запись в боте. Напишите ваше <b>Имя</b> в чат:",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

@dp.message(BookingState.name)
async def enter_name(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, введите ваше имя текстом:")
        return

    await state.update_data(name=message.text.strip())
    await state.set_state(BookingState.phone)

    kb = ReplyKeyboardBuilder()
    kb.button(text="📱 Отправить мой номер телефона", request_contact=True)

    nav_kb = InlineKeyboardBuilder()
    data = await state.get_data()
    nav_kb.button(text="⬅️ Назад", callback_data=f"time:{data['time']}")
    nav_kb.button(text="🏠 Домой", callback_data="go_home")
    nav_kb.adjust(2)

    await message.answer(
        "Оставьте ваш <b>номер телефона</b> для связи (нажмите кнопку внизу или введите цифрами):",
        reply_markup=kb.as_markup(resize_keyboard=True, one_time_keyboard=True),
        parse_mode="HTML"
    )
    await message.answer("Или используйте кнопки навигации:", reply_markup=nav_kb.as_markup())

@dp.message(BookingState.phone)
async def enter_phone(message: types.Message, state: FSMContext):
    if message.contact:
        phone = message.contact.phone_number
    elif message.text:
        phone = message.text.strip()
    else:
        await message.answer("Пожалуйста, нажмите кнопку отправки контакта или введите номер цифрами:")
        return

    data = await state.get_data()
    user = message.from_user
    username = user.username if user.username else "нет"
    name = data["name"]
    time_str = data["time"]

    save_client_profile(user.id, name, phone)

    success = book_slot_db(
        date_str=data["date"],
        time_str=time_str,
        service_key=data["service"],
        name=name,
        phone=phone,
        username=username,
        tg_id=user.id
    )
    await state.clear()

    if not success:
        await message.answer(
            "Ой! Это окошко только что заняли 😔 Пожалуйста, выберите другое время.",
            reply_markup=get_main_reply_kb(user.id)
        )
        return

    human_date = format_date_human(data["date"])
    service_title = SERVICES[data["service"]]

    await message.answer(
        f"Здравствуйте☀️\n"
        f"✅ Вы успешно записаны!\n"
        f"💅 Процедура: {service_title}\n"
        f"📅 День: {human_date}\n"
        f"🕒 Время: {time_str}\n"
        f"Адрес : Ибрагимова 71, 2-ой подъезд, 1-ый этаж ,23-я квартира\n"
        f"До встречи 🤍",
        reply_markup=get_main_reply_kb(user.id)
    )

    tg_tag = f"@{username}" if username != "нет" else "скрыт"
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>Новая запись!</b>\n\n"
            f"👤 Имя: <b>{html.escape(name)}</b>\n"
            f"🔗 Ник: {tg_tag}\n"
            f"📞 Телефон: <code>{html.escape(phone)}</code>\n"
            f"💅 Процедура: {service_title}\n"
            f"📅 Когда: {human_date} в <b>{time_str}</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass

# ================= АДМИНКА МАСТЕРА =================

@dp.message(F.from_user.id == ADMIN_ID, F.text == "📋 Активные записи клиентов")
async def admin_show_bookings(message: types.Message, state: FSMContext):
    await state.clear()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today_str = get_now().strftime("%Y-%m-%d")
    c.execute("""
        SELECT date_str, time_str, client_name, client_phone, client_username, client_tg_id, service, confirmed 
        FROM slots 
        WHERE is_booked = 1 AND service NOT LIKE '↳%' AND date_str >= ?
        ORDER BY date_str, time_str
    """, (today_str,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await message.answer("Пока нет активных записей клиентов.", reply_markup=get_admin_reply_kb())
        return

    await message.answer("📋 <b>Активные записи клиентов:</b>", parse_mode="HTML", reply_markup=get_admin_reply_kb())

    for d, t, name, phone, username, tg_id, srv, conf in rows:
        if username and username not in ("None", "нет", ""):
            tg_link = f"<a href='https://t.me/{username.lstrip('@')}'>@{username.lstrip('@')}</a>"
        elif tg_id and tg_id > 0:
            tg_link = f"<a href='tg://user?id={tg_id}'>Написать в ЛС</a>"
        else:
            tg_link = "Вне записи (ЛС)"

        conf_mark = "✅ Подтверждена" if conf else "⏳ Ждет подтверждения"
        kb = InlineKeyboardBuilder()
        if not conf:
            kb.button(text="✅ Подтвердить", callback_data=f"adm_conf:{d}:{t}")
        kb.button(text="❌ Отменить запись", callback_data=f"cancel_booking:{d}:{t}")
        kb.adjust(2)

        await message.answer(
            f"📅 <b>{format_date_human(d)} в {t}</b> ({conf_mark})\n"
            f"👤 Имя: <b>{html.escape(name or '')}</b> | Связь: {tg_link}\n"
            f"📞 Тел: <code>{html.escape(phone or '')}</code>\n"
            f"💅 {html.escape(srv or '')}",
            parse_mode="HTML",
            reply_markup=kb.as_markup(),
            disable_web_page_preview=True
        )

@dp.callback_query(F.data.startswith("adm_conf:"))
async def cb_admin_confirm_manual(call: types.CallbackQuery):
    _, date_str, time_str = call.data.split(":", 2)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE slots SET confirmed = 1 WHERE date_str = ? AND time_str = ?", (date_str, time_str))
    conn.commit()
    conn.close()

    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отменить запись", callback_data=f"cancel_booking:{date_str}:{time_str}")
    new_text = call.message.html_text.replace("⏳ Ждет подтверждения", "✅ Подтверждена")
    await call.message.edit_text(new_text, parse_mode="HTML", reply_markup=kb.as_markup(), disable_web_page_preview=True)
    await call.answer("Запись отмечена как подтвержденная!")

@dp.message(F.from_user.id == ADMIN_ID, F.text == "📅 Мое расписание")
async def admin_my_schedule(message: types.Message, state: FSMContext):
    await state.clear()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    today_str = get_now().strftime("%Y-%m-%d")
    
    c.execute("""
        SELECT date_str, time_str, is_booked, client_name, service, confirmed 
        FROM slots 
        WHERE date_str >= ? 
        ORDER BY date_str, time_str
    """, (today_str,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await message.answer("В базе данных пока нет созданного расписания.", reply_markup=get_admin_reply_kb())
        return

    schedule_by_date = {}
    for d, t, booked, client, srv, confirmed in rows:
        schedule_by_date.setdefault(d, []).append((t, booked, client, srv, confirmed))

    lines = ["📅 <b>Ваше полное расписание:</b>\n"]
    for d, slots in schedule_by_date.items():
        lines.append(f"📌 <b>{format_date_human(d)}</b>:")
        for t, booked, client, srv, confirmed in slots:
            if booked:
                short_srv = html.escape((srv or "").replace("💅 ", "").replace("🦶 ", "").replace("✨ ", ""))
                safe_client = html.escape(client or "Клиент")
                if confirmed:
                    lines.append(f"  🔴 <code>{t}</code> — <b>ЗАНЯТО</b> ({safe_client} | {short_srv})")
                else:
                    lines.append(f"  🟡 <code>{t}</code> — <b>ЖДЕТ ПОДТВЕРЖДЕНИЯ</b> ({safe_client} | {short_srv})")
            else:
                lines.append(f"  🟢 <code>{t}</code> — Свободно")
        lines.append("")

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=get_admin_reply_kb())


@dp.message(F.from_user.id == ADMIN_ID, F.text == "📅 Обновить расписание")
async def admin_update_schedule_btn(message: types.Message, state: FSMContext):
    await state.set_state(AdminState.waiting_schedule)
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Отмена (Домой)", callback_data="go_home")
    await message.answer(
        "✍️ <b>Отправьте новые окошки</b>. Старые свободные даты сохранятся, а новые добавятся к ним!\n\n"
        "Пример:\n"
        "<code>Расписание на ОКТЯБРЬ\n"
        "21 ср\n 12:00\n"
        "26 пн\n 12:00;14:00;16:00</code>",
        reply_markup=kb.as_markup(),
        parse_mode="HTML"
    )

@dp.message(AdminState.waiting_schedule, F.from_user.id == ADMIN_ID, F.text)
async def admin_process_new_schedule(message: types.Message, state: FSMContext):
    parsed, month_name = parse_master_schedule(message.text)
    if not parsed:
        await message.answer("Не удалось распознать даты и время. Попробуйте еще раз:")
        return

    await state.clear()
    added_count = sync_free_slots_db(parsed)

    bot_info = await bot.get_me()
    channel_kb = InlineKeyboardBuilder()
    channel_kb.button(text="💅 Записаться онлайн", url=f"https://t.me/{bot_info.username}")
    channel_text = generate_channel_post(month_name)

    channel_status = "✅ Опубликовано в канале!"
    try:
        await bot.send_message(CHANNEL_ID, channel_text, reply_markup=channel_kb.as_markup(), parse_mode="HTML")
    except Exception:
        channel_status = f"⚠️ Не удалось отправить в канал ({CHANNEL_ID})."

    users = get_all_users()
    push_kb = InlineKeyboardBuilder()
    push_kb.button(text="💅 Записаться", callback_data="open_booking")
    sent_count = 0
    for uid in users:
        try:
            await bot.send_message(
                uid,
                f"🌸 <b>Появились новые окошки на {month_name}!</b>\n"
                f"Успевайте занять удобное время 👇",
                reply_markup=push_kb.as_markup(),
                parse_mode="HTML"
            )
            sent_count += 1
        except Exception:
            pass

    await message.answer(
        f"✅ <b>Расписание дополнено новыми окошками!</b> (Добавлено новых слотов: {added_count})\n"
        f"📢 Канал: {channel_status}\n"
        f"🔔 Пуш-уведомлений отправлено клиентам: {sent_count}\n\n"
        f"Текст поста:\n\n{channel_text}",
        reply_markup=get_admin_reply_kb(),
        parse_mode="HTML"
    )

@dp.message(F.from_user.id == ADMIN_ID, F.text, ~F.text.startswith("/"))
async def admin_free_text_handler(message: types.Message, state: FSMContext):
    if await state.get_state() is not None:
        return

    urgent = parse_manual_urgent_booking(message.text)
    if urgent and not re.search(r"\b(12:00;14:00|расписание)\b", message.text.lower()):
        date_str, time_str, name, service_desc = urgent
        add_manual_booking_db(date_str, time_str, name, service_desc)
        
        del_kb = InlineKeyboardBuilder()
        del_kb.button(text="❌ Удалить эту запись", callback_data=f"cancel_booking:{date_str}:{time_str}")

        today_str = get_now().strftime("%Y-%m-%d")
        past_warning = (
            "\n\n⚠️ <i>Обратите внимание: эта дата уже в прошлом, поэтому она не отображается в списке будущих записей!</i>"
            if date_str < today_str else ""
        )

        await message.answer(
            f"✅ <b>Запись вне времени добавлена в расписание!</b>\n\n"
            f"👤 Клиент: <b>{html.escape(name)}</b>\n"
            f"💅 Заметка: {html.escape(service_desc)}\n"
            f"📅 День: {format_date_human(date_str)}\n"
            f"🕒 Время: <b>{time_str}</b>{past_warning}",
            reply_markup=del_kb.as_markup(),
            parse_mode="HTML"
        )
        return

    parsed, month_name = parse_master_schedule(message.text)
    if parsed:
        await admin_process_new_schedule(message, state)

# ================= ПОДТВЕРЖДЕНИЕ И ФОНОВЫЙ ВОРКЕР =================

@dp.callback_query(F.data.startswith("conf_yes:"))
async def cb_client_confirm_visit(call: types.CallbackQuery):
    _, date_str, time_str = call.data.split(":", 2)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT is_booked, client_name, service, service_key FROM slots WHERE date_str = ? AND time_str = ?", (date_str, time_str))
    row = c.fetchone()
    if not row or row[0] == 0:
        conn.close()
        await call.message.edit_text("Эта запись уже была отменена 😔")
        return

    # Если это комплекс (2 слота подряд), подтверждаем оба слота сразу
    slots_to_conf = [time_str]
    if row[3] == "combo":
        if time_str == "12:00":
            slots_to_conf.append("14:00")
        elif time_str == "14:00":
            slots_to_conf.append("16:00")

    for t in slots_to_conf:
        c.execute("UPDATE slots SET confirmed = 1 WHERE date_str = ? AND time_str = ?", (date_str, t))
    conn.commit()
    conn.close()

    await call.message.edit_text(
        f"✅ <b>Спасибо! Ваша запись на {format_date_human(date_str)} в {time_str} подтверждена!</b>\n"
        f"Ждем вас по адресу: Ибрагимова 71, 2-ой подъезд, 1-ый этаж, 23-я квартира 🤍",
        parse_mode="HTML"
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            f"✅ Клиент <b>{html.escape(row[1] or '')}</b> подтвердил(а) запись на <b>{format_date_human(date_str)} в {time_str}</b> ({html.escape(row[2] or '')})!",
            parse_mode="HTML"
        )
    except Exception:
        pass

@dp.callback_query(F.data.startswith("adm_restore:"))
async def cb_admin_restore_booking(call: types.CallbackQuery):
    _, date_str, time_str = call.data.split(":", 2)
    ok = restore_booking_db(date_str, time_str)
    if not ok:
        await call.answer("Не удалось восстановить: окошко уже занято другим клиентом!", show_alert=True)
        return

    await call.message.edit_text(
        call.message.text + "\n\n✅ <b>Запись успешно восстановлена и подтверждена! Окошко снова закрыто.</b>",
        parse_mode="HTML"
    )
    await call.answer("Запись восстановлена!")

async def background_reminder_worker():
    while True:
        try:
            now = get_now()
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()

            # 1. НАПОМИНАНИЯ ЗА 24 ЧАСА
            c.execute("""
                SELECT date_str, time_str, client_name, client_tg_id, service 
                FROM slots 
                WHERE is_booked = 1 AND reminded = 0 AND confirmed = 0 AND client_tg_id > 0 AND service NOT LIKE '↳%'
            """)
            to_remind = c.fetchall()

            for date_str, time_str, name, tg_id, srv in to_remind:
                visit_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                hours_left = (visit_dt - now).total_seconds() / 3600

                if 0 < hours_left <= 24:
                    kb = InlineKeyboardBuilder()
                    kb.button(text="✅ Подтверждаю запись", callback_data=f"conf_yes:{date_str}:{time_str}")
                    try:
                        await bot.send_message(
                            tg_id,
                            f"🔔 <b>Напоминание о записи!</b>\n\n"
                            f"Здравствуйте, {html.escape(name or '')}☀️ Напоминаем, что вы записаны:\n"
                            f"💅 Процедура: {html.escape(srv or '')}\n"
                            f"📅 День: {format_date_human(date_str)}\n"
                            f"🕒 Время: {time_str}\n"
                            f"📍 Адрес: Ибрагимова 71, 2-ой подъезд, 1-ый этаж, 23-я квартира\n\n"
                            f"Пожалуйста, нажмите кнопку ниже для подтверждения визита.\n"
                            f"<i>⚠️ На подтверждение записи у вас есть 6 часов (иначе она отменится автоматически).</i>",
                            reply_markup=kb.as_markup(),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

                    c.execute(
                        "UPDATE slots SET reminded = 1, reminded_at = ? WHERE date_str = ? AND time_str = ?",
                        (now.strftime("%Y-%m-%d %H:%M:%S"), date_str, time_str)
                    )
                    conn.commit()

            # 2. АВТО-ОТМЕНА ЧЕРЕЗ 6 ЧАСОВ БЕЗ ПОДТВЕРЖДЕНИЯ
            c.execute("""
                SELECT date_str, time_str, client_name, client_phone, client_username, client_tg_id, service, reminded_at
                FROM slots
                WHERE is_booked = 1 AND reminded = 1 AND confirmed = 0 AND reminded_at IS NOT NULL AND service NOT LIKE '↳%'
            """)
            unconfirmed = c.fetchall()

            # 3. ПУШ-БЛАГОДАРНОСТЬ ПОСЛЕ ВИЗИТА (без удаления записи из БД!)
            c.execute("""
                SELECT date_str, time_str, client_name, client_tg_id, service, service_key 
                FROM slots 
                WHERE is_booked = 1 AND confirmed = 1 AND thanks_sent = 0 AND client_tg_id > 0 AND service NOT LIKE '↳%'
            """)
            completed_visits = c.fetchall()

            for date_str, time_str, name, tg_id, srv, srv_key in completed_visits:
                visit_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                duration_hours = 4.0 if srv_key == "combo" else 2.0
                end_dt = visit_dt + timedelta(hours=duration_hours)
                time_passed_mins = (now - end_dt).total_seconds() / 60

                if 0 <= time_passed_mins <= 60:
                    thanks_kb = InlineKeyboardBuilder()
                    thanks_kb.button(text="💅 Записаться снова", callback_data="open_booking")

                    try:
                        await bot.send_message(
                            tg_id,
                            f"💖 <b>Спасибо, что выбрали Linails!</b>\n\n"
                            f"Здравствуйте, {html.escape(name or '')}! Надеемся, вам очень понравилась процедура ({html.escape(srv or '')}) ✨\n"
                            f"Мы всегда ждем вас снова. Будем рады видеть вас у нас еще! 🤍",
                            reply_markup=thanks_kb.as_markup(),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

                    c.execute(
                        "UPDATE slots SET thanks_sent = 1 WHERE date_str = ? AND time_str = ?",
                        (date_str, time_str)
                    )
                    conn.commit()

            conn.close()

            for date_str, time_str, name, phone, username, tg_id, srv, reminded_at_str in unconfirmed:
                rem_dt = datetime.strptime(reminded_at_str, "%Y-%m-%d %H:%M:%S")
                if (now - rem_dt).total_seconds() >= 6 * 3600:
                    cancel_booking_db(date_str, time_str, save_backup=True)

                    if tg_id:
                        try:
                            await bot.send_message(
                                tg_id,
                                f"❌ <b>Ваша запись на {format_date_human(date_str)} в {time_str} была отменена</b>, "
                                f"так как не была подтверждена в течение 6 часов.\n\n"
                                f"Если вы просто не успели нажать кнопку — напишите мастеру в ЛС!",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass

                    tg_tag = f"@{username}" if username and username != "нет" else "скрыт"
                    adm_kb = InlineKeyboardBuilder()
                    adm_kb.button(
                        text="🔄 Это ошибка, запись подтверждена",
                        callback_data=f"adm_restore:{date_str}:{time_str}"
                    )
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"⚠️ <b>Авто-отмена записи (прошло 6 часов без подтверждения)!</b>\n\n"
                            f"👤 Клиент: <b>{html.escape(name or '')}</b> ({tg_tag})\n"
                            f"📞 Тел: <code>{html.escape(phone or '')}</code>\n"
                            f"📅 Когда: {format_date_human(date_str)} в {time_str} ({html.escape(srv or '')})\n\n"
                            f"<i>Окошко снова открыто. Если клиент подтвердил визит в ЛС — нажмите кнопку восстановления:</i>",
                            reply_markup=adm_kb.as_markup(),
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

        except Exception as e:
            print(f"Ошибка в фоновой проверке: {e}")

        await asyncio.sleep(60)

async def main():
    init_db()
    asyncio.create_task(background_reminder_worker())
    print("Бот запущен со всеми обновлениями!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
