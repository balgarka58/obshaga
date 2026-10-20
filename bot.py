"""
Telegram Bot для изучения терминов обществознания (Версия 5.0 — Полная экосистема)
=============================================================================
Архитектура и возможности:
1. Термины загружаются из terms.json (при его отсутствии — резервная автозагрузка из social_bot.db).
2. Полное теоретическое досье по кнопке «💡 Пояснение»:
   - Развёрнутое эссе (concept_explanation)
   - Формы и классификации (types_or_forms)
   - Функции в обществе (functions_or_goals)
   - Примеры из реалий РФ (illustration_rf)
   - Ловушки и типичные ошибки на экзамене (common_pitfalls)
3. Универсальный интерфейс:
   - Удалены все жесткие метки «№18 ЕГЭ» — формулировки подходят как для ОГЭ (9 класс),
     так и для фундаментальной подготовки к ЕГЭ (10–11 класс).
4. Алгоритм интервального повторения для КАРТОЧЕК:
   - Вероятностная выборка (Weighted Random / Roulette Selection): вес определяет шанс выпадения.
   - Трудные термины выпадают часто, новые имеют высокий приоритет, выученные периодически всплывают.
   - Кулдаун карточек: карточка не повторяется минимум 10 шагов.
5. Режим ТЕСТЫ:
   - Честный экзаменационный рандом (True Random) без повторов за сессию.
   - Защита от мискликов: ответы быстрее 3 секунд не записываются в общую базу.
   - Экран завершения теста: разбор ошибок, рестарт и выход.
6. Учительская система с 8-значными кодами и мульти-привязкой:
   - Любой желающий может стать преподавателем по команде /teacher.
   - Генерация случайного 8-значного кода (например, 83910245).
   - Автоматическая очистка: если за 7 дней к учителю не подключился ни один ученик — профиль удаляется.
   - Ученик может подключиться по ссылке (?start=t_83910245) или просто отправив 8-значный код сообщением.
   - Мульти-учителя: ученик может быть одновременно подключён и к репетитору, и к школьному учителю.
   - Управление учителями: команда /teachers с кнопками быстрого отключения.
7. Персональная веб-аналитика для учеников:
   - В /menu:stats формируется безопасная ссылка с токеном на персональный кабинет в онлайн-дашборде.
8. Сохранение имени и юзернейма пользователя (full_name + @username).
9. Встроенный фоновый веб-сервер для бесплатного деплоя на Render Web Service ($0/мес).
"""

import os
import sys
import json
import time
import random
import hashlib
import logging
import sqlite3
import asyncio
from typing import Optional, Dict, Any, List, Tuple
from collections import deque

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.client.default import DefaultBotProperties

# =====================================================================
# 1. КОНФИГУРАЦИЯ И ЛОГИРОВАНИЕ
# =====================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH = os.getenv("DB_PATH", "social_bot.db")
TERMS_JSON_PATH = os.getenv("TERMS_JSON_PATH", "terms.json")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://obshaga.streamlit.app").rstrip("/")
SECRET_KEY = os.getenv("SECRET_KEY", "obshaga_secret_key_2025")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("social_bot")

# =====================================================================
# 2. АЛГОРИТМ ИНТЕРВАЛЬНОГО ПОВТОРЕНИЯ ДЛЯ КАРТОЧЕК
# =====================================================================

class SpacedRepetitionAlgorithm:
    MIN_WEIGHT = 1.0
    MAX_WEIGHT = 10.0
    DEFAULT_WEIGHT = 5.0
    UNREVIEWED_PRIORITY_WEIGHT = 6.5  # Непросмотренные карточки идут впереди хорошо изученных

    MIN_TIME_WINDOW = 3.0    # сек (меньше 3с - мисклик, время не учитывается)
    MAX_TIME_WINDOW = 180.0  # сек (больше 180с - отвлекся, время не учитывается)

    FAST_THRESHOLD = 10.0   # <= 10с -> уверенный ответ (-1.0)
    SLOW_THRESHOLD = 40.0   # > 40с -> долгие раздумья (+1.0)

    DELTA_ANSWER_MAP = {
        "know_well": -1.0,   # «Знаю»
        "correct": -1.0,     # Верный ответ в тесте
        "know_poorly": 0.5,  # «Неуверенно»
        "dont_know": 2.0,    # «Забыл»
        "incorrect": 2.0,    # Ошибка в тесте
    }

    @classmethod
    def calculate_time_delta(cls, response_time_seconds: Optional[float], is_correct: bool = True) -> float:
        if response_time_seconds is None or not is_correct:
            return 0.0
        if response_time_seconds < cls.MIN_TIME_WINDOW or response_time_seconds > cls.MAX_TIME_WINDOW:
            return 0.0
        if response_time_seconds <= cls.FAST_THRESHOLD:
            return -1.0
        elif response_time_seconds <= cls.SLOW_THRESHOLD:
            return 0.0
        else:
            return 1.0

    @classmethod
    def calculate_answer_delta(cls, answer_type: str) -> float:
        return cls.DELTA_ANSWER_MAP.get(answer_type, 0.0)

    @classmethod
    def update_weight(cls, current_weight: float, delta_answer: float, delta_time: float) -> float:
        new_weight = current_weight + delta_answer + delta_time
        return max(cls.MIN_WEIGHT, min(cls.MAX_WEIGHT, round(new_weight, 2)))


# =====================================================================
# 3. КАТАЛОГ ТЕРМИНОВ (JSON С РЕЗЕРВОМ ИЗ SQLITE)
# =====================================================================

class TermsCatalog:
    """Управляет терминами в оперативной памяти."""
    def __init__(self, json_path: str = TERMS_JSON_PATH, sqlite_path: str = DB_PATH):
        self.json_path = json_path
        self.sqlite_path = sqlite_path
        self.terms_by_code: Dict[str, Dict[str, Any]] = {}
        self.terms_by_module: Dict[int, List[Dict[str, Any]]] = {1: [], 2: [], 3: [], 4: [], 5: []}
        self.all_terms: List[Dict[str, Any]] = []

    def load(self):
        self.terms_by_code.clear()
        self.all_terms.clear()
        for m in self.terms_by_module:
            self.terms_by_module[m].clear()

        # Вариант 1: Загрузка из terms.json
        if os.path.exists(self.json_path):
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._populate_terms(data)
                logger.info(f"Загружено {len(self.all_terms)} терминов из {self.json_path}.")
                return
            except Exception as e:
                logger.warning(f"Ошибка чтения {self.json_path}: {e}. Пробуем резерв из БД.")

        # Вариант 2: Резервная автозагрузка напрямую из таблицы terms в SQLite
        if os.path.exists(self.sqlite_path):
            try:
                conn = sqlite3.connect(self.sqlite_path)
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='terms';")
                if cur.fetchone():
                    cur.execute("""
                        SELECT code, module_number, module_title, term, concept_explanation, 
                               genus, traits_json, structure_json, ege_context_json, pitfalls_json 
                        FROM terms ORDER BY code ASC
                    """)
                    db_terms = []
                    for r in cur.fetchall():
                        db_terms.append({
                            "code": str(r[0]).strip(),
                            "module_number": int(r[1]) if r[1] else 1,
                            "module_title": str(r[2]).strip(),
                            "term": str(r[3]).strip(),
                            "concept_explanation": str(r[4]).strip(),
                            "genus": str(r[5]).strip(),
                            "traits": json.loads(r[6]) if r[6] else [],
                            "structure_for_tests": json.loads(r[7]) if r[7] else {},
                            "ege_context_and_examples": json.loads(r[8]) if r[8] else {},
                            "common_pitfalls": json.loads(r[9]) if r[9] else []
                        })
                    conn.close()

                    self._populate_terms(db_terms)
                    logger.info(f"Резервная загрузка: успешно получено {len(self.all_terms)} терминов из {self.sqlite_path}.")
                    return
            except Exception as e:
                logger.error(f"Не удалось прочитать термины из SQLite: {e}")

        logger.error(f"Файл {self.json_path} не найден, и в базе нет таблицы terms!")

    def _populate_terms(self, data: List[Dict[str, Any]]):
        for item in data:
            code = str(item.get("code", "")).strip()
            if not code:
                continue

            try:
                mod_num = int(code.split(".")[0])
            except (ValueError, IndexError):
                mod_num = 1

            term_obj = {
                "code": code,
                "module_number": mod_num,
                "module_title": item.get("module_title", f"Модуль {mod_num}"),
                "term": item.get("term", ""),
                "concept_explanation": item.get("concept_explanation", ""),
                "genus": item.get("genus", ""),
                "traits": item.get("traits", []),
                "structure_for_tests": item.get("structure_for_tests", {}),
                "ege_context_and_examples": item.get("ege_context_and_examples", {}),
                "common_pitfalls": item.get("common_pitfalls", []),
            }
            self.terms_by_code[code] = term_obj
            self.all_terms.append(term_obj)
            if mod_num in self.terms_by_module:
                self.terms_by_module[mod_num].append(term_obj)
            else:
                self.terms_by_module[mod_num] = [term_obj]

    def get_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        return self.terms_by_code.get(code)

    def get_terms(self, module_filter: Optional[int] = None) -> List[Dict[str, Any]]:
        if module_filter is not None:
            return self.terms_by_module.get(module_filter, [])
        return self.all_terms


catalog = TermsCatalog()

# =====================================================================
# 4. ХРАНИЛИЩЕ ДАННЫХ (NEON POSTGRESQL / SQLITE FALLBACK)
# =====================================================================

class Database:
    """
    В базе данных сохраняются:
    1. Ответы учеников: user_id, username, full_name, строка entry.
    2. Таблица учителей: teacher_code (8-значное число), user_id, full_name, created_at.
    3. Связующая таблица student_teachers (student_id, teacher_code, joined_at).
    """
    def __init__(self, db_url: Optional[str] = DATABASE_URL, sqlite_path: str = DB_PATH):
        self.db_url = db_url
        self.sqlite_path = sqlite_path
        self.is_postgres = bool(db_url and db_url.startswith("postgres"))

    def get_connection(self):
        if self.is_postgres:
            import psycopg2
            return psycopg2.connect(self.db_url)
        else:
            conn = sqlite3.connect(self.sqlite_path)
            conn.row_factory = sqlite3.Row
            return conn

    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        
        if self.is_postgres:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_answers (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                full_name TEXT,
                entry TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            ALTER TABLE user_answers ADD COLUMN IF NOT EXISTS username TEXT;
            ALTER TABLE user_answers ADD COLUMN IF NOT EXISTS full_name TEXT;
            CREATE INDEX IF NOT EXISTS idx_user_answers_user ON user_answers(user_id);

            CREATE TABLE IF NOT EXISTS teachers (
                teacher_code VARCHAR(8) PRIMARY KEY,
                user_id BIGINT NOT NULL,
                full_name TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_teachers_user ON teachers(user_id);

            CREATE TABLE IF NOT EXISTS student_teachers (
                student_id BIGINT NOT NULL,
                teacher_code VARCHAR(8) NOT NULL,
                joined_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (student_id, teacher_code)
            );
            CREATE INDEX IF NOT EXISTS idx_student_teachers_teacher ON student_teachers(teacher_code);
            CREATE INDEX IF NOT EXISTS idx_student_teachers_student ON student_teachers(student_id);
            """)
        else:
            cursor.executescript("""
            CREATE TABLE IF NOT EXISTS user_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                entry TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_user_answers_user ON user_answers(user_id);

            CREATE TABLE IF NOT EXISTS teachers (
                teacher_code TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                full_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_teachers_user ON teachers(user_id);

            CREATE TABLE IF NOT EXISTS student_teachers (
                student_id INTEGER NOT NULL,
                teacher_code TEXT NOT NULL,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (student_id, teacher_code)
            );
            CREATE INDEX IF NOT EXISTS idx_student_teachers_teacher ON student_teachers(teacher_code);
            CREATE INDEX IF NOT EXISTS idx_student_teachers_student ON student_teachers(student_id);
            """)
            try:
                cursor.execute("ALTER TABLE user_answers ADD COLUMN username TEXT;")
            except Exception:
                pass
            try:
                cursor.execute("ALTER TABLE user_answers ADD COLUMN full_name TEXT;")
            except Exception:
                pass

        conn.commit()
        cursor.close()
        conn.close()

        # Запускаем автоочистку учителей без учеников старше 7 дней
        self.cleanup_inactive_teachers()

        mode_str = "Neon PostgreSQL" if self.is_postgres else f"SQLite ({self.sqlite_path})"
        logger.info(f"База данных успешно инициализирована в режиме: {mode_str}.")

    def save_answer_entry(self, user_id: int, username: Optional[str], full_name: Optional[str], entry_str: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        if self.is_postgres:
            cursor.execute(
                "INSERT INTO user_answers (user_id, username, full_name, entry) VALUES (%s, %s, %s, %s);",
                (user_id, username, full_name, entry_str)
            )
        else:
            cursor.execute(
                "INSERT INTO user_answers (user_id, username, full_name, entry) VALUES (?, ?, ?, ?);",
                (user_id, username, full_name, entry_str)
            )
        conn.commit()
        cursor.close()
        conn.close()

    def get_user_progress_map(self, user_id: int) -> Dict[str, Dict[str, Any]]:
        """
        Вычисляет интервальные веса карточек ТОЛЬКО по режиму карточек (mode != 'test'),
        чтобы результаты экзаменационных тестов не сбивали обучение в карточках.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        if self.is_postgres:
            cursor.execute("SELECT entry FROM user_answers WHERE user_id = %s ORDER BY id ASC;", (user_id,))
        else:
            cursor.execute("SELECT entry FROM user_answers WHERE user_id = ? ORDER BY id ASC;", (user_id,))
        
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        progress: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            entry_str = row[0] if self.is_postgres else row["entry"]
            parts = str(entry_str).split(";")
            if len(parts) >= 4:
                mode = parts[4].strip() if len(parts) >= 5 else "card"
                if mode == "test":
                    continue

                code = parts[0].strip()
                try:
                    d_ans = float(parts[1])
                    d_tm = float(parts[2])
                except ValueError:
                    continue

                if code not in progress:
                    progress[code] = {
                        "total_weight": SpacedRepetitionAlgorithm.DEFAULT_WEIGHT,
                        "review_count": 0
                    }

                cur_w = progress[code]["total_weight"]
                progress[code]["total_weight"] = SpacedRepetitionAlgorithm.update_weight(cur_w, d_ans, d_tm)
                progress[code]["review_count"] += 1

        return progress

    def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        """Возвращает раздельные метрики для карточек и тестов."""
        progress_map = self.get_user_progress_map(user_id)
        
        conn = self.get_connection()
        cursor = conn.cursor()
        if self.is_postgres:
            cursor.execute("SELECT entry FROM user_answers WHERE user_id = %s;", (user_id,))
        else:
            cursor.execute("SELECT entry FROM user_answers WHERE user_id = ?;", (user_id,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        cards_count = 0
        tests_count = 0
        tests_correct = 0
        tests_errors = 0
        test_times = []

        for r in rows:
            entry_str = r[0] if self.is_postgres else r["entry"]
            parts = str(entry_str).split(";")
            if len(parts) >= 4:
                try:
                    d_ans = float(parts[1])
                    sec = float(parts[3])
                except ValueError:
                    continue

                mode = parts[4].strip() if len(parts) >= 5 else "card"
                if mode == "test":
                    if sec >= 3.0:
                        tests_count += 1
                        if d_ans <= 0:
                            tests_correct += 1
                        else:
                            tests_errors += 1
                        if 3.0 <= sec <= 180.0:
                            test_times.append(sec)
                else:
                    cards_count += 1

        tests_accuracy = round(tests_correct / tests_count * 100, 1) if tests_count > 0 else 0.0
        tests_avg_time = round(sum(test_times) / len(test_times), 1) if test_times else 0.0

        learned = sum(1 for p in progress_map.values() if p["total_weight"] <= 3.0)
        in_progress = sum(1 for p in progress_map.values() if 3.0 < p["total_weight"] <= 7.0)
        needs_review = sum(1 for p in progress_map.values() if p["total_weight"] > 7.0)

        return {
            "cards_count": cards_count,
            "learned": learned,
            "in_progress": in_progress,
            "needs_review": needs_review,
            "tests_count": tests_count,
            "tests_correct": tests_correct,
            "tests_errors": tests_errors,
            "tests_accuracy": tests_accuracy,
            "tests_avg_time": tests_avg_time,
        }

    # --- УЧИТЕЛЯ И КЛАССЫ ---

    def cleanup_inactive_teachers(self):
        """
        Удаляет учителей, созданных более 7 дней назад, к которым
        не подключился ни один ученик (чистота базы от неактивных аккаунтов).
        """
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            if self.is_postgres:
                cursor.execute("""
                    DELETE FROM teachers 
                    WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '7 days'
                      AND teacher_code NOT IN (SELECT DISTINCT teacher_code FROM student_teachers);
                """)
            else:
                cursor.execute("""
                    DELETE FROM teachers 
                    WHERE created_at < datetime('now', '-7 days')
                      AND teacher_code NOT IN (SELECT DISTINCT teacher_code FROM student_teachers);
                """)
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            logger.warning(f"Ошибка при очистке неактивных учителей: {e}")

    def get_teacher(self, teacher_code: str) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cursor = conn.cursor()
        if self.is_postgres:
            cursor.execute("SELECT teacher_code, user_id, full_name, created_at FROM teachers WHERE teacher_code = %s;", (teacher_code,))
        else:
            cursor.execute("SELECT teacher_code, user_id, full_name, created_at FROM teachers WHERE teacher_code = ?;", (teacher_code,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row:
            return {
                "teacher_code": str(row[0]),
                "user_id": int(row[1]),
                "full_name": str(row[2]) if row[2] else f"Учитель {row[0]}",
                "created_at": row[3]
            }
        return None

    def get_teacher_by_user_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cursor = conn.cursor()
        if self.is_postgres:
            cursor.execute("SELECT teacher_code, user_id, full_name, created_at FROM teachers WHERE user_id = %s;", (user_id,))
        else:
            cursor.execute("SELECT teacher_code, user_id, full_name, created_at FROM teachers WHERE user_id = ?;", (user_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row:
            return {
                "teacher_code": str(row[0]),
                "user_id": int(row[1]),
                "full_name": str(row[2]) if row[2] else f"Учитель {row[0]}",
                "created_at": row[3]
            }
        return None

    def register_teacher(self, user_id: int, full_name: str) -> str:
        """Генерирует уникальный случайный 8-значный код и регистрирует преподавателя."""
        conn = self.get_connection()
        cursor = conn.cursor()
        while True:
            code = str(random.randint(10000000, 99999999))
            if self.is_postgres:
                cursor.execute("SELECT 1 FROM teachers WHERE teacher_code = %s;", (code,))
            else:
                cursor.execute("SELECT 1 FROM teachers WHERE teacher_code = ?;", (code,))
            if not cursor.fetchone():
                break

        if self.is_postgres:
            cursor.execute(
                "INSERT INTO teachers (teacher_code, user_id, full_name) VALUES (%s, %s, %s);",
                (code, user_id, full_name)
            )
        else:
            cursor.execute(
                "INSERT INTO teachers (teacher_code, user_id, full_name) VALUES (?, ?, ?);",
                (code, user_id, full_name)
            )
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"Зарегистрирован новый преподаватель {full_name} с кодом {code}.")
        return code

    def is_student_connected(self, student_id: int, teacher_code: str) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        if self.is_postgres:
            cursor.execute("SELECT 1 FROM student_teachers WHERE student_id = %s AND teacher_code = %s;", (student_id, teacher_code))
        else:
            cursor.execute("SELECT 1 FROM student_teachers WHERE student_id = ? AND teacher_code = ?;", (student_id, teacher_code))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return bool(row)

    def connect_student_to_teacher(self, student_id: int, teacher_code: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        if self.is_postgres:
            cursor.execute("""
                INSERT INTO student_teachers (student_id, teacher_code) 
                VALUES (%s, %s) 
                ON CONFLICT (student_id, teacher_code) DO NOTHING;
            """, (student_id, teacher_code))
        else:
            cursor.execute("""
                INSERT OR IGNORE INTO student_teachers (student_id, teacher_code) 
                VALUES (?, ?);
            """, (student_id, teacher_code))
        conn.commit()
        cursor.close()
        conn.close()

    def disconnect_student_from_teacher(self, student_id: int, teacher_code: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        if self.is_postgres:
            cursor.execute("DELETE FROM student_teachers WHERE student_id = %s AND teacher_code = %s;", (student_id, teacher_code))
        else:
            cursor.execute("DELETE FROM student_teachers WHERE student_id = ? AND teacher_code = ?;", (student_id, teacher_code))
        conn.commit()
        cursor.close()
        conn.close()

    def get_student_teachers(self, student_id: int) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        cursor = conn.cursor()
        query = """
            SELECT t.teacher_code, t.full_name, st.joined_at 
            FROM student_teachers st
            JOIN teachers t ON st.teacher_code = t.teacher_code
            WHERE st.student_id = %s
            ORDER BY st.joined_at DESC;
        """ if self.is_postgres else """
            SELECT t.teacher_code, t.full_name, st.joined_at 
            FROM student_teachers st
            JOIN teachers t ON st.teacher_code = t.teacher_code
            WHERE st.student_id = ?
            ORDER BY st.joined_at DESC;
        """
        cursor.execute(query, (student_id,))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        teachers = []
        for r in rows:
            teachers.append({
                "teacher_code": str(r[0]),
                "full_name": str(r[1]) if r[1] else f"Учитель {r[0]}",
                "joined_at": r[2]
            })
        return teachers

    def get_teacher_students_count(self, teacher_code: str) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        if self.is_postgres:
            cursor.execute("SELECT COUNT(*) FROM student_teachers WHERE teacher_code = %s;", (teacher_code,))
        else:
            cursor.execute("SELECT COUNT(*) FROM student_teachers WHERE teacher_code = ?;", (teacher_code,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return row[0] if row else 0


db = Database()

# =====================================================================
# 5. СЕССИОННЫЙ МЕНЕДЖЕР И ВЫБОРКА КАРТОЧЕК
# =====================================================================

class SessionTracker:
    """Отслеживает последние 10 показанных карточек для исключения повторений подряд."""
    def __init__(self):
        self.recent_history: Dict[int, deque] = {}

    def push(self, user_id: int, code: str):
        if user_id not in self.recent_history:
            self.recent_history[user_id] = deque(maxlen=10)
        self.recent_history[user_id].append(code)

    def get_recent(self, user_id: int) -> List[str]:
        if user_id in self.recent_history:
            return list(self.recent_history[user_id])
        return []


session_tracker = SessionTracker()


def select_next_card(user_id: int, module_filter: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Вероятностный алгоритм (Weighted Random / Roulette Selection):
    Карточки не идут по скучной очереди. Вес определяет шанс выпадения:
    трудные (вес 8-10) выпадают часто, новые (6.5) — в приоритете,
    а выученные (1.0) время от времени неожиданно всплывают для проверки.
    """
    available_terms = catalog.get_terms(module_filter)
    if not available_terms:
        return None

    progress_map = db.get_user_progress_map(user_id)
    recent_codes = set(session_tracker.get_recent(user_id))

    candidates = [t for t in available_terms if t["code"] not in recent_codes]
    if not candidates:
        candidates = available_terms

    def get_effective_weight(term_dict: Dict[str, Any]) -> float:
        code = term_dict["code"]
        if code not in progress_map or progress_map[code]["review_count"] == 0:
            return SpacedRepetitionAlgorithm.UNREVIEWED_PRIORITY_WEIGHT  # 6.5
        return progress_map[code]["total_weight"]

    weights = [get_effective_weight(t) for t in candidates]

    chosen = random.choices(candidates, weights=weights, k=1)[0]
    session_tracker.push(user_id, chosen["code"])
    return chosen


# =====================================================================
# 6. ГЕНЕРАТОР ТЕСТОВ (БЕЗ ДУБЛИРОВАНИЯ ВАРИАНТОВ)
# =====================================================================

class TestGenerator:
    @staticmethod
    def generate(correct_term: Dict[str, Any], module_filter: Optional[int] = None) -> Dict[str, Any]:
        all_pool = catalog.get_terms(module_filter)
        if len(all_pool) < 4:
            all_pool = catalog.get_terms(None)

        distractors = [t for t in all_pool if t["code"] != correct_term["code"]]
        if len(distractors) < 3:
            distractors = [t for t in catalog.all_terms if t["code"] != correct_term["code"]]

        available_types = [1, 2, 3]
        struct = correct_term.get("structure_for_tests", {})
        if struct.get("types_or_forms"):
            available_types.append(4)
        if struct.get("functions_or_goals"):
            available_types.append(5)

        chosen_type = random.choice(available_types)
        explanation_note = ""

        # ТИП 1: По ключевым признакам угадать термин
        if chosen_type == 1:
            traits = correct_term.get("traits", [])
            traits_str = "\n".join([f"• {t}" for t in traits[:3]]) if traits else f"• {correct_term['genus']}"
            question_text = (
                f"К какому понятию относятся следующие ключевые признаки:\n\n"
                f"{traits_str}\n\n"
                f"Родовое понятие: _{correct_term['genus']}_"
            )
            picked = random.sample(distractors, 3)
            options = [
                {"id": "A", "code": correct_term["code"], "text": correct_term["term"], "is_correct": True},
                {"id": "B", "code": picked[0]["code"], "text": picked[0]["term"], "is_correct": False},
                {"id": "C", "code": picked[1]["code"], "text": picked[1]["term"], "is_correct": False},
                {"id": "D", "code": picked[2]["code"], "text": picked[2]["term"], "is_correct": False},
            ]

        # ТИП 2: По термину выбрать родовую характеристику (уникальные genus)
        elif chosen_type == 2:
            question_text = f"Выбери верную родовую характеристику термина *{correct_term['term'].upper()}*:"
            
            target_genus = correct_term.get("genus", "").strip()
            seen_genera = {target_genus}
            valid_distractors = []
            
            shuffled_pool = random.sample(catalog.all_terms, len(catalog.all_terms))
            for d in shuffled_pool:
                g = d.get("genus", "").strip()
                if g and g not in seen_genera:
                    seen_genera.add(g)
                    valid_distractors.append(d)
                if len(valid_distractors) == 3:
                    break

            while len(valid_distractors) < 3:
                valid_distractors.append(random.choice(distractors))

            options = [
                {"id": "A", "code": correct_term["code"], "text": target_genus, "is_correct": True},
                {"id": "B", "code": valid_distractors[0]["code"], "text": valid_distractors[0]["genus"], "is_correct": False},
                {"id": "C", "code": valid_distractors[1]["code"], "text": valid_distractors[1]["genus"], "is_correct": False},
                {"id": "D", "code": valid_distractors[2]["code"], "text": valid_distractors[2]["genus"], "is_correct": False},
            ]

        # ТИП 3: «Найди лишнее» (чужой признак)
        elif chosen_type == 3:
            question_text = (
                f"Все перечисленные ниже признаки, за исключением одного, относятся к понятию *{correct_term['term'].upper()}*.\n\n"
                f"Найди выпадающий (чужой) признак:"
            )
            real_traits = correct_term.get("traits", [])
            sample_real = real_traits[:3] if len(real_traits) >= 3 else [correct_term["genus"]]
            
            foreign_term = random.choice(distractors)
            f_traits = foreign_term.get("traits", [])
            foreign_trait = f_traits[0] if f_traits else foreign_term.get("genus", "Чужой признак")
            explanation_note = f"Лишний признак: «{foreign_trait}» (относится к понятию «{foreign_term['term']}»)."

            options = [
                {"id": "A", "code": correct_term["code"], "text": foreign_trait, "is_correct": True},
                {"id": "B", "code": correct_term["code"], "text": sample_real[0], "is_correct": False},
                {"id": "C", "code": correct_term["code"], "text": sample_real[1] if len(sample_real) > 1 else "Специфический признак", "is_correct": False},
                {"id": "D", "code": correct_term["code"], "text": sample_real[2] if len(sample_real) > 2 else "Общая характеристика", "is_correct": False},
            ]

        # ТИП 4: Классификации и формы
        elif chosen_type == 4:
            types_list = struct.get("types_or_forms", [])
            types_str = ", ".join(types_list[:3])
            question_text = f"Для какого из терминов характерны следующие формы/виды: _{types_str}_?"
            picked = random.sample(distractors, 3)
            options = [
                {"id": "A", "code": correct_term["code"], "text": correct_term["term"], "is_correct": True},
                {"id": "B", "code": picked[0]["code"], "text": picked[0]["term"], "is_correct": False},
                {"id": "C", "code": picked[1]["code"], "text": picked[1]["term"], "is_correct": False},
                {"id": "D", "code": picked[2]["code"], "text": picked[2]["term"], "is_correct": False},
            ]

        # ТИП 5: Функции в обществе
        else:
            funcs = struct.get("functions_or_goals", [])
            funcs_str = ", ".join(funcs[:3])
            question_text = f"Какое понятие выполняет в обществе следующие функции: _{funcs_str}_?"
            picked = random.sample(distractors, 3)
            options = [
                {"id": "A", "code": correct_term["code"], "text": correct_term["term"], "is_correct": True},
                {"id": "B", "code": picked[0]["code"], "text": picked[0]["term"], "is_correct": False},
                {"id": "C", "code": picked[1]["code"], "text": picked[1]["term"], "is_correct": False},
                {"id": "D", "code": picked[2]["code"], "text": picked[2]["term"], "is_correct": False},
            ]

        random.shuffle(options)

        return {
            "chosen_type": chosen_type,
            "question_text": question_text,
            "options": options,
            "correct_term": correct_term,
            "explanation_note": explanation_note
        }


# =====================================================================
# 7. СОСТОЯНИЯ И КЛАВИАТУРЫ
# =====================================================================

class UserFlow(StatesGroup):
    selecting_mode = State()
    selecting_topic = State()
    in_flashcards = State()
    in_tests = State()


class Keyboards:
    @staticmethod
    def main_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗂 Карточки (Интервальные)", callback_data="menu:cards")],
            [InlineKeyboardButton(text="🎯 Тесты (Контрольный срез)", callback_data="menu:tests")],
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="menu:stats")],
        ])

    @staticmethod
    def topic_selection(mode: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Все темы кодификатора", callback_data=f"topic:{mode}:all")],
            [InlineKeyboardButton(text="1. Человек и общество", callback_data=f"topic:{mode}:1")],
            [InlineKeyboardButton(text="2. Экономика", callback_data=f"topic:{mode}:2")],
            [InlineKeyboardButton(text="3. Социальные отношения", callback_data=f"topic:{mode}:3")],
            [InlineKeyboardButton(text="4. Политика", callback_data=f"topic:{mode}:4")],
            [InlineKeyboardButton(text="5. Право", callback_data=f"topic:{mode}:5")],
            [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")],
        ])

    @staticmethod
    def flashcard_front(code: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Перевернуть карточку", callback_data=f"card:reveal:{code}")],
            [InlineKeyboardButton(text="🚪 В главное меню", callback_data="back_to_main")]
        ])

    @staticmethod
    def flashcard_back(code: str, has_detail: bool = False) -> InlineKeyboardMarkup:
        buttons = [
            [
                InlineKeyboardButton(text="🟢 Знаю", callback_data=f"card:ans:{code}:know_well"),
                InlineKeyboardButton(text="🟡 Неуверенно", callback_data=f"card:ans:{code}:know_poorly"),
                InlineKeyboardButton(text="🔴 Забыл", callback_data=f"card:ans:{code}:dont_know"),
            ]
        ]
        second_row = []
        if not has_detail:
            second_row.append(InlineKeyboardButton(text="💡 Пояснение", callback_data=f"card:detail:{code}"))
        second_row.append(InlineKeyboardButton(text="🚪 Меню", callback_data="back_to_main"))
        buttons.append(second_row)
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def test_options(code: str, options: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
        buttons = []
        for idx, opt in enumerate(options):
            buttons.append([
                InlineKeyboardButton(
                    text=f"👉 {opt['text']}",
                    callback_data=f"test:ans:{code}:{idx}"
                )
            ])
        buttons.append([InlineKeyboardButton(text="🚪 Завершить тест", callback_data="back_to_main")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def test_next() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Следующий вопрос", callback_data="test:next")],
            [InlineKeyboardButton(text="🚪 В главное меню", callback_data="back_to_main")],
        ])

    @staticmethod
    def test_finish(has_errors: bool = True) -> InlineKeyboardMarkup:
        buttons = []
        if has_errors:
            buttons.append([InlineKeyboardButton(text="❌ Просмотреть ошибки", callback_data="test:show_errors")])
        buttons.append([InlineKeyboardButton(text="🔄 Начать заново", callback_data="test:restart")])
        buttons.append([InlineKeyboardButton(text="🚪 В главное меню", callback_data="back_to_main")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def back_to_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")]
        ])


# =====================================================================
# 8. ХЕНДЛЕРЫ
# =====================================================================

router = Router()

@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext):
    await state.clear()
    
    # Проверяем deep link реферального подключения к учителю (?start=t_83910245)
    connect_notice = ""
    parts = message.text.split() if message.text else []
    if len(parts) > 1 and parts[1].startswith("t_"):
        teacher_code = parts[1][2:].strip()
        teacher = db.get_teacher(teacher_code)
        if teacher:
            if not db.is_student_connected(message.from_user.id, teacher_code):
                db.connect_student_to_teacher(message.from_user.id, teacher_code)
                connect_notice = (
                    f"✅ *Ты успешно прикреплён к преподавателю:*\n"
                    f"*{teacher['full_name']}* (код `{teacher_code}`).\n"
                    f"Твой прогресс теперь отображается в его ведомости.\n\n"
                )
            else:
                connect_notice = (
                    f"ℹ️ *Ты уже прикреплён к преподавателю:*\n"
                    f"*{teacher['full_name']}* (код `{teacher_code}`).\n\n"
                )

    text = (
        f"{connect_notice}"
        "👋 *Добро пожаловать в тренажер терминов по обществознанию!*\n\n"
        "Здесь ты сможешь выучить все ключевые понятия кодификатора без зубрёжки:\n\n"
        "• 🗂 *Карточки* — умное интервальное повторение. Трудные термины выпадают чаще, "
        "а выученные периодически всплывают для проверки памяти.\n"
        "• 🎯 *Тесты* — честный случайный экзамен без подсказок. Решай задания и разбирай ошибки.\n\n"
        "Выбери формат тренировки:"
    )
    await message.answer(text, reply_markup=Keyboards.main_menu(), parse_mode=ParseMode.MARKDOWN)


@router.message(Command("help"))
async def handle_help(message: Message):
    text = (
        "📖 *Как устроен тренажер:*\n\n"
        "1. *Карточки (Интервальное повторение):*\n"
        "• Карточки не идут по скучной очереди. Алгоритм рассчитывает вес каждого термина.\n"
        "• Если жмёшь `🟢 Знаю`, карточка отдаляется. Если `🔴 Забыл` — сразу летит в приоритет.\n"
        "• Кнопка `💡 Пояснение` открывает полное теоретическое досье (эссе, формы, функции, примеры РФ, типичные ловушки).\n"
        "• Есть защита от повторений: один и тот же термин не выпадает 10 шагов подряд.\n\n"
        "2. *Тесты (Контрольный срез):*\n"
        "• Случайная выборка вопросов по выбранной теме без повторений за сессию.\n"
        "• Ответы быстрее 3 секунд не считаются в общую статистику (защита от случайных мискликов).\n"
        "• В конце теста ты увидишь процент точности и сможешь разобрать ошибки.\n\n"
        "3. *Преподавателям и репетиторам:*\n"
        "• Напиши команду `/teacher`, чтобы получить свой личный 8-значный код класса и персональную веб-аналитику.\n\n"
        "4. *Подключение к преподавателю:*\n"
        "• Просто отправь 8-значный код учителя сообщением в этот чат.\n"
        "• Список подключённых преподавателей и отключение: команда `/teachers`."
    )
    await message.answer(text, reply_markup=Keyboards.back_to_menu(), parse_mode=ParseMode.MARKDOWN)


@router.message(Command("teacher"))
async def handle_teacher_profile(message: Message):
    user_id = message.from_user.id
    existing = db.get_teacher_by_user_id(user_id)
    
    bot_info = await message.bot.get_me()
    bot_username = bot_info.username or "obshaga_bot"

    if existing:
        code = existing["teacher_code"]
        st_count = db.get_teacher_students_count(code)
        text = (
            f"👨‍🏫 *Кабинет преподавателя*\n\n"
            f"👤 Преподаватель: *{existing['full_name']}*\n"
            f"🔑 Личный код класса: `{code}`\n"
            f"👥 Подключено учеников: *{st_count}*\n\n"
            f"🔗 *Ссылка для учеников (отправь в чат класса):*\n"
            f"https://t.me/{bot_username}?start=t_{code}\n\n"
            f"📊 *Твоя онлайн-аналитика класса:*\n"
            f"{DASHBOARD_URL}/?teacher={code}\n\n"
            f"_💡 Ученики могут перейти по ссылке выше или просто отправить в чат бота твой 8-значный код `{code}`._"
        )
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return

    # Если еще не зарегистрирован как учитель:
    user = message.from_user
    full_name = user.full_name or (f"@{user.username}" if user.username else f"Преподаватель {user.id}")
    code = db.register_teacher(user_id, full_name)
    
    text = (
        f"🎉 *Вы успешно зарегистрированы как преподаватель!*\n\n"
        f"👤 Имя: *{full_name}*\n"
        f"🔑 Ваш личный 8-значный код: `{code}`\n\n"
        f"👥 *Ссылка для учеников (отправьте её ребятам):*\n"
        f"https://t.me/{bot_username}?start=t_{code}\n\n"
        f"📊 *Ваша онлайн-аналитика класса:*\n"
        f"{DASHBOARD_URL}/?teacher={code}\n\n"
        f"💡 _Ученики могут перейти по ссылке или просто отправить в чат бота ваш код `{code}`._\n"
        f"⏳ _Если за 7 дней к вам не подключится ни один ученик, профиль учителя автоматически аннулируется для чистоты базы._"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


@router.message(Command("teachers"))
async def handle_teachers_list(message: Message):
    teachers = db.get_student_teachers(message.from_user.id)
    if not teachers:
        await message.answer(
            "ℹ️ Ты пока не прикреплён ни к одному преподавателю.\n\n"
            "Чтобы подключиться, отправь его 8-значный код сообщением в этот чат "
            "или перейди по ссылке-приглашению от учителя.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    buttons = []
    lines = ["👨‍🏫 *Твои преподаватели:*\n"]
    for idx, t in enumerate(teachers, 1):
        lines.append(f"{idx}. *{t['full_name']}* (код `{t['teacher_code']}`)")
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ Отключить: {t['full_name']}",
                callback_data=f"teacher:unlink:{t['teacher_code']}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")])

    text = "\n".join(lines) + "\n\n_Нажми кнопку ниже, если хочешь отключиться от преподавателя:_"
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.MARKDOWN)


@router.message(F.text.regexp(r"^\d{8}$"))
async def handle_teacher_code_input(message: Message):
    code = message.text.strip()
    teacher = db.get_teacher(code)
    if not teacher:
        await message.answer(
            f"❌ Преподаватель с кодом `{code}` не найден.\n"
            f"Проверь правильность ввода 8 цифр или уточни код у своего преподавателя.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if db.is_student_connected(message.from_user.id, code):
        await message.answer(
            f"ℹ️ Ты уже подключён к преподавателю *{teacher['full_name']}* (код `{code}`).",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    db.connect_student_to_teacher(message.from_user.id, code)
    await message.answer(
        f"✅ *Преподаватель найден:*\n"
        f"*{teacher['full_name']}* (код `{code}`).\n\n"
        f"Ты успешно подключён! Твой прогресс теперь отображается в его ведомости.",
        parse_mode=ParseMode.MARKDOWN
    )


@router.callback_query(F.data.startswith("teacher:unlink:"))
async def handle_unlink_teacher(callback: CallbackQuery):
    code = callback.data.split(":")[2]
    teacher = db.get_teacher(code)
    t_name = teacher["full_name"] if teacher else f"Код {code}"
    db.disconnect_student_from_teacher(callback.from_user.id, code)
    await callback.answer(f"Отключено от {t_name}", show_alert=True)

    teachers = db.get_student_teachers(callback.from_user.id)
    if not teachers:
        if callback.message:
            await callback.message.edit_text(
                "ℹ️ Ты отключился от всех преподавателей.\n\n"
                "Чтобы подключиться снова, просто отправь 8-значный код преподавателя.",
                reply_markup=Keyboards.back_to_menu(),
                parse_mode=ParseMode.MARKDOWN
            )
        return

    buttons = []
    lines = ["👨‍🏫 *Твои преподаватели:*\n"]
    for idx, t in enumerate(teachers, 1):
        lines.append(f"{idx}. *{t['full_name']}* (код `{t['teacher_code']}`)")
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ Отключить: {t['full_name']}",
                callback_data=f"teacher:unlink:{t['teacher_code']}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")])
    text = "\n".join(lines) + "\n\n_Нажми кнопку ниже, если хочешь отключиться от преподавателя:_"
    if callback.message:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data == "back_to_main")
async def handle_back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text = "🏠 *Главное меню*\n\nВыбери формат тренировки:"
    if callback.message:
        await callback.message.edit_text(text, reply_markup=Keyboards.main_menu(), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data == "menu:stats")
async def handle_show_stats(callback: CallbackQuery):
    user_id = callback.from_user.id
    stats = db.get_user_stats(user_id)

    # Формируем персональную защищённую ссылку для ученика
    token = hashlib.md5(f"{user_id}_{SECRET_KEY}".encode()).hexdigest()[:8]
    student_web_url = f"{DASHBOARD_URL}/?student={user_id}&token={token}"

    text = (
        "📊 *Твоя статистика изучения*\n\n"
        "🗂 *Карточки (Интервальное повторение):*\n"
        f"• Всего ответов: *{stats['cards_count']}*\n"
        f"• 🟢 Выучено надёжно: *{stats['learned']}*\n"
        f"• 🟡 В процессе: *{stats['in_progress']}*\n"
        f"• 🔴 Требуют повторения: *{stats['needs_review']}*\n\n"
        "🎯 *Тесты (Контрольный срез):*\n"
        f"• Вопросов решено: *{stats['tests_count']}*\n"
        f"• Точность: *{stats['tests_accuracy']}%* ({stats['tests_correct']} верно / {stats['tests_errors']} ошибок)\n"
        f"• Среднее время: *{stats['tests_avg_time']} сек*"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть подробную веб-аналитику", url=student_web_url)],
        [InlineKeyboardButton(text="👨‍🏫 Мои преподаватели", callback_data="menu:my_teachers")],
        [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")]
    ])
    if callback.message:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data == "menu:my_teachers")
async def handle_cb_my_teachers(callback: CallbackQuery):
    teachers = db.get_student_teachers(callback.from_user.id)
    if not teachers:
        await callback.answer("У тебя пока нет прикреплённых преподавателей. Отправь код учителя сообщением!", show_alert=True)
        return

    buttons = []
    lines = ["👨‍🏫 *Твои преподаватели:*\n"]
    for idx, t in enumerate(teachers, 1):
        lines.append(f"{idx}. *{t['full_name']}* (код `{t['teacher_code']}`)")
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ Отключить: {t['full_name']}",
                callback_data=f"teacher:unlink:{t['teacher_code']}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="⬅️ В статистику", callback_data="menu:stats")])
    text = "\n".join(lines) + "\n\n_Нажми кнопку ниже, чтобы отключить преподавателя:_"
    if callback.message:
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data.startswith("menu:"))
async def handle_menu_selection(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.split(":")[1]
    await state.update_data(mode=mode)
    await state.set_state(UserFlow.selecting_topic)

    mode_name = "🗂 Карточки" if mode == "cards" else "🎯 Тесты"
    text = (
        f"Настройка режима *{mode_name}*:\n\n"
        "Выбери, какие термины тренировать:\n"
        "• *Все темы* — перемешиваются все понятия кодификатора;\n"
        "• *Отдельный модуль* — только термины выбранного блока (1..5)."
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=Keyboards.topic_selection(mode), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data.startswith("topic:"))
async def handle_topic_selection(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    mode = parts[1]
    topic_val = parts[2]

    module_filter = None if topic_val == "all" else int(topic_val)
    await state.update_data(module_filter=module_filter, mode=mode)

    if mode == "cards":
        await state.set_state(UserFlow.in_flashcards)
        await send_next_flashcard(callback, state)
    else:
        await state.set_state(UserFlow.in_tests)
        available = catalog.get_terms(module_filter)
        if not available:
            empty_text = "❌ В выбранном модуле пока недостаточно терминов."
            if callback.message:
                await callback.message.edit_text(empty_text, reply_markup=Keyboards.back_to_menu())
            await callback.answer()
            return

        pool = [t["code"] for t in available]
        random.shuffle(pool)

        await state.update_data(
            test_pool=pool,
            test_total_in_session=len(pool),
            test_session_correct=0,
            test_session_errors=[],
            test_session_times=[]
        )
        await send_next_test(callback, state)

    await callback.answer()


# ---------------------------------------------------------------------
# КАРТОЧКИ С ПОЛНЫМ ДОСЬЕ И ЧИСТЫМ ИНТЕРФЕЙСОМ
# ---------------------------------------------------------------------

def format_card_back(card: Dict[str, Any], show_detail: bool = False) -> str:
    traits = card.get("traits", [])
    traits_formatted = "\n".join([f"• {t}" for t in traits]) if traits else "• Признаки уточняются"

    text = (
        f"🏷 *{card['module_title']}*\n\n"
        f"🎓 *{card['term'].upper()}*\n\n"
        f"🏛 *Родовое понятие:*\n_{card['genus']}_\n\n"
        f"📌 *Ключевые признаки:*\n{traits_formatted}"
    )

    if show_detail:
        extra_parts = []
        if card.get("concept_explanation"):
            extra_parts.append(f"💡 *Теоретическое пояснение:*\n_{card['concept_explanation']}_")

        struct = card.get("structure_for_tests", {})
        if struct.get("types_or_forms"):
            types_str = "\n".join([f"• {item}" for item in struct["types_or_forms"]])
            extra_parts.append(f"🧩 *Формы и классификации:*\n{types_str}")

        if struct.get("functions_or_goals"):
            funcs_str = "\n".join([f"• {item}" for item in struct["functions_or_goals"]])
            extra_parts.append(f"⚙️ *Функции в обществе:*\n{funcs_str}")

        ege = card.get("ege_context_and_examples", {})
        if ege.get("illustration_rf"):
            extra_parts.append(f"🇷🇺 *Пример из реалий РФ:*\n_{ege['illustration_rf']}_")

        pitfalls = card.get("common_pitfalls", [])
        if pitfalls:
            pitfalls_str = "\n".join([f"• {item}" for item in pitfalls])
            extra_parts.append(f"⚠️ *Ловушки и частые ошибки:*\n{pitfalls_str}")

        if extra_parts:
            text += "\n\n" + "\n\n".join(extra_parts)

    return text


async def send_next_flashcard(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    module_filter = data.get("module_filter")
    user_id = callback.from_user.id

    card = select_next_card(user_id=user_id, module_filter=module_filter)
    if not card:
        empty_text = "❌ В выбранном модуле пока нет терминов. Попробуй выбрать «Все темы»."
        if callback.message:
            await callback.message.edit_text(empty_text, reply_markup=Keyboards.back_to_menu())
        return

    shown_at = time.time()
    await state.update_data(current_card_code=card["code"], shown_at=shown_at)

    text = (
        f"🏷 *{card['module_title']}*\n\n"
        f"✨ *{card['term'].upper()}*\n\n"
        f"💭 _Вспомни род и ключевые признаки понятия._"
    )

    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=Keyboards.flashcard_front(card["code"]),
            parse_mode=ParseMode.MARKDOWN
        )


@router.callback_query(F.data.startswith("card:reveal:"))
async def handle_card_reveal(callback: CallbackQuery, state: FSMContext):
    code = callback.data.split(":")[2]
    card = catalog.get_by_code(code)

    if not card:
        await callback.answer("Термин не найден.")
        return

    text = format_card_back(card, show_detail=False)

    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=Keyboards.flashcard_back(code, has_detail=False),
            parse_mode=ParseMode.MARKDOWN
        )
    await callback.answer()


@router.callback_query(F.data.startswith("card:detail:"))
async def handle_card_detail(callback: CallbackQuery, state: FSMContext):
    code = callback.data.split(":")[2]
    card = catalog.get_by_code(code)

    if not card:
        await callback.answer("Термин не найден.")
        return

    text = format_card_back(card, show_detail=True)

    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=Keyboards.flashcard_back(code, has_detail=True),
            parse_mode=ParseMode.MARKDOWN
        )
    await callback.answer("Подробный теоретический разбор открыт!")


@router.callback_query(F.data.startswith("card:ans:"))
async def handle_card_answer(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    code = parts[2]
    answer_type = parts[3]

    data = await state.get_data()
    shown_at = data.get("shown_at", time.time())
    elapsed = round(time.time() - shown_at, 1)

    is_positive = (answer_type == "know_well")
    delta_ans = SpacedRepetitionAlgorithm.calculate_answer_delta(answer_type)
    delta_tm = SpacedRepetitionAlgorithm.calculate_time_delta(elapsed, is_correct=is_positive)

    # Сохраняем с меткой 'card', username и full_name
    entry_str = f"{code};{delta_ans};{delta_tm};{elapsed};card"
    user = callback.from_user
    username = f"@{user.username}" if user.username else None
    full_name = user.full_name or (user.first_name or f"Пользователь {user.id}")
    db.save_answer_entry(user.id, username, full_name, entry_str)

    if answer_type == "know_well":
        feedback_toast = "🟢 Отлично! Закреплено."
    elif answer_type == "know_poorly":
        feedback_toast = "🟡 Повторим в ближайшее время."
    else:
        feedback_toast = "🔴 Добавлено в частый цикл."

    await callback.answer(feedback_toast, show_alert=False)
    await send_next_flashcard(callback, state)


# ---------------------------------------------------------------------
# ТЕСТЫ (СЛУЧАЙНАЯ СЕССИЯ БЕЗ ПОВТОРОВ И ЭКРАНОМ ЗАВЕРШЕНИЯ)
# ---------------------------------------------------------------------

async def send_next_test(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pool = data.get("test_pool", [])
    module_filter = data.get("module_filter")

    # Если пул терминов пуст — тест завершён!
    if not pool:
        total = data.get("test_total_in_session", 0)
        correct = data.get("test_session_correct", 0)
        errors = data.get("test_session_errors", [])
        err_count = len(errors)
        times = data.get("test_session_times", [])
        avg_t = round(sum(times) / len(times), 1) if times else 0.0
        pct = round(correct / total * 100, 1) if total > 0 else 0.0

        text = (
            f"🏆 *Тестирование завершено!*\n\n"
            f"Ты прошёл все термины темы ({total} из {total}).\n\n"
            f"🎯 *Результат:* {correct} верно / {err_count} ошибок (*{pct}%*)\n"
            f"⏱ *Среднее время:* {avg_t} сек"
        )
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=Keyboards.test_finish(has_errors=bool(errors)),
                parse_mode=ParseMode.MARKDOWN
            )
        return

    next_code = pool.pop()
    await state.update_data(test_pool=pool)

    card = catalog.get_by_code(next_code)
    if not card:
        card = catalog.all_terms[0]

    test_data = TestGenerator.generate(card, module_filter)
    shown_at = time.time()
    await state.update_data(current_test_data=test_data, test_shown_at=shown_at)

    total_num = data.get("test_total_in_session", len(pool) + 1)
    curr_num = total_num - len(pool)

    text = (
        f"🎯 *Тест: {card['module_title']}* `[{curr_num}/{total_num}]`\n\n"
        f"{test_data['question_text']}"
    )

    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=Keyboards.test_options(card["code"], test_data["options"]),
            parse_mode=ParseMode.MARKDOWN
        )


@router.callback_query(F.data.startswith("test:ans:"))
async def handle_test_answer(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    code = parts[2]
    option_idx = int(parts[3])

    data = await state.get_data()
    test_data = data.get("current_test_data")
    shown_at = data.get("test_shown_at", time.time())
    elapsed = round(time.time() - shown_at, 1)

    if not test_data or option_idx >= len(test_data["options"]):
        await callback.answer("Ошибка сессии теста или повторный клик.")
        return

    await state.update_data(current_test_data=None)

    chosen_option = test_data["options"][option_idx]
    is_correct = chosen_option["is_correct"]
    correct_term = test_data["correct_term"]

    correct_count = data.get("test_session_correct", 0)
    session_errors = data.get("test_session_errors", [])
    session_times = data.get("test_session_times", [])

    if is_correct:
        correct_count += 1
    else:
        session_errors.append({
            "term": correct_term["term"],
            "genus": correct_term["genus"],
            "traits": correct_term.get("traits", []),
            "explanation": test_data.get("explanation_note", "") or f"Правильный ответ: {correct_term['term']}"
        })

    session_times.append(elapsed)

    await state.update_data(
        test_session_correct=correct_count,
        test_session_errors=session_errors,
        test_session_times=session_times
    )

    # Защита от мискликов: если ответ быстрее 3 секунд, в базу НЕ пишем!
    if elapsed >= 3.0:
        answer_type = "correct" if is_correct else "incorrect"
        delta_ans = SpacedRepetitionAlgorithm.calculate_answer_delta(answer_type)
        delta_tm = SpacedRepetitionAlgorithm.calculate_time_delta(elapsed, is_correct=is_correct)
        entry_str = f"{code};{delta_ans};{delta_tm};{elapsed};test"

        user = callback.from_user
        username = f"@{user.username}" if user.username else None
        full_name = user.full_name or (user.first_name or f"Пользователь {user.id}")
        db.save_answer_entry(user.id, username, full_name, entry_str)

    if is_correct:
        status_text = "🎉 *Верно!* Отличная работа."
    else:
        if test_data.get("explanation_note"):
            status_text = f"❌ *Неверно.*\n💡 {test_data['explanation_note']}"
        else:
            status_text = f"❌ *Неверно.* Правильный термин: *{correct_term['term']}*."

    traits = correct_term.get("traits", [])
    traits_hint = f"• {traits[0]}" if traits else f"• {correct_term['genus']}"
    text = (
        f"{status_text}\n\n"
        f"🎓 *{correct_term['term'].upper()}*\n"
        f"🏛 _{correct_term['genus']}_\n"
        f"📌 {traits_hint}"
    )

    if elapsed < 3.0:
        text += "\n\n⚡️ _Ответ < 3 сек (мисклик) — не учтён в общей статистике._"

    if callback.message:
        await callback.message.edit_text(text, reply_markup=Keyboards.test_next(), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data == "test:next")
async def handle_test_next(callback: CallbackQuery, state: FSMContext):
    await send_next_test(callback, state)
    await callback.answer()


@router.callback_query(F.data == "test:show_errors")
async def handle_test_show_errors(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    errors = data.get("test_session_errors", [])
    if not errors:
        await callback.answer("🎉 Ошибок нет — 100% результат!", show_alert=True)
        return

    lines = ["📝 *Разбор твоих ошибок в этом тесте:*\n"]
    for idx, err in enumerate(errors[:8], start=1):
        lines.append(f"*{idx}. {err['term'].upper()}*")
        lines.append(f"🏛 Род: _{err['genus']}_")
        if err.get("traits"):
            lines.append(f"📌 Признак: _{err['traits'][0]}_")
        lines.append("")

    if len(errors) > 8:
        lines.append(f"_...и ещё {len(errors) - 8} ошибок._")

    text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Начать заново", callback_data="test:restart")],
        [InlineKeyboardButton(text="🚪 В главное меню", callback_data="back_to_main")],
    ])
    if callback.message:
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data == "test:restart")
async def handle_test_restart(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    module_filter = data.get("module_filter")
    available = catalog.get_terms(module_filter)
    pool = [t["code"] for t in available]
    random.shuffle(pool)

    await state.update_data(
        test_pool=pool,
        test_total_in_session=len(pool),
        test_session_correct=0,
        test_session_errors=[],
        test_session_times=[]
    )
    await send_next_test(callback, state)
    await callback.answer()


# =====================================================================
# 9. ТОЧКА ВХОДА (С ПОДДЕРЖКОЙ RENDER FREE WEB SERVICE)
# =====================================================================

async def start_dummy_web_server(port: int = 10000):
    """
    Легковесный фоновый веб-сервер на чистом asyncio.
    Отвечает 200 OK на проверки работоспособности Render,
    чтобы бот мог работать на бесплатном тарифе Web Service ($0/мес).
    """
    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await reader.read(1024)
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"Content-Length: 2\r\n"
                b"Connection: close\r\n\r\nOK"
            )
            writer.write(response)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    server = await asyncio.start_server(handle_client, "0.0.0.0", port)
    logger.info(f"Фоновый веб-сервер успешно запущен на порту {port} для Render.")
    return server


async def main():
    if not BOT_TOKEN:
        logger.error("Токен бота не найден в переменной BOT_TOKEN!")
        print("\n[!] ОШИБКА: Задай переменную окружения BOT_TOKEN в файле .env или в переменных среды.\n")
        return

    catalog.load()
    db.init_db()

    # Запускаем мини веб-сервер для бесплатного тарифа Render Web Service ($0)
    port = int(os.getenv("PORT", "10000"))
    try:
        await start_dummy_web_server(port)
    except Exception as e:
        logger.warning(f"Не удалось запустить фоновый веб-сервер на порту {port}: {e}")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот версии 5.0 успешно запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
