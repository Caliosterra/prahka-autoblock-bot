import os
import logging
import asyncio
import random
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

db = Database("prachka.db")


def apply_log_level(level_name: str):
    lvl = getattr(logging, level_name.upper(), logging.INFO)
    logging.getLogger().setLevel(lvl)
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING if lvl >= logging.INFO else logging.INFO)


if not db.get_windows():
    for w in [("20:58", "21:05"), ("21:58", "22:05"), ("23:58", "00:05")]:
        db.add_window(w[0], w[1])


class Form(StatesGroup):
    waiting_for_room = State()
    waiting_for_custom_date = State()
    waiting_for_window = State()
    waiting_for_sheet_url = State()
    waiting_for_preset_name = State()


class RuntimeState:
    def __init__(self):
        self.is_running: bool = False
        self.known_left: str = ""
        self.known_right: str = ""
        self.last_ping: str = "Никогда"
        self.mode_str: str = "Остановлен"
        self.task: Optional[asyncio.Task] = None


runtime = RuntimeState()
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

dp.message.middleware(AccessMiddleware(ALLOWED_USER_IDS))
dp.callback_query.middleware(AccessMiddleware(ALLOWED_USER_IDS))


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

            base_col = None
            if target_date and target_date in left_h:
                base_col = 0
                logging.info(f"🎯 Найдена целевая дата '{target_date}' в ЛЕВОМ блоке (A2)!")
            elif target_date and target_date in right_h:
                base_col = 12
                logging.info(f"🎯 Найдена целевая дата '{target_date}' в ПРАВОМ блоке (M2)!")

            if base_col is not None:
                wash_targets = [t for t in targets if t[1].startswith("М")]
                dry_targets = [t for t in targets if t[1].startswith("С")]
                other_targets = [t for t in targets if not t[1].startswith("М") and not t[1].startswith("С")]

                categories_to_book = []
                if wash_targets:
                    categories_to_book.append(("Стирка", wash_targets))
                if dry_targets:
                    categories_to_book.append(("Сушка", dry_targets))
                if other_targets:
                    categories_to_book.append(("Прочее", other_targets))

                successful_bookings = []
                failed_categories = []

                for cat_name, cat_slots in categories_to_book:
                    cat_booked = False
                    logging.info(f"Начинаю бронирование ветки: {cat_name} (вариантов: {len(cat_slots)})")

                    for idx, (time_str, mach_code) in enumerate(cat_slots):
                        is_first_priority = (idx == 0)

                        if mach_code not in MACHINE_OFFSETS:
                            continue

                        offset, mach_name = MACHINE_OFFSETS[mach_code]
                        col_idx = base_col + offset

                        is_double = "(2ч)" in time_str or "2ч" in time_str
                        raw_h = time_str.replace("(2ч)", "").replace("2ч", "").strip()
                        if ":" not in raw_h:
                            raw_h = f"{raw_h}:00"
                        h_start = int(raw_h.split(":")[0])

                        if is_double:
                            t1 = f"{h_start}:00"
                            t2 = f"{h_start + 1}:00"

                            if t1 not in HOURS_TO_ROW or t2 not in HOURS_TO_ROW:
                                logging.warning(f"Интервал {t1}-{t2} выходит за границы таблицы.")
                                continue

                            r1_idx = HOURS_TO_ROW[t1] - 1
                            r2_idx = HOURS_TO_ROW[t2] - 1
                            cell1 = f"{col_to_letter(col_idx)}{r1_idx + 1}"
                            cell2 = f"{col_to_letter(col_idx)}{r2_idx + 1}"

                            row1 = grid[r1_idx] if r1_idx < len(grid) else []
                            row2 = grid[r2_idx] if r2_idx < len(grid) else []
                            val1 = row1[col_idx].strip() if col_idx < len(row1) else ""
                            val2 = row2[col_idx].strip() if col_idx < len(row2) else ""

                            logging.info(f"[{cat_name}] Проверка 2х часов: {t1}+{t2} {mach_name} ({cell1}, {cell2}): '{val1}', '{val2}'")

                            if is_free(val1) and is_free(val2):
                                if is_first_priority:
                                    human_delay = round(random.uniform(1.8, 2.4), 2)
                                    logging.info(f"[{cat_name}] Первый приоритет свободен! Пауза имитации человека: {human_delay} с...")
                                    await asyncio.sleep(human_delay)

                                logging.info(f"Оба часа свободны! Записываю {room} в {cell1} и {cell2}...")
                                await asyncio.to_thread(sheet.update_acell, cell1, room)
                                await asyncio.to_thread(sheet.update_acell, cell2, room)
                                await asyncio.sleep(0.7)

                                chk1 = (await asyncio.to_thread(sheet.acell, cell1)).value or ""
                                chk2 = (await asyncio.to_thread(sheet.acell, cell2)).value or ""

                                if chk1.strip() == room and chk2.strip() == room:
                                    logging.info(f"✅ Успешно забронированы 2 часа подряд: {cell1} и {cell2}")
                                    while len(grid) <= max(r1_idx, r2_idx):
                                        grid.append([])
                                    while len(grid[r1_idx]) <= col_idx: grid[r1_idx].append("")
                                    while len(grid[r2_idx]) <= col_idx: grid[r2_idx].append("")
                                    grid[r1_idx][col_idx] = room
                                    grid[r2_idx][col_idx] = room

                                    successful_bookings.append({
                                        "cat": cat_name,
                                        "time": f"{h_start}:00–{h_start + 2}:00 (2 часа)",
                                        "mach": mach_name,
                                        "cell": f"{cell1}, {cell2}"
                                    })
                                    cat_booked = True
                                    break
                                else:
                                    logging.warning(f"Коллизия при 2х часах в {cell1} или {cell2}")
                                    await bot.send_message(
                                        chat_id,
                                        f"⚠️ Коллизия при брони 2х часов на `{mach_name}`. Пробую следующий вариант ветки...",
                                        parse_mode="Markdown"
                                    )
                            else:
                                logging.info(f"[{cat_name}] Один из часов занят ({cell1}='{val1}', {cell2}='{val2}'). Перехожу дальше.")
                                continue

                        else:
                            t1 = f"{h_start}:00"
                            if t1 not in HOURS_TO_ROW:
                                continue

                            r1_idx = HOURS_TO_ROW[t1] - 1
                            cell1 = f"{col_to_letter(col_idx)}{r1_idx + 1}"

                            row1 = grid[r1_idx] if r1_idx < len(grid) else []
                            val1 = row1[col_idx].strip() if col_idx < len(row1) else ""

                            logging.info(f"[{cat_name}] Проверка одиночного слота: {t1} {mach_name} ({cell1}): '{val1}'")

                            if is_free(val1):
                                if is_first_priority:
                                    human_delay = round(random.uniform(1.8, 2.4), 2)
                                    logging.info(f"[{cat_name}] Первый приоритет свободен! Пауза имитации человека: {human_delay} с...")
                                    await asyncio.sleep(human_delay)

                                logging.info(f"Слот {cell1} свободен, записываю {room}...")
                                await asyncio.to_thread(sheet.update_acell, cell1, room)
                                await asyncio.sleep(0.7)
                                chk1 = (await asyncio.to_thread(sheet.acell, cell1)).value or ""

                                if chk1.strip() == room:
                                    logging.info(f"✅ Успешно забронирован слот: {cell1}")
                                    while len(grid) <= r1_idx: grid.append([])
                                    while len(grid[r1_idx]) <= col_idx: grid[r1_idx].append("")
                                    grid[r1_idx][col_idx] = room

                                    successful_bookings.append({
                                        "cat": cat_name,
                                        "time": t1,
                                        "mach": mach_name,
                                        "cell": cell1
                                    })
                                    cat_booked = True
                                    break
                                else:
                                    logging.warning(f"Коллизия в {cell1}: занял {chk1}")
                                    await bot.send_message(
                                        chat_id,
                                        f"⚠️ Коллизия в `{cell1}` (занял `{chk1}`). Пробую следующий вариант...",
                                        parse_mode="Markdown"
                                    )
                            else:
                                logging.info(f"[{cat_name}] Слот {cell1} занят: '{val1}'. Перехожу дальше.")
                                continue

                    if not cat_booked:
                        failed_categories.append(cat_name)

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


def get_main_text(note: str = "") -> str:
    target_date = db.get_setting("target_date", "Не задана")
    room = db.get_setting("room", DEFAULT_ROOM)
    targets = db.get_targets()
    windows = db.get_windows()
    logs = db.get_recent_logs(2)

    status_str = "🟢 **АКТИВЕН**" if runtime.is_running else "⚪ **Остановлен**"

    wash_targets = [f"`{t[0]} {t[1]}`" for t in targets if t[1].startswith("М")]
    dry_targets = [f"`{t[0]} {t[1]}`" for t in targets if t[1].startswith("С")]
    other_targets = [f"`{t[0]} {t[1]}`" for t in targets if not t[1].startswith("М") and not t[1].startswith("С")]

    wash_str = " → ".join(wash_targets) if wash_targets else "_Не выбрана_"
    dry_str = " → ".join(dry_targets) if dry_targets else "_Не выбрана_"

    wins_str = ", ".join([f"`{w[0]}-{w[1]}`" for w in windows]) if windows else "_Нет_"
    logs_str = ", ".join([f"`{l}`" for l in logs]) if logs else "_Нет данных_"

    queue_block = (
        f"📋 **Очередь бронирования:**\n"
        f"🧺 **Стирка:** {wash_str}\n"
        f"💨 **Сушка:** {dry_str}"
    )
    if other_targets:
        queue_block += f"\n⚙️ **Прочее:** {' → '.join(other_targets)}"

    text = (
        f"🧺 **Панель управления снайпером**\n\n"
        f"🤖 **Статус:** {status_str} ({runtime.mode_str})\n"
        f"📅 **Дата:** `{target_date}` | 🏠 **Комната:** `{room}`\n"
        f"🕒 **Пинг:** `{runtime.last_ping}`\n\n"
        f"🔥 **Горячие окна (1 сек):** {wins_str}\n"
        f"🎯 **Срабатывания:** {logs_str}\n\n"
        f"{queue_block}"
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
        [InlineKeyboardButton(text="📁 Пресеты", callback_data="m_preset"), InlineKeyboardButton(text="🗑 Очистить", callback_data="c_slots")],
        [InlineKeyboardButton(text="🛠 Отладка", callback_data="m_debug"), InlineKeyboardButton(text="🔄 Обновить", callback_data="back")]
    ])


def get_debug_kb() -> InlineKeyboardMarkup:
    cur_log = db.get_setting("log_level", "INFO")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Проверить таблицу", callback_data="check_tbl"), InlineKeyboardButton(text="🔗 Ссылка на таблицу", callback_data="m_url")],
        [InlineKeyboardButton(text="🔥 Горячие окна", callback_data="m_win"), InlineKeyboardButton(text="🏠 Сменить комнату", callback_data="m_room")],
        [InlineKeyboardButton(text=f"📜 Логирование: [{cur_log}]", callback_data="m_log_lvl")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back")]
    ])


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
    runtime.mode_str = "Запуск..."
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


# Слоты с выбором длительности (1ч или 2ч)
@dp.callback_query(F.data == "m_slot")
async def cb_slot(call: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏱ 1 час", callback_data="slot_dur:1"),
            InlineKeyboardButton(text="⏱ 2 часа подряд", callback_data="slot_dur:2")
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])
    await call.message.edit_text("⏳ **Выберите длительность бронирования:**", parse_mode="Markdown", reply_markup=kb)
    await call.answer()


@dp.callback_query(F.data.startswith("slot_dur:"))
async def cb_slot_dur(call: CallbackQuery):
    dur = int(call.data.split(":")[1])
    max_h = 22 if dur == 1 else 21
    rows, row = [], []
    for h in range(8, max_h + 1):
        t_label = f"{h}:00" if dur == 1 else f"{h}:00–{h+2}:00"
        row.append(InlineKeyboardButton(text=t_label, callback_data=f"pick_h:{dur}:{h}:00"))
        per_row = 3 if dur == 1 else 2
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="m_slot")])

    title = "⏰ **Выберите время (1 час):**" if dur == 1 else "⏰ **Выберите интервал (2 часа подряд):**"
    await call.message.edit_text(title, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@dp.callback_query(F.data.startswith("pick_h:"))
async def cb_pick_h(call: CallbackQuery):
    parts = call.data.split(":")
    dur = int(parts[1])
    hour = f"{parts[2]}:{parts[3]}"
    dur_label = "1 час" if dur == 1 else "2 часа подряд"

    kb = [
        [
            InlineKeyboardButton(text="🧺 М1", callback_data=f"pick_m:{dur}:{hour}:М1"),
            InlineKeyboardButton(text="🧺 М3", callback_data=f"pick_m:{dur}:{hour}:М3")
        ],
        [InlineKeyboardButton(text="🧺 М5", callback_data=f"pick_m:{dur}:{hour}:М5")],
        [
            InlineKeyboardButton(text="💨 С2", callback_data=f"pick_m:{dur}:{hour}:С2"),
            InlineKeyboardButton(text="💨 С4", callback_data=f"pick_m:{dur}:{hour}:С4")
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"slot_dur:{dur}")]
    ]
    await call.message.edit_text(
        f"🧺 **Выберите машинку или сушилку:**\n⏰ Время: `{hour}` ({dur_label})",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("pick_m:"))
async def cb_pick_m(call: CallbackQuery):
    parts = call.data.split(":")
    dur = int(parts[1])
    hour = f"{parts[2]}:{parts[3]}"
    mach = parts[4]

    slot_label = f"{hour} (2ч)" if dur == 2 else hour
    db.add_target(slot_label, mach)

    await call.message.edit_text(
        get_main_text(f"✅ _Добавлен слот: {slot_label} ({mach})_"),
        parse_mode="Markdown",
        reply_markup=get_main_kb()
    )
    await call.answer()


@dp.callback_query(F.data == "c_slots")
async def cb_c_slots(call: CallbackQuery):
    db.clear_targets()
    await call.message.edit_text(get_main_text("🗑 _Слоты очищены_"), parse_mode="Markdown", reply_markup=get_main_kb())
    await call.answer()


# --- Пресеты ---
@dp.callback_query(F.data == "m_preset")
async def cb_preset_menu(call: CallbackQuery):
    presets = db.get_presets()
    kb_rows = []
    for p_name in presets:
        kb_rows.append([
            InlineKeyboardButton(text=f"📌 {p_name}", callback_data=f"load_p:{p_name}"),
            InlineKeyboardButton(text="❌", callback_data=f"del_p:{p_name}")
        ])
    kb_rows.append([InlineKeyboardButton(text="💾 Сохранить текущую очередь", callback_data="save_p")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])

    text = (
        "📁 **Управление пресетами очереди**\n\n"
        "Нажмите на сохранённый пресет, чтобы мгновенно загрузить его в очередь, "
        "или сохраните текущую настроенную очередь под новым именем:"
    )
    await call.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await call.answer()


@dp.callback_query(F.data == "save_p")
async def cb_save_preset(call: CallbackQuery, state: FSMContext):
    targets = db.get_targets()
    if not targets:
        await call.answer("Очередь пуста! Сначала добавьте слоты через «➕ Слот».", show_alert=True)
        return
    await state.set_state(Form.waiting_for_preset_name)
    await call.message.edit_text(
        "💾 Введите название для этого пресета (например: `Будни 2ч` или `Выходные`):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="m_preset")]])
    )
    await call.answer()


@dp.message(Form.waiting_for_preset_name)
async def proc_preset_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name or len(name) > 30:
        await message.answer("❌ Название должно содержать от 1 до 30 символов.")
        return

    targets = db.get_targets()
    db.save_preset(name, targets)
    await state.clear()
    await message.answer(
        get_main_text(f"✅ _Пресет «{name}» успешно сохранён!_"),
        parse_mode="Markdown",
        reply_markup=get_main_kb()
    )


@dp.callback_query(F.data.startswith("load_p:"))
async def cb_load_preset(call: CallbackQuery):
    p_name = call.data.split(":", 1)[1]
    if db.load_preset(p_name):
        await call.message.edit_text(
            get_main_text(f"✅ _Пресет «{p_name}» применён!_"),
            parse_mode="Markdown",
            reply_markup=get_main_kb()
        )
    else:
        await call.answer("Не удалось загрузить пресет.", show_alert=True)
    await call.answer()


@dp.callback_query(F.data.startswith("del_p:"))
async def cb_del_preset(call: CallbackQuery):
    p_name = call.data.split(":", 1)[1]
    db.delete_preset(p_name)
    await call.answer(f"Пресет «{p_name}» удалён")
    await cb_preset_menu(call)


# Окна
@dp.callback_query(F.data == "m_win")
async def cb_win(call: CallbackQuery):
    buttons = []
    for s, e in db.get_windows():
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
    raw_times = call.data.split(":", 1)[1]
    s, e = raw_times.split("_")
    db.delete_window(s, e)
    await cb_win(call)


@dp.callback_query(F.data.startswith("add_w:"))
async def cb_add_w(call: CallbackQuery):
    raw_times = call.data.split(":", 1)[1]
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