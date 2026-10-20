"""
Telegram Bot для изучения терминов обществознания (Версия 3 — Исправленная)
=============================================================================
Архитектура:
1. Термины загружаются из terms.json (при его отсутствии — резервная автозагрузка из social_bot.db).
2. В Neon (PostgreSQL) или SQLite сохраняются user_id и логированная строка ответа:
   f"{code};{delta_answer};{delta_time};{time_seconds}".
3. Двойной режим базы:
   - Если задана переменная DATABASE_URL -> прямое подключение к Neon (PostgreSQL).
   - Если DATABASE_URL не задана -> локальный резерв SQLite (social_bot.db).
4. Алгоритм интервального повторения:
   - Кулдаун карточек: карточка не повторяется минимум 10 шагов.
   - Приоритет непросмотренным терминам (эффективный вес 6.5 vs базовый 5.0).
   - 2 веса: ответ (-1.0 / +0.5 / +2.0) и время реакции (бонус -1.0 только при верных ответах).
5. 5 сбалансированных типов тестов с уникальными дистракторами и защитой от дублирования вариантов.
6. Чистый пользовательский интерфейс.
"""

import os
import sys
import json
import time
import random
import logging
import sqlite3
from typing import Optional, Dict, Any, List, Tuple
from collections import deque

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("social_bot")

# =====================================================================
# 2. АЛГОРИТМ ИНТЕРВАЛЬНОГО ПОВТОРЕНИЯ
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
        "know_well": -1.0,   # «Знаю хорошо»
        "correct": -1.0,     # Верный ответ в тесте
        "know_poorly": 0.5,  # «Знаю плохо»
        "dont_know": 2.0,    # «Не знаю»
        "incorrect": 2.0,    # Ошибка в тесте
    }

    @classmethod
    def calculate_time_delta(cls, response_time_seconds: Optional[float], is_correct: bool = True) -> float:
        """
        Учитывает время реакции:
        - Бонус за скорость (-1.0) начисляется ТОЛЬКО при правильном/уверенном ответе.
        - Быстрый неверный ответ или быстрый клик 'Не знаю' не должны уменьшать вес карточки.
        """
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

                    # Создаем terms.json для последующих быстрых запусков
                    try:
                        with open(self.json_path, "w", encoding="utf-8") as f:
                            json.dump(db_terms, f, ensure_ascii=False, indent=2)
                        logger.info(f"Автоматически сгенерирован файл {self.json_path}.")
                    except Exception:
                        pass
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
    В базе данных сохраняются user_id и сериализованные логи ответов.
    Формат записи entry: "{code};{delta_answer};{delta_time};{time_seconds}"
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
                entry TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_user_answers_user ON user_answers(user_id);
            """)
        else:
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                entry TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_user_answers_user ON user_answers(user_id);
            """)

        conn.commit()
        cursor.close()
        conn.close()
        mode_str = "Neon PostgreSQL" if self.is_postgres else f"SQLite ({self.sqlite_path})"
        logger.info(f"База данных успешно инициализирована в режиме: {mode_str}.")

    def save_answer_entry(self, user_id: int, entry_str: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        if self.is_postgres:
            cursor.execute(
                "INSERT INTO user_answers (user_id, entry) VALUES (%s, %s);",
                (user_id, entry_str)
            )
        else:
            cursor.execute(
                "INSERT INTO user_answers (user_id, entry) VALUES (?, ?);",
                (user_id, entry_str)
            )
        conn.commit()
        cursor.close()
        conn.close()

    def get_user_progress_map(self, user_id: int) -> Dict[str, Dict[str, Any]]:
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

        total_answers = len(rows)
        valid_times = []
        for r in rows:
            entry_str = r[0] if self.is_postgres else r["entry"]
            parts = str(entry_str).split(";")
            if len(parts) >= 4:
                try:
                    sec = float(parts[3])
                    if 3.0 <= sec <= 180.0:
                        valid_times.append(sec)
                except ValueError:
                    pass

        avg_time = round(sum(valid_times) / len(valid_times), 1) if valid_times else 0.0

        learned = sum(1 for p in progress_map.values() if p["total_weight"] <= 3.0)
        in_progress = sum(1 for p in progress_map.values() if 3.0 < p["total_weight"] <= 7.0)
        needs_review = sum(1 for p in progress_map.values() if p["total_weight"] > 7.0)

        return {
            "total_answers": total_answers,
            "avg_time": avg_time,
            "learned": learned,
            "in_progress": in_progress,
            "needs_review": needs_review,
        }


db = Database()

# =====================================================================
# 5. СЕССИОННЫЙ МЕНЕДЖЕР
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
    available_terms = catalog.get_terms(module_filter)
    if not available_terms:
        return None

    progress_map = db.get_user_progress_map(user_id)
    recent_codes = set(session_tracker.get_recent(user_id))

    candidates = [t for t in available_terms if t["code"] not in recent_codes]
    if not candidates:
        candidates = available_terms

    def get_sort_weight(term_dict: Dict[str, Any]) -> float:
        code = term_dict["code"]
        if code not in progress_map or progress_map[code]["review_count"] == 0:
            return SpacedRepetitionAlgorithm.UNREVIEWED_PRIORITY_WEIGHT
        return progress_map[code]["total_weight"]

    sorted_candidates = sorted(
        candidates,
        key=lambda t: get_sort_weight(t) + random.uniform(-0.1, 0.1),
        reverse=True
    )

    chosen = sorted_candidates[0]
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

        # ТИП 2: По термину выбрать определение / родовую характеристику (исключаем одинаковые genus)
        elif chosen_type == 2:
            question_text = f"Выбери верную родовую характеристику термина *{correct_term['term'].upper()}*:"
            
            # Собираем дистракторы с уникальными родовыми понятиями
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

            # Fallback
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
            [InlineKeyboardButton(text="🎯 Тесты (Проверка знаний)", callback_data="menu:tests")],
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
            [InlineKeyboardButton(text="🔍 Показать определение и признаки", callback_data=f"card:reveal:{code}")],
            [InlineKeyboardButton(text="🚪 Завершить тренировку", callback_data="back_to_main")]
        ])

    @staticmethod
    def flashcard_back(code: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Знаю хорошо", callback_data=f"card:ans:{code}:know_well"),
                InlineKeyboardButton(text="⚠️ Знаю плохо", callback_data=f"card:ans:{code}:know_poorly"),
            ],
            [
                InlineKeyboardButton(text="❌ Не знаю", callback_data=f"card:ans:{code}:dont_know")
            ],
            [
                InlineKeyboardButton(text="🚪 Завершить тренировку", callback_data="back_to_main")
            ]
        ])

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
    text = (
        "👋 *Добро пожаловать в тренажер терминов по обществознанию!*\n\n"
        "Здесь ты сможешь эффективно выучить понятия кодификатора ЕГЭ/ОГЭ "
        "с помощью *интервального повторения*.\n\n"
        "Алгоритм подстраивается под твой прогресс:\n"
        "• Карточки с ошибками появляются чаще, а надежно выученные уходят в редкий показ;\n"
        "• Учитывается не только правильность ответа, но и уверенность (скорость реакции).\n\n"
        "Выбери формат тренировки:"
    )
    await message.answer(text, reply_markup=Keyboards.main_menu(), parse_mode=ParseMode.MARKDOWN)


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

    text = (
        "📊 *Твоя статистика изучения*\n\n"
        f"• Всего ответов дано: *{stats['total_answers']}*\n"
        f"• Среднее время ответа: *{stats['avg_time']} сек*\n\n"
        "📈 *Статус терминов:*\n"
        f"• 🟢 Выучено надежно: *{stats['learned']}*\n"
        f"• 🟡 В процессе: *{stats['in_progress']}*\n"
        f"• 🔴 Требуют повторения: *{stats['needs_review']}*"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=Keyboards.back_to_menu(), parse_mode=ParseMode.MARKDOWN)
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
        await send_next_test(callback, state)

    await callback.answer()


# ---------------------------------------------------------------------
# КАРТОЧКИ
# ---------------------------------------------------------------------

async def send_next_flashcard(callback: CallbackQuery, state: FSMContext, feedback_header: str = ""):
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

    prefix = f"{feedback_header}\n\n" if feedback_header else ""
    text = (
        f"{prefix}"
        f"📚 *{card['module_title']}*\n\n"
        f"Термин:\n"
        f"👉 *{card['term'].upper()}*\n\n"
        f"_Вспомни родовое понятие и не менее 3 признаков понятия. "
        f"Затем нажми кнопку ниже, чтобы проверить себя._"
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

    traits = card.get("traits", [])
    traits_formatted = "\n".join([f"• {t}" for t in traits]) if traits else "• Признаки уточняются"

    text = (
        f"📚 *{card['term'].upper()}*\n\n"
        f"🔹 *Родовое понятие:*\n_{card['genus']}_\n\n"
        f"🔹 *Ключевые признаки:*\n{traits_formatted}\n\n"
        f"💡 *Определение:*\n{card['concept_explanation']}\n\n"
        "Честно оцени, насколько хорошо ты помнил этот термин:"
    )

    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=Keyboards.flashcard_back(code),
            parse_mode=ParseMode.MARKDOWN
        )
    await callback.answer()


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

    entry_str = f"{code};{delta_ans};{delta_tm};{elapsed}"
    db.save_answer_entry(callback.from_user.id, entry_str)

    if answer_type == "know_well":
        feedback = "✅ *Отлично!* Термин надежно закреплен."
    elif answer_type == "know_poorly":
        feedback = "⚠️ *Понятно.* Мы повторим этот термин в ближайшее время."
    else:
        feedback = "❌ *Ничего страшного!* Мы добавили термин в частый цикл повторений."

    await send_next_flashcard(callback, state, feedback_header=feedback)
    await callback.answer()


# ---------------------------------------------------------------------
# ТЕСТЫ
# ---------------------------------------------------------------------

async def send_next_test(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    module_filter = data.get("module_filter")
    user_id = callback.from_user.id

    card = select_next_card(user_id=user_id, module_filter=module_filter)
    if not card:
        empty_text = "❌ В выбранном модуле пока недостаточно терминов."
        if callback.message:
            await callback.message.edit_text(empty_text, reply_markup=Keyboards.back_to_menu())
        return

    test_data = TestGenerator.generate(card, module_filter)
    shown_at = time.time()
    await state.update_data(current_test_data=test_data, test_shown_at=shown_at)

    text = (
        f"🎯 *Тест: {card['module_title']}*\n\n"
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

    # Очищаем test_data от повторных кликов
    await state.update_data(current_test_data=None)

    chosen_option = test_data["options"][option_idx]
    is_correct = chosen_option["is_correct"]
    answer_type = "correct" if is_correct else "incorrect"

    delta_ans = SpacedRepetitionAlgorithm.calculate_answer_delta(answer_type)
    delta_tm = SpacedRepetitionAlgorithm.calculate_time_delta(elapsed, is_correct=is_correct)

    entry_str = f"{code};{delta_ans};{delta_tm};{elapsed}"
    db.save_answer_entry(callback.from_user.id, entry_str)

    correct_term = test_data["correct_term"]
    if is_correct:
        status_text = "🎉 *Верно!* Отличная работа."
    else:
        if test_data.get("explanation_note"):
            status_text = f"❌ *Неверно.*\n💡 {test_data['explanation_note']}"
        else:
            status_text = f"❌ *Неверно.* Правильный ответ относится к термину: *{correct_term['term']}*."

    text = (
        f"{status_text}\n\n"
        f"💡 *Определение {correct_term['term']}:*\n{correct_term['concept_explanation']}"
    )

    if callback.message:
        await callback.message.edit_text(text, reply_markup=Keyboards.test_next(), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()


@router.callback_query(F.data == "test:next")
async def handle_test_next(callback: CallbackQuery, state: FSMContext):
    await send_next_test(callback, state)
    await callback.answer()


# =====================================================================
# 9. ТОЧКА ВХОДА
# =====================================================================

async def main():
    if not BOT_TOKEN:
        logger.error("Токен бота не найден в переменной BOT_TOKEN!")
        print("\n[!] ОШИБКА: Задай переменную окружения BOT_TOKEN в файле .env или в переменных среды.\n")
        return

    catalog.load()
    db.init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Бот версии 3 успешно запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")