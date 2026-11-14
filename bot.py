import os
import json
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
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

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
TOKEN_FILE = os.getenv("TOKEN_FILE", "token.json")
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


def get_booking_times() -> list[dict]:
    raw = db.get_setting("booking_times", "[]")
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def set_booking_times(times: list[dict]):
    db.set_setting("booking_times", json.dumps(times, ensure_ascii=False))


def get_selected_machines(kind: str) -> list[str]:
    """kind: 'wash' или 'dry'"""
    key = "wash_machines" if kind == "wash" else "dry_machines"
    prefix = "М" if kind == "wash" else "С"
    default_list = [k for k in MACHINE_OFFSETS.keys() if k.startswith(prefix)]
    raw = db.get_setting(key, "")
    if not raw:
        return default_list
    try:
        data = json.loads(raw)
        return [m for m in data if m in MACHINE_OFFSETS] if isinstance(data, list) else default_list
    except Exception:
        return default_list


def set_selected_machines(kind: str, machines: list[str]):
    key = "wash_machines" if kind == "wash" else "dry_machines"
    db.set_setting(key, json.dumps(machines, ensure_ascii=False))


def col_to_letter(col_idx: int) -> str:
    """Переводит 0-based индекс столбца (0 -> A, 3 -> D, 12 -> M) в букву Excel."""
    res = ""
    n = col_idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        res = chr(65 + rem) + res
    return res


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
        self.guardian_active: bool = False
        self.guardian_task: Optional[asyncio.Task] = None
        self.guardian_info: str = ""


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
    if not os.path.exists(TOKEN_FILE):
        raise FileNotFoundError(
            f"Файл {TOKEN_FILE} не найден! Сгенерируйте его на ПК через OAuth2 и загрузите в папку бота."
        )

    creds = Credentials.from_authorized_user_file(TOKEN_FILE, scopes=scopes)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            try:
                with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                    f.write(creds.to_json())
            except OSError as e:
                logging.warning(f"Не удалось перезаписать обновлённый {TOKEN_FILE} на диск: {e}")
        else:
            raise RuntimeError("OAuth2 токен недействителен и не может быть обновлён. Перевыпустите token.json.")

    client = gspread.authorize(creds)

    sheet_url = db.get_setting("sheet_url")
    if sheet_url:
        return client.open_by_url(sheet_url).worksheet(SHEET_NAME)

    return client.open(SPREADSHEET_TITLE).worksheet(SHEET_NAME)


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


async def minute_guardian_worker(chat_id: int, target_date: str, base_col: int, room: str, bookings_to_watch: list[dict]):
    """Минутный караул: раз в минуту проверяет сохранность брони после Sentry Mode."""
    tracked_entries = []
    for b in bookings_to_watch:
        cells = [c.strip() for c in b["cell"].split(",") if c.strip()]
        tracked_entries.append({
            "cat": b["cat"],
            "mach": b["mach"],
            "cells": cells,
            "row_nums": [int("".join(ch for ch in c if ch.isdigit())) for c in cells],
            "start_hour": int(str(b["time"]).split(":", 1)[0].strip()),
        })

    if not tracked_entries:
        runtime.guardian_active = False
        return

    earliest_hour = min(e["start_hour"] for e in tracked_entries)
    started_at = datetime.now(MSK_TZ)

    guardian_restores = 0
    MAX_GUARDIAN_RESTORES = 3
    cur_base_col = base_col

    def _col_letter(c_idx: int) -> str:
        res = ""
        n = c_idx + 1
        while n > 0:
            n, rem = divmod(n - 1, 26)
            res = chr(65 + rem) + res
        return res

    def _cell_to_rc(cell_addr: str) -> tuple[int, int]:
        col_str = "".join(ch for ch in cell_addr if ch.isalpha()).upper()
        row_str = "".join(ch for ch in cell_addr if ch.isdigit())
        c_idx = 0
        for ch in col_str:
            c_idx = c_idx * 26 + (ord(ch) - 64)
        return int(row_str) - 1, c_idx - 1

    logging.info(
        f"👁 Минутный караул запущен: слотов={len(tracked_entries)}, "
        f"блок={'Левый' if cur_base_col == 0 else 'Правый'}, earliest_hour={earliest_hour}"
    )

    try:
        while runtime.guardian_active and tracked_entries:
            for _ in range(60):
                if not runtime.guardian_active:
                    return
                await asyncio.sleep(1)

            now = datetime.now(MSK_TZ)
            if (now.strftime("%d.%m") in target_date and now.hour >= earliest_hour) or \
                    (now - started_at).total_seconds() >= 12 * 3600:
                logging.info("👁 Минутный караул штатно завершён по времени.")
                runtime.guardian_active = False
                return

            try:
                sheet = await asyncio.to_thread(get_worksheet)
                grid = await asyncio.to_thread(sheet.get_values, "A1:W20")
            except Exception as e:
                logging.warning(f"Минутный караул: ошибка чтения таблицы: {e}")
                continue

            left_val = grid[1][0].strip() if len(grid) > 1 and len(grid[1]) > 0 else ""
            right_val = grid[1][12].strip() if len(grid) > 1 and len(grid[1]) > 12 else ""
            in_left = target_date in left_val
            in_right = target_date in right_val

            # --- Сценарий А: переезд целевой даты между блоками ---
            if (cur_base_col == 0 and not in_left and in_right) or (cur_base_col == 12 and not in_right and in_left):
                new_base_col = 12 if cur_base_col == 0 else 0
                block_name = "Правый" if new_base_col == 12 else "Левый"

                if guardian_restores >= MAX_GUARDIAN_RESTORES:
                    await bot.send_message(
                        chat_id,
                        f"⚠️ **Минутный караул:** исчерпан лимит восстановлений "
                        f"({MAX_GUARDIAN_RESTORES}/{MAX_GUARDIAN_RESTORES}). Снимаю бронь с контроля.",
                        parse_mode="Markdown"
                    )
                    runtime.guardian_active = False
                    return

                guardian_restores += 1
                moved = []
                moved_failed = []
                for entry in list(tracked_entries):
                    prefix = entry["mach"][0]
                    candidates = [entry["mach"]] + [
                        m for m in MACHINE_OFFSETS if m.startswith(prefix) and m != entry["mach"]
                    ]
                    placed = None
                    for mach in candidates:
                        col_idx = new_base_col + MACHINE_OFFSETS[mach][0]
                        col_let = _col_letter(col_idx)
                        new_cells = [f"{col_let}{r}" for r in entry["row_nums"]]
                        free = True
                        for cell in new_cells:
                            r_idx, c_idx = _cell_to_rc(cell)
                            if r_idx < len(grid) and c_idx < len(grid[r_idx]):
                                if grid[r_idx][c_idx].strip() != "":
                                    free = False
                                    break
                        if free:
                            placed = (mach, new_cells)
                            break

                    if not placed:
                        moved_failed.append(f"{entry['cat']} {entry['mach']}")
                        tracked_entries.remove(entry)
                        continue

                    mach, new_cells = placed
                    try:
                        await asyncio.to_thread(
                            sheet.batch_update,
                            [{"range": c, "values": [[room]]} for c in new_cells]
                        )
                        entry["cells"] = new_cells
                        entry["mach"] = mach
                        moved.append(f"{entry['cat']}: {mach} ({', '.join(new_cells)})")
                    except Exception as e:
                        logging.warning(f"Минутный караул: не удалось перебронировать {new_cells}: {e}")
                        moved_failed.append(f"{entry['cat']} {mach}")

                cur_base_col = new_base_col

                msg = (
                    f"🔄 **Минутный караул:** дата `{target_date}` переехала в {block_name} блок. "
                    f"Перебронировал слоты ({guardian_restores}/{MAX_GUARDIAN_RESTORES}):\n"
                    + "\n".join(f"• {m}" for m in moved)
                )
                if moved_failed:
                    msg += f"\n\n⚠️ Не удалось: {', '.join(moved_failed)}"
                await bot.send_message(chat_id, msg, parse_mode="Markdown")

                if not tracked_entries:
                    runtime.guardian_active = False
                    return
                continue

            # --- Сценарии Б и В: проверка ячеек в текущем блоке ---
            if (cur_base_col == 0 and in_left) or (cur_base_col == 12 and in_right):
                for entry in list(tracked_entries):
                    cell_vals = {}
                    for cell in entry["cells"]:
                        r_idx, c_idx = _cell_to_rc(cell)
                        if r_idx < len(grid) and c_idx < len(grid[r_idx]):
                            cell_vals[cell] = grid[r_idx][c_idx].strip()
                        else:
                            cell_vals[cell] = ""

                    foreign = {c: v for c, v in cell_vals.items() if v != "" and v != room}
                    empty_cells = [c for c, v in cell_vals.items() if v == ""]

                    # Сценарий В: поздняя перезапись другим человеком — НЕ перетираем
                    if foreign:
                        cell, val = next(iter(foreign.items()))
                        await bot.send_message(
                            chat_id,
                            f"⚠️ **Минутный караул:** ячейка {cell} была занята пользователем {val}. "
                            f"По истечении первых 15 сек перезапись поверх другого человека отключена, "
                            f"снимаю слот ({entry['cat']} {entry['mach']}) с контроля.",
                            parse_mode="Markdown"
                        )
                        tracked_entries.remove(entry)
                        if not tracked_entries:
                            runtime.guardian_active = False
                            return
                        continue

                    # Сценарий Б: ячейки случайно очищены
                    if empty_cells and all(v in ("", room) for v in cell_vals.values()):
                        if guardian_restores >= MAX_GUARDIAN_RESTORES:
                            await bot.send_message(
                                chat_id,
                                f"⚠️ **Минутный караул:** исчерпан лимит восстановлений "
                                f"({MAX_GUARDIAN_RESTORES}/{MAX_GUARDIAN_RESTORES}). Снимаю бронь с контроля.",
                                parse_mode="Markdown"
                            )
                            runtime.guardian_active = False
                            return

                        guardian_restores += 1
                        try:
                            await asyncio.to_thread(
                                sheet.batch_update,
                                [{"range": c, "values": [[room]]} for c in empty_cells]
                            )
                            await bot.send_message(
                                chat_id,
                                f"♻️ **Минутный караул:** ячейки {', '.join(empty_cells)} "
                                f"были очищены — бронь комнаты {room} восстановлена "
                                f"({guardian_restores}/{MAX_GUARDIAN_RESTORES}).",
                                parse_mode="Markdown"
                            )
                        except Exception as e:
                            logging.warning(f"Минутный караул: не удалось восстановить {empty_cells}: {e}")
    except asyncio.CancelledError:
        logging.info("👁 Минутный караул остановлен.")
        runtime.guardian_active = False
        raise
    finally:
        runtime.guardian_info = ""


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

    runtime.known_left = ""
    runtime.known_right = ""
    is_first_check = True
    consecutive_errors = 0

    while runtime.is_running:
        if is_first_check:
            is_first_check = False
            first_run = True
        else:
            await smart_sleep()
            first_run = False
        if not runtime.is_running:
            break

        target_date = db.get_setting("target_date")
        room = db.get_setting("room", DEFAULT_ROOM)

        try:
            grid = await asyncio.to_thread(sheet.get_values, "A1:W20")
            consecutive_errors = 0
            now = datetime.now(MSK_TZ)
            runtime.last_ping = now.strftime("%H:%M:%S")

            if len(grid) < 2:
                logging.warning("Таблица вернула менее 2 строк данных.")
                continue

            left_val = grid[1][0].strip() if len(grid) > 1 and len(grid[1]) > 0 else ""
            right_val = grid[1][12].strip() if len(grid) > 1 and len(grid[1]) > 12 else ""
            left_h = grid[1][0] if len(grid[1]) > 0 else ""
            right_h = grid[1][12] if len(grid[1]) > 12 else ""

            if first_run and not (target_date and (target_date in left_val or target_date in right_val)):
                runtime.known_left = left_val
                runtime.known_right = right_val
                logging.info(
                    f"🔍 Стартовая полная проверка выполнена (A2='{left_val}', M2='{right_val}'). "
                    f"Ожидаю '{target_date}'..."
                )
                continue

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
                base_col_idx: int = base_col
                booking_times = get_booking_times()
                wash_machines = get_selected_machines("wash")
                dry_machines = get_selected_machines("dry")

                if not booking_times or not wash_machines:
                    await bot.send_message(
                        chat_id,
                        "⚠️ Не задано время брони или не выбрана ни одна стиральная машина!",
                        reply_markup=get_main_kb()
                    )
                    runtime.is_running = False
                    break

                def is_cell_free(r_idx: int, c_idx: int, current_grid: list[list[str]]) -> bool:
                    if r_idx < 0 or r_idx >= len(current_grid):
                        return True
                    row_vals = current_grid[r_idx]
                    val = row_vals[c_idx].strip() if c_idx < len(row_vals) else ""
                    return val == ""

                async def try_book_machine(
                    mach_code: str,
                    start_h: int,
                    dur: int,
                    current_grid: list[list[str]],
                    use_delay: bool
                ) -> tuple[bool, str, bool]:
                    """Возвращает (успех, строка_ячеек, была_ли_задержка)."""
                    col_idx = base_col_idx + MACHINE_OFFSETS[mach_code][0]
                    col_let = col_to_letter(col_idx)
                    row1_num = start_h - 3  # 1-based номер строки в таблице (15:00 -> 12)
                    r1_idx = row1_num - 1

                    if dur == 2:
                        row2_num = row1_num + 1
                        r2_idx = r1_idx + 1
                        if not (is_cell_free(r1_idx, col_idx, current_grid) and is_cell_free(r2_idx, col_idx, current_grid)):
                            return False, "", False

                        if use_delay:
                            h_delay = round(random.uniform(1.8, 2.4), 2)
                            logging.info(f"[{mach_code}] Слот свободен! Пауза имитации человека: {h_delay} с...")
                            await asyncio.sleep(h_delay)

                        c1, c2 = f"{col_let}{row1_num}", f"{col_let}{row2_num}"
                        logging.info(f"Записываю {room} в {c1} и {c2} ({mach_code})...")
                        await asyncio.to_thread(
                            sheet.batch_update,
                            [
                                {"range": c1, "values": [[room]]},
                                {"range": c2, "values": [[room]]},
                            ]
                        )
                        # Проверка коллизии после записи
                        check = await asyncio.to_thread(sheet.batch_get, [c1, c2])
                        v1 = check[0][0][0].strip() if (check[0] and check[0][0]) else ""
                        v2 = check[1][0][0].strip() if (check[1] and check[1][0]) else ""
                        if v1 == room and v2 == room:
                            return True, f"{c1}, {c2}", use_delay
                        logging.warning(f"Коллизия при записи 2ч в {c1}, {c2} ({v1!r}, {v2!r})")
                        return False, "", use_delay
                    else:
                        if not is_cell_free(r1_idx, col_idx, current_grid):
                            return False, "", False

                        if use_delay:
                            h_delay = round(random.uniform(1.8, 2.4), 2)
                            logging.info(f"[{mach_code}] Слот свободен! Пауза имитации человека: {h_delay} с...")
                            await asyncio.sleep(h_delay)

                        c1 = f"{col_let}{row1_num}"
                        logging.info(f"Записываю {room} в {c1} ({mach_code})...")
                        await asyncio.to_thread(sheet.update_acell, c1, room)
                        v1 = ((await asyncio.to_thread(sheet.acell, c1)).value or "").strip()
                        if v1 == room:
                            return True, c1, use_delay
                        logging.warning(f"Коллизия при записи 1ч в {c1} ({v1!r})")
                        return False, "", use_delay

                successful_bookings = []
                failed_categories = []
                delay_enabled = db.get_setting("human_delay", "1") == "1"
                human_delay_done = not delay_enabled

                for slot_cfg in booking_times:
                    w_hour = int(slot_cfg["hour"])
                    duration = int(slot_cfg["duration"])
                    d_hour = w_hour + 1

                    wash_booked = False
                    for w_mach in wash_machines:
                        ok, cells_str, delayed = await try_book_machine(
                            w_mach, w_hour, duration, grid, not human_delay_done
                        )
                        if delayed:
                            human_delay_done = True
                        if ok:
                            wash_booked = True
                            successful_bookings.append({
                                "cat": "Стирка",
                                "time": f"{w_hour}:00–{w_hour + duration}:00 ({duration}ч)",
                                "mach": w_mach,
                                "cell": cells_str
                            })
                            logging.info(f"✅ Стирка забронирована: {w_mach} ({cells_str}) на {w_hour}:00 ({duration}ч)")
                            break

                    if not wash_booked:
                        logging.info(f"Все выбранные стиралки заняты на {w_hour}:00 ({duration}ч), пробую следующее время...")
                        continue

                    # Стирка забронирована -> бронируем сушку на час позже (d_hour = w_hour + 1)
                    if dry_machines:
                        logging.info(f"🔄 Обновляю сетку перед бронированием сушки на {d_hour}:00 ({duration}ч)...")
                        try:
                            grid = await asyncio.to_thread(sheet.get_values, "A1:W20")
                        except Exception as e:
                            logging.warning(f"Не удалось обновить сетку перед сушкой: {e}")

                        dry_booked = False
                        for d_mach in dry_machines:
                            ok, cells_str, _ = await try_book_machine(
                                d_mach, d_hour, duration, grid, False
                            )
                            if ok:
                                dry_booked = True
                                successful_bookings.append({
                                    "cat": "Сушка",
                                    "time": f"{d_hour}:00–{d_hour + duration}:00 ({duration}ч)",
                                    "mach": d_mach,
                                    "cell": cells_str
                                })
                                logging.info(f"✅ Сушка забронирована: {d_mach} ({cells_str}) на {d_hour}:00 ({duration}ч)")
                                break

                        if not dry_booked:
                            failed_categories.append(f"Сушка на {d_hour}:00 ({duration}ч)")

                    # Сессия забронирована, выходим из перебора запасных времён
                    break

                runtime.is_running = False
                runtime.mode_str = "Завершено" if successful_bookings else "Остановлен"

                if successful_bookings:
                    lines = [f"• **{b['cat']}**: `{b['time']}` — {b['mach']} (`{b['cell']}`)" for b in successful_bookings]
                    booked_text = "\n".join(lines)
                    fail_text = f"\n\n⚠️ Не удалось занять: {', '.join(failed_categories)}" if failed_categories else ""

                    raw_cells_for_cancel = []
                    for b in successful_bookings:
                        for c in b["cell"].split(","):
                            c_clean = c.strip()
                            if c_clean:
                                raw_cells_for_cancel.append(c_clean)
                    cancel_cb_data = f"cancel_b:{'_'.join(raw_cells_for_cancel)}"

                    success_kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="❌ Снять мою бронь", callback_data=cancel_cb_data)],
                        [InlineKeyboardButton(text="🔄 Главное меню", callback_data="back")]
                    ])

                    await bot.send_message(
                        chat_id,
                        f"✅ **УСПЕШНАЯ БРОНЬ!**\n\n"
                        f"📅 Дата: `{target_date}`\n"
                        f"🏠 Комната: `{room}`\n\n"
                        f"{booked_text}{fail_text}\n\n"
                        f"🛡 _Включаю защиту ячеек от перезаписи на 15 сек..._",
                        parse_mode="Markdown",
                        reply_markup=success_kb
                    )

                    # --- Охранный режим ячеек (Sentry Mode) ---
                    # Собираем все занятые ячейки в плоский список
                    monitored_cells = []
                    for b in successful_bookings:
                        for c in b["cell"].split(","):
                            c_clean = c.strip()
                            if c_clean and c_clean not in monitored_cells:
                                monitored_cells.append(c_clean)

                    overwrites_count = {cell: 0 for cell in monitored_cells}
                    abandoned_cells = set()

                    logging.info(f"🛡 Запуск Sentry-защиты для ячеек: {monitored_cells}")
                    sentry_checks = 6  # 6 проверок по 2.5 сек = 15 секунд

                    for _ in range(sentry_checks):
                        await asyncio.sleep(2.5)
                        try:
                            # Пакетное чтение всех ячеек за один вызов API
                            batch_res = await asyncio.to_thread(sheet.batch_get, monitored_cells)
                            for idx_cell, cell_name in enumerate(monitored_cells):
                                if cell_name in abandoned_cells:
                                    continue

                                cell_data = batch_res[idx_cell]
                                val = cell_data[0][0].strip() if (cell_data and cell_data[0]) else ""

                                if val != room:
                                    overwrites_count[cell_name] += 1
                                    current_try = overwrites_count[cell_name]
                                    logging.warning(f"🚨 Чужая запись в ячейке {cell_name}: '{val}'! Попытка возврата {current_try}/2")

                                    if current_try <= 2:
                                        await bot.send_message(
                                            chat_id,
                                            f"⚠️ **Внимание!** В вашей ячейке `{cell_name}` обнаружена чужая запись: `{val or 'Пусто'}`.\n"
                                            f"🔁 Переписываю обратно на `{room}` (попытка {current_try}/2)...",
                                            parse_mode="Markdown"
                                        )
                                        await asyncio.to_thread(sheet.update_acell, cell_name, room)
                                    else:
                                        abandoned_cells.add(cell_name)
                                        await bot.send_message(
                                            chat_id,
                                            f"🚨 **Война за слот `{cell_name}`!**\n\n"
                                            f"Комната `{val}` упорно перетирает вашу запись. "
                                            f"Бот остановил авто-перезапись во избежание санкций от администрации таблицы. "
                                            f"Пожалуйста, свяжитесь с соседом или решите вопрос вручную!",
                                            parse_mode="Markdown"
                                        )
                        except Exception as e:
                            logging.warning(f"Ошибка в цикле Sentry-защиты: {e}")

                    logging.info("🛡 Sentry-защита ячеек благополучно завершена.")

                    surviving_bookings = []
                    for b in successful_bookings:
                        b_cells = [c.strip() for c in b["cell"].split(",") if c.strip()]
                        if not all(c in abandoned_cells for c in b_cells):
                            surviving_bookings.append(b)

                    if surviving_bookings:
                        if runtime.guardian_task and not runtime.guardian_task.done():
                            runtime.guardian_task.cancel()
                        runtime.guardian_info = ", ".join(
                            f"{b['cat']} {b['mach']} ({b['cell']})" for b in surviving_bookings
                        )
                        runtime.guardian_active = True
                        runtime.guardian_task = asyncio.create_task(
                            minute_guardian_worker(chat_id, target_date, base_col, room, surviving_bookings)
                        )
                        logging.info("👁 Запущен минутный караул для защиты от очистки таблицы (проверка 1 раз/мин)...")

                else:
                    await bot.send_message(chat_id, "❌ Все выбранные слоты заняты!", reply_markup=get_main_kb())
                break

        except Exception as e:
            consecutive_errors += 1
            backoff = min(16.0, 1.5 * (2 ** min(consecutive_errors - 1, 4))) + round(random.uniform(0.1, 0.5), 2)
            logging.warning(
                f"Сбой в sniper_worker (ошибка #{consecutive_errors}): {e}. "
                f"Экспоненциальный откат: пауза {backoff:.2f} с..."
            )
            await asyncio.sleep(backoff)


def get_main_text(note: str = "") -> str:
    target_date = db.get_setting("target_date", "Не задана")
    room = db.get_setting("room", DEFAULT_ROOM)
    windows = db.get_windows()
    logs = db.get_recent_logs(2)

    status_str = "🟢 **АКТИВЕН**" if runtime.is_running else "⚪ **Остановлен**"

    if runtime.guardian_active:
        guardian_str = "🟢 Активен"
        if runtime.guardian_info:
            guardian_str += f" ({runtime.guardian_info})"
    else:
        guardian_str = "⚪️ Выключен"

    booking_times = get_booking_times()
    wash_machines = get_selected_machines("wash")
    dry_machines = get_selected_machines("dry")

    if booking_times:
        times_str = " → ".join(
            f"`{int(t['hour'])}:00–{int(t['hour']) + int(t['duration'])}:00` ({int(t['duration'])}ч)"
            for t in booking_times
        )
    else:
        times_str = "_Не задано_"

    wash_str = " → ".join(wash_machines) if wash_machines else "_Не выбрана_"
    dry_str = " → ".join(dry_machines) if dry_machines else "_Без сушки_"

    wins_str = ", ".join([f"`{w[0]}-{w[1]}`" for w in windows]) if windows else "_Нет_"
    logs_str = ", ".join([f"`{l}`" for l in logs]) if logs else "_Нет данных_"

    queue_block = (
        f"📋 **Сессия бронирования:**\n"
        f"🕒 **Время:** {times_str}\n"
        f"🧺 **Стирка:** {wash_str}\n"
        f"💨 **Сушка (T+1):** {dry_str}"
    )

    text = (
        f"🧺 **Панель управления снайпером**\n\n"
        f"🤖 **Статус:** {status_str} ({runtime.mode_str})\n"
        f"📅 **Дата:** `{target_date}` | 🏠 **Комната:** `{room}`\n"
        f"🕒 **Пинг:** `{runtime.last_ping}`\n"
        f"👁 **Минутный караул:** {guardian_str}\n\n"
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
        [InlineKeyboardButton(text="📅 Дата", callback_data="m_date"), InlineKeyboardButton(text="🧺 Брони", callback_data="m_slots")],
        [InlineKeyboardButton(text="📁 Пресеты", callback_data="m_preset"), InlineKeyboardButton(text="🗑 Очистить", callback_data="c_slots")],
        [InlineKeyboardButton(text="🛠 Отладка", callback_data="m_debug"), InlineKeyboardButton(text="🔄 Обновить", callback_data="back")]
    ])


def get_debug_text() -> str:
    cur_url = db.get_setting("sheet_url", "по умолчанию из .env")
    room = db.get_setting("room", DEFAULT_ROOM)
    cur_log = db.get_setting("log_level", "INFO")
    return (
        "🛠 **Панель отладки и конфигурации**\n\n"
        f"🏠 **Текущая комната:** `{room}`\n"
        f"📜 **Уровень логов:** `{cur_log}`\n"
        f"🔗 **Целевая таблица:**\n`{cur_url}`\n\n"
        "Выберите действие:"
    )


def get_debug_kb() -> InlineKeyboardMarkup:
    cur_log = db.get_setting("log_level", "INFO")
    delay_on = db.get_setting("human_delay", "1") == "1"
    delay_btn_text = "🎭 Задержка человека: ВКЛ" if delay_on else "⚡️ Задержка человека: ВЫКЛ (Макс. скорость)"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Проверить таблицу", callback_data="check_tbl"), InlineKeyboardButton(text="🔗 Ссылка на таблицу", callback_data="m_url")],
        [InlineKeyboardButton(text="🔥 Горячие окна", callback_data="m_win"), InlineKeyboardButton(text="🏠 Сменить комнату", callback_data="m_room")],
        [InlineKeyboardButton(text=delay_btn_text, callback_data="toggle_delay")],
        [InlineKeyboardButton(text=f"📜 Логирование: [{cur_log}]", callback_data="m_log_lvl")],
        [InlineKeyboardButton(text="🛑 Выключить минутный караул", callback_data="stop_guardian")],
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
    if not get_booking_times() or not get_selected_machines("wash"):
        await call.answer("Задайте время брони и выберите стиральную машину!", show_alert=True)
        return
    runtime.is_running = True
    runtime.mode_str = "Запуск..."
    runtime.guardian_active = False
    runtime.guardian_info = ""
    if runtime.guardian_task and not runtime.guardian_task.done():
        runtime.guardian_task.cancel()
    runtime.task = asyncio.create_task(sniper_worker(call.message.chat.id))
    await call.message.edit_text(get_main_text("🎯 _Снайпер запущен!_"), parse_mode="Markdown", reply_markup=get_main_kb())
    await call.answer()


@dp.callback_query(F.data == "stop")
async def cb_stop(call: CallbackQuery):
    runtime.is_running = False
    runtime.mode_str = "Остановлен"
    if runtime.task:
        runtime.task.cancel()
    runtime.guardian_active = False
    runtime.guardian_info = ""
    if runtime.guardian_task and not runtime.guardian_task.done():
        runtime.guardian_task.cancel()
    await call.message.edit_text(get_main_text("🛑 _Снайпер остановлен._"), parse_mode="Markdown", reply_markup=get_main_kb())
    await call.answer()


@dp.callback_query(F.data == "m_debug")
async def cb_debug(call: CallbackQuery, state: Optional[FSMContext] = None):
    if state is not None:
        await state.clear()
    text = get_debug_text()
    try:
        await call.message.edit_text(text, parse_mode="Markdown", reply_markup=get_debug_kb())
    except TelegramBadRequest as err:
        if "message is not modified" not in str(err):
            raise err
    await call.answer()


@dp.callback_query(F.data == "stop_guardian")
async def cb_stop_guardian(call: CallbackQuery):
    if not runtime.guardian_active:
        await call.answer("Минутный караул уже выключен.", show_alert=False)
        return

    runtime.guardian_active = False
    runtime.guardian_info = ""
    if runtime.guardian_task and not runtime.guardian_task.done():
        runtime.guardian_task.cancel()
    logging.info("🛑 Минутный караул выключен вручную через меню отладки.")
    await call.answer("🛑 Минутный караул выключен.", show_alert=False)

    try:
        await call.message.edit_text(get_debug_text(), parse_mode="Markdown", reply_markup=get_debug_kb())
    except TelegramBadRequest as err:
        if "message is not modified" not in str(err):
            raise err


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
async def cb_set_log(call: CallbackQuery, state: FSMContext):
    lvl = call.data.split(":")[1]
    db.set_setting("log_level", lvl)
    apply_log_level(lvl)
    logging.warning(f"Уровень логирования изменён пользователем на: {lvl}")
    await cb_debug(call, state)


@dp.callback_query(F.data == "toggle_delay")
async def cb_toggle_delay(call: CallbackQuery, state: Optional[FSMContext] = None):
    cur_delay = db.get_setting("human_delay", "1")
    new_delay = "0" if cur_delay == "1" else "1"
    db.set_setting("human_delay", new_delay)
    await cb_debug(call, state)


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


# --- Настройка бронирования: время / стиралки / сушилки ---
PRIORITY_ICONS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


def priority_icon(pos: int) -> str:
    return PRIORITY_ICONS[pos] if 0 <= pos < len(PRIORITY_ICONS) else f"{pos + 1}."


def get_cfg_booking_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕒 Время и длительность", callback_data="cfg_times")],
        [InlineKeyboardButton(text="🧺 Стиральные машины", callback_data="cfg_washers")],
        [InlineKeyboardButton(text="💨 Сушильные машины", callback_data="cfg_dryers")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])


def get_cfg_booking_text() -> str:
    times = get_booking_times()
    wash = get_selected_machines("wash")
    dry = get_selected_machines("dry")

    if times:
        lines = []
        for i, t in enumerate(times, 1):
            h = int(t["hour"])
            d = int(t["duration"])
            lines.append(f"{i}. Стирка {h}:00–{h + d}:00 ({d}ч) ➔ Сушка {h + 1}:00–{h + 1 + d}:00 ({d}ч)")
        times_str = "\n".join(lines)
    else:
        times_str = "_Список пуст_"

    wash_str = ", ".join(wash) if wash else "_Не выбрано_"
    dry_str = ", ".join(dry) if dry else "_Без сушки_"

    return (
        "🧺 **Настройки бронирования**\n\n"
        f"🕒 **Время и длительность:**\n{times_str}\n\n"
        f"🧺 **Стиральные машины (по приоритету):** {wash_str}\n"
        f"💨 **Сушильные машины (по приоритету):** {dry_str}"
    )


@dp.callback_query(F.data == "m_slots")
async def cb_slots_menu(call: CallbackQuery):
    await call.message.edit_text(get_cfg_booking_text(), parse_mode="Markdown", reply_markup=get_cfg_booking_kb())
    await call.answer()


@dp.callback_query(F.data == "cfg_booking")
async def cb_cfg_booking(call: CallbackQuery):
    await call.message.edit_text(get_cfg_booking_text(), parse_mode="Markdown", reply_markup=get_cfg_booking_kb())
    await call.answer()


# --- Раздел «Время и длительность» ---
@dp.callback_query(F.data == "cfg_times")
async def cb_cfg_times(call: CallbackQuery):
    times = get_booking_times()
    if times:
        lines = []
        for i, t in enumerate(times, 1):
            h = int(t["hour"])
            d = int(t["duration"])
            lines.append(f"{i}. Стирка {h}:00–{h + d}:00 ({d}ч) ➔ Сушка {h + 1}:00–{h + 1 + d}:00 ({d}ч)")
        times_str = "\n".join(lines)
    else:
        times_str = "_Список пуст_"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить время", callback_data="add_h")],
        [InlineKeyboardButton(text="🗑 Очистить список времён", callback_data="clear_times")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="cfg_booking")]
    ])
    await call.message.edit_text(
        f"🕒 **Время и длительность**\n\n{times_str}\n\n"
        "Бронь всегда парная: стирка на T, сушка на T+1.",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await call.answer()


@dp.callback_query(F.data == "add_h")
async def cb_add_hour(call: CallbackQuery):
    rows, row = [], []
    for h in range(8, 23):
        row.append(InlineKeyboardButton(text=f"{h}:00", callback_data=f"add_h:{h}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="cfg_times")])
    await call.message.edit_text(
        "⏰ **Выберите час начала стирки:**",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("add_h:"))
async def cb_add_hour_pick(call: CallbackQuery):
    hour = int(call.data.split(":", 1)[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏱ 1 час", callback_data=f"add_dur:{hour}:1"),
            InlineKeyboardButton(text="⏱ 2 часа", callback_data=f"add_dur:{hour}:2")
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="add_h")]
    ])
    await call.message.edit_text(
        f"⏳ **Длительность для стирки в {hour}:00:**\n\n"
        f"Сушка будет забронирована на {hour + 1}:00.",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await call.answer()


@dp.callback_query(F.data.startswith("add_dur:"))
async def cb_add_dur(call: CallbackQuery):
    parts = call.data.split(":")
    hour = int(parts[1])
    dur = int(parts[2])

    times = get_booking_times()
    times = [t for t in times if int(t.get("hour", -1)) != hour]
    times.append({"hour": hour, "duration": dur})
    times.sort(key=lambda t: int(t["hour"]))
    set_booking_times(times)

    await cb_cfg_times(call)


@dp.callback_query(F.data == "clear_times")
async def cb_clear_times(call: CallbackQuery):
    set_booking_times([])
    await cb_cfg_times(call)


# --- Раздел «Стиральные машины» ---
def get_machines_kb(kind: str) -> InlineKeyboardMarkup:
    prefix = "М" if kind == "wash" else "С"
    selected = get_selected_machines(kind)
    rows = []
    for code in MACHINE_OFFSETS:
        if not code.startswith(prefix):
            continue
        if code in selected:
            label = f"{priority_icon(selected.index(code))} {code}"
        else:
            label = f"⬜️ {code}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"tog_m:{kind}:{code}")])
    if kind == "dry":
        rows.append([InlineKeyboardButton(text="🚫 Без сушки", callback_data="clear_dryers")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="cfg_booking")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "cfg_washers")
async def cb_cfg_washers(call: CallbackQuery):
    selected = get_selected_machines("wash")
    sel_str = ", ".join(selected) if selected else "_Не выбрано_"
    await call.message.edit_text(
        f"🧺 **Стиральные машины**\n\nПорядок нажатия = приоритет.\nВыбрано: {sel_str}",
        parse_mode="Markdown",
        reply_markup=get_machines_kb("wash")
    )
    await call.answer()


@dp.callback_query(F.data == "cfg_dryers")
async def cb_cfg_dryers(call: CallbackQuery):
    selected = get_selected_machines("dry")
    sel_str = ", ".join(selected) if selected else "_Без сушки_"
    await call.message.edit_text(
        f"💨 **Сушильные машины**\n\nПорядок нажатия = приоритет.\nВыбрано: {sel_str}",
        parse_mode="Markdown",
        reply_markup=get_machines_kb("dry")
    )
    await call.answer()


@dp.callback_query(F.data.startswith("tog_m:"))
async def cb_toggle_machine(call: CallbackQuery):
    parts = call.data.split(":")
    kind = parts[1]
    code = parts[2]
    if code not in MACHINE_OFFSETS:
        await call.answer("Неизвестная машина", show_alert=True)
        return

    selected = get_selected_machines(kind)
    if code in selected:
        selected.remove(code)
    else:
        selected.append(code)
    set_selected_machines(kind, selected)

    if kind == "wash":
        await cb_cfg_washers(call)
    else:
        await cb_cfg_dryers(call)


@dp.callback_query(F.data == "clear_dryers")
async def cb_clear_dryers(call: CallbackQuery):
    set_selected_machines("dry", [])
    await cb_cfg_dryers(call)


@dp.callback_query(F.data == "c_slots")
async def cb_c_slots(call: CallbackQuery):
    set_booking_times([])
    set_selected_machines("wash", [])
    set_selected_machines("dry", [])
    await call.message.edit_text(get_main_text("🗑 _Настройки бронирования сброшены_"), parse_mode="Markdown", reply_markup=get_main_kb())
    await call.answer()


# --- Быстрая отмена брони ---
@dp.callback_query(F.data.startswith("cancel_b:"))
async def cb_cancel_booking(call: CallbackQuery):
    raw_cells = call.data.split(":", 1)[1]
    cells = [c.strip() for c in raw_cells.split("_") if c.strip()]
    room = db.get_setting("room", DEFAULT_ROOM)

    runtime.guardian_active = False
    runtime.guardian_info = ""
    if runtime.guardian_task and not runtime.guardian_task.done():
        runtime.guardian_task.cancel()

    await call.answer("Снимаю бронь в таблице...", show_alert=False)
    try:
        sheet = await asyncio.to_thread(get_worksheet)
        cleared = []
        for cell in cells:
            cur_val = ((await asyncio.to_thread(sheet.acell, cell)).value or "").strip()
            if cur_val == room:
                await asyncio.to_thread(sheet.update_acell, cell, "")
                cleared.append(cell)

        if cleared:
            await call.message.edit_text(
                f"🗑 **Бронь снята!**\n\n"
                f"Очищены ячейки: `{', '.join(cleared)}`.",
                parse_mode="Markdown",
                reply_markup=get_main_kb()
            )
        else:
            await call.message.edit_text(
                "⚠️ Ячейки уже были пусты или изменены.",
                reply_markup=get_main_kb()
            )
    except Exception as e:
        logging.error(f"Ошибка при отмене брони: {e}")
        await call.answer(f"Ошибка отмены: {e}", show_alert=True)


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
    if not get_booking_times():
        await call.answer("Не задано время брони! Сначала настройте «🧺 Брони».", show_alert=True)
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

    db.save_preset_config(name, {
        "times": get_booking_times(),
        "wash": get_selected_machines("wash"),
        "dry": get_selected_machines("dry"),
    })
    await state.clear()
    await message.answer(
        get_main_text(f"✅ _Пресет «{name}» успешно сохранён!_"),
        parse_mode="Markdown",
        reply_markup=get_main_kb()
    )


@dp.callback_query(F.data.startswith("load_p:"))
async def cb_load_preset(call: CallbackQuery):
    p_name = call.data.split(":", 1)[1]
    cfg = db.get_preset_config(p_name)
    if not cfg:
        await call.answer("Не удалось загрузить пресет.", show_alert=True)
        return
    set_booking_times(cfg.get("times", []))
    set_selected_machines("wash", cfg.get("wash", []))
    set_selected_machines("dry", cfg.get("dry", []))
    await call.message.edit_text(
        get_main_text(f"✅ _Пресет «{p_name}» применён!_"),
        parse_mode="Markdown",
        reply_markup=get_main_kb()
    )
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
            "Убедитесь, что Google-аккаунту, для которого выпущен `token.json`, выдан доступ **Редактора** к таблице."
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