import os
import logging
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Tuple, Optional
from dotenv import load_dotenv

import gspread
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from google.oauth2.service_account import Credentials

from database import Database
from middlewares import AccessMiddleware

# Загрузка переменных из .env, сгенерированного deploy.sh
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env файле!")

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ALLOWED_USER_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit()]

SPREADSHEET_TITLE = os.getenv("SPREADSHEET_TITLE", "Прачка")
SHEET_NAME = os.getenv("SHEET_NAME", "Очередь")
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "credentials.json")
DEFAULT_ROOM = os.getenv("DEFAULT_ROOM", "284")
HOT_INTERVAL = float(os.getenv("HOT_INTERVAL_SEC", "1.05"))
IDLE_SECOND = int(os.getenv("IDLE_SECOND", "5"))

MSK_TZ = ZoneInfo("Europe/Moscow")

MACHINE_OFFSETS = {
    "М1": (1, "Машинка №1"),
    "М3": (3, "Машинка №3"),
    "М5": (5, "Машинка №5"),
    "С2": (7, "Сушилка №2"),
    "С4": (9, "Сушилка №4"),
}

HOURS_TO_ROW = {f"{h}:00": h - 3 for h in range(8, 23)}

# Инициализация базы данных
db = Database("prachka.db")

# Заполнение базовых окон активности при первом запуске
if not db.get_windows():
    for w in [("20:58", "21:05"), ("21:58", "22:05"), ("23:58", "00:05")]:
        db.add_window(w[0], w[1])


class Form(StatesGroup):
    waiting_for_room = State()
    waiting_for_custom_date = State()
    waiting_for_window = State()


class RuntimeState:
    def __init__(self):
        self.is_running: bool = False
        self.known_left: str = ""
        self.known_right: str = ""
        self.temp_hour: Optional[str] = None
        self.last_ping: str = "Никогда"
        self.mode_str: str = "Остановлен"
        self.task: Optional[asyncio.Task] = None


runtime = RuntimeState()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Подключение AccessMiddleware для сообщений и кнопок
dp.message.middleware(AccessMiddleware(ALLOWED_USER_IDS))
dp.callback_query.middleware(AccessMiddleware(ALLOWED_USER_IDS))


# --- Google Sheets ---
def get_worksheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open(SPREADSHEET_TITLE).worksheet(SHEET_NAME)


def col_to_letter(idx: int) -> str:
    return chr(ord("A") + idx)


def is_free(val: str) -> bool:
    return not val.strip()


# --- Тайминги и окна ---
def in_hot_window(now: datetime) -> Tuple[bool, Optional[str]]:
    cur_m = now.hour * 60 + now.minute
    for s_str, e_str in db.get_windows():
        sh, sm = map(int, s_str.split(":"))
        eh, em = map(int, e_str.split(":"))
        s_m, e_m = sh * 60 + sm, eh * 60 + em
        if s_m <= e_m:
            if s_m <= cur_m <= e_m:
                return True, f"{s_str}–{e_str}"
        else:
            if cur_m >= s_m or cur_m <= e_m:
                return True, f"{s_str}–{e_str}"
    return False, None


async def smart_sleep():
    now = datetime.now(MSK_TZ)
    is_hot, win_name = in_hot_window(now)
    if is_hot:
        runtime.mode_str = f"🔥 ТУРБО ({win_name}, 1 раз/сек)"
        await asyncio.sleep(HOT_INTERVAL)
    else:
        runtime.mode_str = "💤 Фоновый (1 раз/мин)"
        cur_sec = now.second + now.microsecond / 1_000_000.0
        wait_sec = IDLE_SECOND - cur_sec if cur_sec < IDLE_SECOND else (60.0 - cur_sec) + IDLE_SECOND
        await asyncio.sleep(max(0.5, wait_sec))


# --- Фоновый воркер-снайпер ---
async def sniper_worker(chat_id: int):
    try:
        sheet = await asyncio.to_thread(get_worksheet)
    except Exception as e:
        runtime.is_running = False
        await bot.send_message(chat_id, f"❌ Ошибка подключения к таблице: {e}")
        return

    try:
        headers = await asyncio.to_thread(sheet.batch_get, ["A2", "M2"])
        runtime.known_left = headers[0][0][0] if (headers[0] and headers[0][0]) else ""
        runtime.known_right = headers[1][0][0] if (headers[1] and headers[1][0]) else ""
    except Exception:
        pass

    target_date = db.get_setting("target_date")
    room = db.get_setting("room", DEFAULT_ROOM)
    targets = db.get_targets()

    while runtime.is_running:
        await smart_sleep()
        if not runtime.is_running:
            break

        try:
            grid = await asyncio.to_thread(sheet.get_values, "A1:W19")
            now = datetime.now(MSK_TZ)
            runtime.last_ping = now.strftime("%H:%M:%S")

            if len(grid) < 2:
                continue

            left_h = grid[1][0] if len(grid[1]) > 0 else ""
            right_h = grid[1][12] if len(grid[1]) > 12 else ""

            # Проверка обновления таблицы для самообучения
            changed = False
            if runtime.known_left and left_h and left_h != runtime.known_left:
                changed = True
            if runtime.known_right and right_h and right_h != runtime.known_right:
                changed = True

            runtime.known_left, runtime.known_right = left_h, right_h

            if changed:
                s_win = (now - timedelta(minutes=2)).strftime("%H:%M")
                e_win = (now + timedelta(minutes=3)).strftime("%H:%M")
                db.add_window(s_win, e_win)
                db.log_update(now.strftime("%d.%m %H:%M:%S"), f"{s_win}–{e_win}")
                await bot.send_message(
                    chat_id,
                    f"🧠 **Таблица обновилась в {runtime.last_ping}!**\nДобавлено горячее окно `{s_win}–{e_win}`.",
                    parse_mode="Markdown"
                )

            # Проверка целевой даты
            base_col = 0 if target_date in left_h else (12 if target_date in right_h else None)
            if base_col is not None:
                booked = False
                for time_str, mach_code in targets:
                    row_idx = HOURS_TO_ROW[time_str] - 1
                    row_num = row_idx + 1
                    offset, mach_name = MACHINE_OFFSETS[mach_code]
                    col_idx = base_col + offset
                    cell = f"{col_to_letter(col_idx)}{row_num}"

                    val = grid[row_idx][col_idx] if row_idx < len(grid) and col_idx < len(grid[row_idx]) else ""

                    if is_free(val):
                        await asyncio.to_thread(sheet.update_acell, cell, room)
                        await asyncio.sleep(0.7)
                        check_val = (await asyncio.to_thread(sheet.acell, cell)).value or ""

                        if check_val.strip() == room:
                            runtime.is_running = False
                            runtime.mode_str = "Завершено"
                            await bot.send_message(
                                chat_id,
                                f"✅ **УСПЕШНАЯ БРОНЬ!**\n\n"
                                f"📅 Дата: `{target_date}`\n"
                                f"⏰ Время: `{time_str}`\n"
                                f"🧺 Машинка: `{mach_name}` (`{cell}`)\n"
                                f"🏠 Записано: `{room}`",
                                parse_mode="Markdown",
                                reply_markup=get_main_kb()
                            )
                            booked = True
                            break
                        else:
                            await bot.send_message(
                                chat_id,
                                f"⚠️ Коллизия в `{cell}` (занял `{check_val}`). Пробую следующий слот...",
                                parse_mode="Markdown"
                            )

                if not booked:
                    runtime.is_running = False
                    runtime.mode_str = "Остановлен"
                    await bot.send_message(chat_id, "❌ Все выбранные слоты заняты!", reply_markup=get_main_kb())
                break

        except Exception as e:
            logging.error(f"Worker error: {e}")
            await asyncio.sleep(2)


# --- Клавиатуры и визуальное меню ---
def get_main_text(note: str = "") -> str:
    target_date = db.get_setting("target_date", "Не задана")
    room = db.get_setting("room", DEFAULT_ROOM)
    targets = db.get_targets()
    windows = db.get_windows()
    logs = db.get_recent_logs(2)

    status_str = "🟢 **АКТИВЕН**" if runtime.is_running else "⚪ **Остановлен**"
    targets_str = " → ".join([f"`{t[0]} {t[1]}`" for t in targets]) if targets else "_Не выбраны_"
    wins_str = ", ".join([f"`{w[0]}-{w[1]}`" for w in windows]) if windows else "_Нет_"
    logs_str = ", ".join([f"`{l}`" for l in logs]) if logs else "_Нет данных_"

    text = (
        f"🧺 **Панель управления снайпером**\n\n"
        f"🤖 **Статус:** {status_str} ({runtime.mode_str})\n"
        f"📅 **Дата:** `{target_date}` | 🏠 **Комната:** `{room}`\n"
        f"🕒 **Пинг:** `{runtime.last_ping}`\n\n"
        f"🔥 **Горячие окна (1 сек):** {wins_str}\n"
        f"🎯 **Срабатывания:** {logs_str}\n\n"
        f"📋 **Очередь брони:**\n{targets_str}"
    )
    if note:
        text += f"\n\n{note}"
    return text


def get_main_kb() -> InlineKeyboardMarkup:
    toggle_btn = (
        InlineKeyboardButton(text="🛑 Стоп", callback_data="stop")
        if runtime.is_running
        else InlineKeyboardButton(text="🚀 Старт", callback_data="start")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [toggle_btn],
        [InlineKeyboardButton(text="📅 Дата", callback_data="m_date"), InlineKeyboardButton(text="🔥 Окна", callback_data="m_win")],
        [InlineKeyboardButton(text="➕ Слот", callback_data="m_slot"), InlineKeyboardButton(text="🗑 Очистить", callback_data="c_slots")],
        [InlineKeyboardButton(text="🏠 Комната", callback_data="m_room"), InlineKeyboardButton(text="📊 Таблица", callback_data="check_tbl")],
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="back")]
    ])


# --- Хэндлеры команд и кнопок ---
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(get_main_text(), parse_mode="Markdown", reply_markup=get_main_kb())


@dp.callback_query(F.data == "back")
async def cb_back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(get_main_text(), parse_mode="Markdown", reply_markup=get_main_kb())
    await call.answer()


@dp.callback_query(F.data == "start")
async def cb_start(call: CallbackQuery):
    if not db.get_targets():
        await call.answer("Добавьте слоты перед запуском!", show_alert=True)
        return
    runtime.is_running = True
    runtime.task = asyncio.create_task(sniper_worker(call.message.chat.id))
    await call.message.edit_text(get_main_text("🎯 _Снайпер запущен!_"), parse_mode="Markdown", reply_markup=get_main_kb())
    await call.answer()


@dp.callback_query(F.data == "stop")
async def cb_stop(call: CallbackQuery):
    runtime.is_running = False
    runtime.mode_str = "Остановлен"
    if runtime.task:
        runtime.task.cancel()
    await call.message.edit_text(get_main_text("🛑 _Снайпер остановлен._"), parse_mode="Markdown", reply_markup=get_main_kb())
    await call.answer()


# Дата
@dp.callback_query(F.data == "m_date")
async def cb_date(call: CallbackQuery):
    now = datetime.now(MSK_TZ)
    buttons = []
    for i in range(4):
        d_str = (now + timedelta(days=i)).strftime("%d.%m.%Y")
        label = "Сегодня" if i == 0 else ("Завтра" if i == 1 else d_str)
        buttons.append([InlineKeyboardButton(text=f"📅 {d_str} ({label})", callback_data=f"set_d:{d_str}")])
    buttons.append([InlineKeyboardButton(text="⌨️ Ввести вручную", callback_data="custom_d")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    await call.message.edit_text("📅 **Выберите дату бронирования:**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@dp.callback_query(F.data.startswith("set_d:"))
async def cb_set_d(call: CallbackQuery):
    d = call.data.split(":")[1]
    db.set_setting("target_date", d)
    await call.message.edit_text(get_main_text(f"✅ _Дата установлена: {d}_"), parse_mode="Markdown", reply_markup=get_main_kb())
    await call.answer()


@dp.callback_query(F.data == "custom_d")
async def cb_custom_d(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_for_custom_date)
    await call.message.edit_text("⌨️ Введите дату `ДД.ММ.ГГГГ` сообщением:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back")]]))
    await call.answer()


@dp.message(Form.waiting_for_custom_date)
async def proc_custom_d(message: Message, state: FSMContext):
    try:
        datetime.strptime(message.text.strip(), "%d.%m.%Y")
        db.set_setting("target_date", message.text.strip())
        await state.clear()
        await message.answer(get_main_text(f"✅ _Дата: {message.text.strip()}_"), parse_mode="Markdown", reply_markup=get_main_kb())
    except ValueError:
        await message.answer("❌ Формат должен быть `ДД.ММ.ГГГГ`")


# Слоты
@dp.callback_query(F.data == "m_slot")
async def cb_slot(call: CallbackQuery):
    rows, row = [], []
    for h in range(8, 23):
        t = f"{h}:00"
        row.append(InlineKeyboardButton(text=t, callback_data=f"pick_h:{t}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    await call.message.edit_text("⏰ **Выберите время:**", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data.startswith("pick_h:"))
async def cb_pick_h(call: CallbackQuery):
    runtime.temp_hour = call.data.split(":")[1]
    kb = [
        [InlineKeyboardButton(text="🧺 М1", callback_data="pick_m:М1"), InlineKeyboardButton(text="🧺 М3", callback_data="pick_m:М3")],
        [InlineKeyboardButton(text="🧺 М5", callback_data="pick_m:М5")],
        [InlineKeyboardButton(text="💨 С2", callback_data="pick_m:С2"), InlineKeyboardButton(text="💨 С4", callback_data="pick_m:С4")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="m_slot")]
    ]
    await call.message.edit_text(f"🧺 Выберите машинку на `{runtime.temp_hour}`:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await call.answer()


@dp.callback_query(F.data.startswith("pick_m:"))
async def cb_pick_m(call: CallbackQuery):
    mach = call.data.split(":")[1]
    hour = runtime.temp_hour or "19:00"
    db.add_target(hour, mach)
    await call.message.edit_text(get_main_text(f"✅ _Добавлен слот: {hour} ({mach})_"), parse_mode="Markdown", reply_markup=get_main_kb())
    await call.answer()


@dp.callback_query(F.data == "c_slots")
async def cb_c_slots(call: CallbackQuery):
    db.clear_targets()
    await call.message.edit_text(get_main_text("🗑 _Слоты очищены_"), parse_mode="Markdown", reply_markup=get_main_kb())
    await call.answer()


# Окна
@dp.callback_query(F.data == "m_win")
async def cb_win(call: CallbackQuery):
    buttons = []
    for s, e in db.get_windows():
        # Было:
        # buttons.append([InlineKeyboardButton(text=f"❌ Удалить {s}–{e}", callback_data=f"del_w:{s}:{e}")])
        
        # Стало (разделяем дефисом или нижним подчеркиванием):
        buttons.append([InlineKeyboardButton(text=f"❌ Удалить {s}–{e}", callback_data=f"del_w:{s}_{e}")])
    buttons.append([
        InlineKeyboardButton(text="➕ 20:58-21:05", callback_data="add_w:20:58_21:05"),
        InlineKeyboardButton(text="➕ 21:58-22:05", callback_data="add_w:21:58_22:05"),
    ])
    buttons.append([InlineKeyboardButton(text="⌨️ Свой интервал", callback_data="custom_w")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    await call.message.edit_text("🔥 **Горячие окна активности:**", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@dp.callback_query(F.data.startswith("del_w:"))
async def cb_del_w(call: CallbackQuery):
    raw_times = call.data.split(":", 1)[1]  # Получаем "20:58_21:05"
    s, e = raw_times.split("_")             # Извлекаем s="20:58", e="21:05"
    db.delete_window(s, e)
    await cb_win(call)


@dp.callback_query(F.data.startswith("add_w:"))
async def cb_add_w(call: CallbackQuery):
    raw_times = call.data.split(":", 1)[1]  # Получаем "20:58_21:05"
    s, e = raw_times.split("_")
    db.add_window(s, e)
    await cb_win(call)


@dp.callback_query(F.data == "custom_w")
async def cb_custom_w(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_for_window)
    await call.message.edit_text("⌨️ Введите интервал `ЧЧ:ММ-ЧЧ:ММ` (например `21:25-21:40`):", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="m_win")]]))
    await call.answer()


@dp.message(Form.waiting_for_window)
async def proc_custom_w(message: Message, state: FSMContext):
    for part in message.text.split(","):
        if "-" in part:
            try:
                s, e = [x.strip() for x in part.split("-")]
                datetime.strptime(s, "%H:%M")
                datetime.strptime(e, "%H:%M")
                db.add_window(s, e)
            except ValueError:
                pass
    await state.clear()
    await message.answer(get_main_text("✅ _Окна обновлены_"), parse_mode="Markdown", reply_markup=get_main_kb())


# Комната
@dp.callback_query(F.data == "m_room")
async def cb_room(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_for_room)
    await call.message.edit_text(f"🏠 Текущая комната: `{db.get_setting('room', DEFAULT_ROOM)}`.\nВведите новый номер сообщением:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back")]]))
    await call.answer()


@dp.message(Form.waiting_for_room)
async def proc_room(message: Message, state: FSMContext):
    db.set_setting("room", message.text.strip())
    await state.clear()
    await message.answer(get_main_text(f"✅ _Комната: {message.text.strip()}_"), parse_mode="Markdown", reply_markup=get_main_kb())


# Проверка
@dp.callback_query(F.data == "check_tbl")
async def cb_check_tbl(call: CallbackQuery):
    await call.answer("Читаю таблицу...")
    try:
        sheet = await asyncio.to_thread(get_worksheet)
        h = await asyncio.to_thread(sheet.batch_get, ["A2", "M2"])
        l = h[0][0][0] if (h[0] and h[0][0]) else "Пусто"
        r = h[1][0][0] if (h[1] and h[1][0]) else "Пусто"
        note = f"📊 **Заголовки в таблице:**\n• Слева: `{l}`\n• Справа: `{r}`"
        await call.message.edit_text(get_main_text(note), parse_mode="Markdown", reply_markup=get_main_kb())
    except Exception as e:
        await call.message.edit_text(get_main_text(f"❌ Ошибка: {e}"), parse_mode="Markdown", reply_markup=get_main_kb())


async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())