import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8648486445:AAFYevP0JxejfsQ1RuH-n-6p4EtWWJWVTHs")
ADMIN_ID = int(os.getenv("ADMIN_ID", 590568180))  # ID мастера
MASTER_USERNAME = os.getenv("MASTER_USERNAME", "LizaSalmi")  # Юзернейм мастера БЕЗ '@'
CHANNEL_ID = os.getenv("CHANNEL_ID", "@linails_kazan")  # Юзернейм канала с '@'

# Безопасный путь к базе данных для хостинга (чтобы данные не стирались)
DATA_DIR = os.getenv("DATA_DIR", ".")
DB_PATH = os.path.join(DATA_DIR, "nails_schedule.db")
# =============================================

logging.basicConfig(level=logging.INFO)


# Инициализация базы данных
def init_db():
  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()
  # Таблица слотов/расписания
  cursor.execute(
      """
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            time TEXT,
            is_booked INTEGER DEFAULT 0,
            client_id INTEGER,
            client_name TEXT,
            client_phone TEXT
        )
    """
  )
  conn.commit()
  conn.close()


init_db()

router = Router()


# Состояния для записи клиентом
class BookingState(StatesGroup):
  waiting_for_name = State()
  waiting_for_phone = State()


# Состояния для добавления слотов админом
class AdminState(StatesGroup):
  waiting_for_slots = State()


# --- ГЛАВНОЕ МЕНЮ ---
def get_main_menu(user_id: int):
  keyboard = [
      [InlineKeyboardButton(text="💅 Записаться", callback_data="client_book")],
      [
          InlineKeyboardButton(
              text="📅 Мои записи", callback_data="client_my_records"
          )
      ],
  ]
  if user_id == ADMIN_ID:
    keyboard.append(
        [
            InlineKeyboardButton(
                text="⚙️ Панель администратора", callback_data="admin_panel"
            )
        ]
    )
  return InlineKeyboardMarkup(inline_keyboard=keyboard)


@router.message(F.text == "/start")
async def cmd_start(message: Message):
  await message.answer(
      "Приветствую! 🌸 Добро пожаловать в салон красоты Linails (Казань).\nВыберите нужное действие:",
      reply_markup=get_main_menu(message.from_user.id),
  )


# --- КЛИЕНТСКАЯ ЧАСТЬ: ЗАПИСЬ ---
@router.callback_query(F.data == "client_book")
async def client_book_handler(callback: CallbackQuery):
  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT id, date, time FROM slots WHERE is_booked = 0 ORDER BY date,"
      " time"
  )
  slots = cursor.fetchall()
  conn.close()

  if not slots:
    await callback.message.edit_text(
        "К сожалению, сейчас нет свободных окошек для записи. Загляните позже!"
        " 😔",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="« Назад в меню", callback_data="back_to_menu"
                    )
                ]
            ]
        ),
    )
    return

  keyboard = []
  for slot_id, date, time in slots:
    keyboard.append([
        InlineKeyboardButton(
            text=f"📅 {date} ⏰ {time}", callback_data=f"book_slot_{slot_id}"
        )
    ])
  keyboard.append([
      InlineKeyboardButton(text="« Назад в меню", callback_data="back_to_menu")
  ])

  await callback.message.edit_text(
      "Выберите свободное окошко для записи:",
      reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
  )


@router.callback_query(F.data.startswith("book_slot_"))
async def select_slot(callback: CallbackQuery, state: FSMContext):
  slot_id = int(callback.data.split("_")[2])
  await state.update_data(slot_id=slot_id)

  await callback.message.answer(
      "Введите ваше **Имя** (как к вам обращаться):", parse_mode="Markdown"
  )
  await state.set_state(BookingState.waiting_for_name)
  await callback.answer()


@router.message(BookingState.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
  await state.update_data(client_name=message.text)
  await message.answer(
      "Теперь отправьте ваш **номер телефона** (для связи):",
      parse_mode="Markdown",
  )
  await state.set_state(BookingState.waiting_for_phone)


@router.message(BookingState.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
  phone = message.text
  data = await state.get_data()
  slot_id = data["slot_id"]
  client_name = data["client_name"]
  client_id = message.from_user.id

  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()
  # Проверяем, не занял ли слот кто-то другой параллельно
  cursor.execute("SELECT is_booked, date, time FROM slots WHERE id = ?", (slot_id,))
  slot = cursor.fetchone()

  if not slot or slot[0] == 1:
    conn.close()
    await message.answer(
        "Эту окошко уже кто-то успел занять! Пожалуйста, выберите другое.",
        reply_markup=get_main_menu(message.from_user.id),
    )
    await state.clear()
    return

  date, time = slot[1], slot[2]
  cursor.execute(
      """
        UPDATE slots 
        SET is_booked = 1, client_id = ?, client_name = ?, client_phone = ? 
        WHERE id = ?
    """,
      (client_id, client_name, phone, slot_id),
  )
  conn.commit()
  conn.close()

  await state.clear()
  await message.answer(
      f"✅ **Вы успешно записаны!**\n\n📅 Дата: {date}\n⏰ Время: {time}\n💅 Мастер"
      " ждет вас!",
      parse_mode="Markdown",
      reply_markup=get_main_menu(message.from_user.id),
  )

  # Уведомление администратору
  try:
    await message.bot.send_message(
        ADMIN_ID,
        f"🔔 **Новая запись!**\n\n👤 Клиент: {client_name}\n📞 Телефон:"
        f" {phone}\n📅 Дата: {date}\n⏰ Время: {time}",
        parse_mode="Markdown",
    )
  except Exception:
    pass


# --- МОИ ЗАПИСИ КЛИЕНТА ---
@router.callback_query(F.data == "client_my_records")
async def client_my_records(callback: CallbackQuery):
  client_id = callback.from_user.id
  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT id, date, time FROM slots WHERE client_id = ? AND is_booked = 1",
      (client_id,),
  )
  records = cursor.fetchall()
  conn.close()

  if not records:
    await callback.message.edit_text(
        "У вас пока нет активных записей.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="« Назад в меню", callback_data="back_to_menu"
                    )
                ]
            ]
        ),
    )
    return

  keyboard = []
  for rec_id, date, time in records:
    keyboard.append([
        InlineKeyboardButton(
            text=f"❌ Отменить ({date} {time})",
            callback_data=f"cancel_record_{rec_id}",
        )
    ])
  keyboard.append([
      InlineKeyboardButton(text="« Назад в меню", callback_data="back_to_menu")
  ])

  await callback.message.edit_text(
      "Ваши активные записи (вы можете отменить их при необходимости):",
      reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
  )


@router.callback_query(F.data.startswith("cancel_record_"))
async def cancel_record(callback: CallbackQuery):
  rec_id = int(callback.data.split("_")[2])
  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()
  cursor.execute(
      """
        UPDATE slots 
        SET is_booked = 0, client_id = NULL, client_name = NULL, client_phone ="
        NULL 
        WHERE id = ?
    """,
      (rec_id,),
  )
  conn.commit()
  conn.close()

  await callback.answer("Запись успешно отменена!", show_alert=True)
  await callback.message.edit_text(
      "Запись отменена.",
      reply_markup=InlineKeyboardMarkup(
          inline_keyboard=[
              [
                  InlineKeyboardButton(
                      text="« Назад в меню", callback_data="back_to_menu"
                  )
              ]
          ]
      ),
  )


# --- ПАНЕЛЬ АДМИНИСТРАТОРА ---
@router.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
  if callback.from_user.id != ADMIN_ID:
    await callback.answer("У вас нет доступа к этой панели.", show_alert=True)
    return

  keyboard = [
      [
          InlineKeyboardButton(
              text="➕ Добавить окошки", callback_data="admin_add_slots"
          )
      ],
      [
          InlineKeyboardButton(
              text="📋 Просмотр записей", callback_data="admin_view_records"
          )
      ],
      [
          InlineKeyboardButton(text="« Назад в меню", callback_data="back_to_menu")
      ],
  ]
  await callback.message.edit_text(
      "⚙️ **Панель администратора**",
      parse_mode="Markdown",
      reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
  )


@router.callback_query(F.data == "admin_add_slots")
async def admin_add_slots_prompt(callback: CallbackQuery, state: FSMContext):
  if callback.from_user.id != ADMIN_ID:
    return
  await callback.message.edit_text(
      "Введите новые окошки в формате:\n`ДД.ММ.ГГГГ ЧЧ:ММ`\n(Каждое окошко с"
      " новой строки)\n\nПример:\n`25.10.2026 10:00`\n`25.10.2026 14:00`",
      parse_mode="Markdown",
      reply_markup=InlineKeyboardMarkup(
          inline_keyboard=[
              [
                  InlineKeyboardButton(
                      text="« Назад", callback_data="admin_panel"
                  )
              ]
          ]
      ),
  )
  await state.set_state(AdminState.waiting_for_slots)


@router.message(AdminState.waiting_for_slots)
async def admin_save_slots(message: Message, state: FSMContext):
  if message.from_user.id != ADMIN_ID:
    return

  lines = message.text.strip().split("\n")
  added_count = 0

  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()

  for line in lines:
    parts = line.strip().split()
    if len(parts) == 2:
      date_str, time_str = parts
      cursor.execute(
          "INSERT INTO slots (date, time, is_booked) VALUES (?, ?, 0)",
          (date_str, time_str),
      )
      added_count += 1

  conn.commit()
  conn.close()
  await state.clear()

  await message.answer(
      f"✅ Успешно добавлено свободных окошек: {added_count}",
      reply_markup=InlineKeyboardMarkup(
          inline_keyboard=[
              [
                  InlineKeyboardButton(
                      text="⚙️ В админ-панель", callback_data="admin_panel"
                  )
              ]
          ]
      ),
  )


@router.callback_query(F.data == "admin_view_records")
async def admin_view_records(callback: CallbackQuery):
  if callback.from_user.id != ADMIN_ID:
    return

  conn = sqlite3.connect(DB_PATH)
  cursor = conn.cursor()
  cursor.execute(
      "SELECT date, time, client_name, client_phone FROM slots WHERE is_booked ="
      " 1 ORDER BY date, time"
  )
  records = cursor.fetchall()
  conn.close()

  if not records:
    await callback.message.edit_text(
        "На данный момент нет записей клиентов.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="« Назад", callback_data="admin_panel"
                    )
                ]
            ]
        ),
    )
    return

  text = "📋 **Актуальные записи клиентов:**\n\n"
  for date, time, name, phone in records:
    text += f"📅 {date} ⏰ {time}\n👤 {name}\n📞 {phone}\n-------------------\n"

  await callback.message.edit_text(
      text,
      parse_mode="Markdown",
      reply_markup=InlineKeyboardMarkup(
          inline_keyboard=[
              [InlineKeyboardButton(text="« Назад", callback_data="admin_panel")]
          ]
      ),
  )


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
  await callback.message.edit_text(
      "Главное меню:", reply_markup=get_main_menu(callback.from_user.id)
  )


async def main():
  bot = Bot(
      token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
  )
  dp = Dispatcher()
  dp.include_router(router)

  await bot.delete_webhook(drop_pending_updates=True)
  logging.info("Бот запущен")
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
