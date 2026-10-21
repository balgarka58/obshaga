import io
import os
import re
import sys
import json
import html
import hmac
import hashlib
import sqlite3
import warnings
import urllib.parse
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List, Tuple
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import plotly.express as px

warnings.filterwarnings("ignore", category=UserWarning, module="pandas")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =====================================================================
# 1. НАСТРОЙКА СТРАНИЦЫ, БЕЗОПАСНОСТИ И ТОКЕНОВ (ВЕРСИЯ 6.2)
# =====================================================================

st.set_page_config(
    page_title="Аналитика терминов по обществознанию",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed"  # Сайдбар полностью скрыт
)

# Полностью скрываем стандартный сайдбар и меню Streamlit
st.markdown("""
<style>
#MainMenu {visibility: hidden; display: none !important;}
header {visibility: hidden; display: none !important;}
footer {visibility: hidden; display: none !important;}
[data-testid="stToolbar"] {visibility: hidden; display: none !important;}
[data-testid="stDecoration"] {visibility: hidden; display: none !important;}
[data-testid="stStatusWidget"] {visibility: hidden; display: none !important;}
[data-testid="stSidebar"] {display: none !important;} /* Скрываем сайдбар */
.viewerBadge_container__1QSob {display: none !important;}
div[class*="viewerBadge"] {display: none !important;}
div[class*="ProfileBadge"] {display: none !important;}
#manage-app-button {display: none !important;}
</style>
""", unsafe_allow_html=True)

DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH = os.getenv("DB_PATH", "social_bot.db")
TERMS_JSON_PATH = os.getenv("TERMS_JSON_PATH", "terms.json")
SECRET_KEY = os.getenv("SECRET_KEY", "obshaga_secret_key_2025")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "obshaga_admin_2025")
TIMEZONE_STR = os.getenv("TIMEZONE", "Europe/Moscow")

try:
    TZ = ZoneInfo(TIMEZONE_STR)
except Exception:
    TZ = timezone.utc

TIME_WINDOW_OPTIONS = [
    "За всё время",
    "Сегодня",
    "Последние 7 дней",
    "Последние 30 дней",
    "Этот учебный год",
    "Свой диапазон дат",
]

WEEKDAY_NAMES_RU = [
    "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"
]
WEEKDAY_SHORT_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTH_SHORT_RU = {
    1: "Янв", 2: "Фев", 3: "Мар", 4: "Апр", 5: "Май", 6: "Июн",
    7: "Июл", 8: "Авг", 9: "Сен", 10: "Окт", 11: "Ноя", 12: "Дек"
}


def verify_secret(provided: Optional[str], expected: str) -> bool:
    """Защищённое от timing-атак сравнение секретов и токенов."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(
        str(provided).strip().encode("utf-8"),
        str(expected).strip().encode("utf-8")
    )


def _get_query_param(query_params: Any, key: str) -> Optional[str]:
    """Безопасное извлечение строкового GET-параметра из st.query_params."""
    if query_params is None:
        return None
    val = query_params.get(key)
    if isinstance(val, (list, tuple)):
        val = val[0] if val else None
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def generate_student_token(user_id: Any, token_version: int = 1, secret_key: Optional[str] = None) -> str:
    """Генерирует персональный 8-символьный токен доступа ученика к веб-аналитике."""
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


# =====================================================================
# 2. МЕТАДАННЫЕ ТЕРМИНОВ (JSON ИЛИ РЕЗЕРВ ИЗ SQLITE)
# =====================================================================

@st.cache_data
def load_terms_metadata() -> Dict[str, Dict[str, str]]:
    modules_map = {
        1: "1. Человек и общество",
        2: "2. Экономика",
        3: "3. Социальные отношения",
        4: "4. Политика",
        5: "5. Право"
    }
    terms_dict: Dict[str, Dict[str, str]] = {}

    if os.path.exists(TERMS_JSON_PATH):
        try:
            with open(TERMS_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("terms"), list):
                data = data["terms"]
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    code = str(item.get("code") or "").strip().replace(";", "_").replace(":", "_")[:24]
                    if not code:
                        continue
                    try:
                        mod_num = int(code.split(".")[0])
                    except (ValueError, IndexError):
                        mod_num = int(item.get("module_number") or 1) if str(item.get("module_number") or "").isdigit() else 1

                    terms_dict[code] = {
                        "term": str(item.get("term") or f"Термин {code}").strip(),
                        "module": modules_map.get(mod_num, f"Модуль {mod_num}"),
                        "genus": str(item.get("genus") or "").strip()
                    }
            if terms_dict:
                return terms_dict
        except Exception:
            pass

    if os.path.exists(DB_PATH):
        conn = None
        cur = None
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10.0)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='terms';")
            if cur.fetchone():
                cur.execute("SELECT code, module_number, term, genus FROM terms;")
                for r in cur.fetchall():
                    code = str(r[0] or "").strip()
                    if not code:
                        continue
                    mod_num = int(r[1]) if r[1] and str(r[1]).isdigit() else 1
                    terms_dict[code] = {
                        "term": str(r[2] or f"Термин {code}").strip(),
                        "module": modules_map.get(mod_num, f"Модуль {mod_num}"),
                        "genus": str(r[3] or "").strip()
                    }
        except Exception:
            pass
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

    return terms_dict


TERMS_MAP = load_terms_metadata()

# =====================================================================
# 3. ПОДКЛЮЧЕНИЕ К БАЗЕ ДАННЫХ И ПРОВЕРКА ВЕРСИЙ ТОКЕНОВ
# =====================================================================

def get_db_connection():
    db_url = os.getenv("DATABASE_URL")
    if db_url and db_url.startswith("postgres"):
        import psycopg2
        return psycopg2.connect(db_url, connect_timeout=10), True
    else:
        sqlite_path = os.getenv("DB_PATH", "social_bot.db")
        conn = sqlite3.connect(sqlite_path, timeout=10.0)
        return conn, False


def get_current_student_token_version(student_id: str) -> int:
    """Получает актуальную версию токена ученика напрямую из БД (без кэша)."""
    uid_clean = str(student_id or "").strip()
    if not uid_clean.isdigit():
        return 1
    conn = None
    cur = None
    try:
        conn, is_postgres = get_db_connection()
        cur = conn.cursor()
        if is_postgres:
            cur.execute("SELECT token_version FROM user_settings WHERE user_id = %s;", (int(uid_clean),))
        else:
            cur.execute("SELECT token_version FROM user_settings WHERE user_id = ?;", (int(uid_clean),))
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 1
    except Exception:
        return 1
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


def get_current_teacher_token_version(teacher_code: str) -> int:
    """Получает актуальную версию токена преподавателя напрямую из БД (без кэша)."""
    conn = None
    cur = None
    try:
        conn, is_postgres = get_db_connection()
        cur = conn.cursor()
        if is_postgres:
            cur.execute("SELECT COALESCE(token_version, 1) FROM teachers WHERE teacher_code = %s;", (str(teacher_code),))
        else:
            cur.execute("SELECT COALESCE(token_version, 1) FROM teachers WHERE teacher_code = ?;", (str(teacher_code),))
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 1
    except Exception:
        return 1
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


def verify_student_access(student_id: str, provided_token: Optional[str]) -> bool:
    """Проверяет токен ученика с учётом актуальной версии token_version."""
    uid_clean = str(student_id or "").strip()
    if not uid_clean.isdigit():
        return False
    ver = get_current_student_token_version(uid_clean)
    expected = generate_student_token(uid_clean, ver)
    return verify_secret(provided_token, expected)


def verify_teacher_access(teacher_code: str, provided_token: Optional[str]) -> bool:
    """Проверяет доступ к кабинету преподавателя по защищённому токену версии token_version."""
    code_clean = str(teacher_code or "").strip()
    if not code_clean or not provided_token:
        return False
    ver = get_current_teacher_token_version(code_clean)
    expected = generate_teacher_token(code_clean, ver)
    return verify_secret(provided_token, expected)


@st.cache_data(ttl=15)
def fetch_all_data() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Загружает логи ответов, список учителей и связи ученик-учитель с нормализацией типов."""
    COLUMNS = [
        "user_id", "username", "full_name", "mode", "code", "term",
        "module", "delta_ans", "delta_time", "time_sec", "status",
        "is_problem", "created_at"
    ]
    TEACHER_COLUMNS = ["teacher_code", "user_id", "full_name", "token_version", "created_at"]
    ST_COLUMNS = ["student_id", "teacher_code", "joined_at"]

    try:
        conn, is_postgres = get_db_connection()
    except Exception as e:
        st.error(f"Не удалось подключиться к базе данных: {e}")
        return (
            pd.DataFrame(columns=COLUMNS),
            pd.DataFrame(columns=TEACHER_COLUMNS),
            pd.DataFrame(columns=ST_COLUMNS),
        )

    answers_df = pd.DataFrame()
    teachers_df = pd.DataFrame(columns=TEACHER_COLUMNS)
    st_teachers_df = pd.DataFrame(columns=ST_COLUMNS)

    try:
        try:
            query_answers = "SELECT user_id, username, full_name, entry, created_at FROM user_answers ORDER BY created_at DESC;"
            answers_df = pd.read_sql(query_answers, conn)
        except Exception:
            try:
                query_answers_legacy = "SELECT user_id, username, entry, created_at FROM user_answers ORDER BY created_at DESC;"
                answers_df = pd.read_sql(query_answers_legacy, conn)
                answers_df["full_name"] = None
            except Exception:
                answers_df = pd.DataFrame()

        try:
            query_teachers = "SELECT teacher_code, user_id, full_name, COALESCE(token_version, 1) AS token_version, created_at FROM teachers;"
            t_raw = pd.read_sql(query_teachers, conn)
            if not t_raw.empty:
                t_raw["teacher_code"] = t_raw["teacher_code"].astype(str).str.strip()
                t_raw["user_id"] = t_raw["user_id"].astype(str).str.strip()
                t_raw["full_name"] = t_raw["full_name"].fillna("").astype(str).str.strip()
                t_raw["token_version"] = pd.to_numeric(t_raw["token_version"], errors="coerce").fillna(1).astype(int)
                teachers_df = t_raw
        except Exception:
            try:
                query_teachers_legacy = "SELECT teacher_code, user_id, full_name, created_at FROM teachers;"
                t_raw = pd.read_sql(query_teachers_legacy, conn)
                if not t_raw.empty:
                    t_raw["teacher_code"] = t_raw["teacher_code"].astype(str).str.strip()
                    t_raw["user_id"] = t_raw["user_id"].astype(str).str.strip()
                    t_raw["full_name"] = t_raw["full_name"].fillna("").astype(str).str.strip()
                    t_raw["token_version"] = 1
                    teachers_df = t_raw
            except Exception:
                teachers_df = pd.DataFrame(columns=TEACHER_COLUMNS)

        try:
            query_st = "SELECT student_id, teacher_code, joined_at FROM student_teachers;"
            st_raw = pd.read_sql(query_st, conn)
            if not st_raw.empty:
                st_raw["student_id"] = st_raw["student_id"].astype(str).str.strip()
                st_raw["teacher_code"] = st_raw["teacher_code"].astype(str).str.strip()
                st_teachers_df = st_raw
        except Exception:
            st_teachers_df = pd.DataFrame(columns=ST_COLUMNS)

    finally:
        conn.close()

    if answers_df.empty:
        return pd.DataFrame(columns=COLUMNS), teachers_df, st_teachers_df

    if "username" not in answers_df.columns:
        answers_df["username"] = None
    if "full_name" not in answers_df.columns:
        answers_df["full_name"] = None

    parsed_rows = []
    for _, row in answers_df.iterrows():
        entry_str = str(row.get("entry") or "")
        parts = entry_str.split(";")
        if len(parts) >= 4:
            code = parts[0].strip()
            if not code:
                continue
            try:
                d_ans = float(parts[1])
                d_time = float(parts[2])
                sec = max(0.0, float(parts[3]))
            except ValueError:
                continue

            raw_mode = parts[4].strip() if len(parts) >= 5 else "card"
            # Единообразие с ботом: в режиме тестов ответы быстрее 3.0 сек считаются мискликами и не учитываются
            if raw_mode == "test" and sec < 3.0:
                continue

            mode_display = "Тесты" if raw_mode == "test" else "Карточки"

            term_info = TERMS_MAP.get(code, {
                "term": f"Код {code}",
                "module": f"Модуль {code.split('.')[0] if '.' in code else '?'}"
            })

            if d_ans <= 0:
                result_status = "Верно / Знаю"
                is_problem = False
            elif d_ans < 1.5:
                result_status = "Неуверенно"
                is_problem = True
            else:
                result_status = "Ошибка / Не знаю"
                is_problem = True

            raw_username = row.get("username")
            username_val = str(raw_username).strip() if pd.notna(raw_username) and raw_username is not None else ""
            if username_val in ["None", "nan", ""]:
                username_val = None

            raw_fname = row.get("full_name")
            fname_val = str(raw_fname).strip() if pd.notna(raw_fname) and raw_fname is not None else ""
            if fname_val in ["None", "nan", ""]:
                fname_val = username_val or f"ID: {row['user_id']}"

            parsed_rows.append({
                "user_id": str(row["user_id"]).strip(),
                "username": username_val,
                "full_name": fname_val,
                "mode": mode_display,
                "code": code,
                "term": term_info["term"],
                "module": term_info["module"],
                "delta_ans": d_ans,
                "delta_time": d_time,
                "time_sec": sec,
                "status": result_status,
                "is_problem": is_problem,
                "created_at": row["created_at"]
            })

    if not parsed_rows:
        return pd.DataFrame(columns=COLUMNS), teachers_df, st_teachers_df

    return pd.DataFrame(parsed_rows), teachers_df, st_teachers_df


# =====================================================================
# 4. ФИЛЬТРАЦИЯ ПО ВРЕМЕННОМУ ОКНУ И КАЛЕНДАРЬ АКТИВНОСТИ
# =====================================================================

def _normalize_timestamps(series: pd.Series) -> pd.Series:
    """Безопасно приводит столбец created_at к Timestamp в целевом часовом поясе."""
    parsed = pd.to_datetime(series, utc=True, errors="coerce")
    # Конвертируем в локальный пояс и убираем tzinfo для совместимости с наивными датами
    return parsed.dt.tz_convert(TZ).dt.tz_localize(None)


def filter_df_by_time_window(
    df: pd.DataFrame,
    period_choice: str = "За всё время",
    custom_dates: Optional[Any] = None,
    reference_now: Optional[datetime] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, str]:
    """Фильтрует DataFrame по выбранному временному окну."""
    if df.empty or period_choice == "За всё время":
        return df.copy(), pd.DataFrame(columns=df.columns), "За всё время"

    now_dt = reference_now or datetime.now(TZ).replace(tzinfo=None)
    if isinstance(now_dt, pd.Timestamp):
        now_dt = now_dt.to_pydatetime().replace(tzinfo=None)
    today_start = datetime(now_dt.year, now_dt.month, now_dt.day)

    work_df = df.copy()
    ts_col = _normalize_timestamps(work_df["created_at"])
    work_df["_ts"] = ts_col.fillna(pd.Timestamp(now_dt))

    prev_df = pd.DataFrame(columns=df.columns)
    period_label = period_choice

    if period_choice == "Сегодня":
        start_dt = today_start
        prev_start = start_dt - timedelta(days=1)
        mask = work_df["_ts"] >= start_dt
        prev_mask = (work_df["_ts"] >= prev_start) & (work_df["_ts"] < start_dt)
        filtered = work_df[mask].drop(columns=["_ts"])
        prev_df = work_df[prev_mask].drop(columns=["_ts"])
        period_label = f"Сегодня ({today_start.strftime('%d.%m.%Y')})"

    elif period_choice == "Последние 7 дней":
        start_dt = today_start - timedelta(days=6)
        prev_start = start_dt - timedelta(days=7)
        mask = work_df["_ts"] >= start_dt
        prev_mask = (work_df["_ts"] >= prev_start) & (work_df["_ts"] < start_dt)
        filtered = work_df[mask].drop(columns=["_ts"])
        prev_df = work_df[prev_mask].drop(columns=["_ts"])
        period_label = "Последние 7 дней"

    elif period_choice == "Последние 30 дней":
        start_dt = today_start - timedelta(days=29)
        prev_start = start_dt - timedelta(days=30)
        mask = work_df["_ts"] >= start_dt
        prev_mask = (work_df["_ts"] >= prev_start) & (work_df["_ts"] < start_dt)
        filtered = work_df[mask].drop(columns=["_ts"])
        prev_df = work_df[prev_mask].drop(columns=["_ts"])
        period_label = "Последние 30 дней"

    elif period_choice == "Этот учебный год":
        acad_year = now_dt.year if now_dt.month >= 9 else now_dt.year - 1
        start_dt = datetime(acad_year, 9, 1)
        mask = work_df["_ts"] >= start_dt
        filtered = work_df[mask].drop(columns=["_ts"])
        period_label = f"Учебный год (с 01.09.{acad_year})"

    elif period_choice == "Свой диапазон дат" and custom_dates:
        if isinstance(custom_dates, (list, tuple)) and len(custom_dates) >= 2:
            d_from, d_to = custom_dates[0], custom_dates[1]
        elif isinstance(custom_dates, (list, tuple)) and len(custom_dates) == 1:
            d_from = d_to = custom_dates[0]
        elif not isinstance(custom_dates, (list, tuple)):
            d_from = d_to = custom_dates
        else:
            d_from = d_to = None

        if d_from is None or d_to is None:
            return work_df.drop(columns=["_ts"]), prev_df, "За всё время"

        if isinstance(d_from, datetime):
            d_from = d_from.date()
        if isinstance(d_to, datetime):
            d_to = d_to.date()
        if d_from > d_to:
            d_from, d_to = d_to, d_from

        start_dt = datetime(d_from.year, d_from.month, d_from.day, 0, 0, 0)
        end_dt = datetime(d_to.year, d_to.month, d_to.day, 23, 59, 59, 999999)
        span_days = max(1, (d_to - d_from).days + 1)
        prev_start = start_dt - timedelta(days=span_days)

        mask = (work_df["_ts"] >= start_dt) & (work_df["_ts"] <= end_dt)
        prev_mask = (work_df["_ts"] >= prev_start) & (work_df["_ts"] < start_dt)
        filtered = work_df[mask].drop(columns=["_ts"])
        prev_df = work_df[prev_mask].drop(columns=["_ts"])
        period_label = f"{d_from.strftime('%d.%m.%Y')} — {d_to.strftime('%d.%m.%Y')}"
    else:
        filtered = work_df.drop(columns=["_ts"])

    return filtered, prev_df, period_label


def build_calendar_data(
    unfiltered_df: pd.DataFrame,
    counter_mode: str = "Все режимы",
    end_date: Optional[date] = None
) -> Dict[str, Any]:
    """Формирует полную 53-недельную сетку активности за последние 365 дней."""
    today_d = end_date or datetime.now(TZ).date()
    if isinstance(today_d, datetime):
        today_d = today_d.date()

    mode_norm = str(counter_mode or "Все режимы").strip()
    if mode_norm in ["Только карточки", "Карточки"]:
        mode_label = "Только карточки"
        mode_df = unfiltered_df[unfiltered_df["mode"] == "Карточки"].copy() if not unfiltered_df.empty else pd.DataFrame()
    elif mode_norm in ["Только тесты", "Тесты"]:
        mode_label = "Только тесты"
        mode_df = unfiltered_df[unfiltered_df["mode"] == "Тесты"].copy() if not unfiltered_df.empty else pd.DataFrame()
    else:
        mode_label = "Все действия"
        mode_df = unfiltered_df.copy()

    end_week_monday = today_d - timedelta(days=today_d.weekday())
    start_monday = end_week_monday - timedelta(weeks=52)

    daily_counts: Dict[date, int] = {}
    daily_cards: Dict[date, int] = {}
    daily_tests: Dict[date, int] = {}
    daily_errors: Dict[date, int] = {}

    if not mode_df.empty and "created_at" in mode_df.columns:
        ts_series = _normalize_timestamps(mode_df["created_at"]).fillna(pd.Timestamp(today_d))
        mode_df["_date"] = ts_series.dt.date
        for d_val, grp in mode_df.groupby("_date"):
            cnt = len(grp)
            daily_counts[d_val] = cnt
            daily_cards[d_val] = int((grp["mode"] == "Карточки").sum())
            daily_tests[d_val] = int((grp["mode"] == "Тесты").sum())
            daily_errors[d_val] = int(grp["is_problem"].sum())

    active_dates = sorted([d for d, c in daily_counts.items() if c > 0])
    year_start_365 = today_d - timedelta(days=364)
    active_days_year = sum(1 for d in active_dates if year_start_365 <= d <= today_d)
    total_actions_year = sum(c for d, c in daily_counts.items() if year_start_365 <= d <= today_d)

    best_streak = 0
    if active_dates:
        best_streak = 1
        cur_run = 1
        for i in range(1, len(active_dates)):
            diff = (active_dates[i] - active_dates[i - 1]).days
            if diff == 1:
                cur_run += 1
                if cur_run > best_streak:
                    best_streak = cur_run
            elif diff > 1:
                cur_run = 1

    if today_d in daily_counts and daily_counts[today_d] > 0:
        chk = today_d
    elif (today_d - timedelta(days=1)) in daily_counts and daily_counts[today_d - timedelta(days=1)] > 0:
        chk = today_d - timedelta(days=1)
    else:
        chk = None

    current_streak = 0
    while chk is not None and daily_counts.get(chk, 0) > 0:
        current_streak += 1
        chk -= timedelta(days=1)

    def _get_level(cnt: int) -> int:
        if cnt <= 0:
            return 0
        if cnt <= 4:
            return 1
        if cnt <= 14:
            return 2
        if cnt <= 29:
            return 3
        return 4

    def _plural_actions(n: int) -> str:
        n_abs = abs(n) % 100
        n1 = n_abs % 10
        if 11 <= n_abs <= 19:
            return "действий"
        if n1 == 1:
            return "действие"
        if 2 <= n1 <= 4:
            return "действия"
        return "действий"

    weeks_count = 53
    z_matrix = [[0 for _ in range(weeks_count)] for _ in range(7)]
    hover_matrix = [["" for _ in range(weeks_count)] for _ in range(7)]
    cells: List[Dict[str, Any]] = []
    month_labels: Dict[int, str] = {}
    last_month = None

    for w_idx in range(weeks_count):
        week_monday = start_monday + timedelta(weeks=w_idx)
        for d_idx in range(7):
            cur_d = week_monday + timedelta(days=d_idx)
            if cur_d.day <= 7 and cur_d.month != last_month and d_idx == 0:
                month_labels[w_idx] = MONTH_SHORT_RU.get(cur_d.month, "")
                last_month = cur_d.month

            is_future = cur_d > today_d
            cnt = 0 if is_future else daily_counts.get(cur_d, 0)
            c_cards = 0 if is_future else daily_cards.get(cur_d, 0)
            c_tests = 0 if is_future else daily_tests.get(cur_d, 0)
            c_err = 0 if is_future else daily_errors.get(cur_d, 0)
            acc = round((1 - c_err / cnt) * 100, 1) if cnt > 0 else 0.0

            w_name = WEEKDAY_NAMES_RU[d_idx]
            d_fmt = cur_d.strftime("%d.%m.%Y")
            act_word = _plural_actions(cnt)

            if is_future:
                tooltip = f"{d_fmt} ({w_name}) — будущая дата"
            elif cnt == 0:
                tooltip = f"{d_fmt} ({w_name}): 0 действий ({mode_label})"
            else:
                if mode_label == "Все действия":
                    tooltip = (
                        f"{d_fmt} ({w_name}): {cnt} {act_word} "
                        f"(Карточек: {c_cards}, Тестов: {c_tests} | Точность: {acc}%)"
                    )
                else:
                    tooltip = (
                        f"{d_fmt} ({w_name}): {cnt} {act_word} "
                        f"({mode_label} | Точность: {acc}%)"
                    )

            lvl = _get_level(cnt) if not is_future else -1
            z_matrix[d_idx][w_idx] = cnt
            hover_matrix[d_idx][w_idx] = tooltip

            cells.append({
                "week_idx": w_idx,
                "weekday_idx": d_idx,
                "weekday_name": w_name,
                "date": cur_d.isoformat(),
                "date_formatted": d_fmt,
                "count": cnt,
                "cards_count": c_cards,
                "tests_count": c_tests,
                "accuracy": acc,
                "level": lvl,
                "is_future": is_future,
                "tooltip": tooltip,
            })

    return {
        "mode_label": mode_label,
        "start_date": start_monday.isoformat(),
        "end_date": today_d.isoformat(),
        "weeks_count": weeks_count,
        "current_streak": current_streak,
        "best_streak": best_streak,
        "active_days_year": active_days_year,
        "total_actions_year": total_actions_year,
        "z_matrix": z_matrix,
        "hover_matrix": hover_matrix,
        "cells": cells,
        "month_labels": month_labels,
    }


def build_calendar_html(cal_data: Dict[str, Any]) -> str:
    """Генерирует адаптивную сетку календаря активности (HTML/CSS) с всплывающими подсказками."""
    level_colors = {
        -1: "rgba(255, 255, 255, 0.02)",
        0: "#161b22",
        1: "#0e4429",
        2: "#006d32",
        3: "#26a641",
        4: "#39d353",
    }
    weeks_count = cal_data["weeks_count"]
    month_labels = cal_data["month_labels"]

    cell_size = 11
    gap = 2
    label_width = 24
    total_width = label_width + (cell_size + gap) * weeks_count

    month_cells_html = [f'<div style="width: {label_width}px;"></div>']
    for w_idx in range(weeks_count):
        m_txt = html.escape(month_labels.get(w_idx, ""))
        month_cells_html.append(
            f'<div style="width: {cell_size}px; font-size: 9px; color: #94a3b8; text-align: left; overflow: visible; white-space: nowrap;">{m_txt}</div>'
        )

    grid_by_pos = {(c["weekday_idx"], c["week_idx"]): c for c in cal_data["cells"]}
    rows_html = []
    for d_idx in range(7):
        day_label = WEEKDAY_SHORT_RU[d_idx] if d_idx in (0, 2, 4, 6) else ""
        row_squares = [
            f'<div style="width: {label_width}px; font-size: 9px; color: #94a3b8; line-height: {cell_size}px; padding-right: 4px; flex-shrink: 0;">{day_label}</div>'
        ]
        for w_idx in range(weeks_count):
            cell = grid_by_pos[(d_idx, w_idx)]
            color = level_colors.get(cell["level"], "#161b22")
            border = "1px solid rgba(255,255,255,0.06)" if cell["level"] <= 0 else "1px solid rgba(255,255,255,0.15)"
            safe_tip = html.escape(cell["tooltip"], quote=True)
            
            safe_mode = html.escape(cal_data['mode_label'], quote=True)
            click_action = f"showCalInfo('{cell['date_formatted']}', '{cell['count']}', '{cell['cards_count']}', '{cell['tests_count']}', '{cell['accuracy']}', '{safe_mode}')"
            
            row_squares.append(
                f'<div title="{safe_tip}" data-date="{cell["date"]}" data-count="{cell["count"]}" '
                f'onclick="{click_action}" '
                f'style="width: {cell_size}px; height: {cell_size}px; border-radius: 2px; background: {color}; border: {border}; cursor: pointer; flex-shrink: 0;"></div>'
            )
        rows_html.append(
            f'<div style="display: flex; gap: {gap}px; align-items: center; margin-bottom: {gap}px;">{"".join(row_squares)}</div>'
        )

    legend_squares = "".join([
        f'<span style="display:inline-block; width:{cell_size}px; height:{cell_size}px; border-radius:2px; background:{level_colors[lvl]}; border:1px solid rgba(255,255,255,0.1);"></span>'
        for lvl in [0, 1, 2, 3, 4]
    ])

    html_template = """
    <div style="padding: 12px; background: rgba(15, 23, 42, 0.65); border-radius: 12px; border: 1px solid rgba(255, 255, 255, 0.1); overflow-x: auto; margin-top: 8px; font-family: sans-serif;">
        <div style="min-width: {{TOTAL_WIDTH}}px;">
            <div style="display: flex; gap: {{GAP}}px; margin-bottom: 4px;">
                {{MONTH_CELLS}}
            </div>
            <div>
                {{ROWS_HTML}}
            </div>
            <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 8px; font-size: 11px; color: #94a3b8; flex-wrap: wrap; gap: 4px;">
                <div>Нажмите или наведите на ячейку, чтобы увидеть детали.</div>
                <div style="display: flex; align-items: center; gap: 4px;">
                    <span>Меньше</span>
                    {{LEGEND_SQUARES}}
                    <span>Больше</span>
                </div>
            </div>
        </div>
        
        <div id="cal-info" style="margin-top: 12px; padding: 10px; background: rgba(255,255,255,0.05); border-radius: 8px; font-size: 13px; color: #e2e8f0; display: none;">
            Нажмите на клетку, чтобы увидеть детали.
        </div>

        <script>
            function showCalInfo(date, count, cards, tests, acc, mode) {
                var infoBox = document.getElementById('cal-info');
                if (infoBox) {
                    if (count === '0') {
                        infoBox.innerHTML = '📅 <b>' + date + '</b> | ⚡️ Пройдено: <b>0</b> действий (' + mode + ')';
                    } else if (mode === 'Все действия') {
                        infoBox.innerHTML = '📅 <b>' + date + '</b> | ⚡️ Пройдено: <b>' + count + '</b> (Карточек: ' + cards + ', Тестов: ' + tests + ' | Точность: ' + acc + '%)';
                    } else {
                        infoBox.innerHTML = '📅 <b>' + date + '</b> | ⚡️ Пройдено: <b>' + count + '</b> (' + mode + ' | Точность: ' + acc + '%)';
                    }
                    infoBox.style.display = 'block';
                }
            }
        </script>
    </div>
    """

    return (
        html_template
        .replace("{{TOTAL_WIDTH}}", str(total_width))
        .replace("{{GAP}}", str(gap))
        .replace("{{MONTH_CELLS}}", "".join(month_cells_html))
        .replace("{{ROWS_HTML}}", "".join(rows_html))
        .replace("{{LEGEND_SQUARES}}", legend_squares)
    )


def render_activity_calendar(
    unfiltered_date_df: pd.DataFrame,
    default_mode: str = "Все режимы",
    key_prefix: str = "gh"
):
    """Отрисовывает в нижней части страницы годовой календарь активности."""
    st.divider()
    st.subheader("🟩 Календарь активности за год")
    
    if unfiltered_date_df.empty:
        st.info("ℹ️ Пока нет данных для отображения календаря.")
        return

    counter_options = ["Все режимы", "Только карточки", "Только тесты"]
    default_idx = counter_options.index(default_mode) if default_mode in counter_options else 0

    if hasattr(st, "radio"):
        chosen_counter = st.radio(
            "Счётчик календаря активности:",
            counter_options,
            index=default_idx,
            horizontal=True,
            key=f"{key_prefix}_calendar_counter"
        )
    else:
        chosen_counter = default_mode

    cal_data = build_calendar_data(unfiltered_date_df, counter_mode=chosen_counter)

    c_s1, c_s2, c_s3, c_s4 = st.columns(4)
    c_s1.metric("🔥 Текущая серия", f"{cal_data['current_streak']} дн.")
    c_s2.metric("🏆 Рекорд серии", f"{cal_data['best_streak']} дн.")
    c_s3.metric("🗓 Активных дней за год", f"{cal_data['active_days_year']} из 365")
    c_s4.metric(f"⚡️ Всего за год ({cal_data['mode_label']})", cal_data["total_actions_year"])

    components.html(build_calendar_html(cal_data), height=280, scrolling=False)


# =====================================================================
# 5. ГЕНЕРАЦИЯ PDF-ОТЧЁТА УСПЕВАЕМОСТИ
# =====================================================================

def _find_cyrillic_font() -> Optional[str]:
    """Ищет системный или встроенный в matplotlib TTF-шрифт с поддержкой кириллицы."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    try:
        import matplotlib
        mpl_ttf = os.path.join(os.path.dirname(matplotlib.__file__), "mpl-data", "fonts", "ttf", "DejaVuSans.ttf")
        candidates.insert(0, mpl_ttf)
    except Exception:
        pass

    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def generate_pdf_report(
    title: str,
    subtitle: str,
    df: pd.DataFrame,
    period_label: str = "За всё время",
    class_summary_df: Optional[pd.DataFrame] = None
) -> bytes:
    """Формирует PDF-отчёт успеваемости (для ученика или класса) с поддержкой кириллицы."""
    total_answers = len(df)
    valid_times = df[(df["time_sec"] >= 0) & (df["time_sec"] <= 180.0)]["time_sec"] if not df.empty else pd.Series(dtype=float)
    avg_time = round(float(valid_times.mean()), 1) if not valid_times.empty else 0.0
    errors_count = int(df["is_problem"].sum()) if not df.empty else 0
    accuracy = round((1 - (errors_count / total_answers)) * 100, 1) if total_answers > 0 else 0.0

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)

        font_path = _find_cyrillic_font()
        font_name = "Helvetica"
        if font_path:
            try:
                pdfmetrics.registerFont(TTFont("CyrillicSans", font_path))
                font_name = "CyrillicSans"
            except Exception:
                font_name = "Helvetica"

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "RepTitle", parent=styles["Normal"], fontName=font_name, fontSize=16, leading=20, spaceAfter=6, textColor=colors.HexColor("#1e293b")
        )
        sub_style = ParagraphStyle(
            "RepSub", parent=styles["Normal"], fontName=font_name, fontSize=10, leading=14, spaceAfter=14, textColor=colors.HexColor("#475569")
        )
        h2_style = ParagraphStyle(
            "RepH2", parent=styles["Normal"], fontName=font_name, fontSize=12, leading=16, spaceBefore=12, spaceAfter=8, textColor=colors.HexColor("#0f172a")
        )
        cell_style = ParagraphStyle(
            "RepCell", parent=styles["Normal"], fontName=font_name, fontSize=9, leading=11
        )

        story = []
        story.append(Paragraph(html.escape(title), title_style))
        story.append(Paragraph(html.escape(f"{subtitle} | Период: {period_label}"), sub_style))

        story.append(Paragraph("1. Ключевые показатели успеваемости", h2_style))
        kpi_data = [
            ["Всего ответов", "Точность (%)", "Среднее время", "Ошибок / затруднений"],
            [str(total_answers), f"{accuracy}%", f"{avg_time} сек", str(errors_count)]
        ]
        kpi_table = Table(kpi_data, colWidths=[125, 125, 125, 140])
        kpi_table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name), ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(kpi_table)
        story.append(Spacer(1, 14))

        if not df.empty:
            story.append(Paragraph("2. Топ трудных терминов (требующих повторения)", h2_style))
            df_calc = df.copy()
            df_calc["time_sec_clamped"] = df_calc["time_sec"].clip(0, 180)
            term_problems = df_calc.groupby(["code", "term", "module"]).agg(
                total_tries=("status", "count"), errors=("is_problem", "sum"), avg_seconds=("time_sec_clamped", "mean")
            ).reset_index()
            term_problems["error_rate"] = (term_problems["errors"] / term_problems["total_tries"] * 100).round(1)
            term_problems["avg_seconds"] = term_problems["avg_seconds"].round(1)
            top_prob = term_problems.sort_values(by=["errors", "error_rate"], ascending=False).head(10)

            prob_rows = [["Код", "Термин", "Модуль", "Ошибок", "Попыток", "% ошибок"]]
            for _, r in top_prob.iterrows():
                prob_rows.append([
                    str(r["code"]), Paragraph(html.escape(str(r["term"])[:60]), cell_style),
                    Paragraph(html.escape(str(r["module"])[:35]), cell_style), str(int(r["errors"])), str(int(r["total_tries"])), f"{r['error_rate']}%",
                ])
            prob_table = Table(prob_rows, colWidths=[45, 165, 130, 55, 60, 60])
            prob_table.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), font_name), ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")), ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.append(prob_table)
            story.append(Spacer(1, 14))

        if class_summary_df is not None and not class_summary_df.empty:
            story.append(Paragraph("3. Сводная ведомость учеников класса", h2_style))
            cls_rows = [["Ученик", "Юзернейм", "Ответов", "Точность (%)", "Ошибок", "Ср. время (с)"]]
            for _, r in class_summary_df.iterrows():
                cls_rows.append([
                    Paragraph(html.escape(str(r.get("Ученик", ""))[:45]), cell_style), str(r.get("Юзернейм", "—")),
                    str(r.get("Всего ответов", 0)), f"{r.get('Точность (%)', 0.0)}%", str(r.get("Ошибок", 0)), str(r.get("Ср. время (сек)", 0.0)),
                ])
            cls_table = Table(cls_rows, colWidths=[150, 90, 65, 75, 60, 75])
            cls_table.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), font_name), ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")), ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 5),
            ]))
            story.append(cls_table)

        doc.build(story)
        return buf.getvalue()
    except Exception:
        fallback_pdf = (
            b"%PDF-1.4\n"
            b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
            b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >> endobj\n"
            b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
            b"0000000058 00000 n \n0000000115 00000 n \n"
            b"trailer << /Size 4 /Root 1 0 R >>\nstartxref\n190\n%%EOF\n"
        )
        return fallback_pdf


# =====================================================================
# 6. КОМПОНЕНТЫ ИНТЕРФЕЙСА (ПОВТОРНО ИСПОЛЬЗУЕМЫЙ ВИЗУАЛ)
# =====================================================================

def build_user_avatar_html(full_name: str, username: Optional[str], user_id: str) -> str:
    """Формирует безопасный HTML-бейдж пользователя."""
    safe_name = html.escape(str(full_name or "").strip() or f"ID: {user_id}")
    safe_uid = html.escape(str(user_id or "").strip())
    raw_uname = str(username or "").replace("@", "").strip()
    clean_uname = raw_uname if re.match(r"^[A-Za-z0-9_]{3,32}$", raw_uname) else ""
    safe_uname = html.escape(clean_uname)
    alnum_words = ["".join(ch for ch in part if ch.isalnum()) for part in str(full_name or "").split()]
    alnum_words = [w for w in alnum_words if w]
    initials = "".join(w[0].upper() for w in alnum_words[:2]) if alnum_words else "УЧ"
    encoded_initials = urllib.parse.quote(initials, safe="")
    fallback_url = f"https://ui-avatars.com/api/?name={encoded_initials}&background=4f46e5&color=fff&size=128"

    if clean_uname:
        primary_url = f"https://t.me/i/userpic/320/{clean_uname}.jpg"
        img_tag = f'<img src="{primary_url}" onerror="this.onerror=null; this.src=\'{fallback_url}\';" style="width: 48px; height: 48px; border-radius: 50%; object-fit: cover; border: 2px solid #6366f1;" />'
    else:
        img_tag = f'<img src="{fallback_url}" style="width: 48px; height: 48px; border-radius: 50%; object-fit: cover; border: 2px solid #6366f1;" />'

    subtitle = f"@{safe_uname}" if safe_uname else f"ID: {safe_uid}"
    return f"""
    <div style="display: flex; align-items: center; gap: 14px; margin-bottom: 20px; padding: 12px 16px; background: rgba(255, 255, 255, 0.04); border-radius: 12px; border: 1px solid rgba(255, 255, 255, 0.1);">
        {img_tag}
        <div>
            <div style="font-size: 1.15rem; font-weight: 600; line-height: 1.2;">{safe_name}</div>
            <div style="font-size: 0.88rem; color: #94a3b8;">{subtitle}</div>
        </div>
    </div>
    """


def render_user_avatar_badge(full_name: str, username: Optional[str], user_id: str):
    """Отображает компактный аватар из Telegram CDN с fallback на инициалы."""
    st.markdown(build_user_avatar_html(full_name, username, user_id), unsafe_allow_html=True)


def build_class_summary_df(class_student_ids: List[str], class_df: pd.DataFrame) -> pd.DataFrame:
    """Формирует сводную таблицу успеваемости класса по всем прикреплённым ученикам."""
    summary_rows = []
    for uid in class_student_ids:
        u_df = class_df[class_df["user_id"] == uid] if not class_df.empty else pd.DataFrame()
        if not u_df.empty:
            r0 = u_df.iloc[0]
            u_total = len(u_df)
            u_err = int(u_df["is_problem"].sum())
            u_acc = round((1 - (u_err / u_total)) * 100, 1) if u_total > 0 else 0.0
            u_valid_time = u_df[(u_df["time_sec"] >= 0) & (u_df["time_sec"] <= 180.0)]["time_sec"]
            u_time = round(float(u_valid_time.mean()), 1) if not u_valid_time.empty else 0.0
            uname_raw = str(r0["username"]).lstrip("@") if r0["username"] else ""
            summary_rows.append({
                "Ученик": r0["full_name"], "Юзернейм": f"@{uname_raw}" if uname_raw else "—",
                "ID Telegram": uid, "Всего ответов": u_total, "Точность (%)": u_acc, "Ошибок": u_err, "Ср. время (сек)": u_time
            })
        else:
            summary_rows.append({
                "Ученик": f"ID: {uid}", "Юзернейм": "—", "ID Telegram": uid,
                "Всего ответов": 0, "Точность (%)": 0.0, "Ошибок": 0, "Ср. время (сек)": 0.0
            })
    return pd.DataFrame(summary_rows)


def render_analytics_dashboard(
    df: pd.DataFrame,
    is_class_view: bool = False,
    prev_df: Optional[pd.DataFrame] = None,
    period_label: str = "За всё время"
):
    """Единый визуальный модуль аналитики (за выбранное временное окно)."""
    if df.empty:
        st.warning("Нет данных за выбранный период.")
        return

    st.caption(f"📅 Отображаемый период: **{period_label}**")

    c1, c2, c3, c4 = st.columns(4)
    total_answers = len(df)
    valid_times = df[(df["time_sec"] >= 0) & (df["time_sec"] <= 180.0)]["time_sec"]
    avg_time = round(float(valid_times.mean()), 1) if not valid_times.empty else 0.0
    errors_count = int(df["is_problem"].sum())
    accuracy = round((1 - (errors_count / total_answers)) * 100, 1) if total_answers > 0 else 0.0

    delta_total_str, delta_acc_str, delta_time_str, delta_err_str = None, None, None, None
    if prev_df is not None and not prev_df.empty:
        p_total = len(prev_df)
        p_err = int(prev_df["is_problem"].sum())
        p_acc = round((1 - (p_err / p_total)) * 100, 1) if p_total > 0 else 0.0
        p_valid_t = prev_df[(prev_df["time_sec"] >= 0) & (prev_df["time_sec"] <= 180.0)]["time_sec"]
        p_time = round(float(p_valid_t.mean()), 1) if not p_valid_t.empty else 0.0
        delta_total_str = f"{total_answers - p_total:+d} к прошл. периоду"
        delta_acc_str = f"{round(accuracy - p_acc, 1):+.1f}% к прошл. периоду"
        delta_time_str = f"{round(avg_time - p_time, 1):+.1f} сек"
        delta_err_str = f"{errors_count - p_err:+d}"

    c1.metric("Всего ответов", total_answers, delta=delta_total_str)
    c2.metric("Успешность / Точность", f"{accuracy}%", delta=delta_acc_str)
    c3.metric("Среднее время", f"{avg_time} сек", delta=delta_time_str, delta_color="inverse")
    c4.metric("Ошибок / Затруднений", errors_count, delta=delta_err_str, delta_color="inverse")

    st.divider()

    col_pie1, col_pie2 = st.columns(2)
    with col_pie1:
        st.subheader("🥧 Качество ответов")
        fig_status = px.pie(df, names="status", color="status", color_discrete_map={"Верно / Знаю": "#2ecc71", "Неуверенно": "#f1c40f", "Ошибка / Не знаю": "#e74c3c"}, hole=0.4)
        st.plotly_chart(fig_status, use_container_width=True)

    with col_pie2:
        title_suffix = "для класса" if is_class_view else "по блокам"
        st.subheader(f"🥧 Ошибки {title_suffix}")
        problem_df = df[df["is_problem"]]
        if not problem_df.empty:
            fig_modules = px.pie(problem_df, names="module", hole=0.4)
            st.plotly_chart(fig_modules, use_container_width=True)
        else:
            st.success("🎉 Затруднений и ошибок за выбранный период нет — все ответы уверенные!")

    st.divider()

    col_bar1, col_bar2 = st.columns(2)
    df_calc = df.copy()
    df_calc["time_sec_clamped"] = df_calc["time_sec"].clip(0, 180)

    with col_bar1:
        table_title = "⚠️ Топ-10 трудных терминов для класса" if is_class_view else "⚠️ Ваши трудные термины"
        st.subheader(table_title)
        term_problems = df_calc.groupby(["code", "term", "module"]).agg(
            total_tries=("status", "count"), errors=("is_problem", "sum"), avg_seconds=("time_sec_clamped", "mean")
        ).reset_index()
        term_problems["error_rate"] = (term_problems["errors"] / term_problems["total_tries"] * 100).round(1)
        term_problems["avg_seconds"] = term_problems["avg_seconds"].round(1)
        top_problematic = term_problems.sort_values(by=["errors", "error_rate"], ascending=False).head(10)

        st.dataframe(
            top_problematic[["code", "term", "module", "errors", "total_tries", "error_rate", "avg_seconds"]].rename(
                columns={"code": "Код", "term": "Термин", "module": "Модуль", "errors": "Ошибок", "total_tries": "Попыток", "error_rate": "% ошибок", "avg_seconds": "Ср. время (с)"}
            ),
            use_container_width=True, hide_index=True
        )

    with col_bar2:
        st.subheader("⏱ Скорость ответа по блокам")
        module_time = df_calc.groupby("module")["time_sec_clamped"].mean().round(1).reset_index()
        fig_time = px.bar(module_time, x="module", y="time_sec_clamped", labels={"module": "Модуль", "time_sec_clamped": "Секунды"}, color="time_sec_clamped", color_continuous_scale="Blues")
        st.plotly_chart(fig_time, use_container_width=True)


# =====================================================================
# 7. ИНТЕРФЕЙС И РОУТИНГ ПО РОЛЯМ (ВЕРСИЯ 6.2)
# =====================================================================

def run_dashboard_app():
    raw_df, teachers_df, st_teachers_df = fetch_all_data()

    query_params = st.query_params
    student_param = _get_query_param(query_params, "student")
    token_param = _get_query_param(query_params, "token")
    teacher_param = _get_query_param(query_params, "teacher")
    admin_param = _get_query_param(query_params, "admin")

    # -----------------------------------------------------------------
    # СЦЕНАРИЙ 1: ПЕРСОНАЛЬНЫЙ КАБИНЕТ УЧЕНИКА
    # -----------------------------------------------------------------
    if student_param:
        student_id_str = str(student_param).strip()
        st.title("🎓 Личный кабинет ученика")

        if not verify_student_access(student_id_str, token_param):
            st.error("🔒 Доступ ограничен: ссылка устарела или была сброшена.")
            st.stop()
            return

        if raw_df.empty:
            st.info("ℹ️ В базе пока нет данных об ответах.")
            st.stop()
            return

        student_df = raw_df[raw_df["user_id"] == student_id_str]
        if student_df.empty:
            st.warning(f"Ученик с ID `{student_id_str}` пока не совершил ни одного действия.")
            st.stop()
            return

        sample_row = student_df.iloc[0]
        render_user_avatar_badge(sample_row["full_name"], sample_row["username"], student_id_str)

        # --- ВЕРХНЕЕ ОКНО С ФИЛЬТРАМИ ---
        with st.popover("⚙️ Параметры и фильтры", use_container_width=True):
            st.subheader("Настройки отображения")
            mode_options = ["Все режимы", "Только карточки", "Только тесты"]
            selected_mode = st.selectbox("Режим тренировки:", mode_options, key="student_mode")
            
            st.divider()
            period_choice = st.selectbox("Период для графиков:", TIME_WINDOW_OPTIONS, key="student_period")
            custom_dates = None
            if period_choice == "Свой диапазон дат":
                today_d = datetime.now(TZ).date()
                custom_dates = st.date_input("Выберите даты:", value=(today_d - timedelta(days=14), today_d), key="student_custom_dates")
            
            st.divider()
            if selected_mode == "Только карточки":
                mode_filtered_df = student_df[student_df["mode"] == "Карточки"]
            elif selected_mode == "Только тесты":
                mode_filtered_df = student_df[student_df["mode"] == "Тесты"]
            else:
                mode_filtered_df = student_df

            window_df, prev_window_df, period_label = filter_df_by_time_window(mode_filtered_df, period_choice, custom_dates)
            
            pdf_bytes = generate_pdf_report(
                title=f"Отчёт успеваемости: {sample_row['full_name']}",
                subtitle=f"ID: {student_id_str} | Режим: {selected_mode}",
                df=window_df, period_label=period_label,
            )
            st.download_button("📄 Скачать PDF-отчёт", data=pdf_bytes, file_name=f"Отчет_{student_id_str}.pdf", mime="application/pdf", use_container_width=True)

        # --- ОСНОВНОЙ КОНТЕНТ ---
        if window_df.empty:
            st.warning(f"За выбранный период («{period_label}») по режиму «{selected_mode}» у вас пока нет ответов.")
        else:
            render_analytics_dashboard(window_df, is_class_view=False, prev_df=prev_window_df, period_label=period_label)

        render_activity_calendar(unfiltered_date_df=student_df, default_mode=selected_mode, key_prefix="student_gh")
        st.stop()
        return

    # -----------------------------------------------------------------
    # СЦЕНАРИЙ 2: КАБИНЕТ ПРЕПОДАВАТЕЛЯ
    # -----------------------------------------------------------------
    elif teacher_param:
        teacher_code = re.sub(r"[^0-9A-Za-z_-]", "", str(teacher_param).strip())
        st.title("👨‍🏫 Кабинет преподавателя")

        if not teacher_code or not verify_teacher_access(teacher_code, token_param):
            st.error("🔒 Доступ ограничен: отсутствует токен или ссылка обновлена.")
            st.stop()
            return

        teacher_name = f"Код класса: {teacher_code}"
        teacher_exists = False
        if not teachers_df.empty:
            t_match = teachers_df[teachers_df["teacher_code"].astype(str) == teacher_code]
            if not t_match.empty:
                teacher_exists = True
                t_full = str(t_match.iloc[0]["full_name"]).strip() or f"Учитель {teacher_code}"
                teacher_name = f"{t_full} (Класс `{teacher_code}`)"

        st.caption(f"Просмотр: **{teacher_name}**")

        if st_teachers_df.empty or not class_student_ids:
            class_student_ids = st_teachers_df[st_teachers_df["teacher_code"].astype(str) == teacher_code]["student_id"].astype(str).unique().tolist() if not st_teachers_df.empty else []
            if not class_student_ids:
                st.info(f"ℹ️ К классу `{teacher_code}` пока не прикрепился ни один ученик.")
                st.stop()
                return

        class_df = raw_df[raw_df["user_id"].isin(class_student_ids)] if not raw_df.empty else pd.DataFrame(columns=raw_df.columns)
        
        # --- ВЕРХНЕЕ ОКНО С ФИЛЬТРАМИ ---
        with st.popover("⚙️ Параметры и фильтры", use_container_width=True):
            st.subheader("Настройки отображения")
            
            student_labels = {"👥 Весь класс (Сводка)": "ALL"}
            for uid in class_student_ids:
                sub = class_df[class_df["user_id"] == uid]
                if not sub.empty:
                    row_s = sub.iloc[0]
                    uname_clean = str(row_s["username"]).lstrip("@") if row_s["username"] else ""
                    label = f"{row_s['full_name']} ({'@' + uname_clean if uname_clean else 'ID: ' + uid})"
                else:
                    label = f"ID: {uid} (0 ответов)"
                student_labels[label] = uid
            
            selected_student_label = st.selectbox("Выберите ученика:", list(student_labels.keys()), key="teacher_student_sel")
            selected_uid = student_labels[selected_student_label]
            
            mode_options = ["Все режимы", "Только карточки", "Только тесты"]
            selected_mode = st.selectbox("Режим тренировки:", mode_options, key="teacher_mode")
            
            st.divider()
            period_choice = st.selectbox("Период для графиков:", TIME_WINDOW_OPTIONS, key="teacher_period")
            custom_dates = None
            if period_choice == "Свой диапазон дат":
                today_d = datetime.now(TZ).date()
                custom_dates = st.date_input("Выберите даты:", value=(today_d - timedelta(days=14), today_d), key="teacher_custom_dates")

            st.divider()
            entity_unfiltered_df = class_df if selected_uid == "ALL" else class_df[class_df["user_id"] == selected_uid]
            
            if selected_mode == "Только карточки":
                mode_entity_df = entity_unfiltered_df[entity_unfiltered_df["mode"] == "Карточки"]
            elif selected_mode == "Только тесты":
                mode_entity_df = entity_unfiltered_df[entity_unfiltered_df["mode"] == "Тесты"]
            else:
                mode_entity_df = entity_unfiltered_df

            active_window_df, prev_window_df, period_label = filter_df_by_time_window(mode_entity_df, period_choice, custom_dates)
            
            report_df = build_class_summary_df(class_student_ids, active_window_df)
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                report_df.to_excel(writer, index=False, sheet_name="Ведомость класса")
            st.download_button("📥 Скачать ведомость (.xlsx)", data=buffer.getvalue(), file_name=f"Класс_{teacher_code}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
            
            pdf_class_bytes = generate_pdf_report(title=f"Ведомость класса {teacher_code}", subtitle=teacher_name, df=active_window_df, period_label=period_label, class_summary_df=report_df)
            st.download_button("📄 Скачать PDF-отчёт (.pdf)", data=pdf_class_bytes, file_name=f"Класс_{teacher_code}.pdf", mime="application/pdf", use_container_width=True)

        # --- ОСНОВНОЙ КОНТЕНТ ---
        if selected_uid != "ALL" and not entity_unfiltered_df.empty:
            s_row = entity_unfiltered_df.iloc[0]
            render_user_avatar_badge(s_row["full_name"], s_row["username"], selected_uid)

        if active_window_df.empty:
            st.warning(f"За выбранный период («{period_label}») пока нет данных.")
        else:
            render_analytics_dashboard(active_window_df, is_class_view=(selected_uid == "ALL"), prev_df=prev_window_df, period_label=period_label)

        render_activity_calendar(unfiltered_date_df=entity_unfiltered_df, default_mode=selected_mode, key_prefix="teacher_gh")
        st.stop()
        return

    # -----------------------------------------------------------------
    # СЦЕНАРИЙ 3: АДМИН-ПАНЕЛЬ И ГЛАВНАЯ
    # -----------------------------------------------------------------
    else:
        if "is_admin_authed" not in st.session_state:
            st.session_state["is_admin_authed"] = False

        if admin_param and verify_secret(admin_param, ADMIN_PASSWORD):
            st.session_state["is_admin_authed"] = True

        # --- ГЛАВНАЯ СТРАНИЦА (ДЛЯ ГОСТЕЙ) ---
        if not st.session_state["is_admin_authed"]:
            st.title("📊 Аналитический портал тренажера")
            st.write("Добро пожаловать! Выберите свой раздел или войдите как администратор.")
            
            c_info1, c_info2 = st.columns(2)
            with c_info1:
                st.info("🎓 **Для учеников:**\nОткройте Telegram-бот и перейдите по кнопке «🌐 Открыть подробную веб-аналитику».")
            with c_info2:
                st.success("👨‍🏫 **Для преподавателей:**\nИспользуйте персональную ссылку, которую бот выдал по команде `/teacher`.")

            with st.popover("🔒 Вход для администратора", use_container_width=True):
                admin_input = st.text_input("Пароль:", type="password", key="admin_pass")
                if st.button("Войти", use_container_width=True):
                    if verify_secret(admin_input, ADMIN_PASSWORD):
                        st.session_state["is_admin_authed"] = True
                        st.rerun()
                    else:
                        st.error("Неверный пароль.")
            st.stop()
            return

        # --- АДМИН-ПАНЕЛЬ ---
        st.title("🛡 Глобальная панель администратора")
        if st.button("🚪 Выйти из панели"):
            st.session_state["is_admin_authed"] = False
            st.rerun()
            return

        if raw_df.empty:
            st.info("ℹ️ В базе пока нет ответов.")
            st.stop()
            return

        user_labels_map = {}
        for uid in raw_df["user_id"].unique():
            sub_df = raw_df[raw_df["user_id"] == uid]
            r0 = sub_df.iloc[0]
            fname = r0["full_name"]
            uname_clean = str(r0["username"]).lstrip("@") if r0["username"] else ""
            uname = f"@{uname_clean}" if uname_clean else f"ID: {uid}"
            user_labels_map[f"{fname} ({uname})"] = uid

        # --- ВЕРХНЕЕ ОКНО С ФИЛЬТРАМИ ---
        with st.popover("⚙️ Параметры и фильтры", use_container_width=True):
            st.subheader("Настройки отображения")
            
            users_options = ["Все пользователи"] + sorted(list(user_labels_map.keys()))
            selected_option = st.selectbox("Выберите пользователя:", users_options, key="admin_user_sel")
            
            mode_options = ["Все режимы", "Только карточки", "Только тесты"]
            selected_mode = st.selectbox("Режим тренировки:", mode_options, key="admin_mode")
            
            st.divider()
            period_choice = st.selectbox("Период для графиков:", TIME_WINDOW_OPTIONS, key="admin_period")
            custom_dates = None
            if period_choice == "Свой диапазон дат":
                today_d = datetime.now(TZ).date()
                custom_dates = st.date_input("Выберите даты:", value=(today_d - timedelta(days=14), today_d), key="admin_custom_dates")

            st.divider()
            if st.button("🔄 Обновить данные из БД", use_container_width=True):
                st.cache_data.clear()
                st.rerun()

            if selected_option == "Все пользователи":
                entity_df = raw_df
            else:
                chosen_uid = user_labels_map[selected_option]
                entity_df = raw_df[raw_df["user_id"] == chosen_uid]

            if selected_mode == "Только карточки":
                mode_df = entity_df[entity_df["mode"] == "Карточки"]
            elif selected_mode == "Только тесты":
                mode_df = entity_df[entity_df["mode"] == "Тесты"]
            else:
                mode_df = entity_df

            window_df, prev_window_df, period_label = filter_df_by_time_window(mode_df, period_choice, custom_dates)

        # --- ОСНОВНОЙ КОНТЕНТ ---
        if selected_option != "Все пользователи" and not entity_df.empty:
            s_row = entity_df.iloc[0]
            render_user_avatar_badge(s_row["full_name"], s_row["username"], s_row["user_id"])

        if window_df.empty:
            st.warning(f"За выбранный период («{period_label}») пока нет данных.")
        else:
            render_analytics_dashboard(window_df, is_class_view=(selected_option == "Все пользователи"), prev_df=prev_window_df, period_label=period_label)

        render_activity_calendar(unfiltered_date_df=entity_df, default_mode=selected_mode, key_prefix="admin_gh")


if __name__ == "__main__" or ("streamlit" in sys.modules if "sys" in globals() else True):
    if os.getenv("DASHBOARD_SKIP_AUTORUN") != "1":
        run_dashboard_app()
