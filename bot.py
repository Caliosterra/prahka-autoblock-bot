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
from aiogram.exceptions import TelegramBadRequest
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


def apply_log_level(level_name: str):
    lvl = getattr(logging, level_name.upper(), logging.INFO)
    logging.getLogger().setLevel(lvl)
    # Сторонние библиотеки держим в тихом режиме
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING if lvl >= logging.INFO else logging.INFO)

# Заполнение базовых окон активности при первом запуске
if not db.get_windows():
    for w in [("20:58", "21:05"), ("21:58", "22:05"), ("23:58", "00:05")]:
        db.add_window(w[0], w[1])


class Form(StatesGroup):
    waiting_for_room = State()
    waiting_for_custom_date = State()
    waiting_for_window = State()
    waiting_for_sheet_url = State()


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

    sheet_url = db.get_setting("sheet_url")
    if sheet_url:
        return client.open_by_url(sheet_url).worksheet(SHEET_NAME)

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
    logging.info("🎯 Снайпер запущен в фоновом режиме.")
    try:
        sheet = await asyncio.to_thread(get_worksheet)
        logging.info(f"Успешное подключение к таблице: «{sheet.spreadsheet.title}», лист: «{sheet.title}»")
    except Exception as e:
        runtime.is_running = False
        runtime.mode_str = "Ошибка"
        logging.exception("Не удалось открыть Google Таблицу:")
        await bot.send_message(chat_id, f"❌ Ошибка подключения к таблице: {e}")
        return

    try:
        headers = await asyncio.to_thread(sheet.batch_get, ["A2", "M2"])
        runtime.known_left = headers[0][0][0] if (headers[0] and headers[0][0]) else ""
        runtime.known_right = headers[1][0][0] if (headers[1] and headers[1][0]) else ""
        logging.info(f"Начальные заголовки: Левый='{runtime.known_left}', Правый='{runtime.known_right}'")
    except Exception:
        logging.warning("Не удалось прочитать начальные заголовки A2/M2.")

    while runtime.is_running:
        await smart_sleep()
        if not runtime.is_running:
            break

        target_date = db.get_setting("target_date")
        room = db.get_setting("room", DEFAULT_ROOM)
        targets = db.get_targets()

        try:
            grid = await asyncio.to_thread(sheet.get_values, "A1:W20")
            now = datetime.now(MSK_TZ)
            runtime.last_ping = now.strftime("%H:%M:%S")

            if len(grid) < 2:
                logging.warning("Таблица вернула менее 2 строк данных.")
                continue

            left_h = grid[1][0] if len(grid[1]) > 0 else ""
            right_h = grid[1][12] if len(grid[1]) > 12 else ""

            logging.debug(
                f"[{runtime.last_ping}] Опрос таблицы. Цель: '{target_date}'. В таблице: A2='{left_h}', M2='{right_h}'"
            )

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
                logging.info(f"Обнаружено обновление таблицы в {runtime.last_ping}! Новое окно: {s_win}–{e_win}")
                await bot.send_message(
                    chat_id,
                    f"🧠 **Таблица обновилась в {runtime.last_ping}!**\nДобавлено горячее окно `{s_win}–{e_win}`.",
                    parse_mode="Markdown"
                )

            # Поиск целевой даты в шапках
            base_col = None
            if target_date and target_date in left_h:
                base_col = 0
                logging.info(f"🎯 Найдена целевая дата '{target_date}' в ЛЕВОМ блоке (A2)!")
            elif target_date and target_date in right_h:
                base_col = 12
                logging.info(f"🎯 Найдена целевая дата '{target_date}' в ПРАВОМ блоке (M2)!")
            else:
                logging.debug(f"Целевая дата '{target_date}' пока не появилась в таблице.")

            if base_col is not None:
                # Разделяем слоты на категории: Стирка (М) и Сушка (С)
                wash_targets = [t for t in targets if t[1].startswith("М")]
                dry_targets = [t for t in targets if t[1].startswith("С")]

                categories_to_book = []
                if wash_targets:
                    categories_to_book.append(("Стирка", wash_targets))
                if dry_targets:
                    categories_to_book.append(("Сушка", dry_targets))

                # Если заданы слоты без префиксов М/С, обрабатываем их общим списком
                other_targets = [t for t in targets if not t[1].startswith("М") and not t[1].startswith("С")]
                if other_targets:
                    categories_to_book.append(("Остальное", other_targets))

                successful_bookings = []
                failed_categories = []

                for cat_name, cat_slots in categories_to_book:
                    cat_booked = False
                    logging.info(f"Начинаю бронирование категории: {cat_name} (вариантов: {len(cat_slots)})")

                    # Проверяем, настроена ли сдвоенная бронь (2 часа подряд на одной машинке)
                    double_slot_pairs = []
                    i = 0
                    while i < len(cat_slots) - 1:
                        t1_str, m1 = cat_slots[i]
                        t2_str, m2 = cat_slots[i + 1]
                        
                        h1 = int(t1_str.split(":")[0])
                        h2 = int(t2_str.split(":")[0])
                        
                        # Если одинаковая машинка и время идет подряд (например, 18:00 и 19:00)
                        if m1 == m2 and h2 == h1 + 1:
                            double_slot_pairs.append(((t1_str, m1), (t2_str, m2)))
                            i += 2
                        else:
                            i += 1

                    # 1. Сначала пробуем занять сдвоенные слоты (если они были заданы)
                    for (slot1, slot2) in double_slot_pairs:
                        t1_str, m_code = slot1
                        t2_str, _ = slot2

                        def get_slot_coord(t_str, m_cd):
                            tc = t_str.strip()
                            if ":" not in tc: tc = f"{tc}:00"
                            if tc.startswith("0") and len(tc) == 5: tc = tc[1:]
                            if tc not in HOURS_TO_ROW or m_cd not in MACHINE_OFFSETS:
                                return None
                            r_idx = HOURS_TO_ROW[tc] - 1
                            c_idx = base_col + MACHINE_OFFSETS[m_cd][0]
                            return r_idx, c_idx, f"{col_to_letter(c_idx)}{r_idx + 1}"

                        c1 = get_slot_coord(t1_str, m_code)
                        c2 = get_slot_coord(t2_str, m_code)
                        if not c1 or not c2:
                            continue

                        r1_idx, col_idx, cell1 = c1
                        r2_idx, _, cell2 = c2

                        val1 = grid[r1_idx][col_idx].strip() if r1_idx < len(grid) and col_idx < len(grid[r1_idx]) else ""
                        val2 = grid[r2_idx][col_idx].strip() if r2_idx < len(grid) and col_idx < len(grid[r2_idx]) else ""

                        logging.info(f"[{cat_name}] Проверка 2х часов подряд: {t1_str}+{t2_str} {m_code} ({cell1}, {cell2}): '{val1}', '{val2}'")

                        if is_free(val1) and is_free(val2):
                            logging.info(f"Оба слота {cell1} и {cell2} свободны! Записываю {room}...")
                            await asyncio.to_thread(sheet.update_acell, cell1, room)
                            await asyncio.to_thread(sheet.update_acell, cell2, room)
                            await asyncio.sleep(0.7)

                            chk1 = (await asyncio.to_thread(sheet.acell, cell1)).value or ""
                            chk2 = (await asyncio.to_thread(sheet.acell, cell2)).value or ""

                            if chk1.strip() == room and chk2.strip() == room:
                                logging.info(f"✅ Успешно забронированы 2 часа подряд: {cell1} и {cell2}")
                                successful_bookings.append({
                                    "cat": cat_name,
                                    "time": f"{t1_str} и {t2_str}",
                                    "mach": MACHINE_OFFSETS[m_code][1],
                                    "cell": f"{cell1}, {cell2}"
                                })
                                cat_booked = True
                                break
                            else:
                                logging.warning(f"Коллизия при сдвоенной брони ({cell1}='{chk1}', {cell2}='{chk2}')")
                                await bot.send_message(
                                    chat_id,
                                    f"⚠️ Коллизия при брони 2х часов на `{m_code}`. Пробую следующий вариант...",
                                    parse_mode="Markdown"
                                )

                    if cat_booked:
                        continue

                    # 2. Если сдвоенные слоты не нашли или они не заданы, пробуем одиночные слоты по порядку
                    for time_str, mach_code in cat_slots:
                        t_clean = time_str.strip()
                        if ":" not in t_clean: t_clean = f"{t_clean}:00"
                        if t_clean.startswith("0") and len(t_clean) == 5: t_clean = t_clean[1:]

                        if t_clean not in HOURS_TO_ROW or mach_code not in MACHINE_OFFSETS:
                            continue

                        row_idx = HOURS_TO_ROW[t_clean] - 1
                        row_num = row_idx + 1
                        offset, mach_name = MACHINE_OFFSETS[mach_code]
                        col_idx = base_col + offset
                        cell = f"{col_to_letter(col_idx)}{row_num}"

                        row_data = grid[row_idx] if row_idx < len(grid) else []
                        val = row_data[col_idx].strip() if col_idx < len(row_data) else ""

                        logging.info(f"[{cat_name}] Проверка одиночного слота {t_clean} {mach_name} ({cell}): '{val}'")

                        if is_free(val):
                            logging.info(f"Ячейка {cell} свободна, записываю {room}...")
                            await asyncio.to_thread(sheet.update_acell, cell, room)
                            await asyncio.sleep(0.7)
                            check_val = (await asyncio.to_thread(sheet.acell, cell)).value or ""

                            if check_val.strip() == room:
                                # Обновляем локальную сетку, чтобы следующие проверки учитывали эту запись
                                while len(grid) <= row_idx:
                                    grid.append([])
                                while len(grid[row_idx]) <= col_idx:
                                    grid[row_idx].append("")
                                grid[row_idx][col_idx] = room

                                logging.info(f"✅ Успешно забронирован слот: {cell}")
                                successful_bookings.append({
                                    "cat": cat_name,
                                    "time": time_str,
                                    "mach": mach_name,
                                    "cell": cell
                                })
                                cat_booked = True
                                break
                            else:
                                logging.warning(f"Коллизия в {cell}: записалось '{check_val}' вместо '{room}'")
                                await bot.send_message(
                                    chat_id,
                                    f"⚠️ Коллизия в `{cell}` (занял `{check_val}`). Пробую дальше...",
                                    parse_mode="Markdown"
                                )
                        else:
                            logging.info(f"Слот {cell} занят значением: '{val}'. Пропускаю.")

                    if not cat_booked:
                        failed_categories.append(cat_name)

                # Завершение работы снайпера и отчёт пользователю
                runtime.is_running = False
                runtime.mode_str = "Завершено" if successful_bookings else "Остановлен"

                if successful_bookings:
                    lines = [f"• **{b['cat']}**: `{b['time']}` — {b['mach']} (`{b['cell']}`)" for b in successful_bookings]
                    booked_text = "\n".join(lines)
                    fail_text = f"\n\n⚠️ Не удалось занять: {', '.join(failed_categories)}" if failed_categories else ""

                    await bot.send_message(
                        chat_id,
                        f"✅ **УСПЕШНАЯ БРОНЬ!**\n\n"
                        f"📅 Дата: `{target_date}`\n"
                        f"🏠 Комната: `{room}`\n\n"
                        f"{booked_text}{fail_text}",
                        parse_mode="Markdown",
                        reply_markup=get_main_kb()
                    )
                else:
                    await bot.send_message(chat_id, "❌ Все выбранные слоты заняты!", reply_markup=get_main_kb())
                break

        except Exception:
            logging.exception("Исключение в итерации sniper_worker:")
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
        [InlineKeyboardButton(text="📅 Дата", callback_data="m_date"), InlineKeyboardButton(text="➕ Слот", callback_data="m_slot")],
        [InlineKeyboardButton(text="🗑 Очистить слоты", callback_data="c_slots"), InlineKeyboardButton(text="🔄 Обновить", callback_data="back")],
        [InlineKeyboardButton(text="🛠 Отладка и настройки", callback_data="m_debug")]
    ])


def get_debug_kb() -> InlineKeyboardMarkup:
    cur_log = db.get_setting("log_level", "INFO")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Проверить таблицу", callback_data="check_tbl"), InlineKeyboardButton(text="🔗 Ссылка на таблицу", callback_data="m_url")],
        [InlineKeyboardButton(text="🔥 Горячие окна", callback_data="m_win"), InlineKeyboardButton(text="🏠 Сменить комнату", callback_data="m_room")],
        [InlineKeyboardButton(text=f"📜 Логирование: [{cur_log}]", callback_data="m_log_lvl")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back")]
    ])


# --- Хэндлеры команд и кнопок ---
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(get_main_text(), parse_mode="Markdown", reply_markup=get_main_kb())


@dp.callback_query(F.data == "back")
async def cb_back(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text(get_main_text(), parse_mode="Markdown", reply_markup=get_main_kb())
    except TelegramBadRequest as err:
        if "message is not modified" not in str(err):
            raise err
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


@dp.callback_query(F.data == "m_debug")
async def cb_debug(call: CallbackQuery, state: FSMContext):
    await state.clear()
    cur_url = db.get_setting("sheet_url", "по умолчанию из .env")
    room = db.get_setting("room", DEFAULT_ROOM)
    cur_log = db.get_setting("log_level", "INFO")
    text = (
        "🛠 **Панель отладки и конфигурации**\n\n"
        f"🏠 **Текущая комната:** `{room}`\n"
        f"📜 **Уровень логов:** `{cur_log}`\n"
        f"🔗 **Целевая таблица:**\n`{cur_url}`\n\n"
        "Выберите действие:"
    )
    try:
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=get_debug_kb())
    except TelegramBadRequest as err:
        if "message is not modified" not in str(err):
            raise err
    await call.answer()


@dp.callback_query(F.data == "m_log_lvl")
async def cb_log_lvl_menu(call: CallbackQuery):
    cur_log = db.get_setting("log_level", "INFO")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'✅ ' if cur_log == 'DEBUG' else ''}🐞 DEBUG (Максимум)", callback_data="set_log:DEBUG")],
        [InlineKeyboardButton(text=f"{'✅ ' if cur_log == 'INFO' else ''}ℹ️ INFO (Стандартный)", callback_data="set_log:INFO")],
        [InlineKeyboardButton(text=f"{'✅ ' if cur_log == 'WARNING' else ''}⚠️ WARNING (Только ошибки / Сервер)", callback_data="set_log:WARNING")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="m_debug")]
    ])
    text = (
        "📜 **Настройка логирования бота**\n\n"
        "• **DEBUG** — подробные логи каждого запроса к Google Таблице и состояний ячеек (для локальной отладки).\n"
        "• **INFO** — фиксация стартов, бронирований и смены целевых дней.\n"
        "• **WARNING** — тихий режим, выводятся только критические ошибки и предупреждения (рекомендуется для VDS/сервера, чтобы не забивать диск)."
    )
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("set_log:"))
async def cb_set_log(call: CallbackQuery):
    lvl = call.data.split(":")[1]
    db.set_setting("log_level", lvl)
    apply_log_level(lvl)
    logging.warning(f"Уровень логирования изменён пользователем на: {lvl}")
    await cb_debug(call, None)


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
    if ":" not in hour:
        hour = f"{hour}:00"
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
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="m_debug")])
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


@dp.callback_query(F.data == "m_url")
async def cb_url(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_for_sheet_url)
    cur_url = db.get_setting("sheet_url", "по умолчанию (из конфигурации)")
    text = (
        f"🔗 **Текущая целевая таблица:**\n`{cur_url}`\n\n"
        "Отправьте сообщением новую ссылку на Google Таблицу (боевую или тестовую копию):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="m_debug")]])
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await call.answer()


@dp.message(Form.waiting_for_sheet_url)
async def proc_sheet_url(message: Message, state: FSMContext):
    url = message.text.strip()
    if not url.startswith("https://docs.google.com/spreadsheets/"):
        await message.answer("❌ Неверный формат. Отправьте валидную ссылку вида `https://docs.google.com/spreadsheets/d/...`")
        return

    old_url = db.get_setting("sheet_url")
    try:
        db.set_setting("sheet_url", url)
        sheet = await asyncio.to_thread(get_worksheet)
        title = sheet.spreadsheet.title
        await state.clear()
        await message.answer(
            f"✅ _Таблица успешно переключена на: «{title}»!_",
            parse_mode="Markdown",
            reply_markup=get_debug_kb()
        )
    except Exception as e:
        if old_url:
            db.set_setting("sheet_url", old_url)
        else:
            with db._get_connection() as conn:
                conn.execute("DELETE FROM settings WHERE key = 'sheet_url'")
                conn.commit()
        await message.answer(
            f"⚠️ Ошибка подключения к таблице:\n`{e}`\n\n"
            "Убедитесь, что сервисному аккаунту из `credentials.json` выдан доступ **Редактора**."
        )


# Комната
@dp.callback_query(F.data == "m_room")
async def cb_room(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_for_room)
    await call.message.edit_text(f"🏠 Текущая комната: `{db.get_setting('room', DEFAULT_ROOM)}`.\nВведите новый номер сообщением:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="m_debug")]]))
    await call.answer()


@dp.message(Form.waiting_for_room)
async def proc_room(message: Message, state: FSMContext):
    db.set_setting("room", message.text.strip())
    await state.clear()
    await message.answer(
        f"✅ _Комната сохранена: {message.text.strip()}_",
        parse_mode="Markdown",
        reply_markup=get_debug_kb()
    )


# Проверка
@dp.callback_query(F.data == "check_tbl")
async def cb_check_tbl(call: CallbackQuery):
    await call.answer("Читаю таблицу...")
    try:
        sheet = await asyncio.to_thread(get_worksheet)
        h = await asyncio.to_thread(sheet.batch_get, ["A2", "M2"])
        l = h[0][0][0] if (h[0] and h[0][0]) else "Пусто"
        r = h[1][0][0] if (h[1] and h[1][0]) else "Пусто"
        now_str = datetime.now(MSK_TZ).strftime("%H:%M:%S")
        text = f"🛠 **Результат проверки таблицы (на `{now_str}`):**\n\n• Слева (A2): `{l}`\n• Справа (M2): `{r}`"
        try:
            await call.message.edit_text(text, parse_mode="Markdown", reply_markup=get_debug_kb())
        except TelegramBadRequest as err:
            if "message is not modified" in str(err):
                await call.answer("Таблица проверена: изменений нет", show_alert=False)
            else:
                raise err
    except Exception as e:
        await call.message.edit_text(f"❌ **Ошибка подключения:**\n`{e}`", parse_mode="Markdown", reply_markup=get_debug_kb())


async def main():
    saved_lvl = db.get_setting("log_level", "INFO")
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    )
    apply_log_level(saved_lvl)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())