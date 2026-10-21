"""
Telegram Bot для изучения терминов обществознания (Версия 5.2 — Стабильная экосистема)
=============================================================================
Архитектура и возможности:
1. Термины загружаются из terms.json (при его отсутствии — резервная автозагрузка из social_bot.db).
2. Полное теоретическое досье по кнопке «💡 Пояснение»:
   - Развёрнутое эссе (concept_explanation)
   - Формы и классификации (types_or_forms)
   - Функции в обществе (functions_or_goals)
   - Примеры из реалий РФ (illustration_rf)
   - Ловушки и типичные ошибки на экзамене (common_pitfalls)
   - Безопасная защита от превышения лимита сообщения Telegram (4096 символов) без разрыва Markdown-разметки.
3. Универсальный интерфейс:
   - Формулировки подходят как для ОГЭ (9 класс), так и для ЕГЭ (10–11 класс).
4. Алгоритм интервального повторения для КАРТОЧЕК:
   - Вероятностная выборка (Weighted Random / Roulette Selection): вес определяет шанс выпадения.
   - Трудные термины выпадают часто, новые имеют высокий приоритет, выученные периодически всплывают.
   - Кулдаун карточек: карточка не повторяется минимум 10 шагов.
   - Защита от дублирующих записей при быстром двойном клике (race condition).
   - Фиксация времени размышления в момент переворота карточки (чтение теоретической справки не штрафует вес).
5. Режим ТЕСТЫ:
   - Честный экзаменационный рандом (True Random) без повторов за сессию.
   - Защита от мискликов: ответы быстрее 3 секунд не записываются в общую базу.
   - Защита от кликов по старым вопросам в истории переписки.
   - Полные тексты длинных вариантов ответа в теле вопроса (защита от обрезки кнопок на мобильных экранах).
   - Экран завершения теста: разбор ошибок, рестарт и выход.
6. Учительская система с 8-значными кодами и мульти-привязкой:
   - Регистрация по команде /teacher с генерацией 8-значного кода и защитой от дублей.
   - Защита от самопривязки учителя в качестве ученика к самому себе.
   - Информирование ученика при недействительном коде в реферальной ссылке.
   - Автоматическая очистка неактивных учителей (раз в 24 часа в фоне).
   - Мульти-учителя: ученик может быть одновременно подключён к нескольким учителям.
   - Управление учителями: команда /teachers с кнопками быстрого отключения.
7. Персональная веб-аналитика для учеников:
   - В /menu:stats формируется безопасная ссылка с токеном на персональный кабинет.
8. Безопасность и стабильность:
   - Полное экранирование спецсимволов Markdown v1 во всех динамических полях (термины, признаки, URL, имена).
   - Безопасное редактирование сообщений (safe_edit_text) с защитой от TelegramBadRequest (message is not modified / parse error).
   - Безопасное закрытие соединений БД в блоках finally (защита от утечек пула) и режим WAL для SQLite.
   - Таймауты для сокетов Render keep-alive (3.0с) и корректное завершение фоновых задач.
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
from aiogram.exceptions import TelegramBadRequest
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


def escape_md(text: Any) -> str:
    """
    Экранирует спецсимволы Markdown v1 для безопасного вывода в Telegram.
    Сначала экранируется обратный слэш, чтобы исключить экранирование закрывающих тегов разметки.
    """
    if text is None:
        return ""
    s = str(text)
    for ch in ["\\", "_", "*", "`", "["]:
        s = s.replace(ch, f"\\{ch}")
    return s


def strip_md_delimiters(text: Any) -> str:
    """Очищает текст для безопасного размещения внутри курсива/жирного в Markdown v1."""
    return escape_md(text).strip()


def truncate_plain(text: str, max_len: int) -> str:
    """Безопасно обрезает сырой текст ДО применения Markdown-экранирования."""
    s = str(text or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max(0, max_len - 3)].rstrip() + "..."


async def safe_edit_text(
    message: Optional[Message],
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = ParseMode.MARKDOWN,
    disable_web_page_preview: Optional[bool] = None,
):
    """
    Безопасная обёртка над message.edit_text:
    - Игнорирует ошибку 'message is not modified' при повторных кликах.
    - При ошибке парсинга сущностей Markdown делает fallback на отправку без разметки,
      чтобы бот никогда не падал из-за нестандартных символов в базе.
    """
    if not message:
        return
    kwargs: Dict[str, Any] = {"reply_markup": reply_markup, "parse_mode": parse_mode}
    if disable_web_page_preview is not None:
        kwargs["disable_web_page_preview"] = disable_web_page_preview

    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        err_msg = str(e).lower()
        if "message is not modified" in err_msg:
            return
        if "can't parse entities" in err_msg:
            logger.warning(f"Ошибка разметки Markdown, выполняется fallback без parse_mode: {e}")
            kwargs["parse_mode"] = None
            try:
                await message.edit_text(text, **kwargs)
            except TelegramBadRequest as inner_e:
                if "message is not modified" not in str(inner_e).lower():
                    raise
            return
        raise


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

def _safe_json_loads(raw_val: Any, default: Any) -> Any:
    """Безопасный парсинг JSON-полей из SQLite с проверкой типа возвращаемого значения."""
    if not raw_val:
        return default
    try:
        parsed = json.loads(raw_val) if isinstance(raw_val, str) else raw_val
        if isinstance(default, list) and not isinstance(parsed, list):
            return default
        if isinstance(default, dict) and not isinstance(parsed, dict):
            return default
        return parsed if parsed is not None else default
    except Exception:
        return default


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
        for m in list(self.terms_by_module.keys()):
            self.terms_by_module[m].clear()

        # Вариант 1: Загрузка из terms.json
        if os.path.exists(self.json_path):
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and "terms" in data and isinstance(data["terms"], list):
                    data = data["terms"]
                if isinstance(data, list):
                    self._populate_terms(data)
                    if self.all_terms:
                        logger.info(f"Загружено {len(self.all_terms)} терминов из {self.json_path}.")
                        return
                logger.warning(f"Файл {self.json_path} пуст или имеет некорректный формат. Пробуем резерв из БД.")
            except Exception as e:
                logger.warning(f"Ошибка чтения {self.json_path}: {e}. Пробуем резерв из БД.")

        # Вариант 2: Резервная автозагрузка напрямую из таблицы terms в SQLite
        if os.path.exists(self.sqlite_path):
            conn = None
            cur = None
            try:
                conn = sqlite3.connect(self.sqlite_path, timeout=10.0)
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
                            "code": str(r[0] or "").strip(),
                            "module_number": int(r[1]) if r[1] and str(r[1]).isdigit() else 1,
                            "module_title": str(r[2] or "").strip(),
                            "term": str(r[3] or "").strip(),
                            "concept_explanation": str(r[4] or "").strip(),
                            "genus": str(r[5] or "").strip(),
                            "traits": _safe_json_loads(r[6], []),
                            "structure_for_tests": _safe_json_loads(r[7], {}),
                            "ege_context_and_examples": _safe_json_loads(r[8], {}),
                            "common_pitfalls": _safe_json_loads(r[9], [])
                        })

                    self._populate_terms(db_terms)
                    logger.info(f"Резервная загрузка: успешно получено {len(self.all_terms)} терминов из {self.sqlite_path}.")
                    return
            except Exception as e:
                logger.error(f"Не удалось прочитать термины из SQLite: {e}")
            finally:
                if cur is not None:
                    try:
                        cur.close()
                    except Exception:
                        pass
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        logger.error(f"Файл {self.json_path} не найден, и в базе нет таблицы terms!")

    def _populate_terms(self, data: List[Dict[str, Any]]):
        for item in data:
            if not isinstance(item, dict):
                continue
            # Очищаем код от ';' и ':' для безопасности сериализации логов и callback_data
            raw_code = str(item.get("code") or "").strip().replace(";", "_").replace(":", "_")
            code = raw_code[:24]
            if not code or code in self.terms_by_code:
                continue

            try:
                mod_num = int(code.split(".")[0])
            except (ValueError, IndexError):
                mod_num = int(item.get("module_number") or 1) if str(item.get("module_number") or "").isdigit() else 1

            raw_traits = item.get("traits")
            traits = [str(t).strip() for t in raw_traits if str(t or "").strip()] if isinstance(raw_traits, list) else []

            raw_struct = item.get("structure_for_tests")
            struct = raw_struct if isinstance(raw_struct, dict) else {}

            raw_ege = item.get("ege_context_and_examples")
            ege = raw_ege if isinstance(raw_ege, dict) else {}

            raw_pitfalls = item.get("common_pitfalls")
            pitfalls = [str(p).strip() for p in raw_pitfalls if str(p or "").strip()] if isinstance(raw_pitfalls, list) else []

            term_name = str(item.get("term") or "").strip() or f"Термин {code}"
            genus_val = str(item.get("genus") or "").strip() or "Родовое понятие не указано"
            mod_title = str(item.get("module_title") or "").strip() or f"Модуль {mod_num}"
            concept_exp = str(item.get("concept_explanation") or "").strip()

            term_obj = {
                "code": code,
                "module_number": mod_num,
                "module_title": mod_title,
                "term": term_name,
                "concept_explanation": concept_exp,
                "genus": genus_val,
                "traits": traits,
                "structure_for_tests": struct,
                "ege_context_and_examples": ege,
                "common_pitfalls": pitfalls,
            }
            self.terms_by_code[code] = term_obj
            self.all_terms.append(term_obj)
            if mod_num in self.terms_by_module:
                self.terms_by_module[mod_num].append(term_obj)
            else:
                self.terms_by_module[mod_num] = [term_obj]

    def get_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        return self.terms_by_code.get(str(code).strip())

    def get_terms(self, module_filter: Optional[int] = None) -> List[Dict[str, Any]]:
        if module_filter is not None:
            return self.terms_by_module.get(module_filter, [])
        return self.all_terms


catalog = TermsCatalog()

# =====================================================================
# 4. ХРАНИЛИЩЕ ДАННЫХ (БЕЗОПАСНАЯ РАБОТА С СОЕДИНЕНИЯМИ)
# =====================================================================

class Database:
    """
    В базе данных сохраняются:
    1. Ответы учеников: user_id, username, full_name, строка entry.
    2. Таблица учителей: teacher_code (8-значное число), user_id, full_name, created_at.
    3. Связующая таблица student_teachers (student_id, teacher_code, joined_at).
    Все методы обёрнуты в try-finally для гарантии закрытия курсоров и соединений.
    """
    def __init__(self, db_url: Optional[str] = DATABASE_URL, sqlite_path: str = DB_PATH):
        self.db_url = db_url
        self.sqlite_path = sqlite_path
        self.is_postgres = bool(db_url and db_url.startswith("postgres"))

    def get_connection(self):
        if self.is_postgres:
            import psycopg2
            return psycopg2.connect(self.db_url, connect_timeout=10)
        else:
            conn = sqlite3.connect(self.sqlite_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA busy_timeout=5000;")
            except Exception:
                pass
            return conn

    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
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
        finally:
            cursor.close()
            conn.close()

        # Запускаем автоочистку учителей без учеников старше 7 дней
        self.cleanup_inactive_teachers()

        mode_str = "Neon PostgreSQL" if self.is_postgres else f"SQLite ({self.sqlite_path})"
        logger.info(f"База данных успешно инициализирована в режиме: {mode_str}.")

    def save_answer_entry(self, user_id: int, username: Optional[str], full_name: Optional[str], entry_str: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
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
        finally:
            cursor.close()
            conn.close()

    def _fetch_user_entries(self, user_id: int) -> List[str]:
        """Возвращает упорядоченный по возрастанию id список ответов ученика за один запрос."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute("SELECT entry FROM user_answers WHERE user_id = %s ORDER BY id ASC;", (user_id,))
            else:
                cursor.execute("SELECT entry FROM user_answers WHERE user_id = ? ORDER BY id ASC;", (user_id,))
            rows = cursor.fetchall()
        finally:
            cursor.close()
            conn.close()
        return [str(r[0] if self.is_postgres else r["entry"]) for r in rows]

    @staticmethod
    def _compute_progress_from_entries(entries: List[str]) -> Dict[str, Dict[str, Any]]:
        progress: Dict[str, Dict[str, Any]] = {}
        for entry_str in entries:
            parts = entry_str.split(";")
            if len(parts) >= 4:
                mode = parts[4].strip() if len(parts) >= 5 else "card"
                if mode == "test":
                    continue

                code = parts[0].strip()
                if not code:
                    continue
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

    def get_user_progress_map(self, user_id: int) -> Dict[str, Dict[str, Any]]:
        """
        Вычисляет интервальные веса карточек ТОЛЬКО по режиму карточек (mode != 'test'),
        чтобы результаты экзаменационных тестов не сбивали обучение в карточках.
        """
        entries = self._fetch_user_entries(user_id)
        return self._compute_progress_from_entries(entries)

    def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        """Возвращает раздельные метрики для карточек и тестов за один запрос к БД."""
        entries = self._fetch_user_entries(user_id)
        progress_map = self._compute_progress_from_entries(entries)

        cards_count = 0
        tests_count = 0
        tests_correct = 0
        tests_errors = 0
        test_times = []

        for entry_str in entries:
            parts = entry_str.split(";")
            if len(parts) >= 4:
                try:
                    d_ans = float(parts[1])
                    sec = float(parts[3])
                except ValueError:
                    continue

                mode = parts[4].strip() if len(parts) >= 5 else "card"
                if mode == "test":
                    if sec >= SpacedRepetitionAlgorithm.MIN_TIME_WINDOW:
                        tests_count += 1
                        if d_ans <= 0:
                            tests_correct += 1
                        else:
                            tests_errors += 1
                        if SpacedRepetitionAlgorithm.MIN_TIME_WINDOW <= sec <= SpacedRepetitionAlgorithm.MAX_TIME_WINDOW:
                            test_times.append(sec)
                else:
                    cards_count += 1

        tests_accuracy = round(tests_correct / tests_count * 100, 1) if tests_count > 0 else 0.0
        tests_avg_time = round(sum(test_times) / len(test_times), 1) if test_times else 0.0

        # Вес >= 7.0 (например, после нажатия «Забыл» на новой карточке: 5.0 + 2.0 = 7.0)
        # сразу попадает в категорию «Требуют повторения».
        learned = sum(1 for p in progress_map.values() if p["total_weight"] <= 3.0)
        in_progress = sum(1 for p in progress_map.values() if 3.0 < p["total_weight"] < 7.0)
        needs_review = sum(1 for p in progress_map.values() if p["total_weight"] >= 7.0)

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
            try:
                if self.is_postgres:
                    cursor.execute("""
                        DELETE FROM teachers t
                        WHERE t.created_at < CURRENT_TIMESTAMP - INTERVAL '7 days'
                          AND NOT EXISTS (
                              SELECT 1 FROM student_teachers st WHERE st.teacher_code = t.teacher_code
                          );
                    """)
                else:
                    cursor.execute("""
                        DELETE FROM teachers
                        WHERE created_at < datetime('now', '-7 days')
                          AND NOT EXISTS (
                              SELECT 1 FROM student_teachers st WHERE st.teacher_code = teachers.teacher_code
                          );
                    """)
                conn.commit()
            finally:
                cursor.close()
                conn.close()
        except Exception as e:
            logger.warning(f"Ошибка при очистке неактивных учителей: {e}")

    def get_teacher(self, teacher_code: str) -> Optional[Dict[str, Any]]:
        clean_code = str(teacher_code or "").strip()
        if not clean_code:
            return None
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute("SELECT teacher_code, user_id, full_name, created_at FROM teachers WHERE teacher_code = %s;", (clean_code,))
            else:
                cursor.execute("SELECT teacher_code, user_id, full_name, created_at FROM teachers WHERE teacher_code = ?;", (clean_code,))
            row = cursor.fetchone()
        finally:
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
        try:
            if self.is_postgres:
                cursor.execute("SELECT teacher_code, user_id, full_name, created_at FROM teachers WHERE user_id = %s;", (user_id,))
            else:
                cursor.execute("SELECT teacher_code, user_id, full_name, created_at FROM teachers WHERE user_id = ?;", (user_id,))
            row = cursor.fetchone()
        finally:
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
        """
        Генерирует уникальный случайный 8-значный код и регистрирует преподавателя.
        Защищено от дублирования при параллельных запросах от одного user_id.
        """
        existing = self.get_teacher_by_user_id(user_id)
        if existing:
            return existing["teacher_code"]

        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            # Повторная проверка внутри транзакции
            if self.is_postgres:
                cursor.execute("SELECT teacher_code FROM teachers WHERE user_id = %s;", (user_id,))
            else:
                cursor.execute("SELECT teacher_code FROM teachers WHERE user_id = ?;", (user_id,))
            row = cursor.fetchone()
            if row:
                return str(row[0])

            code = ""
            for _ in range(1000):
                candidate = str(random.randint(10000000, 99999999))
                if self.is_postgres:
                    cursor.execute("SELECT 1 FROM teachers WHERE teacher_code = %s;", (candidate,))
                else:
                    cursor.execute("SELECT 1 FROM teachers WHERE teacher_code = ?;", (candidate,))
                if not cursor.fetchone():
                    code = candidate
                    break

            if not code:
                raise RuntimeError("Не удалось сгенерировать уникальный 8-значный код учителя.")

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
        finally:
            cursor.close()
            conn.close()

        logger.info(f"Зарегистрирован новый преподаватель {full_name} с кодом {code}.")
        return code

    def is_student_connected(self, student_id: int, teacher_code: str) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute("SELECT 1 FROM student_teachers WHERE student_id = %s AND teacher_code = %s;", (student_id, str(teacher_code)))
            else:
                cursor.execute("SELECT 1 FROM student_teachers WHERE student_id = ? AND teacher_code = ?;", (student_id, str(teacher_code)))
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()

        return bool(row)

    def connect_student_to_teacher(self, student_id: int, teacher_code: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute("""
                    INSERT INTO student_teachers (student_id, teacher_code) 
                    VALUES (%s, %s) 
                    ON CONFLICT (student_id, teacher_code) DO NOTHING;
                """, (student_id, str(teacher_code)))
            else:
                cursor.execute("""
                    INSERT OR IGNORE INTO student_teachers (student_id, teacher_code) 
                    VALUES (?, ?);
                """, (student_id, str(teacher_code)))
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def disconnect_student_from_teacher(self, student_id: int, teacher_code: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute("DELETE FROM student_teachers WHERE student_id = %s AND teacher_code = %s;", (student_id, str(teacher_code)))
            else:
                cursor.execute("DELETE FROM student_teachers WHERE student_id = ? AND teacher_code = ?;", (student_id, str(teacher_code)))
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def get_student_teachers(self, student_id: int) -> List[Dict[str, Any]]:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
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
        finally:
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
        try:
            if self.is_postgres:
                cursor.execute("SELECT COUNT(*) FROM student_teachers WHERE teacher_code = %s;", (str(teacher_code),))
            else:
                cursor.execute("SELECT COUNT(*) FROM student_teachers WHERE teacher_code = ?;", (str(teacher_code),))
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()

        return int(row[0]) if row else 0


db = Database()

# =====================================================================
# 5. СЕССИОННЫЙ МЕНЕДЖЕР И ВЫБОРКА КАРТОЧЕК
# =====================================================================

class SessionTracker:
    """Отслеживает последние 10 показанных карточек для исключения повторений подряд."""
    def __init__(self, max_users: int = 10000):
        self.recent_history: Dict[int, deque] = {}
        self.max_users = max_users

    def push(self, user_id: int, code: str):
        if user_id not in self.recent_history:
            if len(self.recent_history) >= self.max_users:
                # Защита от неограниченного роста памяти при долгом аптайме
                oldest_uid = next(iter(self.recent_history))
                self.recent_history.pop(oldest_uid, None)
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
        # Если все доступные термины уже в кулдауне (например, в модуле < 10 терминов),
        # исключаем хотя бы последнюю показанную карточку, если терминов больше одного.
        last_shown = session_tracker.get_recent(user_id)[-1] if session_tracker.get_recent(user_id) else None
        if len(available_terms) > 1 and last_shown is not None:
            candidates = [t for t in available_terms if t["code"] != last_shown]
        if not candidates:
            candidates = available_terms

    def get_effective_weight(term_dict: Dict[str, Any]) -> float:
        code = term_dict["code"]
        if code not in progress_map or progress_map[code]["review_count"] == 0:
            return SpacedRepetitionAlgorithm.UNREVIEWED_PRIORITY_WEIGHT  # 6.5
        return max(SpacedRepetitionAlgorithm.MIN_WEIGHT, float(progress_map[code]["total_weight"]))

    weights = [get_effective_weight(t) for t in candidates]

    chosen = random.choices(candidates, weights=weights, k=1)[0]
    session_tracker.push(user_id, chosen["code"])
    return chosen


# =====================================================================
# 6. ГЕНЕРАТОР ТЕСТОВ (БЕЗ ДУБЛИРОВАНИЯ И С ЗАЩИТОЙ МАЛЫХ ВЫБОРОК)
# =====================================================================

class TestGenerator:
    @staticmethod
    def _pick_unique_distractors(distractors: List[Dict[str, Any]], count: int, correct_term: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Возвращает ровно `count` дистракторов без падения ValueError даже при малой базе терминов.
        """
        seen_terms = {str(correct_term.get("term", "")).strip().lower()}
        unique_pool = []
        shuffled = random.sample(distractors, len(distractors)) if distractors else []
        for d in shuffled:
            t_name = str(d.get("term", "")).strip().lower()
            if t_name and t_name not in seen_terms:
                seen_terms.add(t_name)
                unique_pool.append(d)
            if len(unique_pool) >= count:
                return unique_pool

        # Если уникальных по названию меньше count, добираем из оставшихся дистракторов без повторов по id
        for d in shuffled:
            if d not in unique_pool:
                unique_pool.append(d)
            if len(unique_pool) >= count:
                return unique_pool

        # Крайний случай: если в базе всего 1-3 термина
        fallback_idx = 1
        while len(unique_pool) < count:
            unique_pool.append({
                "code": f"fallback_{fallback_idx}",
                "term": f"Вариант {fallback_idx}",
                "genus": f"Альтернативная родовая характеристика {fallback_idx}",
                "traits": [f"Альтернативный признак {fallback_idx}"],
            })
            fallback_idx += 1
        return unique_pool

    @staticmethod
    def generate(correct_term: Dict[str, Any], module_filter: Optional[int] = None) -> Dict[str, Any]:
        all_pool = catalog.get_terms(module_filter)
        if len(all_pool) < 4:
            all_pool = catalog.get_terms(None)

        distractors = [t for t in all_pool if t["code"] != correct_term["code"]]
        if len(distractors) < 3:
            distractors = [t for t in catalog.all_terms if t["code"] != correct_term["code"]]

        real_traits = [str(t).strip() for t in (correct_term.get("traits") or []) if str(t).strip()]
        real_traits_lower = {t.lower() for t in real_traits}

        # Проверяем наличие подходящего чужого признака для вопроса типа 3 («Найди лишнее»)
        foreign_candidates: List[Tuple[Dict[str, Any], str]] = []
        for d in distractors:
            d_traits = [str(t).strip() for t in (d.get("traits") or []) if str(t).strip()]
            for dt in d_traits:
                if dt.lower() not in real_traits_lower:
                    foreign_candidates.append((d, dt))

        available_types = [1, 2]
        if len(real_traits) >= 3 and foreign_candidates:
            available_types.append(3)

        struct = correct_term.get("structure_for_tests") or {}
        types_or_forms = [str(x).strip() for x in (struct.get("types_or_forms") or []) if str(x).strip()]
        functions_or_goals = [str(x).strip() for x in (struct.get("functions_or_goals") or []) if str(x).strip()]

        if types_or_forms:
            available_types.append(4)
        if functions_or_goals:
            available_types.append(5)

        chosen_type = random.choice(available_types)
        explanation_note = ""

        # ТИП 1: По ключевым признакам угадать термин
        if chosen_type == 1:
            traits_str = (
                "\n".join([f"• {escape_md(t)}" for t in real_traits[:3]])
                if real_traits
                else f"• {escape_md(correct_term.get('genus') or 'Признаки понятия')}"
            )
            genus_escaped = strip_md_delimiters(correct_term.get("genus") or "Общее понятие")
            question_text = (
                f"К какому понятию относятся следующие ключевые признаки:\n\n"
                f"{traits_str}\n\n"
                f"Родовое понятие: _{genus_escaped}_"
            )
            picked = TestGenerator._pick_unique_distractors(distractors, 3, correct_term)
            options = [
                {"id": "A", "code": correct_term["code"], "text": correct_term["term"], "is_correct": True},
                {"id": "B", "code": picked[0]["code"], "text": picked[0]["term"], "is_correct": False},
                {"id": "C", "code": picked[1]["code"], "text": picked[1]["term"], "is_correct": False},
                {"id": "D", "code": picked[2]["code"], "text": picked[2]["term"], "is_correct": False},
            ]

        # ТИП 2: По термину выбрать родовую характеристику (уникальные genus без повторов)
        elif chosen_type == 2:
            term_escaped = strip_md_delimiters(correct_term["term"].upper())
            question_text = f"Выбери верную родовую характеристику термина *{term_escaped}*:"

            target_genus = str(correct_term.get("genus") or "Родовое понятие").strip()
            seen_genera = {target_genus.lower()}
            valid_distractors = []

            pool_for_genus = catalog.all_terms if len(catalog.all_terms) >= 4 else distractors
            shuffled_pool = random.sample(pool_for_genus, len(pool_for_genus)) if pool_for_genus else []
            for d in shuffled_pool:
                if d.get("code") == correct_term.get("code"):
                    continue
                g = str(d.get("genus") or "").strip()
                if g and g.lower() not in seen_genera:
                    seen_genera.add(g.lower())
                    valid_distractors.append(d)
                if len(valid_distractors) == 3:
                    break

            if len(valid_distractors) < 3:
                fallback_picks = TestGenerator._pick_unique_distractors(distractors, 3, correct_term)
                for fp in fallback_picks:
                    if fp not in valid_distractors:
                        valid_distractors.append(fp)
                    if len(valid_distractors) == 3:
                        break

            options = [
                {"id": "A", "code": correct_term["code"], "text": target_genus, "is_correct": True},
                {"id": "B", "code": valid_distractors[0]["code"], "text": valid_distractors[0].get("genus") or "Вариант 1", "is_correct": False},
                {"id": "C", "code": valid_distractors[1]["code"], "text": valid_distractors[1].get("genus") or "Вариант 2", "is_correct": False},
                {"id": "D", "code": valid_distractors[2]["code"], "text": valid_distractors[2].get("genus") or "Вариант 3", "is_correct": False},
            ]

        # ТИП 3: «Найди лишнее» (чужой признак)
        elif chosen_type == 3:
            term_escaped = strip_md_delimiters(correct_term["term"].upper())
            question_text = (
                f"Все перечисленные ниже признаки, за исключением одного, относятся к понятию *{term_escaped}*.\n\n"
                f"Найди выпадающий (чужой) признак:"
            )
            sample_real = random.sample(real_traits, 3)
            foreign_term, foreign_trait = random.choice(foreign_candidates)
            explanation_note = f"Лишний признак: «{foreign_trait}» (относится к понятию «{foreign_term['term']}»)."

            options = [
                {"id": "A", "code": correct_term["code"], "text": foreign_trait, "is_correct": True},
                {"id": "B", "code": correct_term["code"], "text": sample_real[0], "is_correct": False},
                {"id": "C", "code": correct_term["code"], "text": sample_real[1], "is_correct": False},
                {"id": "D", "code": correct_term["code"], "text": sample_real[2], "is_correct": False},
            ]

        # ТИП 4: Классификации и формы
        elif chosen_type == 4:
            types_str = strip_md_delimiters(", ".join(types_or_forms[:3]))
            question_text = f"Для какого из терминов характерны следующие формы/виды: _{types_str}_?"
            picked = TestGenerator._pick_unique_distractors(distractors, 3, correct_term)
            options = [
                {"id": "A", "code": correct_term["code"], "text": correct_term["term"], "is_correct": True},
                {"id": "B", "code": picked[0]["code"], "text": picked[0]["term"], "is_correct": False},
                {"id": "C", "code": picked[1]["code"], "text": picked[1]["term"], "is_correct": False},
                {"id": "D", "code": picked[2]["code"], "text": picked[2]["term"], "is_correct": False},
            ]

        # ТИП 5: Функции в обществе
        else:
            funcs_str = strip_md_delimiters(", ".join(functions_or_goals[:3]))
            question_text = f"Какое понятие выполняет в обществе следующие функции: _{funcs_str}_?"
            picked = TestGenerator._pick_unique_distractors(distractors, 3, correct_term)
            options = [
                {"id": "A", "code": correct_term["code"], "text": correct_term["term"], "is_correct": True},
                {"id": "B", "code": picked[0]["code"], "text": picked[0]["term"], "is_correct": False},
                {"id": "C", "code": picked[1]["code"], "text": picked[1]["term"], "is_correct": False},
                {"id": "D", "code": picked[2]["code"], "text": picked[2]["term"], "is_correct": False},
            ]

        random.shuffle(options)

        # Если хотя бы один вариант ответа длинный (> 35 символов), дублируем полный список
        # вариантов в текст вопроса, чтобы на узких экранах смартфонов текст не обрезался кнопками.
        if any(len(str(opt.get("text", ""))) > 35 for opt in options):
            option_lines = []
            for idx, opt in enumerate(options, start=1):
                option_lines.append(f"*{idx})* {escape_md(opt['text'])}")
            question_text += "\n\n" + "\n".join(option_lines)

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
        has_long = any(len(str(opt.get("text", ""))) > 35 for opt in options)
        buttons = []
        for idx, opt in enumerate(options):
            raw_text = str(opt.get("text", ""))
            btn_label = f"👉 {idx + 1}) {truncate_plain(raw_text, 45)}" if has_long else f"👉 {raw_text}"
            buttons.append([
                InlineKeyboardButton(
                    text=btn_label,
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
    if not message.from_user:
        return
    await state.clear()

    # Проверяем deep link реферального подключения к учителю (?start=t_83910245)
    connect_notice = ""
    parts = message.text.split(maxsplit=1) if message.text else []
    if len(parts) > 1 and parts[1].strip().startswith("t_"):
        teacher_code = parts[1].strip()[2:].strip()
        teacher = db.get_teacher(teacher_code)
        if teacher:
            if teacher["user_id"] == message.from_user.id:
                connect_notice = "⚠️ *Ты не можешь прикрепиться к своему собственному классу в качестве ученика.*\n\n"
            elif not db.is_student_connected(message.from_user.id, teacher_code):
                db.connect_student_to_teacher(message.from_user.id, teacher_code)
                connect_notice = (
                    f"✅ *Ты успешно прикреплён к преподавателю:*\n"
                    f"*{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(teacher_code)}`).\n"
                    f"Твой прогресс теперь отображается в его ведомости.\n\n"
                )
            else:
                connect_notice = (
                    f"ℹ️ *Ты уже прикреплён к преподавателю:*\n"
                    f"*{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(teacher_code)}`).\n\n"
                )
        else:
            connect_notice = (
                f"❌ *Преподаватель по ссылке-коду* `{escape_md(teacher_code)}` *не найден.*\n"
                f"Проверь ссылку или уточни код у своего преподавателя.\n\n"
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
    if not message.from_user:
        return
    user_id = message.from_user.id
    existing = db.get_teacher_by_user_id(user_id)

    bot_info = await message.bot.get_me()
    bot_username = bot_info.username or "obshaga_bot"

    if existing:
        code = existing["teacher_code"]
        st_count = db.get_teacher_students_count(code)
        invite_url = escape_md(f"https://t.me/{bot_username}?start=t_{code}")
        dash_url = escape_md(f"{DASHBOARD_URL}/?teacher={code}")
        text = (
            f"👨‍🏫 *Кабинет преподавателя*\n\n"
            f"👤 Преподаватель: *{strip_md_delimiters(existing['full_name'])}*\n"
            f"🔑 Личный код класса: `{escape_md(code)}`\n"
            f"👥 Подключено учеников: *{st_count}*\n\n"
            f"🔗 *Ссылка для учеников (отправь в чат класса):*\n"
            f"{invite_url}\n\n"
            f"📊 *Твоя онлайн-аналитика класса:*\n"
            f"{dash_url}\n\n"
            f"💡 _Ученики могут перейти по ссылке выше или просто отправить в чат бота твой 8-значный код_ `{escape_md(code)}`."
        )
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return

    # Если еще не зарегистрирован как учитель:
    user = message.from_user
    full_name = user.full_name or (f"@{user.username}" if user.username else f"Преподаватель {user.id}")
    code = db.register_teacher(user_id, full_name)
    invite_url = escape_md(f"https://t.me/{bot_username}?start=t_{code}")
    dash_url = escape_md(f"{DASHBOARD_URL}/?teacher={code}")

    text = (
        f"🎉 *Вы успешно зарегистрированы как преподаватель!*\n\n"
        f"👤 Имя: *{strip_md_delimiters(full_name)}*\n"
        f"🔑 Ваш личный 8-значный код: `{escape_md(code)}`\n\n"
        f"👥 *Ссылка для учеников (отправьте её ребятам):*\n"
        f"{invite_url}\n\n"
        f"📊 *Ваша онлайн-аналитика класса:*\n"
        f"{dash_url}\n\n"
        f"💡 _Ученики могут перейти по ссылке или просто отправить в чат бота ваш код_ `{escape_md(code)}`.\n"
        f"⏳ _Если за 7 дней к вам не подключится ни один ученик, профиль учителя автоматически аннулируется для чистоты базы._"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


@router.message(Command("teachers"))
async def handle_teachers_list(message: Message):
    if not message.from_user:
        return
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
        lines.append(f"{idx}. *{strip_md_delimiters(t['full_name'])}* (код `{escape_md(t['teacher_code'])}`)")
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ Отключить: {t['full_name'][:25]}",
                callback_data=f"teacher:unlink:{t['teacher_code']}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")])

    text = "\n".join(lines) + "\n\n_Нажми кнопку ниже, если хочешь отключиться от преподавателя:_"
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode=ParseMode.MARKDOWN)


@router.message(F.text.regexp(r"^\s*\d{8}\s*$"))
async def handle_teacher_code_input(message: Message):
    if not message.from_user or not message.text:
        return
    code = message.text.strip()
    teacher = db.get_teacher(code)
    if not teacher:
        await message.answer(
            f"❌ Преподаватель с кодом `{escape_md(code)}` не найден.\n"
            f"Проверь правильность ввода 8 цифр или уточни код у своего преподавателя.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if teacher["user_id"] == message.from_user.id:
        await message.answer(
            "❌ Ты не можешь прикрепиться к своему собственному классу в качестве ученика.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if db.is_student_connected(message.from_user.id, code):
        await message.answer(
            f"ℹ️ Ты уже подключён к преподавателю *{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(code)}`).",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    db.connect_student_to_teacher(message.from_user.id, code)
    await message.answer(
        f"✅ *Преподаватель найден:*\n"
        f"*{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(code)}`).\n\n"
        f"Ты успешно подключён! Твой прогресс теперь отображается в его ведомости.",
        parse_mode=ParseMode.MARKDOWN
    )


@router.callback_query(F.data.startswith("teacher:unlink:"))
async def handle_unlink_teacher(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Некорректный код преподавателя.", show_alert=True)
        return
    code = parts[2]
    teacher = db.get_teacher(code)
    t_name = teacher["full_name"] if teacher else f"Код {code}"
    db.disconnect_student_from_teacher(callback.from_user.id, code)
    await callback.answer(f"Отключено от {t_name}", show_alert=True)

    teachers = db.get_student_teachers(callback.from_user.id)
    if not teachers:
        await safe_edit_text(
            callback.message,
            "ℹ️ Ты отключился от всех преподавателей.\n\n"
            "Чтобы подключиться снова, просто отправь 8-значный код преподавателя.",
            reply_markup=Keyboards.back_to_menu(),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    buttons = []
    lines = ["👨‍🏫 *Твои преподаватели:*\n"]
    for idx, t in enumerate(teachers, 1):
        lines.append(f"{idx}. *{strip_md_delimiters(t['full_name'])}* (код `{escape_md(t['teacher_code'])}`)")
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ Отключить: {t['full_name'][:25]}",
                callback_data=f"teacher:unlink:{t['teacher_code']}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")])
    text = "\n".join(lines) + "\n\n_Нажми кнопку ниже, если хочешь отключиться от преподавателя:_"
    await safe_edit_text(
        callback.message,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.MARKDOWN
    )


@router.callback_query(F.data == "back_to_main")
async def handle_back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text = "🏠 *Главное меню*\n\nВыбери формат тренировки:"
    await safe_edit_text(callback.message, text, reply_markup=Keyboards.main_menu(), parse_mode=ParseMode.MARKDOWN)
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
    await safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
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
        lines.append(f"{idx}. *{strip_md_delimiters(t['full_name'])}* (код `{escape_md(t['teacher_code'])}`)")
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ Отключить: {t['full_name'][:25]}",
                callback_data=f"teacher:unlink:{t['teacher_code']}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="⬅️ В статистику", callback_data="menu:stats")])
    text = "\n".join(lines) + "\n\n_Нажми кнопку ниже, чтобы отключить преподавателя:_"
    await safe_edit_text(
        callback.message,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.MARKDOWN
    )
    await callback.answer()


@router.callback_query(F.data.in_({"menu:cards", "menu:tests"}))
async def handle_menu_selection(callback: CallbackQuery, state: FSMContext):
    mode = (callback.data or "").split(":")[1]
    await state.update_data(mode=mode)
    await state.set_state(UserFlow.selecting_topic)

    mode_name = "🗂 Карточки" if mode == "cards" else "🎯 Тесты"
    text = (
        f"Настройка режима *{mode_name}*:\n\n"
        "Выбери, какие термины тренировать:\n"
        "• *Все темы* — перемешиваются все понятия кодификатора;\n"
        "• *Отдельный модуль* — только термины выбранного блока (1..5)."
    )
    await safe_edit_text(callback.message, text, reply_markup=Keyboards.topic_selection(mode), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data.startswith("topic:"))
async def handle_topic_selection(callback: CallbackQuery, state: FSMContext):
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Некорректный выбор темы.", show_alert=True)
        return
    mode = parts[1]
    topic_val = parts[2]

    if topic_val == "all":
        module_filter = None
    elif topic_val.isdigit():
        module_filter = int(topic_val)
    else:
        await callback.answer("Неизвестный модуль.", show_alert=True)
        return

    await state.update_data(module_filter=module_filter, mode=mode)

    if mode == "cards":
        await state.set_state(UserFlow.in_flashcards)
        await send_next_flashcard(callback, state)
    else:
        await state.set_state(UserFlow.in_tests)
        available = catalog.get_terms(module_filter)
        if not available:
            empty_text = "❌ В выбранном модуле пока недостаточно терминов."
            await safe_edit_text(callback.message, empty_text, reply_markup=Keyboards.back_to_menu(), parse_mode=None)
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
    """
    Формирует оборотную сторону карточки с полным экранированием спецсимволов Markdown v1
    и безопасным контролем длины (< 4000 символов) без разрыва открытых сущностей разметки.
    """
    traits = card.get("traits") or []
    traits_formatted = (
        "\n".join([f"• {escape_md(truncate_plain(t, 300))}" for t in traits[:10] if str(t).strip()])
        if traits
        else "• Признаки уточняются"
    )

    mod_title = strip_md_delimiters(truncate_plain(card.get("module_title") or "Обществознание", 120))
    term_title = strip_md_delimiters(truncate_plain(str(card.get("term") or "").upper(), 150))
    genus_text = strip_md_delimiters(truncate_plain(card.get("genus") or "Родовое понятие не указано", 400))

    base_text = (
        f"🏷 *{mod_title}*\n\n"
        f"🎓 *{term_title}*\n\n"
        f"🏛 *Родовое понятие:*\n_{genus_text}_\n\n"
        f"📌 *Ключевые признаки:*\n{traits_formatted}"
    )

    if not show_detail:
        return base_text

    extra_blocks: List[str] = []

    concept_raw = str(card.get("concept_explanation") or "").strip()
    if concept_raw:
        concept_safe = strip_md_delimiters(truncate_plain(concept_raw, 900))
        extra_blocks.append(f"💡 *Теоретическое пояснение:*\n_{concept_safe}_")

    struct = card.get("structure_for_tests") or {}
    types_list = [str(x).strip() for x in (struct.get("types_or_forms") or []) if str(x).strip()]
    if types_list:
        types_str = "\n".join([f"• {escape_md(truncate_plain(item, 220))}" for item in types_list[:8]])
        extra_blocks.append(f"🧩 *Формы и классификации:*\n{types_str}")

    funcs_list = [str(x).strip() for x in (struct.get("functions_or_goals") or []) if str(x).strip()]
    if funcs_list:
        funcs_str = "\n".join([f"• {escape_md(truncate_plain(item, 220))}" for item in funcs_list[:8]])
        extra_blocks.append(f"⚙️ *Функции в обществе:*\n{funcs_str}")

    ege = card.get("ege_context_and_examples") or {}
    rf_raw = str(ege.get("illustration_rf") or "").strip()
    if rf_raw:
        rf_safe = strip_md_delimiters(truncate_plain(rf_raw, 700))
        extra_blocks.append(f"🇷🇺 *Пример из реалий РФ:*\n_{rf_safe}_")

    pitfalls = [str(x).strip() for x in (card.get("common_pitfalls") or []) if str(x).strip()]
    if pitfalls:
        pitfalls_str = "\n".join([f"• {escape_md(truncate_plain(item, 250))}" for item in pitfalls[:6]])
        extra_blocks.append(f"⚠️ *Ловушки и частые ошибки:*\n{pitfalls_str}")

    # Собираем блоки по очереди, гарантируя, что ни один блок не будет разрезан посередине тега
    result_text = base_text
    truncated = False
    for block in extra_blocks:
        candidate = result_text + "\n\n" + block
        if len(candidate) <= 3900:
            result_text = candidate
        else:
            truncated = True
            break

    if truncated:
        result_text += "\n\n_...[текст сокращен для экрана Telegram]_"

    return result_text


async def send_next_flashcard(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    module_filter = data.get("module_filter")
    user_id = callback.from_user.id

    card = select_next_card(user_id=user_id, module_filter=module_filter)
    if not card:
        empty_text = "❌ В выбранном модуле пока нет терминов. Попробуй выбрать «Все темы»."
        await safe_edit_text(callback.message, empty_text, reply_markup=Keyboards.back_to_menu(), parse_mode=None)
        return

    shown_at = time.time()
    await state.update_data(current_card_code=card["code"], shown_at=shown_at, revealed_elapsed=None)

    mod_title = strip_md_delimiters(card.get("module_title") or "Обществознание")
    term_title = strip_md_delimiters(str(card.get("term") or "").upper())

    text = (
        f"🏷 *{mod_title}*\n\n"
        f"✨ *{term_title}*\n\n"
        f"💭 _Вспомни род и ключевые признаки понятия._"
    )

    await safe_edit_text(
        callback.message,
        text,
        reply_markup=Keyboards.flashcard_front(card["code"]),
        parse_mode=ParseMode.MARKDOWN
    )


@router.callback_query(F.data.startswith("card:reveal:"))
async def handle_card_reveal(callback: CallbackQuery, state: FSMContext):
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Некорректные данные карточки.")
        return
    code = parts[2]

    data = await state.get_data()
    current_card_code = data.get("current_card_code")
    if current_card_code and current_card_code != code:
        await callback.answer("Эта карточка уже неактуальна.", show_alert=False)
        return

    card = catalog.get_by_code(code)
    if not card:
        await callback.answer("Термин не найден.")
        return

    # Фиксируем время размышления в момент переворота карточки, чтобы чтение оборота/пояснения не штрафовало ученика
    shown_at = data.get("shown_at", time.time())
    if data.get("revealed_elapsed") is None:
        revealed_elapsed = round(max(0.0, time.time() - shown_at), 1)
        await state.update_data(revealed_elapsed=revealed_elapsed)

    text = format_card_back(card, show_detail=False)

    await safe_edit_text(
        callback.message,
        text,
        reply_markup=Keyboards.flashcard_back(code, has_detail=False),
        parse_mode=ParseMode.MARKDOWN
    )
    await callback.answer()


@router.callback_query(F.data.startswith("card:detail:"))
async def handle_card_detail(callback: CallbackQuery, state: FSMContext):
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("Некорректные данные карточки.")
        return
    code = parts[2]

    data = await state.get_data()
    current_card_code = data.get("current_card_code")
    if current_card_code and current_card_code != code:
        await callback.answer("Эта карточка уже неактуальна.", show_alert=False)
        return

    card = catalog.get_by_code(code)
    if not card:
        await callback.answer("Термин не найден.")
        return

    text = format_card_back(card, show_detail=True)

    await safe_edit_text(
        callback.message,
        text,
        reply_markup=Keyboards.flashcard_back(code, has_detail=True),
        parse_mode=ParseMode.MARKDOWN
    )
    await callback.answer("Подробный теоретический разбор открыт!")


@router.callback_query(F.data.startswith("card:ans:"))
async def handle_card_answer(callback: CallbackQuery, state: FSMContext):
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer("Некорректный ответ.")
        return
    code = parts[2]
    answer_type = parts[3]

    data = await state.get_data()
    current_card_code = data.get("current_card_code")

    # Защита от race condition при быстром двойном клике
    if not current_card_code or current_card_code != code:
        await callback.answer("Карточка уже оценена.", show_alert=False)
        return

    # Сбрасываем текущую карточку
    await state.update_data(current_card_code=None)

    shown_at = data.get("shown_at", time.time())
    revealed_elapsed = data.get("revealed_elapsed")
    elapsed = revealed_elapsed if revealed_elapsed is not None else round(max(0.0, time.time() - shown_at), 1)

    is_positive = (answer_type == "know_well")
    delta_ans = SpacedRepetitionAlgorithm.calculate_answer_delta(answer_type)
    delta_tm = SpacedRepetitionAlgorithm.calculate_time_delta(elapsed, is_correct=is_positive)

    # Сохраняем с меткой 'card', username и full_name
    safe_code = str(code).replace(";", "_")
    entry_str = f"{safe_code};{delta_ans};{delta_tm};{elapsed};card"
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
    pool = list(data.get("test_pool") or [])
    module_filter = data.get("module_filter")

    # Если пул терминов пуст — тест завершён!
    if not pool:
        total = data.get("test_total_in_session", 0)
        if total <= 0:
            await safe_edit_text(
                callback.message,
                "❌ В выбранном модуле пока недостаточно терминов.",
                reply_markup=Keyboards.back_to_menu(),
                parse_mode=None
            )
            return

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
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=Keyboards.test_finish(has_errors=bool(errors)),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    next_code = pool.pop()
    await state.update_data(test_pool=pool)

    card = catalog.get_by_code(next_code)
    if not card:
        if not catalog.all_terms:
            await safe_edit_text(
                callback.message,
                "❌ База терминов пуста.",
                reply_markup=Keyboards.back_to_menu(),
                parse_mode=None
            )
            return
        card = catalog.all_terms[0]

    test_data = TestGenerator.generate(card, module_filter)
    shown_at = time.time()
    await state.update_data(current_test_data=test_data, test_shown_at=shown_at)

    total_num = data.get("test_total_in_session", len(pool) + 1)
    curr_num = total_num - len(pool)

    mod_title = strip_md_delimiters(card.get("module_title") or "Обществознание")
    text = (
        f"🎯 *Тест: {mod_title}* `[{curr_num}/{total_num}]`\n\n"
        f"{test_data['question_text']}"
    )

    await safe_edit_text(
        callback.message,
        text,
        reply_markup=Keyboards.test_options(card["code"], test_data["options"]),
        parse_mode=ParseMode.MARKDOWN
    )


@router.callback_query(F.data.startswith("test:ans:"))
async def handle_test_answer(callback: CallbackQuery, state: FSMContext):
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer("Некорректные данные ответа.", show_alert=True)
        return
    code = parts[2]
    try:
        option_idx = int(parts[3])
    except ValueError:
        await callback.answer("Некорректный индекс ответа.", show_alert=True)
        return

    data = await state.get_data()
    test_data = data.get("current_test_data")
    shown_at = data.get("test_shown_at", time.time())
    elapsed = round(max(0.0, time.time() - shown_at), 1)

    # Защита от кликов по старым вопросам из истории переписки и отрицательных индексов
    if (
        not test_data
        or test_data.get("correct_term", {}).get("code") != code
        or option_idx < 0
        or option_idx >= len(test_data.get("options", []))
    ):
        await callback.answer("Этот вопрос уже неактуален.", show_alert=True)
        return

    await state.update_data(current_test_data=None)

    chosen_option = test_data["options"][option_idx]
    is_correct = chosen_option["is_correct"]
    correct_term = test_data["correct_term"]

    correct_count = data.get("test_session_correct", 0)
    session_errors = list(data.get("test_session_errors") or [])
    session_times = list(data.get("test_session_times") or [])

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
    if elapsed >= SpacedRepetitionAlgorithm.MIN_TIME_WINDOW:
        answer_type = "correct" if is_correct else "incorrect"
        delta_ans = SpacedRepetitionAlgorithm.calculate_answer_delta(answer_type)
        delta_tm = SpacedRepetitionAlgorithm.calculate_time_delta(elapsed, is_correct=is_correct)
        safe_code = str(code).replace(";", "_")
        entry_str = f"{safe_code};{delta_ans};{delta_tm};{elapsed};test"

        user = callback.from_user
        username = f"@{user.username}" if user.username else None
        full_name = user.full_name or (user.first_name or f"Пользователь {user.id}")
        db.save_answer_entry(user.id, username, full_name, entry_str)

    if is_correct:
        status_text = "🎉 *Верно!* Отличная работа."
    else:
        if test_data.get("explanation_note"):
            status_text = f"❌ *Неверно.*\n💡 {escape_md(test_data['explanation_note'])}"
        else:
            status_text = f"❌ *Неверно.* Правильный термин: *{strip_md_delimiters(correct_term['term'])}*."

    traits = correct_term.get("traits") or []
    traits_hint = f"• {escape_md(traits[0])}" if traits else f"• {escape_md(correct_term.get('genus') or '')}"
    term_upper = strip_md_delimiters(str(correct_term.get("term") or "").upper())
    genus_safe = strip_md_delimiters(correct_term.get("genus") or "Родовое понятие")
    text = (
        f"{status_text}\n\n"
        f"🎓 *{term_upper}*\n"
        f"🏛 _{genus_safe}_\n"
        f"📌 {traits_hint}"
    )

    if elapsed < SpacedRepetitionAlgorithm.MIN_TIME_WINDOW:
        text += "\n\n⚡️ _Ответ < 3 сек (мисклик) — не учтён в общей статистике._"

    await safe_edit_text(callback.message, text, reply_markup=Keyboards.test_next(), parse_mode=ParseMode.MARKDOWN)
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
        t_name = strip_md_delimiters(str(err.get("term") or "").upper())
        g_name = strip_md_delimiters(truncate_plain(err.get("genus") or "", 250))
        lines.append(f"*{idx}. {t_name}*")
        lines.append(f"🏛 Род: _{g_name}_")
        err_traits = err.get("traits") or []
        if err_traits and str(err_traits[0]).strip():
            tr_safe = strip_md_delimiters(truncate_plain(err_traits[0], 250))
            lines.append(f"📌 Признак: _{tr_safe}_")
        lines.append("")

    if len(errors) > 8:
        lines.append(f"_...и ещё {len(errors) - 8} ошибок._")

    text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Начать заново", callback_data="test:restart")],
        [InlineKeyboardButton(text="🚪 В главное меню", callback_data="back_to_main")],
    ])
    await safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data == "test:restart")
async def handle_test_restart(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    module_filter = data.get("module_filter")
    available = catalog.get_terms(module_filter)
    if not available:
        await safe_edit_text(
            callback.message,
            "❌ В выбранном модуле пока недостаточно терминов.",
            reply_markup=Keyboards.back_to_menu(),
            parse_mode=None
        )
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


# =====================================================================
# 9. ТОЧКА ВХОДА (С ПОДДЕРЖКОЙ RENDER FREE WEB SERVICE)
# =====================================================================

async def start_dummy_web_server(port: int = 10000):
    """
    Легковесный фоновый веб-сервер на чистом asyncio.
    Отвечает 200 OK на проверки работоспособности Render,
    чтобы бот мог работать на бесплатном тарифе Web Service ($0/мес).
    Использует таймаут 3.0с для исключения зависания сокетов.
    """
    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            await asyncio.wait_for(reader.read(1024), timeout=3.0)
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"Content-Length: 2\r\n"
                b"Connection: close\r\n\r\nOK"
            )
            writer.write(response)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle_client, "0.0.0.0", port)
    logger.info(f"Фоновый веб-сервер успешно запущен на порту {port} для Render.")
    return server


async def periodic_cleanup_task():
    """Периодическая фоновая очистка учителей без учеников старше 7 дней (каждые 24 часа)."""
    while True:
        try:
            await asyncio.sleep(86400)
            db.cleanup_inactive_teachers()
            logger.info("Выполнена плановая очистка неактивных преподавателей (старше 7 дней).")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Ошибка в periodic_cleanup_task: {e}")


async def main():
    if not BOT_TOKEN:
        logger.error("Токен бота не найден в переменной BOT_TOKEN!")
        print("\n[!] ОШИБКА: Задай переменную окружения BOT_TOKEN в файле .env или в переменных среды.\n")
        return

    catalog.load()
    db.init_db()

    # Запускаем мини веб-сервер для бесплатного тарифа Render Web Service ($0)
    port = int(os.getenv("PORT", "10000"))
    web_server = None
    try:
        web_server = await start_dummy_web_server(port)
    except Exception as e:
        logger.warning(f"Не удалось запустить фоновый веб-сервер на порту {port}: {e}")

    # Сохраняем ссылку на задачу, чтобы сборщик мусора не уничтожил фоновый корутин
    cleanup_task = asyncio.create_task(periodic_cleanup_task())

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот версии 5.2 успешно запущен.")
    try:
        await dp.start_polling(bot)
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        if web_server is not None:
            web_server.close()
            await web_server.wait_closed()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
