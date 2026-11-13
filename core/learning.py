"""
Модуль алгоритмов обучения: SpacedRepetitionAlgorithm, TermsCatalog, SessionTracker,
TestGenerator и форматирование карточек (ОГЭ / ЕГЭ) (v7.6).
"""
import os
import json
import threading
import time
import random
import sqlite3
from collections import deque
from typing import Optional, Dict, Any, List, Tuple, Set

from core.common import (
    TERMS_JSON_PATH,
    DB_PATH,
    logger,
    escape_md,
    strip_md_delimiters,
    truncate_plain,
    _safe_json_loads,
    _normalize_search_str,
    _get_trigrams,
    _WORD_SPLIT_RE,
    _compute_similarity_preindexed,
    compute_fuzzy_similarity,
)


# =====================================================================
# 1. АЛГОРИТМ ИНТЕРВАЛЬНОГО ПОВТОРЕНИЯ ДЛЯ КАРТОЧЕК
# =====================================================================

class SpacedRepetitionAlgorithm:
    MIN_WEIGHT = 1.0
    MAX_WEIGHT = 10.0
    DEFAULT_WEIGHT = 5.0
    UNREVIEWED_PRIORITY_WEIGHT = 6.5

    MIN_TIME_WINDOW = 3.0
    MAX_TIME_WINDOW = 180.0

    FAST_THRESHOLD = 10.0
    SLOW_THRESHOLD = 40.0

    DELTA_ANSWER_MAP = {
        "know_well": -1.0,
        "correct": -1.0,
        "know_poorly": 0.5,
        "dont_know": 2.0,
        "incorrect": 2.0,
    }

    @classmethod
    def calculate_time_delta(
        cls, response_time_seconds: Optional[float], is_correct: bool = True
    ) -> float:
        if response_time_seconds is None or not is_correct:
            return 0.0
        if (
            response_time_seconds < cls.MIN_TIME_WINDOW
            or response_time_seconds > cls.MAX_TIME_WINDOW
        ):
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
    def update_weight(
        cls, current_weight: float, delta_answer: float, delta_time: float
    ) -> float:
        new_weight = current_weight + delta_answer + delta_time
        return max(cls.MIN_WEIGHT, min(cls.MAX_WEIGHT, round(new_weight, 2)))


# =====================================================================
# 2. КАТАЛОГ ТЕРМИНОВ И БЫСТРЫЙ НЕЧЁТКИЙ ПОИСК С ИНДЕКСОМ ТРИГРАММ
# =====================================================================

class TermsCatalog:
    def __init__(self, json_path: str = TERMS_JSON_PATH, sqlite_path: str = DB_PATH):
        self.json_path = json_path
        self.sqlite_path = sqlite_path
        self.terms_by_code: Dict[str, Dict[str, Any]] = {}
        self.terms_by_button_label: Dict[str, Dict[str, Any]] = {}
        self.terms_by_module: Dict[int, List[Dict[str, Any]]] = {
            1: [],
            2: [],
            3: [],
            4: [],
            5: [],
        }
        self.all_terms: List[Dict[str, Any]] = []
        self._search_index: List[
            Tuple[Dict[str, Any], str, str, Set[str], List[Tuple[str, Set[str]]]]
        ] = []

    def load(self):
        self.terms_by_code.clear()
        self.terms_by_button_label.clear()
        self.all_terms.clear()
        self._search_index.clear()
        if not hasattr(self, "_fuzzy_cache_lock"):
            self._fuzzy_cache_lock = threading.Lock()
        if not hasattr(self, "_fuzzy_cache"):
            self._fuzzy_cache: Dict[Tuple[str, int], List[Tuple[Dict[str, Any], float]]] = {}
            self._fuzzy_cache_max = 512
        with self._fuzzy_cache_lock:
            self._fuzzy_cache.clear()
        for m in list(self.terms_by_module.keys()):
            self.terms_by_module[m].clear()

        if os.path.exists(self.json_path):
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if (
                    isinstance(data, dict)
                    and "terms" in data
                    and isinstance(data["terms"], list)
                ):
                    data = data["terms"]
                if isinstance(data, list):
                    self._populate_terms(data)
                    if self.all_terms:
                        logger.info(
                            f"Загружено {len(self.all_terms)} терминов из {self.json_path}."
                        )
                        return
                logger.warning(
                    f"Файл {self.json_path} пуст или некорректен. Пробуем резерв из БД."
                )
            except Exception as e:
                logger.warning(
                    f"Ошибка чтения {self.json_path}: {e}. Пробуем резерв из БД."
                )

        if os.path.exists(self.sqlite_path):
            conn = None
            cur = None
            try:
                conn = sqlite3.connect(self.sqlite_path, timeout=10.0)
                cur = conn.cursor()
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='terms';"
                )
                if cur.fetchone():
                    cur.execute(
                        """
                        SELECT code, module_number, module_title, term, concept_explanation, 
                               genus, traits_json, structure_json, ege_context_json, pitfalls_json 
                        FROM terms ORDER BY code ASC
                        """
                    )
                    db_terms = []
                    for r in cur.fetchall():
                        db_terms.append(
                            {
                                "code": str(r[0] or "").strip(),
                                "module_number": int(r[1])
                                if r[1] and str(r[1]).isdigit()
                                else 1,
                                "module_title": str(r[2] or "").strip(),
                                "term": str(r[3] or "").strip(),
                                "concept_explanation": str(r[4] or "").strip(),
                                "genus": str(r[5] or "").strip(),
                                "traits": _safe_json_loads(r[6], []),
                                "structure_for_tests": _safe_json_loads(r[7], {}),
                                "ege_context_and_examples": _safe_json_loads(r[8], {}),
                                "common_pitfalls": _safe_json_loads(r[9], []),
                            }
                        )

                    self._populate_terms(db_terms)
                    logger.info(
                        f"Резервная загрузка: получено {len(self.all_terms)} терминов из {self.sqlite_path}."
                    )
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

        logger.error(
            f"Файл {self.json_path} не найден, и в базе нет таблицы terms!"
        )

    def _populate_terms(self, data: List[Dict[str, Any]]):
        for item in data:
            if not isinstance(item, dict):
                continue
            raw_code = (
                str(item.get("code") or "")
                .strip()
                .replace(";", "_")
                .replace(":", "_")
            )
            code = raw_code[:24]
            if not code or code in self.terms_by_code:
                continue

            try:
                mod_num = int(code.split(".")[0])
            except (ValueError, IndexError):
                mod_num = (
                    int(item.get("module_number") or 1)
                    if str(item.get("module_number") or "").isdigit()
                    else 1
                )

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
            pitfalls = (
                [str(p).strip() for p in raw_pitfalls if str(p or "").strip()]
                if isinstance(raw_pitfalls, list)
                else []
            )

            term_name = str(item.get("term") or "").strip() or f"Термин {code}"
            genus_val = (
                str(item.get("genus") or "").strip()
                or "Родовое понятие не указано"
            )
            mod_title = (
                str(item.get("module_title") or "").strip()
                or f"Модуль {mod_num}"
            )
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
            btn_label = f"📖 {truncate_plain(term_name, 32)}"
            if btn_label not in self.terms_by_button_label:
                self.terms_by_button_label[btn_label] = term_obj
            self.all_terms.append(term_obj)
            if mod_num in self.terms_by_module:
                self.terms_by_module[mod_num].append(term_obj)
            else:
                self.terms_by_module[mod_num] = [term_obj]

            t_norm = _normalize_search_str(term_name)
            t_tri = _get_trigrams(t_norm)
            t_words = [
                (w, _get_trigrams(w))
                for w in _WORD_SPLIT_RE.split(t_norm)
                if len(w) >= 3
            ]
            self._search_index.append(
                (term_obj, code.lower(), t_norm, t_tri, t_words)
            )

    def get_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        return self.terms_by_code.get(str(code).strip())

    def get_by_button_label(self, btn_text: str) -> Optional[Dict[str, Any]]:
        return self.terms_by_button_label.get(str(btn_text).strip())

    def get_terms(self, module_filter: Optional[int] = None) -> List[Dict[str, Any]]:
        if module_filter is not None:
            return self.terms_by_module.get(module_filter, [])
        return self.all_terms

    def search_terms_fuzzy(
        self, query: str, limit: int = 6
    ) -> List[Tuple[Dict[str, Any], float]]:
        clean_q = _normalize_search_str(str(query or "")[:60])
        if len(clean_q) < 3:
            return []

        if not hasattr(self, "_fuzzy_cache_lock"):
            self._fuzzy_cache_lock = threading.Lock()
        cache = getattr(self, "_fuzzy_cache", None)
        if cache is None:
            cache = {}
            self._fuzzy_cache = cache
            self._fuzzy_cache_max = 512

        cache_key = (clean_q, int(limit))
        with self._fuzzy_cache_lock:
            cached = cache.get(cache_key)
            if cached is not None:
                return list(cached)

        q_tri = _get_trigrams(clean_q)
        scored: List[Tuple[Dict[str, Any], float]] = []

        if len(self._search_index) == len(self.all_terms) and self._search_index:
            for t, code_low, t_norm, t_tri, t_words in self._search_index:
                if clean_q == code_low:
                    score = 1.0
                else:
                    score = _compute_similarity_preindexed(
                        clean_q, q_tri, t_norm, t_tri, t_words, threshold=0.45
                    )
                if score >= 0.55:
                    scored.append((t, score))
        else:
            for t in self.all_terms:
                score = compute_fuzzy_similarity(clean_q, t["term"])
                if clean_q == t["code"].lower():
                    score = 1.0
                if score >= 0.55:
                    scored.append((t, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        res = scored[:limit]
        with self._fuzzy_cache_lock:
            if len(cache) >= getattr(self, "_fuzzy_cache_max", 512):
                try:
                    cache.pop(next(iter(cache)), None)
                except Exception:
                    cache.clear()
            cache[cache_key] = res
        return list(res)


catalog = TermsCatalog()


# =====================================================================
# 3. СЕССИОННЫЙ МЕНЕДЖЕР
# =====================================================================

class SessionTracker:
    def __init__(self, max_users: int = 10000):
        self.recent_history: Dict[int, deque] = {}
        self.last_seen: Dict[int, float] = {}
        self.max_users = max_users

    def push(self, user_id: int, code: str):
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

    def clear_user(self, user_id: int):
        self.recent_history.pop(user_id, None)
        self.last_seen.pop(user_id, None)

    def cleanup_old_sessions(self, max_age_seconds: int = 172800):
        now = time.time()
        to_remove = [
            uid for uid, last_t in self.last_seen.items() if now - last_t > max_age_seconds
        ]
        for uid in to_remove:
            self.recent_history.pop(uid, None)
            self.last_seen.pop(uid, None)


session_tracker = SessionTracker()


# =====================================================================
# 4. ГЕНЕРАТОР ТЕСТОВ
# =====================================================================

class TestGenerator:
    @staticmethod
    def _pick_unique_distractors(
        distractors: List[Dict[str, Any]], count: int, correct_term: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
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
            unique_pool.append(
                {
                    "code": f"fallback_{fallback_idx}",
                    "term": f"Вариант {fallback_idx}",
                    "genus": f"Альтернативная родовая характеристика {fallback_idx}",
                    "traits": [f"Альтернативный признак {fallback_idx}"],
                }
            )
            fallback_idx += 1
        return unique_pool

    @staticmethod
    def generate(
        correct_term: Dict[str, Any], module_filter: Optional[int] = None
    ) -> Dict[str, Any]:
        all_pool = catalog.get_terms(module_filter)
        if len(all_pool) < 4:
            all_pool = catalog.get_terms(None)

        distractors = [t for t in all_pool if t["code"] != correct_term["code"]]
        if len(distractors) < 3:
            distractors = [
                t for t in catalog.all_terms if t["code"] != correct_term["code"]
            ]

        real_traits = [
            str(t).strip()
            for t in (correct_term.get("traits") or [])
            if str(t).strip()
        ]
        real_traits_lower = {t.lower() for t in real_traits}

        sampled_distractors = (
            random.sample(distractors, min(len(distractors), 15))
            if distractors
            else []
        )
        foreign_candidates: List[Tuple[Dict[str, Any], str]] = []
        for d in sampled_distractors:
            d_traits = [
                str(t).strip() for t in (d.get("traits") or []) if str(t).strip()
            ]
            for dt in d_traits:
                if dt.lower() not in real_traits_lower:
                    foreign_candidates.append((d, dt))

        available_types = [1, 2]
        if len(real_traits) >= 3 and foreign_candidates:
            available_types.append(3)

        struct = correct_term.get("structure_for_tests") or {}
        types_or_forms = [
            str(x).strip()
            for x in (struct.get("types_or_forms") or [])
            if str(x).strip()
        ]
        functions_or_goals = [
            str(x).strip()
            for x in (struct.get("functions_or_goals") or [])
            if str(x).strip()
        ]

        if types_or_forms:
            available_types.append(4)
        if functions_or_goals:
            available_types.append(5)

        chosen_type = random.choice(available_types)
        explanation_note = ""

        if chosen_type == 1:
            traits_str = (
                "\n".join([f"• {escape_md(t)}" for t in real_traits[:3]])
                if real_traits
                else f"• {escape_md(correct_term.get('genus') or 'Признаки понятия')}"
            )
            genus_escaped = strip_md_delimiters(
                correct_term.get("genus") or "Общее понятие"
            )
            question_text = (
                f"К какому понятию относятся следующие ключевые признаки:\n\n"
                f"{traits_str}\n\n"
                f"Родовое понятие: _{genus_escaped}_"
            )
            picked = TestGenerator._pick_unique_distractors(
                distractors, 3, correct_term
            )
            options = [
                {
                    "id": "A",
                    "code": correct_term["code"],
                    "text": correct_term["term"],
                    "is_correct": True,
                },
                {
                    "id": "B",
                    "code": picked[0]["code"],
                    "text": picked[0]["term"],
                    "is_correct": False,
                },
                {
                    "id": "C",
                    "code": picked[1]["code"],
                    "text": picked[1]["term"],
                    "is_correct": False,
                },
                {
                    "id": "D",
                    "code": picked[2]["code"],
                    "text": picked[2]["term"],
                    "is_correct": False,
                },
            ]

        elif chosen_type == 2:
            term_escaped = strip_md_delimiters(correct_term["term"].upper())
            question_text = f"Выберите верную родовую характеристику термина *{term_escaped}*:"

            target_genus = str(
                correct_term.get("genus") or "Родовое понятие"
            ).strip()
            seen_genera = {target_genus.lower()}
            valid_distractors = []

            pool_for_genus = (
                catalog.all_terms
                if len(catalog.all_terms) >= 4
                else distractors
            )
            shuffled_pool = (
                random.sample(pool_for_genus, len(pool_for_genus))
                if pool_for_genus
                else []
            )
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
                fallback_picks = TestGenerator._pick_unique_distractors(
                    distractors, 3, correct_term
                )
                for fp in fallback_picks:
                    if fp not in valid_distractors:
                        valid_distractors.append(fp)
                    if len(valid_distractors) == 3:
                        break

            options = [
                {
                    "id": "A",
                    "code": correct_term["code"],
                    "text": target_genus,
                    "is_correct": True,
                },
                {
                    "id": "B",
                    "code": valid_distractors[0]["code"],
                    "text": valid_distractors[0].get("genus") or "Вариант 1",
                    "is_correct": False,
                },
                {
                    "id": "C",
                    "code": valid_distractors[1]["code"],
                    "text": valid_distractors[1].get("genus") or "Вариант 2",
                    "is_correct": False,
                },
                {
                    "id": "D",
                    "code": valid_distractors[2]["code"],
                    "text": valid_distractors[2].get("genus") or "Вариант 3",
                    "is_correct": False,
                },
            ]

        elif chosen_type == 3:
            term_escaped = strip_md_delimiters(correct_term["term"].upper())
            question_text = (
                f"Все перечисленные ниже признаки, за исключением одного, относятся к понятию *{term_escaped}*.\n\n"
                f"Найдите выпадающий (чужой) признак:"
            )
            unique_real_traits = []
            seen_rt = set()
            for rt in random.sample(real_traits, len(real_traits)):
                if rt.lower() not in seen_rt:
                    seen_rt.add(rt.lower())
                    unique_real_traits.append(rt)
                if len(unique_real_traits) == 3:
                    break
            while len(unique_real_traits) < 3:
                unique_real_traits.append(
                    f"Признак понятия ({len(unique_real_traits) + 1})"
                )

            foreign_term, foreign_trait = random.choice(foreign_candidates)
            explanation_note = f"Лишний признак: «{foreign_trait}» (относится к понятию «{foreign_term['term']}»)."

            options = [
                {
                    "id": "A",
                    "code": correct_term["code"],
                    "text": foreign_trait,
                    "is_correct": True,
                },
                {
                    "id": "B",
                    "code": correct_term["code"],
                    "text": unique_real_traits[0],
                    "is_correct": False,
                },
                {
                    "id": "C",
                    "code": correct_term["code"],
                    "text": unique_real_traits[1],
                    "is_correct": False,
                },
                {
                    "id": "D",
                    "code": correct_term["code"],
                    "text": unique_real_traits[2],
                    "is_correct": False,
                },
            ]

        elif chosen_type == 4:
            types_str = strip_md_delimiters(", ".join(types_or_forms[:3]))
            question_text = f"Для какого из терминов характерны следующие формы/виды: _{types_str}_?"
            picked = TestGenerator._pick_unique_distractors(
                distractors, 3, correct_term
            )
            options = [
                {
                    "id": "A",
                    "code": correct_term["code"],
                    "text": correct_term["term"],
                    "is_correct": True,
                },
                {
                    "id": "B",
                    "code": picked[0]["code"],
                    "text": picked[0]["term"],
                    "is_correct": False,
                },
                {
                    "id": "C",
                    "code": picked[1]["code"],
                    "text": picked[1]["term"],
                    "is_correct": False,
                },
                {
                    "id": "D",
                    "code": picked[2]["code"],
                    "text": picked[2]["term"],
                    "is_correct": False,
                },
            ]

        else:
            funcs_str = strip_md_delimiters(", ".join(functions_or_goals[:3]))
            question_text = f"Какое понятие выполняет в обществе следующие функции: _{funcs_str}_?"
            picked = TestGenerator._pick_unique_distractors(
                distractors, 3, correct_term
            )
            options = [
                {
                    "id": "A",
                    "code": correct_term["code"],
                    "text": correct_term["term"],
                    "is_correct": True,
                },
                {
                    "id": "B",
                    "code": picked[0]["code"],
                    "text": picked[0]["term"],
                    "is_correct": False,
                },
                {
                    "id": "C",
                    "code": picked[1]["code"],
                    "text": picked[1]["term"],
                    "is_correct": False,
                },
                {
                    "id": "D",
                    "code": picked[2]["code"],
                    "text": picked[2]["term"],
                    "is_correct": False,
                },
            ]

        random.shuffle(options)

        option_lines = []
        for idx, opt in enumerate(options, start=1):
            option_lines.append(f"*{idx})* {escape_md(opt['text'])}")
        question_text += "\n\n" + "\n".join(option_lines)

        return {
            "chosen_type": chosen_type,
            "question_text": question_text,
            "options": options,
            "correct_term": correct_term,
            "explanation_note": explanation_note,
        }


# =====================================================================
# 5. ФОРМАТИРОВАНИЕ КАРТОЧЕК И ТЕОРЕТИЧЕСКОГО ДОСЬЕ (ОГЭ / ЕГЭ)
# =====================================================================

def format_card_back(
    card: Dict[str, Any], show_detail: bool = False, exam_level: str = "ege"
) -> str:
    is_oge = str(exam_level or "ege").strip().lower() == "oge"
    traits = [str(t).strip() for t in (card.get("traits") or []) if str(t).strip()]

    mod_title = strip_md_delimiters(
        truncate_plain(card.get("module_title") or "Обществознание", 120)
    )
    term_title = strip_md_delimiters(
        truncate_plain(str(card.get("term") or "").upper(), 150)
    )

    if is_oge:
        genus_text = strip_md_delimiters(
            truncate_plain(card.get("genus") or "Родовое понятие не указано", 240)
        )
        traits_formatted = (
            "\n".join([f"• {escape_md(truncate_plain(t, 220))}" for t in traits[:2]])
            if traits
            else "• Признаки уточняются"
        )
        base_text = (
            f"🏷 *{mod_title} (ОГЭ)*\n\n"
            f"🎓 *{term_title}*\n\n"
            f"🏛 *Родовое понятие:*\n_{genus_text}_\n\n"
            f"📌 *Ключевые признаки:*\n{traits_formatted}"
        )
        if not show_detail:
            return base_text

        ege = card.get("ege_context_and_examples") or {}
        rf_raw = str(ege.get("illustration_rf") or "").strip()
        if not rf_raw:
            rf_raw = str(card.get("concept_explanation") or "").strip()
        if rf_raw:
            rf_safe = strip_md_delimiters(truncate_plain(rf_raw, 650))
            return f"{base_text}\n\n🇷🇺 *Пример из реалий РФ:*\n_{rf_safe}_"
        return base_text

    genus_text = strip_md_delimiters(
        truncate_plain(card.get("genus") or "Родовое понятие не указано", 400)
    )
    traits_formatted = (
        "\n".join([f"• {escape_md(truncate_plain(t, 300))}" for t in traits[:10]])
        if traits
        else "• Признаки уточняются"
    )

    if len(traits) > 10:
        traits_formatted += f"\n_...и ещё {len(traits) - 10} признаков_"

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
    types_list = [
        str(x).strip()
        for x in (struct.get("types_or_forms") or [])
        if str(x).strip()
    ]
    if types_list:
        types_str = "\n".join(
            [f"• {escape_md(truncate_plain(item, 220))}" for item in types_list[:8]]
        )
        if len(types_list) > 8:
            types_str += f"\n_...и ещё {len(types_list) - 8} форм_"
        extra_blocks.append(f"🧩 *Формы и классификации:*\n{types_str}")

    funcs_list = [
        str(x).strip()
        for x in (struct.get("functions_or_goals") or [])
        if str(x).strip()
    ]
    if funcs_list:
        funcs_str = "\n".join(
            [f"• {escape_md(truncate_plain(item, 220))}" for item in funcs_list[:8]]
        )
        if len(funcs_list) > 8:
            funcs_str += f"\n_...и ещё {len(funcs_list) - 8} функций_"
        extra_blocks.append(f"⚙️ *Функции в обществе:*\n{funcs_str}")

    ege = card.get("ege_context_and_examples") or {}
    rf_raw = str(ege.get("illustration_rf") or "").strip()
    if rf_raw:
        rf_safe = strip_md_delimiters(truncate_plain(rf_raw, 700))
        extra_blocks.append(f"🇷🇺 *Пример из реалий РФ:*\n_{rf_safe}_")

    pitfalls = [
        str(x).strip()
        for x in (card.get("common_pitfalls") or [])
        if str(x).strip()
    ]
    if pitfalls:
        pitfalls_str = "\n".join(
            [f"• {escape_md(truncate_plain(item, 250))}" for item in pitfalls[:6]]
        )
        if len(pitfalls) > 6:
            pitfalls_str += f"\n_...и ещё {len(pitfalls) - 6} ошибок_"
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
