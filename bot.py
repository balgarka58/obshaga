"""
Telegram Bot для изучения терминов обществознания (Версия 6.2 — Стабильная экосистема)
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
3. Уважительный и дружелюбный тон общения:
   - Все тексты, подсказки и уведомления обращаются к пользователю на «вы» (естественно и вежливо, без канцелярита).
   - Лаконичная команда /help со списком всех доступных команд.
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
6. Режим «🛠 Работа над ошибками» (/work_errors и кнопка в меню):
   - Целенаправленная проработка проблемных терминов (с высоким весом >= 7.0 или недавними ошибками) в формате карточек или контрольного теста.
7. Учёт серий занятий (Стрики 🔥) и мягкие напоминания:
   - Подсчёт текущей серии дней подряд, рекорда и активных дней за год.
   - Фоновая отправка мягких напоминаний вечером тем ученикам, у кого включены уведомления, есть активная серия, но сегодня ещё нет ответов.
8. Защита и ротация ссылок на веб-аналитику (/reset_link и кнопки в профиле):
   - Версионирование токенов (token_version) для учеников и преподавателей.
   - Мгновенная инвалидация старых ссылок на всех устройствах с подтверждением в 1 клик.
9. Учительская система с 8-значными кодами и мульти-привязкой:
   - Регистрация по команде /teacher с генерацией 8-значного кода и защитой от дублей.
   - Защита от самопривязки учителя в качестве ученика к самому себе.
   - Автоматическая очистка неактивных учителей (раз в 24 часа в фоне).
   - Управление учителями: команда /teachers с кнопками быстрого отключения.
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
import urllib.parse
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple
from collections import deque
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
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
# 1. КОНФИГУРАЦИЯ, ТОКЕНЫ И БЕЗОПАСНАЯ РАЗМЕТКА
# =====================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH = os.getenv("DB_PATH", "social_bot.db")
TERMS_JSON_PATH = os.getenv("TERMS_JSON_PATH", "terms.json")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://obshaga.streamlit.app").rstrip("/")
SECRET_KEY = os.getenv("SECRET_KEY", "obshaga_secret_key_2025")
TIMEZONE_STR = os.getenv("TIMEZONE", "Europe/Moscow")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("social_bot")

# --- НОВАЯ ПРАВКА: Безопасный импорт часового пояса ---
try:
    TZ = ZoneInfo(TIMEZONE_STR)
except Exception:
    logger.warning(f"Не удалось загрузить часовой пояс {TIMEZONE_STR}, используется UTC.")
    TZ = timezone.utc

def get_local_today() -> date:
    """Возвращает текущую дату в заданном часовом поясе (по умолчанию Europe/Moscow)."""
    return datetime.now(TZ).date()
# ------------------------------------------------------

def generate_student_token(user_id: Any, token_version: int = 1, secret_key: Optional[str] = None) -> str:
    """
    Генерирует персональный 8-символьный токен доступа ученика к веб-аналитике.
    При token_version == 1 сохраняет обратную совместимость, при сбросе (version > 1)
    выпускает новый хэш, мгновенно делая старые ссылки недействительными.
    """
    key = secret_key if secret_key is not None else os.getenv("SECRET_KEY", SECRET_KEY)
    uid_str = str(user_id).strip()
    ver = int(token_version) if token_version and int(token_version) > 1 else 1
    payload = f"{uid_str}_{key}" if ver == 1 else f"{uid_str}_{key}_v{ver}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:8]


def generate_teacher_token(teacher_code: Any, token_version: int = 1, secret_key: Optional[str] = None) -> str:
    """Генерирует токен доступа к кабинету преподавателя с поддержкой ротации ссылки."""
    key = secret_key if secret_key is not None else os.getenv("SECRET_KEY", SECRET_KEY)
    code_str = str(teacher_code).strip()
    ver = max(1, int(token_version or 1))
    payload = f"teacher_{code_str}_{key}_v{ver}"
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:8]


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
            raw_code = str(item.get("code") or "").strip().replace(";", "_").replace(":", "_")
            code = raw_code[:24]
            if not code or code in self.terms_by_code:
                continue

            try:
                mod_num = int(code.split(".")[0])
            except (ValueError, IndexError):
                mod_num = int(item.get("module_number") or 1) if str(item.get("module_number") or "").isdigit() else 1

            # Защита от None и нестроковых значений в списке признаков
            raw_traits = item.get("traits")
            traits = []
            if isinstance(raw_traits, list):
                for t in raw_traits:
                    if t is not None:
                        s_t = str(t).strip()
                        if s_t:
                            traits.append(s_t)

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
    1. Ответы учеников: user_id, username, full_name, строка entry, created_at.
    2. Таблица учителей: teacher_code (8-значное число), user_id, full_name, token_version, created_at.
    3. Связующая таблица student_teachers (student_id, teacher_code, joined_at).
    4. Таблица настроек и безопасности user_settings (user_id, token_version, reminders_enabled, last_reminder_date).
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
                    user_id BIGINT NOT NULL UNIQUE,  -- Уникальный индекс для защиты от дублей
                    full_name TEXT,
                    token_version INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                ALTER TABLE teachers ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 1;
                
                -- Защита от дублей для старых баз (добавляем UNIQUE, если его нет)
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'teachers_user_id_key'
                    ) THEN
                        ALTER TABLE teachers ADD CONSTRAINT teachers_user_id_key UNIQUE (user_id);
                    END IF;
                END $$;

                CREATE INDEX IF NOT EXISTS idx_teachers_user ON teachers(user_id);

                CREATE TABLE IF NOT EXISTS student_teachers (
                    student_id BIGINT NOT NULL,
                    teacher_code VARCHAR(8) NOT NULL,
                    joined_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (student_id, teacher_code)
                );
                CREATE INDEX IF NOT EXISTS idx_student_teachers_teacher ON student_teachers(teacher_code);
                CREATE INDEX IF NOT EXISTS idx_student_teachers_student ON student_teachers(student_id);

                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT PRIMARY KEY,
                    token_version INTEGER NOT NULL DEFAULT 1,
                    reminders_enabled INTEGER NOT NULL DEFAULT 1,
                    last_reminder_date TEXT,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
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
                    user_id INTEGER NOT NULL UNIQUE,
                    full_name TEXT,
                    token_version INTEGER NOT NULL DEFAULT 1,
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

                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    token_version INTEGER NOT NULL DEFAULT 1,
                    reminders_enabled INTEGER NOT NULL DEFAULT 1,
                    last_reminder_date TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)
                # Безопасное добавление UNIQUE для старых баз SQLite
                try:
                    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_teachers_user_unique ON teachers(user_id);")
                except Exception:
                    pass
                
                for alter_sql in [
                    "ALTER TABLE user_answers ADD COLUMN username TEXT;",
                    "ALTER TABLE user_answers ADD COLUMN full_name TEXT;",
                    "ALTER TABLE teachers ADD COLUMN token_version INTEGER NOT NULL DEFAULT 1;",
                ]:
                    try:
                        cursor.execute(alter_sql)
                    except Exception:
                        pass

            conn.commit()
        finally:
            cursor.close()
            conn.close()

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

    def _fetch_user_entries_with_timestamps(self, user_id: int) -> List[Tuple[str, Any]]:
        """Возвращает список (entry, created_at) ученика в хронологическом порядке."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute("SELECT entry, created_at FROM user_answers WHERE user_id = %s ORDER BY id ASC;", (user_id,))
            else:
                cursor.execute("SELECT entry, created_at FROM user_answers WHERE user_id = ? ORDER BY id ASC;", (user_id,))
            rows = cursor.fetchall()
        finally:
            cursor.close()
            conn.close()
        return [(str(r[0]), r[1]) for r in rows]

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
                        "review_count": 0,
                        "last_delta_ans": 0.0,
                    }

                cur_w = progress[code]["total_weight"]
                progress[code]["total_weight"] = SpacedRepetitionAlgorithm.update_weight(cur_w, d_ans, d_tm)
                progress[code]["review_count"] += 1
                progress[code]["last_delta_ans"] = d_ans
        return progress

    def get_user_progress_map(self, user_id: int) -> Dict[str, Dict[str, Any]]:
        """
        Вычисляет интервальные веса карточек ТОЛЬКО по режиму карточек (mode != 'test'),
        чтобы результаты экзаменационных тестов не сбивали обучение в карточках.
        """
        entries = self._fetch_user_entries(user_id)
        return self._compute_progress_from_entries(entries)

    def get_problem_term_codes(self, user_id: int) -> List[str]:
        """
        Возвращает список кодов проблемных терминов для режима «Работа над ошибками»:
        - термины с текущим весом в карточках >= 7.0;
        - термины, где последний ответ в карточках или тестах был ошибочным / неуверенным (delta_ans > 0).
        Если ученик проработал проблемный термин в карточках (вес стал < 7.0 и ответ «Знаю»),
        термин выходит из списка проблемных. Успех в тесте НЕ закрывает термин навсегда:
        если после этого в карточках снова была ошибка, термин возвращается в проблемные.
        """
        entries = self._fetch_user_entries(user_id)
        progress_map = self._compute_progress_from_entries(entries)

        last_card_problem: Dict[str, bool] = {}
        last_test_problem: Dict[str, bool] = {}
        last_action_mode: Dict[str, str] = {}

        for entry_str in entries:
            parts = entry_str.split(";")
            if len(parts) >= 4:
                code = parts[0].strip()
                if not code:
                    continue
                try:
                    d_ans = float(parts[1])
                    sec = float(parts[3])
                except ValueError:
                    continue
                mode = parts[4].strip() if len(parts) >= 5 else "card"

                if mode == "test":
                    if sec >= SpacedRepetitionAlgorithm.MIN_TIME_WINDOW:
                        last_test_problem[code] = (d_ans > 0)
                        last_action_mode[code] = "test"
                else:
                    # Для карточек тоже проверяем время (защита от мисклика)
                    if sec >= 1.0:
                        last_card_problem[code] = (d_ans > 0)
                        last_action_mode[code] = "card"

        problem_codes = []
        seen = set()

        for code, p in progress_map.items():
            is_high_weight = p["total_weight"] >= 7.0
            is_card_err = last_card_problem.get(code, False)
            is_test_ok = (last_action_mode.get(code) == "test" and not last_test_problem.get(code, False))

            if is_test_ok and not is_card_err and not is_high_weight:
                continue

            if (is_high_weight or is_card_err) and catalog.get_by_code(code) and code not in seen:
                seen.add(code)
                problem_codes.append(code)

        for code, is_prob in last_card_problem.items():
            if is_prob and catalog.get_by_code(code) and code not in seen:
                seen.add(code)
                problem_codes.append(code)

        return problem_codes

    @staticmethod
    def _extract_date_from_ts(raw_ts: Any, target_tz=None) -> Optional[date]:
        """Извлекает дату из timestamp с конвертацией в нужный часовой пояс."""
        if raw_ts is None:
            return None
        if isinstance(raw_ts, datetime):
            if raw_ts.tzinfo is not None and target_tz is not None:
                raw_ts = raw_ts.astimezone(target_tz)
            return raw_ts.date()
        if isinstance(raw_ts, date):
            return raw_ts
        s = str(raw_ts).strip()
        if len(s) >= 10:
            try:
                dt = datetime.fromisoformat(s[:19])
                if dt.tzinfo is not None and target_tz is not None:
                    dt = dt.astimezone(target_tz)
                return dt.date()
            except ValueError:
                pass
        return None

    def get_user_streak_info(self, user_id: int, reference_date: Optional[date] = None) -> Dict[str, int]:
        """
        Вычисляет статистику регулярности занятий (стрик):
        - current_streak: текущая непрерывная серия активных дней (включая сегодня или вчера)
        - best_streak: лучшая непрерывная серия за всё время
        - active_days_year: количество активных дней за последние 365 дней
        - today_actions: количество засчитанных действий за сегодня
        """
        today_dt = reference_date or get_local_today()
        rows = self._fetch_user_entries_with_timestamps(user_id)

        active_dates_set = set()
        today_actions = 0

        for entry_str, created_at in rows:
            parts = entry_str.split(";")
            if len(parts) < 4:
                continue
            try:
                sec = float(parts[3])
            except ValueError:
                continue
            mode = parts[4].strip() if len(parts) >= 5 else "card"
            if mode == "test" and sec < SpacedRepetitionAlgorithm.MIN_TIME_WINDOW:
                continue

            # Конвертируем время в локальный часовой пояс для правильной группировки по дням
            d = self._extract_date_from_ts(created_at, target_tz=TZ) or today_dt
            active_dates_set.add(d)
            if d == today_dt:
                today_actions += 1

        if not active_dates_set:
            return {
                "current_streak": 0,
                "best_streak": 0,
                "active_days_year": 0,
                "today_actions": 0,
            }

        sorted_dates = sorted(active_dates_set)
        year_ago = today_dt - timedelta(days=364)
        active_days_year = sum(1 for d in sorted_dates if year_ago <= d <= today_dt)

        best_streak = 1
        cur_run = 1
        for i in range(1, len(sorted_dates)):
            if (sorted_dates[i] - sorted_dates[i - 1]).days == 1:
                cur_run += 1
                if cur_run > best_streak:
                    best_streak = cur_run
            elif (sorted_dates[i] - sorted_dates[i - 1]).days > 1:
                cur_run = 1

        if today_dt in active_dates_set:
            check_d = today_dt
        elif (today_dt - timedelta(days=1)) in active_dates_set:
            check_d = today_dt - timedelta(days=1)
        else:
            check_d = None

        current_streak = 0
        while check_d is not None and check_d in active_dates_set:
            current_streak += 1
            check_d -= timedelta(days=1)

        return {
            "current_streak": current_streak,
            "best_streak": best_streak,
            "active_days_year": active_days_year,
            "today_actions": today_actions,
        }

    def get_user_stats(self, user_id: int) -> Dict[str, Any]:
        """Возвращает раздельные метрики для карточек, тестов и серий активности."""
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

        learned = sum(1 for p in progress_map.values() if p["total_weight"] <= 3.0)
        in_progress = sum(1 for p in progress_map.values() if 3.0 < p["total_weight"] < 7.0)
        needs_review = sum(1 for p in progress_map.values() if p["total_weight"] >= 7.0)

        streak_info = self.get_user_streak_info(user_id)
        problem_count = len(self.get_problem_term_codes(user_id))

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
            "current_streak": streak_info["current_streak"],
            "best_streak": streak_info["best_streak"],
            "active_days_year": streak_info["active_days_year"],
            "today_actions": streak_info["today_actions"],
            "problem_count": problem_count,
        }

    # --- НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ, ТОКЕНЫ И НАПОМИНАНИЯ ---

    def _ensure_user_settings_row(self, cursor, user_id: int):
        if self.is_postgres:
            cursor.execute(
                "INSERT INTO user_settings (user_id, token_version, reminders_enabled) VALUES (%s, 1, 1) ON CONFLICT (user_id) DO NOTHING;",
                (user_id,)
            )
        else:
            cursor.execute(
                "INSERT OR IGNORE INTO user_settings (user_id, token_version, reminders_enabled) VALUES (?, 1, 1);",
                (user_id,)
            )

    def get_user_token_version(self, user_id: int) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute("SELECT token_version FROM user_settings WHERE user_id = %s;", (user_id,))
            else:
                cursor.execute("SELECT token_version FROM user_settings WHERE user_id = ?;", (user_id,))
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else 1
        except Exception:
            return 1
        finally:
            cursor.close()
            conn.close()

    def rotate_user_token(self, user_id: int) -> int:
        """Увеличивает версию персонального токена ученика на 1, мгновенно отзывая старую ссылку."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            self._ensure_user_settings_row(cursor, user_id)
            if self.is_postgres:
                cursor.execute(
                    "UPDATE user_settings SET token_version = token_version + 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s RETURNING token_version;",
                    (user_id,)
                )
                row = cursor.fetchone()
                new_ver = int(row[0]) if row else 2
            else:
                cursor.execute(
                    "UPDATE user_settings SET token_version = token_version + 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (user_id,)
                )
                cursor.execute("SELECT token_version FROM user_settings WHERE user_id = ?;", (user_id,))
                row = cursor.fetchone()
                new_ver = int(row[0]) if row else 2
            conn.commit()
            return new_ver
        finally:
            cursor.close()
            conn.close()

    def get_user_reminders_enabled(self, user_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute("SELECT reminders_enabled FROM user_settings WHERE user_id = %s;", (user_id,))
            else:
                cursor.execute("SELECT reminders_enabled FROM user_settings WHERE user_id = ?;", (user_id,))
            row = cursor.fetchone()
            return bool(int(row[0])) if row and row[0] is not None else True
        except Exception:
            return True
        finally:
            cursor.close()
            conn.close()

    def toggle_user_reminders(self, user_id: int) -> bool:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            self._ensure_user_settings_row(cursor, user_id)
            if self.is_postgres:
                cursor.execute(
                    "UPDATE user_settings SET reminders_enabled = CASE WHEN reminders_enabled = 1 THEN 0 ELSE 1 END, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s;",
                    (user_id,)
                )
                cursor.execute("SELECT reminders_enabled FROM user_settings WHERE user_id = %s;", (user_id,))
            else:
                cursor.execute(
                    "UPDATE user_settings SET reminders_enabled = CASE WHEN reminders_enabled = 1 THEN 0 ELSE 1 END, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (user_id,)
                )
                cursor.execute("SELECT reminders_enabled FROM user_settings WHERE user_id = ?;", (user_id,))
            row = cursor.fetchone()
            conn.commit()
            return bool(int(row[0])) if row and row[0] is not None else True
        finally:
            cursor.close()
            conn.close()

    def mark_reminder_sent(self, user_id: int, date_iso: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            self._ensure_user_settings_row(cursor, user_id)
            if self.is_postgres:
                cursor.execute(
                    "UPDATE user_settings SET last_reminder_date = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s;",
                    (date_iso, user_id)
                )
            else:
                cursor.execute(
                    "UPDATE user_settings SET last_reminder_date = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (date_iso, user_id)
                )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

    def get_users_for_daily_reminder(self, reference_date: Optional[date] = None) -> List[Dict[str, Any]]:
        """
        Возвращает список пользователей (user_id, current_streak), которым нужно отправить
        мягкое напоминание о сохранении серии:
        1) Включены напоминания (reminders_enabled == 1 или запись ещё не создана).
        2) Сегодня напоминание ещё не отправлялось (last_reminder_date != today_iso).
        3) Есть активная серия (current_streak >= 1), но сегодня ещё 0 действий (today_actions == 0).
        """
        today_dt = reference_date or get_local_today()
        today_iso = today_dt.isoformat()

        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT DISTINCT user_id FROM user_answers;")
            all_uids = [int(r[0]) for r in cursor.fetchall() if r and r[0] is not None]

            cursor.execute("SELECT user_id, reminders_enabled, last_reminder_date FROM user_settings;")
            settings_map = {
                int(r[0]): (bool(int(r[1])) if r[1] is not None else True, str(r[2] or ""))
                for r in cursor.fetchall()
            }
        finally:
            cursor.close()
            conn.close()

        candidates = []
        for uid in all_uids:
            rem_enabled, last_sent = settings_map.get(uid, (True, ""))
            if not rem_enabled or last_sent == today_iso:
                continue
            streak_info = self.get_user_streak_info(uid, reference_date=today_dt)
            if streak_info["current_streak"] >= 1 and streak_info["today_actions"] == 0:
                candidates.append({
                    "user_id": uid,
                    "current_streak": streak_info["current_streak"],
                })
        return candidates

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
                        DELETE FROM student_teachers st
                        WHERE NOT EXISTS (
                            SELECT 1 FROM teachers t WHERE t.teacher_code = st.teacher_code
                        );
                    """)
                else:
                    cursor.execute("""
                        DELETE FROM teachers
                        WHERE created_at < datetime('now', '-7 days')
                          AND NOT EXISTS (
                              SELECT 1 FROM student_teachers st WHERE st.teacher_code = teachers.teacher_code
                          );
                        DELETE FROM student_teachers
                        WHERE NOT EXISTS (
                            SELECT 1 FROM teachers t WHERE t.teacher_code = student_teachers.teacher_code
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
        if not clean_code or not clean_code.isdigit():
            return None
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute("SELECT teacher_code, user_id, full_name, created_at, COALESCE(token_version, 1) FROM teachers WHERE teacher_code = %s;", (clean_code,))
            else:
                cursor.execute("SELECT teacher_code, user_id, full_name, created_at, COALESCE(token_version, 1) FROM teachers WHERE teacher_code = ?;", (clean_code,))
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()

        if row:
            return {
                "teacher_code": str(row[0]),
                "user_id": int(row[1]),
                "full_name": str(row[2]) if row[2] else f"Учитель {row[0]}",
                "created_at": row[3],
                "token_version": int(row[4] if len(row) > 4 and row[4] is not None else 1),
            }
        return None

    def get_teacher_by_user_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute("SELECT teacher_code, user_id, full_name, created_at, COALESCE(token_version, 1) FROM teachers WHERE user_id = %s;", (user_id,))
            else:
                cursor.execute("SELECT teacher_code, user_id, full_name, created_at, COALESCE(token_version, 1) FROM teachers WHERE user_id = ?;", (user_id,))
            row = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()

        if row:
            return {
                "teacher_code": str(row[0]),
                "user_id": int(row[1]),
                "full_name": str(row[2]) if row[2] else f"Учитель {row[0]}",
                "created_at": row[3],
                "token_version": int(row[4] if len(row) > 4 and row[4] is not None else 1),
            }
        return None

    def rotate_teacher_token(self, user_id: int) -> Optional[Tuple[str, int]]:
        """Увеличивает token_version преподавателя на 1 и возвращает (teacher_code, new_version)."""
        existing = self.get_teacher_by_user_id(user_id)
        if not existing:
            return None
        code = existing["teacher_code"]
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute(
                    "UPDATE teachers SET token_version = COALESCE(token_version, 1) + 1 WHERE teacher_code = %s RETURNING token_version;",
                    (code,)
                )
                row = cursor.fetchone()
                new_ver = int(row[0]) if row else existing["token_version"] + 1
            else:
                cursor.execute(
                    "UPDATE teachers SET token_version = COALESCE(token_version, 1) + 1 WHERE teacher_code = ?;",
                    (code,)
                )
                cursor.execute("SELECT COALESCE(token_version, 1) FROM teachers WHERE teacher_code = ?;", (code,))
                row = cursor.fetchone()
                new_ver = int(row[0]) if row else existing["token_version"] + 1
            conn.commit()
            return code, new_ver
        finally:
            cursor.close()
            conn.close()

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
                    "INSERT INTO teachers (teacher_code, user_id, full_name, token_version) VALUES (%s, %s, %s, 1);",
                    (code, user_id, full_name)
                )
            else:
                cursor.execute(
                    "INSERT INTO teachers (teacher_code, user_id, full_name, token_version) VALUES (?, ?, ?, 1);",
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
        self.last_seen: Dict[int, float] = {}
        self.max_users = max_users

    def push(self, user_id: int, code: str):
        # Очистка старых записей перед добавлением, если достигнут лимит
        if user_id not in self.recent_history:
            if len(self.recent_history) >= self.max_users:
                oldest_uid = next(iter(self.recent_history))
                self.recent_history.pop(oldest_uid, None)
                self.last_seen.pop(oldest_uid, None)
            self.recent_history[user_id] = deque(maxlen=10)
        
        self.recent_history[user_id].append(code)
        self.last_seen[user_id] = time.time()

    def get_recent(self, user_id: int) -> List[str]:
        if user_id in self.recent_history:
            self.last_seen[user_id] = time.time()
            return list(self.recent_history[user_id])
        return []

    def cleanup_old_sessions(self, max_age_seconds: int = 172800):  # 48 часов
        """Удаляет записи пользователей, которые не заходили более 48 часов."""
        now = time.time()
        to_remove = [uid for uid, last_t in self.last_seen.items() if now - last_t > max_age_seconds]
        for uid in to_remove:
            self.recent_history.pop(uid, None)
            self.last_seen.pop(uid, None)


session_tracker = SessionTracker()


def select_next_card(
    user_id: int,
    module_filter: Optional[int] = None,
    custom_codes: Optional[List[str]] = None
) -> Optional[Dict[str, Any]]:
    """
    Вероятностный алгоритм (Weighted Random / Roulette Selection):
    Карточки не идут по скучной очереди. Вес определяет шанс выпадения:
    трудные (вес 8-10) выпадают часто, новые (6.5) — в приоритете,
    а выученные (1.0) время от времени всплывают для проверки.
    Поддерживает выборку по списку проблемных терминов (custom_codes) для «Работы над ошибками».
    """
    if custom_codes is not None:
        available_terms = [catalog.get_by_code(c) for c in custom_codes if catalog.get_by_code(c)]
    else:
        available_terms = catalog.get_terms(module_filter)

    if not available_terms:
        return None

    progress_map = db.get_user_progress_map(user_id)
    recent_codes = set(session_tracker.get_recent(user_id))

    candidates = [t for t in available_terms if t["code"] not in recent_codes]
    if not candidates:
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

        for d in shuffled:
            if d not in unique_pool:
                unique_pool.append(d)
            if len(unique_pool) >= count:
                return unique_pool

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

        # ТИП 2: По термину выбрать родовую характеристику
        elif chosen_type == 2:
            term_escaped = strip_md_delimiters(correct_term["term"].upper())
            question_text = f"Выберите верную родовую характеристику термина *{term_escaped}*:"

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
                f"Найдите выпадающий (чужой) признак:"
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
            [InlineKeyboardButton(text="🛠 Работа над ошибками", callback_data="menu:errors")],
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
    def error_workout_menu(count: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🗂 Проработать в карточках ({count})", callback_data="errors:start:cards")],
            [InlineKeyboardButton(text=f"🎯 Проверить в тесте ({count})", callback_data="errors:start:tests")],
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

    connect_notice = ""
    parts = message.text.split(maxsplit=1) if message.text else []
    if len(parts) > 1 and parts[1].strip().startswith("t_"):
        teacher_code = parts[1].strip()[2:].strip()
        teacher = db.get_teacher(teacher_code)
        if teacher:
            if teacher["user_id"] == message.from_user.id:
                connect_notice = "⚠️ *Вы не можете прикрепиться к своему собственному классу в качестве ученика.*\n\n"
            elif not db.is_student_connected(message.from_user.id, teacher_code):
                db.connect_student_to_teacher(message.from_user.id, teacher_code)
                connect_notice = (
                    f"✅ *Вы успешно прикреплены к преподавателю:*\n"
                    f"*{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(teacher_code)}`).\n"
                    f"Ваш прогресс теперь отображается в ведомости класса.\n\n"
                )
            else:
                connect_notice = (
                    f"ℹ️ *Вы уже прикреплены к преподавателю:*\n"
                    f"*{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(teacher_code)}`).\n\n"
                )
        else:
            connect_notice = (
                f"❌ *Преподаватель по ссылке-коду* `{escape_md(teacher_code)}` *не найден.*\n"
                f"Проверьте ссылку или уточните код у своего преподавателя.\n\n"
            )

    text = (
        f"{connect_notice}"
        "👋 *Добро пожаловать в тренажер терминов по обществознанию!*\n\n"
        "Здесь вы сможете выучить все ключевые понятия кодификатора без зубрёжки:\n\n"
        "• 🗂 *Карточки* — умное интервальное повторение. Трудные термины выпадают чаще, "
        "а выученные периодически всплывают для проверки памяти.\n"
        "• 🎯 *Тесты* — контрольный срез без подсказок. Решайте задания и разбирайте ошибки.\n"
        "• 🛠 *Работа над ошибками* — точечная тренировка терминов, вызвавших затруднения.\n\n"
        "Выберите формат тренировки:"
    )
    await message.answer(text, reply_markup=Keyboards.main_menu(), parse_mode=ParseMode.MARKDOWN)


@router.message(Command("help"))
async def handle_help(message: Message):
    text = (
        "📖 *Команды бота:*\n\n"
        "/start — Главное меню и запуск тренировки\n"
        "/help — Список доступных команд\n"
        "/work\\_errors — Работа над ошибками (проблемные термины)\n"
        "/teacher — Кабинет преподавателя (код класса и аналитика)\n"
        "/teachers — Мои преподаватели и управление привязкой\n"
        "/reset\\_link — Сбросить и обновить ссылку на аналитику\n\n"
        "💡 _Чтобы подключиться к преподавателю, просто отправьте его 8-значный код сообщением в чат._"
    )
    await message.answer(text, reply_markup=Keyboards.back_to_menu(), parse_mode=ParseMode.MARKDOWN)


def _build_teacher_cabinet_view(
    full_name: str,
    code: str,
    st_count: int,
    token_version: int,
    bot_username: str,
    is_new: bool = False
) -> Tuple[str, InlineKeyboardMarkup]:
    t_token = generate_teacher_token(code, token_version)
    
    safe_code_url = urllib.parse.quote(code)
    safe_token_url = urllib.parse.quote(t_token)
    
    raw_dash_url = f"{DASHBOARD_URL}/?teacher={safe_code_url}&token={safe_token_url}"
    invite_url = escape_md(f"https://t.me/{bot_username}?start=t_{code}")
    dash_url = escape_md(raw_dash_url)

    if is_new:
        text = (
            f"🎉 *Вы успешно зарегистрированы как преподаватель!*\n\n"
            f"👤 Имя: *{strip_md_delimiters(full_name)}*\n"
            f"🔑 Ваш личный 8-значный код: `{escape_md(code)}`\n\n"
            f"👥 *Ссылка для учеников (отправьте её классу):*\n"
            f"{invite_url}\n\n"
            f"📊 *Ваша онлайн-аналитика класса:*\n"
            f"{dash_url}\n\n"
            f"💡 _Ученики могут перейти по ссылке или просто отправить в чат бота ваш код_ `{escape_md(code)}`.\n"
            f"⏳ _Если за 7 дней к вам не подключится ни один ученик, профиль учителя автоматически аннулируется для чистоты базы._"
        )
    else:
        text = (
            f"👨‍🏫 *Кабинет преподавателя*\n\n"
            f"👤 Преподаватель: *{strip_md_delimiters(full_name)}*\n"
            f"🔑 Личный код класса: `{escape_md(code)}`\n"
            f"👥 Подключено учеников: *{st_count}*\n\n"
            f"🔗 *Ссылка для учеников (отправьте в чат класса):*\n"
            f"{invite_url}\n\n"
            f"📊 *Ваша онлайн-аналитика класса:*\n"
            f"{dash_url}\n\n"
            f"💡 _Ученики могут перейти по ссылке выше или просто отправить в чат бота ваш 8-значный код_ `{escape_md(code)}`."
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть аналитику класса", url=raw_dash_url)],
        [InlineKeyboardButton(text="🔄 Сбросить ссылку на аналитику класса", callback_data="teacher:reset_prompt")],
        [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")],
    ])
    return text, keyboard


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
        t_ver = existing.get("token_version", 1)
        text, kb = _build_teacher_cabinet_view(
            existing["full_name"], code, st_count, t_ver, bot_username, is_new=False
        )
        await message.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return

    user = message.from_user
    full_name = user.full_name or (f"@{user.username}" if user.username else f"Преподаватель {user.id}")
    code = db.register_teacher(user_id, full_name)
    text, kb = _build_teacher_cabinet_view(
        full_name, code, 0, 1, bot_username, is_new=True
    )
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


@router.callback_query(F.data == "teacher:reset_prompt")
async def handle_teacher_reset_prompt(callback: CallbackQuery):
    existing = db.get_teacher_by_user_id(callback.from_user.id)
    if not existing:
        await callback.answer("Профиль преподавателя не найден.", show_alert=True)
        return

    text = (
        "🔐 *Сброс ссылки на аналитику класса*\n\n"
        "Вы уверены, что хотите выпустить новую ссылку для кабинета преподавателя?\n"
        "Предыдущая ссылка аналитики класса сразу перестанет работать на всех устройствах "
        "(код класса для учеников останется прежним)."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, выпустить новую ссылку", callback_data="teacher:reset_confirm")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="teacher:view")],
    ])
    await safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data == "teacher:view")
async def handle_teacher_view_cb(callback: CallbackQuery):
    existing = db.get_teacher_by_user_id(callback.from_user.id)
    if not existing:
        await callback.answer("Профиль преподавателя не найден.", show_alert=True)
        return
    bot_info = await callback.bot.get_me()
    bot_username = bot_info.username or "obshaga_bot"
    code = existing["teacher_code"]
    st_count = db.get_teacher_students_count(code)
    t_ver = existing.get("token_version", 1)
    text, kb = _build_teacher_cabinet_view(existing["full_name"], code, st_count, t_ver, bot_username, is_new=False)
    await safe_edit_text(callback.message, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    await callback.answer()


@router.callback_query(F.data == "teacher:reset_confirm")
async def handle_teacher_reset_confirm(callback: CallbackQuery):
    res = db.rotate_teacher_token(callback.from_user.id)
    if not res:
        await callback.answer("Профиль преподавателя не найден.", show_alert=True)
        return

    code, new_ver = res
    existing = db.get_teacher_by_user_id(callback.from_user.id)
    bot_info = await callback.bot.get_me()
    bot_username = bot_info.username or "obshaga_bot"
    st_count = db.get_teacher_students_count(code)
    full_name = existing["full_name"] if existing else f"Преподаватель {code}"

    text, kb = _build_teacher_cabinet_view(full_name, code, st_count, new_ver, bot_username, is_new=False)
    text = "✅ *Ссылка на аналитику класса успешно обновлена! Старая ссылка больше недействительна.*\n\n" + text
    await safe_edit_text(callback.message, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    await callback.answer("Новая ссылка выпущена!")


@router.message(Command("reset_link"))
async def handle_reset_link_command(message: Message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    new_student_ver = db.rotate_user_token(user_id)
    student_token = generate_student_token(user_id, new_student_ver)
    
    safe_uid = urllib.parse.quote(str(user_id))
    safe_token = urllib.parse.quote(student_token)
    student_url = f"{DASHBOARD_URL}/?student={safe_uid}&token={safe_token}"

    buttons = [
        [InlineKeyboardButton(text="🌐 Открыть личную веб-аналитику", url=student_url)]
    ]
    lines = [
        "🔐 *Ссылка на аналитику успешно обновлена!*\n",
        "Все старые ссылки отозваны и больше не открываются.\n",
        f"📊 *Ваша новая персональная ссылка:*\n{escape_md(student_url)}"
    ]

    teacher_res = db.rotate_teacher_token(user_id)
    if teacher_res:
        t_code, new_t_ver = teacher_res
        t_token = generate_teacher_token(t_code, new_t_ver)
        
        safe_t_code = urllib.parse.quote(t_code)
        safe_t_token = urllib.parse.quote(t_token)
        teacher_url = f"{DASHBOARD_URL}/?teacher={safe_t_code}&token={safe_t_token}"
        
        lines.append(f"\n👨‍🏫 *Новая ссылка аналитики класса ({escape_md(t_code)}):*\n{escape_md(teacher_url)}")
        buttons.append([InlineKeyboardButton(text="👨‍🏫 Открыть аналитику класса", url=teacher_url)])

    buttons.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")])

    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )


@router.message(Command("teachers"))
async def handle_teachers_list(message: Message):
    if not message.from_user:
        return
    teachers = db.get_student_teachers(message.from_user.id)
    if not teachers:
        await message.answer(
            "ℹ️ Вы пока не прикреплены ни к одному преподавателю.\n\n"
            "Чтобы подключиться, отправьте его 8-значный код сообщением в этот чат "
            "или перейдите по ссылке-приглашению от преподавателя.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    buttons = []
    lines = ["👨‍🏫 *Ваши преподаватели:*\n"]
    for idx, t in enumerate(teachers, 1):
        lines.append(f"{idx}. *{strip_md_delimiters(t['full_name'])}* (код `{escape_md(t['teacher_code'])}`)")
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ Отключить: {t['full_name'][:25]}",
                callback_data=f"teacher:unlink:{t['teacher_code']}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")])

    text = "\n".join(lines) + "\n\n_Нажмите кнопку ниже, если хотите отключиться от преподавателя:_"
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
            f"Проверьте правильность ввода 8 цифр или уточните код у своего преподавателя.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if teacher["user_id"] == message.from_user.id:
        await message.answer(
            "❌ Вы не можете прикрепиться к своему собственному классу в качестве ученика.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if db.is_student_connected(message.from_user.id, code):
        await message.answer(
            f"ℹ️ Вы уже подключены к преподавателю *{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(code)}`).",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    db.connect_student_to_teacher(message.from_user.id, code)
    await message.answer(
        f"✅ *Преподаватель найден:*\n"
        f"*{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(code)}`).\n\n"
        f"Вы успешно подключены! Ваш прогресс теперь отображается в ведомости класса.",
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
            "ℹ️ Вы отключились от всех преподавателей.\n\n"
            "Чтобы подключиться снова, просто отправьте 8-значный код преподавателя.",
            reply_markup=Keyboards.back_to_menu(),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    buttons = []
    lines = ["👨‍🏫 *Ваши преподаватели:*\n"]
    for idx, t in enumerate(teachers, 1):
        lines.append(f"{idx}. *{strip_md_delimiters(t['full_name'])}* (код `{escape_md(t['teacher_code'])}`)")
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ Отключить: {t['full_name'][:25]}",
                callback_data=f"teacher:unlink:{t['teacher_code']}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")])
    text = "\n".join(lines) + "\n\n_Нажмите кнопку ниже, если хотите отключиться от преподавателя:_"
    await safe_edit_text(
        callback.message,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.MARKDOWN
    )


@router.callback_query(F.data == "back_to_main")
async def handle_back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text = "🏠 *Главное меню*\n\nВыберите формат тренировки:"
    await safe_edit_text(callback.message, text, reply_markup=Keyboards.main_menu(), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


def _build_stats_view(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    stats = db.get_user_stats(user_id)
    token_ver = db.get_user_token_version(user_id)
    reminders_on = db.get_user_reminders_enabled(user_id)

    token = generate_student_token(user_id, token_ver)
    
    safe_uid = urllib.parse.quote(str(user_id))
    safe_token = urllib.parse.quote(token)
    student_web_url = f"{DASHBOARD_URL}/?student={safe_uid}&token={safe_token}"

    reminder_label = "🔔 Напоминания: Вкл" if reminders_on else "🔕 Напоминания: Выкл"

    text = (
        "📊 *Ваша статистика изучения*\n\n"
        "🔥 *Регулярность и серия занятий:*\n"
        f"• Текущая серия: *{stats['current_streak']} дн. подряд* (рекорд: *{stats['best_streak']} дн.*)\n"
        f"• Действий сегодня: *{stats['today_actions']}* | Активных дней за год: *{stats['active_days_year']}*\n\n"
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
        [InlineKeyboardButton(text="🔄 Сбросить ссылку на аналитику", callback_data="stats:reset_prompt")],
        [
            InlineKeyboardButton(text=reminder_label, callback_data="stats:toggle_reminder"),
            InlineKeyboardButton(text="👨‍🏫 Преподаватели", callback_data="menu:my_teachers"),
        ],
        [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_to_main")]
    ])
    return text, keyboard


@router.callback_query(F.data == "menu:stats")
async def handle_show_stats(callback: CallbackQuery):
    text, keyboard = _build_stats_view(callback.from_user.id)
    await safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data == "stats:toggle_reminder")
async def handle_toggle_reminder(callback: CallbackQuery):
    new_state = db.toggle_user_reminders(callback.from_user.id)
    text, keyboard = _build_stats_view(callback.from_user.id)
    await safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    toast = "🔔 Ежедневные напоминания включены" if new_state else "🔕 Напоминания отключены"
    await callback.answer(toast, show_alert=False)


@router.callback_query(F.data == "stats:reset_prompt")
async def handle_stats_reset_prompt(callback: CallbackQuery):
    text = (
        "🔐 *Сброс персональной ссылки на аналитику*\n\n"
        "Вы уверены, что хотите выпустить новую ссылку?\n"
        "Предыдущая ссылка сразу перестанет открываться на всех устройствах."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, обновить ссылку", callback_data="stats:reset_confirm")],
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="menu:stats")],
    ])
    await safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data == "stats:reset_confirm")
async def handle_stats_reset_confirm(callback: CallbackQuery):
    user_id = callback.from_user.id
    db.rotate_user_token(user_id)
    text, keyboard = _build_stats_view(user_id)
    text = "✅ *Ваша ссылка на аналитику успешно обновлена! Старая ссылка больше недействительна.*\n\n" + text
    await safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    await callback.answer("Ссылка успешно обновлена!")


@router.callback_query(F.data == "menu:my_teachers")
async def handle_cb_my_teachers(callback: CallbackQuery):
    teachers = db.get_student_teachers(callback.from_user.id)
    if not teachers:
        await callback.answer("У вас пока нет прикреплённых преподавателей. Отправьте код учителя сообщением!", show_alert=True)
        return

    buttons = []
    lines = ["👨‍🏫 *Ваши преподаватели:*\n"]
    for idx, t in enumerate(teachers, 1):
        lines.append(f"{idx}. *{strip_md_delimiters(t['full_name'])}* (код `{escape_md(t['teacher_code'])}`)")
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ Отключить: {t['full_name'][:25]}",
                callback_data=f"teacher:unlink:{t['teacher_code']}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="⬅️ В статистику", callback_data="menu:stats")])
    text = "\n".join(lines) + "\n\n_Нажмите кнопку ниже, чтобы отключить преподавателя:_"
    await safe_edit_text(
        callback.message,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode=ParseMode.MARKDOWN
    )
    await callback.answer()


# ---------------------------------------------------------------------
# РЕЖИМ «РАБОТА НАД ОШИБКАМИ»
# ---------------------------------------------------------------------

def _build_errors_workout_screen(user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    problem_codes = db.get_problem_term_codes(user_id)
    if not problem_codes:
        text = (
            "🎉 *Отличный результат!*\n\n"
            "У вас сейчас нет проблемных терминов — все изученные понятия закреплены уверенно.\n\n"
            "Продолжайте тренировку в карточках или проверьте себя в тестах!"
        )
        return text, Keyboards.back_to_menu()

    count = len(problem_codes)
    preview_terms = []
    for c in problem_codes[:5]:
        t_obj = catalog.get_by_code(c)
        if t_obj:
            preview_terms.append(f"• {escape_md(t_obj['term'])}")
    preview_block = "\n".join(preview_terms)
    if count > 5:
        preview_block += f"\n_...и ещё {count - 5} шт._"

    text = (
        f"🛠 *Работа над ошибками*\n\n"
        f"Найдено понятий, требующих повторения: *{count}*.\n"
        f"Сюда попадают термины, в которых вы недавно допустили ошибку или отметили их как трудные:\n\n"
        f"{preview_block}\n\n"
        f"Выберите удобный формат проработки:"
    )
    return text, Keyboards.error_workout_menu(count)


@router.message(Command("work_errors"))
async def handle_work_errors_command(message: Message, state: FSMContext):
    if not message.from_user:
        return
    await state.clear()
    text, kb = _build_errors_workout_screen(message.from_user.id)
    await message.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data == "menu:errors")
async def handle_menu_errors(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = _build_errors_workout_screen(callback.from_user.id)
    await safe_edit_text(callback.message, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data.startswith("errors:start:"))
async def handle_errors_start(callback: CallbackQuery, state: FSMContext):
    parts = (callback.data or "").split(":")
    submode = parts[2] if len(parts) >= 3 else "cards"
    problem_codes = db.get_problem_term_codes(callback.from_user.id)
    if not problem_codes:
        await safe_edit_text(
            callback.message,
            "🎉 Все проблемные термины уже разобраны!",
            reply_markup=Keyboards.back_to_menu(),
            parse_mode=ParseMode.MARKDOWN
        )
        await callback.answer()
        return

    if submode == "cards":
        await state.update_data(mode="cards", module_filter=None, custom_codes=problem_codes, is_error_workout=True)
        await state.set_state(UserFlow.in_flashcards)
        await send_next_flashcard(callback, state)
    else:
        pool = list(problem_codes)
        random.shuffle(pool)
        await state.update_data(
            mode="tests",
            module_filter=None,
            custom_codes=problem_codes,
            is_error_workout=True,
            test_pool=pool,
            test_total_in_session=len(pool),
            test_session_correct=0,
            test_session_errors=[],
            test_session_times=[]
        )
        await state.set_state(UserFlow.in_tests)
        await send_next_test(callback, state)

    await callback.answer()


@router.callback_query(F.data.in_({"menu:cards", "menu:tests"}))
async def handle_menu_selection(callback: CallbackQuery, state: FSMContext):
    mode = (callback.data or "").split(":")[1]
    await state.update_data(mode=mode, custom_codes=None, is_error_workout=False)
    await state.set_state(UserFlow.selecting_topic)

    mode_name = "🗂 Карточки" if mode == "cards" else "🎯 Тесты"
    text = (
        f"Настройка режима *{mode_name}*:\n\n"
        "Выберите, какие термины тренировать:\n"
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

    await state.update_data(module_filter=module_filter, mode=mode, custom_codes=None, is_error_workout=False)

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
    
    if len(traits) > 10:
        traits_formatted += f"\n_...и ещё {len(traits)-10} признаков_"

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
        if len(types_list) > 8:
            types_str += f"\n_...и ещё {len(types_list)-8} форм_"
        extra_blocks.append(f"🧩 *Формы и классификации:*\n{types_str}")

    funcs_list = [str(x).strip() for x in (struct.get("functions_or_goals") or []) if str(x).strip()]
    if funcs_list:
        funcs_str = "\n".join([f"• {escape_md(truncate_plain(item, 220))}" for item in funcs_list[:8]])
        if len(funcs_list) > 8:
            funcs_str += f"\n_...и ещё {len(funcs_list)-8} функций_"
        extra_blocks.append(f"⚙️ *Функции в обществе:*\n{funcs_str}")

    ege = card.get("ege_context_and_examples") or {}
    rf_raw = str(ege.get("illustration_rf") or "").strip()
    if rf_raw:
        rf_safe = strip_md_delimiters(truncate_plain(rf_raw, 700))
        extra_blocks.append(f"🇷🇺 *Пример из реалий РФ:*\n_{rf_safe}_")

    pitfalls = [str(x).strip() for x in (card.get("common_pitfalls") or []) if str(x).strip()]
    if pitfalls:
        pitfalls_str = "\n".join([f"• {escape_md(truncate_plain(item, 250))}" for item in pitfalls[:6]])
        if len(pitfalls) > 6:
            pitfalls_str += f"\n_...и ещё {len(pitfalls)-6} ошибок_"
        extra_blocks.append(f"⚠️ *Ловушки и частые ошибки:*\n{pitfalls_str}")

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
    is_error_workout = data.get("is_error_workout", False)
    user_id = callback.from_user.id

    if not callback.message:
        await callback.answer("Сообщение устарело, начните заново.", show_alert=True)
        return

    custom_codes = None
    if is_error_workout:
        custom_codes = db.get_problem_term_codes(user_id)
        if not custom_codes:
            done_text = (
                "🎉 *Работа над ошибками завершена!*\n\n"
                "Вы успешно закрыли все проблемные термины. Отличная работа!"
            )
            await safe_edit_text(callback.message, done_text, reply_markup=Keyboards.back_to_menu(), parse_mode=ParseMode.MARKDOWN)
            return

    card = select_next_card(user_id=user_id, module_filter=module_filter, custom_codes=custom_codes)
    if not card:
        empty_text = "❌ В выбранном модуле пока нет терминов. Попробуйте выбрать «Все темы»."
        await safe_edit_text(callback.message, empty_text, reply_markup=Keyboards.back_to_menu(), parse_mode=None)
        return

    shown_at = time.time()
    await state.update_data(current_card_code=card["code"], shown_at=shown_at, revealed_elapsed=None)

    mod_title = strip_md_delimiters(card.get("module_title") or "Обществознание")
    term_title = strip_md_delimiters(str(card.get("term") or "").upper())
    workout_badge = "🛠 *Работа над ошибками*\n" if is_error_workout else ""

    text = (
        f"{workout_badge}🏷 *{mod_title}*\n\n"
        f"✨ *{term_title}*\n\n"
        f"💭 _Вспомните родовое понятие и ключевые признаки термина._"
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

    if not current_card_code or current_card_code != code:
        await callback.answer("Карточка уже оценена.", show_alert=False)
        return

    await state.update_data(current_card_code=None)

    shown_at = data.get("shown_at", time.time())
    revealed_elapsed = data.get("revealed_elapsed")
    elapsed = revealed_elapsed if revealed_elapsed is not None else round(max(0.0, time.time() - shown_at), 1)

    is_positive = (answer_type == "know_well")
    delta_ans = SpacedRepetitionAlgorithm.calculate_answer_delta(answer_type)
    delta_tm = SpacedRepetitionAlgorithm.calculate_time_delta(elapsed, is_correct=is_positive)

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
    is_error_workout = data.get("is_error_workout", False)

    if not callback.message:
        await callback.answer("Сообщение устарело.", show_alert=True)
        return

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
            f"Вы прошли все термины сессии ({total} из {total}).\n\n"
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
    workout_badge = "🛠 *Работа над ошибками*\n" if is_error_workout else ""
    text = (
        f"{workout_badge}🎯 *Тест: {mod_title}* `[{curr_num}/{total_num}]`\n\n"
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

    lines = ["📝 *Разбор ваших ошибок в этом тесте:*\n"]
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
    is_error_workout = data.get("is_error_workout", False)

    if is_error_workout:
        custom_codes = db.get_problem_term_codes(callback.from_user.id)
        if not custom_codes:
            await safe_edit_text(
                callback.message,
                "🎉 Все проблемные термины уже разобраны!",
                reply_markup=Keyboards.back_to_menu(),
                parse_mode=ParseMode.MARKDOWN
            )
            await callback.answer()
            return
        pool = list(custom_codes)
    else:
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
# 9. ФОНОВЫЕ ЗАДАЧИ И ТОЧКА ВХОДА (С ПОДДЕРЖКОЙ RENDER FREE WEB SERVICE)
# =====================================================================

async def process_daily_reminders(bot_instance: Bot, reference_date: Optional[date] = None) -> int:
    """
    Проверяет пользователей с активной серией занятий (current_streak >= 1), которые
    сегодня ещё не занимались, и отправляет им одно вежливое напоминание в день.
    """
    today_dt = reference_date or get_local_today()
    today_iso = today_dt.isoformat()
    candidates = db.get_users_for_daily_reminder(reference_date=today_dt)
    sent_count = 0

    for item in candidates:
        uid = item["user_id"]
        streak = item["current_streak"]
        text = (
            f"🔥 *Напоминание о вашей серии занятий!*\n\n"
            f"Ваша текущая серия: *{streak} дн. подряд*.\n"
            f"Сегодня вы ещё не решили ни одной карточки или теста. "
            f"Уделите 2–3 минуты тренировке, чтобы сохранить темп подготовки!\n\n"
            f"_Отключить уведомления можно в разделе «📊 Моя статистика»._"
        )
        try:
            await bot_instance.send_message(
                chat_id=uid,
                text=text,
                reply_markup=Keyboards.main_menu(),
                parse_mode=ParseMode.MARKDOWN
            )
            db.mark_reminder_sent(uid, today_iso)
            sent_count += 1
        except TelegramForbiddenError:
            # Пользователь заблокировал бота — просто помечаем, чтобы не спамить
            logger.info(f"Пользователь {uid} заблокировал бота. Напоминание не отправлено.")
            db.mark_reminder_sent(uid, today_iso)
        except Exception as e:
            logger.warning(f"Не удалось отправить напоминание пользователю {uid}: {e}")
            db.mark_reminder_sent(uid, today_iso)

    return sent_count


async def start_dummy_web_server(port: int = 10000):
    """
    Легковесный фоновый веб-сервер на чистом asyncio.
    Отвечает 200 OK на проверки работоспособности Render,
    чтобы бот мог работать на бесплатном тарифе Web Service ($0/мес).
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


async def periodic_background_task(bot_instance: Optional[Bot] = None):
    """
    Периодическая фоновая задача (каждый час):
    1) Очищает неактивных преподавателей без учеников старше 7 дней.
    2) Очищает старые сессии карточек (TTL 48 часов).
    3) В вечернее время (после 15:00 UTC / 18:00 МСК) рассылает мягкие напоминания о сохранении серии.
    """
    while True:
        try:
            await asyncio.sleep(3600)
            db.cleanup_inactive_teachers()
            session_tracker.cleanup_old_sessions()
            now_utc = datetime.now(timezone.utc)
            if bot_instance is not None and now_utc.hour >= 15:
                await process_daily_reminders(bot_instance, reference_date=get_local_today())
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Ошибка в periodic_background_task: {e}")


async def main():
    if not BOT_TOKEN:
        logger.error("Токен бота не найден в переменной BOT_TOKEN!")
        print("\n[!] ОШИБКА: Задайте переменную окружения BOT_TOKEN в файле .env или в переменных среды.\n")
        return

    catalog.load()
    db.init_db()

    port = int(os.getenv("PORT", "10000"))
    web_server = None
    try:
        web_server = await start_dummy_web_server(port)
    except Exception as e:
        logger.warning(f"Не удалось запустить фоновый веб-сервер на порту {port}: {e}")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    bg_task = asyncio.create_task(periodic_background_task(bot))

    logger.info("Бот версии 6.2 успешно запущен.")
    try:
        await dp.start_polling(bot)
    finally:
        bg_task.cancel()
        try:
            await bg_task
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
