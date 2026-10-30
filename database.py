import sqlite3
import os
import json
from typing import List, Tuple


class Database:
    def __init__(self, db_path: str = "prachka.db"):
        self.db_path = db_path
        try:
            self._init_db()
        except sqlite3.DatabaseError:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            self._init_db()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time_slot TEXT NOT NULL,
                    machine_code TEXT NOT NULL,
                    priority INTEGER DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS hot_windows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS update_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    detected_at TEXT NOT NULL,
                    window_added TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS presets (
                    name TEXT PRIMARY KEY,
                    slots_json TEXT NOT NULL
                )
            """)
            conn.commit()

    # --- Настройки ---
    def get_setting(self, key: str, default: str = "") -> str:
        with self._get_connection() as conn:
            res = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return res["value"] if res else default

    def set_setting(self, key: str, value: str):
        with self._get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
            conn.commit()

    # --- Очередь слотов ---
    def get_targets(self) -> List[Tuple[str, str]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT time_slot, machine_code FROM targets ORDER BY priority ASC, id ASC").fetchall()
            return [(r["time_slot"], r["machine_code"]) for r in rows]

    def add_target(self, time_slot: str, machine_code: str):
        with self._get_connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM targets WHERE time_slot = ? AND machine_code = ?",
                (time_slot, machine_code)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO targets (time_slot, machine_code) VALUES (?, ?)",
                    (time_slot, machine_code)
                )
                conn.commit()

    def clear_targets(self):
        with self._get_connection() as conn:
            conn.execute("DELETE FROM targets")
            conn.commit()

    # --- Окна активности ---
    def get_windows(self) -> List[Tuple[str, str]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT start_time, end_time FROM hot_windows ORDER BY id ASC").fetchall()
            return [(r["start_time"], r["end_time"]) for r in rows]

    def add_window(self, start_time: str, end_time: str):
        with self._get_connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM hot_windows WHERE start_time = ? AND end_time = ?",
                (start_time, end_time)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO hot_windows (start_time, end_time) VALUES (?, ?)",
                    (start_time, end_time)
                )
                conn.commit()

    def delete_window(self, start_time: str, end_time: str):
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM hot_windows WHERE start_time = ? AND end_time = ?",
                (start_time, end_time)
            )
            conn.commit()

    # --- Логи автообучения ---
    def log_update(self, detected_at: str, window_added: str):
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO update_logs (detected_at, window_added) VALUES (?, ?)",
                (detected_at, window_added)
            )
            conn.commit()

    def get_recent_logs(self, limit: int = 3) -> List[str]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT detected_at FROM update_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [r["detected_at"] for r in rows]

    # --- Пресеты очередей ---
    def get_presets(self) -> List[str]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT name FROM presets ORDER BY name ASC").fetchall()
            return [r["name"] for r in rows]

    def save_preset(self, name: str, targets: List[Tuple[str, str]]):
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO presets (name, slots_json) VALUES (?, ?)",
                (name, json.dumps(targets))
            )
            conn.commit()

    def load_preset(self, name: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute("SELECT slots_json FROM presets WHERE name = ?", (name,)).fetchone()
            if not row:
                return False
            slots = json.loads(row["slots_json"])
            self.clear_targets()
            for time_slot, machine_code in slots:
                self.add_target(time_slot, machine_code)
            return True

    def delete_preset(self, name: str):
        with self._get_connection() as conn:
            conn.execute("DELETE FROM presets WHERE name = ?", (name,))
            conn.commit()