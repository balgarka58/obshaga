import io
import os
import re
import sys
import time
import json
import html
import sqlite3
import warnings
from datetime import datetime, date, timedelta, timezone
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
# 1. ИМПОРТ МОДУЛЕЙ ЯДРА И НАСТРОЙКА СТРАНИЦЫ (ВЕРСИЯ 7.6)
# =====================================================================

from core.common import (
    DATABASE_URL,
    DB_PATH,
    TERMS_JSON_PATH,
    SECRET_KEY,
    ADMIN_PASSWORD,
    TIMEZONE_STR,
    TZ,
    normalize_neon_pooler_url,
    verify_secret,
    generate_student_token,
    generate_teacher_token,
    decode_user_id,
    _normalize_search_str,
    _get_trigrams,
    _compute_similarity_preindexed,
    compute_fuzzy_similarity,
)
from core.calendar_ui import (
    TIME_WINDOW_OPTIONS,
    WEEKDAY_NAMES_RU,
    WEEKDAY_SHORT_RU,
    MONTH_SHORT_RU,
    MONTH_FULL_RU,
    MODULE_SHORT_BADGE,
    _normalize_timestamps,
    filter_df_by_time_window,
    build_calendar_data,
    build_calendar_html,
)

st.set_page_config(
    page_title="Аналитика терминов по обществознанию",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Скрываем служебные элементы Streamlit + адаптивная вёрстка под Telegram Mini App и системную тему
st.markdown(
    """
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
try {
    if (window.Telegram && window.Telegram.WebApp) {
        window.Telegram.WebApp.ready();
        window.Telegram.WebApp.expand();
    }
} catch (e) {}

// Дополнительная зачистка плавающего бейджа Streamlit Cloud и GitHub-аватарки в родительском окне
(function() {
    function hideCloudBadges() {
        try {
            var docs = [document];
            try { if (window.parent && window.parent.document) docs.push(window.parent.document); } catch (e) {}
            docs.forEach(function(doc) {
                var selectors = [
                    'div[class*="viewerBadge"]',
                    'div[class*="ProfileBadge"]',
                    'div[class*="profileBadge"]',
                    'a[class*="viewerBadge"]',
                    'a[href*="streamlit.io/cloud"]',
                    '#manage-app-button',
                    '[data-testid="manage-app-button"]',
                    '[data-testid="stElementToolbar"]',
                    '.modebar-container',
                    '.modebar',
                    'img[src*="githubusercontent.com"]',
                    'img[src*="avatars.githubusercontent.com"]'
                ];
                selectors.forEach(function(sel) {
                    doc.querySelectorAll(sel).forEach(function(el) {
                        el.style.setProperty('display', 'none', 'important');
                        el.style.setProperty('visibility', 'hidden', 'important');
                        el.style.setProperty('opacity', '0', 'important');
                        el.style.setProperty('pointer-events', 'none', 'important');
                    });
                });
            });
        } catch (err) {}
    }
    hideCloudBadges();
    setInterval(hideCloudBadges, 800);
})();
</script>
<style>
/* Полное скрытие шапки, футера, тулбаров, Plotly ModeBar и бейджей Streamlit / GitHub */
#MainMenu,
header,
footer,
[data-testid="stHeader"],
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"],
[data-testid="stSidebar"],
[data-testid="stElementToolbar"],
[data-testid="stElementToolbarButton"],
.stElementToolbar,
.modebar-container,
.modebar,
.modebar-group,
.viewerBadge_container__1QSob,
.viewerBadge_link__qRIco,
.viewerBadge_text__1JaDK,
div[class*="viewerBadge"],
a[class*="viewerBadge"],
div[class*="ProfileBadge"],
div[class*="profileBadge"],
div[class*="StatusWidget"],
a[href*="streamlit.io/cloud"],
img[src*="githubusercontent.com"],
img[src*="avatars.githubusercontent.com"],
#manage-app-button,
[data-testid="manage-app-button"] {
    visibility: hidden !important;
    display: none !important;
    opacity: 0 !important;
    pointer-events: none !important;
    width: 0 !important;
    height: 0 !important;
}

/* Компактные отступы для идеального отображения в Telegram Mini App и на мобильных */
.block-container {
    padding-top: 1.0rem !important;
    padding-bottom: 2.0rem !important;
    max-width: 100% !important;
}

/* Уменьшаем лишний вертикальный разрыв между заголовками и графиками */
div[data-testid="stSubheader"], h3 {
    margin-bottom: 0.25rem !important;
    padding-bottom: 0 !important;
}
div[data-testid="stPlotlyChart"] {
    margin-top: -0.35rem !important;
    margin-bottom: -0.25rem !important;
}

/* Автоматическая адаптация плашки профиля под тему устройства (светлая / тёмная) */
.user-avatar-card {
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 14px;
    padding: 12px 16px;
    border-radius: 12px;
    background: rgba(148, 163, 184, 0.10);
    border: 1px solid rgba(148, 163, 184, 0.22);
}
.user-avatar-subtitle {
    font-size: 0.88rem;
    opacity: 0.72;
}

/* Адаптивная таблица терминов без горизонтального скролла на любых экранах */
.terms-table-wrap {
    width: 100%;
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid rgba(148, 163, 184, 0.24);
    background: rgba(148, 163, 184, 0.05);
    margin-top: 4px;
    margin-bottom: 12px;
}
.terms-table {
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
    font-size: 0.86rem;
    line-height: 1.3;
}
.terms-table th {
    text-align: left;
    padding: 9px 10px;
    font-weight: 600;
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.02em;
    opacity: 0.78;
    background: rgba(148, 163, 184, 0.12);
    border-bottom: 1px solid rgba(148, 163, 184, 0.22);
}
.terms-table td {
    padding: 9px 10px;
    vertical-align: middle;
    border-bottom: 1px solid rgba(148, 163, 184, 0.14);
    word-wrap: break-word;
    overflow-wrap: break-word;
}
.terms-table tr:last-child td {
    border-bottom: none;
}
.terms-col-term { width: 42%; font-weight: 600; }
.terms-col-mod  { width: 22%; opacity: 0.85; font-size: 0.80rem; }
.terms-col-stat { width: 36%; }
.terms-stat-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 0.79rem;
    margin-bottom: 4px;
    gap: 4px;
}
.terms-pct-badge {
    font-weight: 700;
    color: #ef4444;
}
.terms-bar-bg {
    width: 100%;
    height: 6px;
    border-radius: 999px;
    background: rgba(148, 163, 184, 0.24);
    overflow: hidden;
}
.terms-bar-fill {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, #f59e0b, #ef4444);
}

/* Автоподгонка под мобильные экраны (сетка 2x2 для метрик и компактные поля) */
@media (max-width: 640px) {
    .block-container {
        padding-left: 0.65rem !important;
        padding-right: 0.65rem !important;
        padding-top: 0.65rem !important;
    }
    div[data-testid="stHorizontalBlock"] {
        display: flex !important;
        flex-direction: row !important;
        flex-wrap: wrap !important;
        gap: 8px !important;
    }
    div[data-testid="stHorizontalBlock"] > div[data-testid="column"],
    div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
        flex: 1 1 calc(50% - 8px) !important;
        min-width: calc(50% - 8px) !important;
        max-width: calc(50% - 4px) !important;
    }
    div[data-testid="stHorizontalBlock"]:has(> div:nth-child(2):last-child) > div[data-testid="column"],
    div[data-testid="stHorizontalBlock"]:has(> div:nth-child(2):last-child) > div[data-testid="stColumn"] {
        flex: 1 1 100% !important;
        min-width: 100% !important;
        max-width: 100% !important;
    }
    div[data-testid="stMetric"] {
        background: rgba(148, 163, 184, 0.08);
        border: 1px solid rgba(148, 163, 184, 0.18);
        border-radius: 10px;
        padding: 8px 10px !important;
    }
    div[data-testid="stMetricLabel"] p {
        font-size: 0.78rem !important;
    }
    div[data-testid="stMetricValue"] {
        font-size: 1.35rem !important;
    }
    .terms-table {
        font-size: 0.80rem;
    }
    .terms-table th, .terms-table td {
        padding: 7px 6px;
    }
    .terms-col-term { width: 41%; }
    .terms-col-mod  { width: 21%; font-size: 0.75rem; }
    .terms-col-stat { width: 38%; }
}
</style>
""",
    unsafe_allow_html=True,
)

if not SECRET_KEY:
    raise RuntimeError("Переменная окружения SECRET_KEY не задана!")
if not ADMIN_PASSWORD:
    raise RuntimeError("Переменная окружения ADMIN_PASSWORD не задана!")


def _get_query_param(query_params: Any, key: str) -> Optional[str]:
    if query_params is None:
        return None
    val = query_params.get(key)
    if isinstance(val, (list, tuple)):
        val = val[0] if val else None
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


# =====================================================================
# 2. МЕТАДАННЫЕ ТЕРМИНОВ
# =====================================================================

@st.cache_data
def load_terms_metadata() -> Dict[str, Dict[str, str]]:
    modules_map = {
        1: "1. Человек и общество",
        2: "2. Экономика",
        3: "3. Социальные отношения",
        4: "4. Политика",
        5: "5. Право",
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
                    code = (
                        str(item.get("code") or "")
                        .strip()
                        .replace(";", "_")
                        .replace(":", "_")[:24]
                    )
                    if not code:
                        continue
                    try:
                        mod_num = int(code.split(".")[0])
                    except (ValueError, IndexError):
                        mod_num = (
                            int(item.get("module_number") or 1)
                            if str(item.get("module_number") or "").isdigit()
                            else 1
                        )

                    terms_dict[code] = {
                        "term": str(item.get("term") or f"Термин {code}").strip(),
                        "module": modules_map.get(mod_num, f"Модуль {mod_num}"),
                        "genus": str(item.get("genus") or "").strip(),
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
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='terms';"
            )
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
                        "genus": str(r[3] or "").strip(),
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
# 3. ПОДКЛЮЧЕНИЕ К БД, ЧТЕНИЕ АРХИВА И КЭШИРОВАНИЕ ТОКЕНОВ В STREAMLIT
# =====================================================================

def get_db_connection():
    db_url = normalize_neon_pooler_url(os.getenv("DATABASE_URL"))
    if db_url and db_url.startswith("postgres"):
        import psycopg2

        return psycopg2.connect(db_url, connect_timeout=10), True
    else:
        sqlite_path = os.getenv("DB_PATH", "social_bot.db")
        conn = sqlite3.connect(sqlite_path, timeout=15.0)
        return conn, False


@st.cache_data(ttl=180)
def get_current_student_token_version(student_id: str) -> int:
    """Кэшированная на 180 сек проверка версии токена ученика (экономит Compute Hours Neon)."""
    uid_clean = str(student_id or "").strip()
    if not uid_clean.isdigit():
        return 1
    conn = None
    cur = None
    try:
        conn, is_postgres = get_db_connection()
        cur = conn.cursor()
        if is_postgres:
            cur.execute(
                "SELECT token_version FROM user_settings WHERE user_id = %s;",
                (int(uid_clean),),
            )
        else:
            cur.execute(
                "SELECT token_version FROM user_settings WHERE user_id = ?;",
                (int(uid_clean),),
            )
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


@st.cache_data(ttl=180)
def get_current_teacher_token_version(teacher_code: str) -> int:
    """Кэшированная на 180 сек проверка версии токена преподавателя."""
    conn = None
    cur = None
    try:
        conn, is_postgres = get_db_connection()
        cur = conn.cursor()
        if is_postgres:
            cur.execute(
                "SELECT COALESCE(token_version, 1) FROM teachers WHERE teacher_code = %s;",
                (str(teacher_code),),
            )
        else:
            cur.execute(
                "SELECT COALESCE(token_version, 1) FROM teachers WHERE teacher_code = ?;",
                (str(teacher_code),),
            )
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
    uid_clean = str(student_id or "").strip()
    if not uid_clean.isdigit():
        return False
    ver = get_current_student_token_version(uid_clean)
    expected = generate_student_token(uid_clean, ver)
    return verify_secret(provided_token, expected)


def verify_teacher_access(teacher_code: str, provided_token: Optional[str]) -> bool:
    code_clean = str(teacher_code or "").strip()
    if not code_clean or not provided_token:
        return False
    ver = get_current_teacher_token_version(code_clean)
    expected = generate_teacher_token(code_clean, ver)
    return verify_secret(provided_token, expected)


ANSWERS_COLUMNS = [
    "user_id",
    "username",
    "full_name",
    "mode",
    "code",
    "term",
    "module",
    "delta_ans",
    "delta_time",
    "time_sec",
    "status",
    "is_problem",
    "created_at",
]
TEACHER_COLUMNS = [
    "teacher_code",
    "user_id",
    "full_name",
    "token_version",
    "created_at",
]
ST_COLUMNS = ["student_id", "teacher_code", "joined_at"]


def _parse_answers_and_archive_rows(
    ans_rows: List[Tuple[Any, ...]], arch_rows: List[Tuple[Any, ...]]
) -> pd.DataFrame:
    """
    Быстрый парсер сырых строк БД (без медленного DataFrame.iterrows):
    работает в 15-20 раз быстрее и минимально расходует RAM.
    """
    if not ans_rows and not arch_rows:
        return pd.DataFrame(columns=ANSWERS_COLUMNS)

    user_profiles: Dict[str, Tuple[Optional[str], str]] = {}

    def _anonymize_display_name(raw_name: str) -> str:
        s = str(raw_name or "").strip().lstrip("=+-@")
        if not s:
            return ""
        words = s.split()
        if len(words) >= 2:
            w0 = "".join(ch for ch in words[0] if ch.isalnum()) or words[0]
            w1_alnum = "".join(ch for ch in words[1] if ch.isalnum())
            if w1_alnum:
                return f"{w0} {w1_alnum[0].upper()}."
        return s[:24]

    def _clean_profile_fields(
        uid_str: str, raw_u: Any, raw_f: Any
    ) -> Tuple[Optional[str], str]:
        u_val = str(raw_u).strip() if raw_u is not None and pd.notna(raw_u) else ""
        if u_val in ("None", "nan", ""):
            u_val = None
        f_val = str(raw_f).strip() if raw_f is not None and pd.notna(raw_f) else ""
        if f_val in ("None", "nan", ""):
            f_val = ""
        f_val = _anonymize_display_name(f_val)
        prev_u, prev_f = user_profiles.get(uid_str, (None, ""))
        best_u = prev_u if prev_u is not None else u_val
        best_f = prev_f if prev_f else f_val
        user_profiles[uid_str] = (best_u, best_f)
        return best_u, (best_f or best_u or f"Ученик #{uid_str[-4:]}")

    parsed_rows = []

    for r in ans_rows:
        uid_str = str(r[0]).strip()
        username_val, fname_val = _clean_profile_fields(uid_str, r[1], r[2])
        entry_str = str(r[3] or "")
        created_at_val = r[4]

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
            if raw_mode == "test" and sec < 3.0:
                continue

            mode_display = "Тесты" if raw_mode == "test" else "Карточки"
            term_info = TERMS_MAP.get(
                code,
                {
                    "term": f"Код {code}",
                    "module": f"Модуль {code.split('.')[0] if '.' in code else '?'}",
                },
            )

            if d_ans <= 0:
                result_status = "Верно / Знаю"
                is_problem = False
            elif d_ans < 1.5:
                result_status = "Неуверенно"
                is_problem = True
            else:
                result_status = "Ошибка / Не знаю"
                is_problem = True

            parsed_rows.append(
                {
                    "user_id": uid_str,
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
                    "created_at": created_at_val,
                }
            )

    for ar in arch_rows:
        uid_str = str(ar[0]).strip()
        username_val, fname_val = _clean_profile_fields(uid_str, ar[1], ar[2])
        arch_date = ar[3]
        raw_mode = str(ar[4] or "card").strip()
        code = str(ar[5] or "").strip()
        if not code:
            continue

        mode_display = "Тесты" if raw_mode == "test" else "Карточки"
        term_info = TERMS_MAP.get(
            code,
            {
                "term": f"Код {code}",
                "module": f"Модуль {code.split('.')[0] if '.' in code else '?'}",
            },
        )

        try:
            tries_cnt = max(1, int(ar[6] or 1))
            err_cnt = max(0, min(tries_cnt, int(ar[7] or 0)))
            total_sec = max(0.0, float(ar[8] or 0.0))
            last_d_ans = float(ar[10] or 0.0)
        except (ValueError, TypeError):
            continue

        ok_cnt = tries_cnt - err_cnt
        avg_sec = round(total_sec / tries_cnt, 1)
        if raw_mode == "test" and avg_sec < 3.0:
            avg_sec = 3.0

        if arch_date is not None:
            try:
                d_iso = str(arch_date)[:10]
                d_obj = date.fromisoformat(d_iso)
                created_ts = (
                    datetime(
                        d_obj.year, d_obj.month, d_obj.day, 12, 0, 0, tzinfo=TZ
                    )
                    .astimezone(timezone.utc)
                    .isoformat()
                )
            except Exception:
                created_ts = f"{str(arch_date)[:10]} 12:00:00"
        else:
            created_ts = datetime.now(timezone.utc).isoformat()

        for _ in range(ok_cnt):
            parsed_rows.append(
                {
                    "user_id": uid_str,
                    "username": username_val,
                    "full_name": fname_val,
                    "mode": mode_display,
                    "code": code,
                    "term": term_info["term"],
                    "module": term_info["module"],
                    "delta_ans": -1.0,
                    "delta_time": 0.0,
                    "time_sec": avg_sec,
                    "status": "Верно / Знаю",
                    "is_problem": False,
                    "created_at": created_ts,
                }
            )

        err_status = (
            "Неуверенно"
            if (0 < last_d_ans < 1.5 and raw_mode != "test")
            else "Ошибка / Не знаю"
        )
        err_d_ans = last_d_ans if last_d_ans > 0 else 2.0
        for _ in range(err_cnt):
            parsed_rows.append(
                {
                    "user_id": uid_str,
                    "username": username_val,
                    "full_name": fname_val,
                    "mode": mode_display,
                    "code": code,
                    "term": term_info["term"],
                    "module": term_info["module"],
                    "delta_ans": err_d_ans,
                    "delta_time": 0.0,
                    "time_sec": avg_sec,
                    "status": err_status,
                    "is_problem": True,
                    "created_at": created_ts,
                }
            )

    if not parsed_rows:
        return pd.DataFrame(columns=ANSWERS_COLUMNS)

    out_df = pd.DataFrame(parsed_rows)
    for uid_str, (u_best, f_best) in user_profiles.items():
        if f_best:
            mask = out_df["user_id"] == uid_str
            out_df.loc[mask, "full_name"] = f_best
            if u_best:
                out_df.loc[mask, "username"] = u_best
    return out_df


@st.cache_data(ttl=120)
def fetch_student_data(student_id: str) -> pd.DataFrame:
    """
    Точечно загружает из БД историю ТОЛЬКО одного конкретного ученика (WHERE user_id = ...).
    """
    uid_clean = str(student_id or "").strip()
    if not uid_clean.isdigit():
        return pd.DataFrame(columns=ANSWERS_COLUMNS)
    uid_int = int(uid_clean)

    try:
        conn, is_postgres = get_db_connection()
    except Exception as e:
        st.error(f"Не удалось подключиться к базе данных: {e}")
        return pd.DataFrame(columns=ANSWERS_COLUMNS)

    ans_rows = []
    arch_rows = []
    cur = conn.cursor()
    try:
        try:
            if is_postgres:
                cur.execute(
                    "SELECT user_id, username, full_name, entry, created_at FROM user_answers WHERE user_id = %s ORDER BY created_at DESC;",
                    (uid_int,),
                )
            else:
                cur.execute(
                    "SELECT user_id, username, full_name, entry, created_at FROM user_answers WHERE user_id = ? ORDER BY created_at DESC;",
                    (uid_int,),
                )
            ans_rows = cur.fetchall()
        except Exception:
            ans_rows = []

        try:
            if is_postgres:
                cur.execute(
                    """
                    SELECT user_id, username, full_name, archive_date, mode, code, 
                           tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                    FROM user_daily_archive WHERE user_id = %s ORDER BY archive_date DESC;
                """,
                    (uid_int,),
                )
            else:
                cur.execute(
                    """
                    SELECT user_id, username, full_name, archive_date, mode, code, 
                           tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                    FROM user_daily_archive WHERE user_id = ? ORDER BY archive_date DESC;
                """,
                    (uid_int,),
                )
            arch_rows = cur.fetchall()
        except Exception:
            arch_rows = []
    finally:
        cur.close()
        conn.close()

    return _parse_answers_and_archive_rows(ans_rows, arch_rows)


@st.cache_data(ttl=180)
def fetch_teacher_class_data(
    teacher_code: str,
) -> Tuple[str, List[str], pd.DataFrame]:
    """
    Точечно загружает из БД данные ТОЛЬКО одного класса преподавателя (WHERE user_id IN ...).
    """
    code_clean = str(teacher_code or "").strip()
    teacher_name = f"Код класса: {code_clean}"
    class_student_ids: List[str] = []

    try:
        conn, is_postgres = get_db_connection()
    except Exception as e:
        st.error(f"Не удалось подключиться к базе данных: {e}")
        return teacher_name, [], pd.DataFrame(columns=ANSWERS_COLUMNS)

    ans_rows = []
    arch_rows = []
    cur = conn.cursor()
    try:
        try:
            if is_postgres:
                cur.execute(
                    "SELECT full_name FROM teachers WHERE teacher_code = %s;",
                    (code_clean,),
                )
            else:
                cur.execute(
                    "SELECT full_name FROM teachers WHERE teacher_code = ?;",
                    (code_clean,),
                )
            t_row = cur.fetchone()
            if t_row and t_row[0]:
                raw_t = str(t_row[0]).strip()
                t_words = raw_t.split()
                if len(t_words) >= 2:
                    w1_clean = "".join(ch for ch in t_words[1] if ch.isalnum())
                    if w1_clean:
                        raw_t = f"{t_words[0]} {w1_clean[0].upper()}."
                teacher_name = f"{raw_t} (Класс `{code_clean}`)"
        except Exception:
            pass

        try:
            if is_postgres:
                cur.execute(
                    "SELECT student_id FROM student_teachers WHERE teacher_code = %s ORDER BY joined_at DESC;",
                    (code_clean,),
                )
            else:
                cur.execute(
                    "SELECT student_id FROM student_teachers WHERE teacher_code = ? ORDER BY joined_at DESC;",
                    (code_clean,),
                )
            st_rows = cur.fetchall()
            seen_s = set()
            for sr in st_rows:
                if sr and sr[0] is not None:
                    s_str = str(sr[0]).strip()
                    if s_str.isdigit() and s_str not in seen_s:
                        seen_s.add(s_str)
                        class_student_ids.append(s_str)
        except Exception:
            pass

        if class_student_ids:
            uid_ints = [int(x) for x in class_student_ids]
            try:
                if is_postgres:
                    cur.execute(
                        "SELECT user_id, username, full_name, entry, created_at FROM user_answers WHERE user_id = ANY(%s) ORDER BY created_at DESC;",
                        (uid_ints,),
                    )
                else:
                    placeholders = ",".join("?" for _ in uid_ints)
                    cur.execute(
                        f"SELECT user_id, username, full_name, entry, created_at FROM user_answers WHERE user_id IN ({placeholders}) ORDER BY created_at DESC;",
                        uid_ints,
                    )
                ans_rows = cur.fetchall()
            except Exception:
                ans_rows = []

            try:
                if is_postgres:
                    cur.execute(
                        """
                        SELECT user_id, username, full_name, archive_date, mode, code, 
                               tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                        FROM user_daily_archive WHERE user_id = ANY(%s) ORDER BY archive_date DESC;
                    """,
                        (uid_ints,),
                    )
                else:
                    placeholders = ",".join("?" for _ in uid_ints)
                    cur.execute(
                        f"""
                        SELECT user_id, username, full_name, archive_date, mode, code, 
                               tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                        FROM user_daily_archive WHERE user_id IN ({placeholders}) ORDER BY archive_date DESC;
                    """,
                        uid_ints,
                    )
                arch_rows = cur.fetchall()
            except Exception:
                arch_rows = []
    finally:
        cur.close()
        conn.close()

    return (
        teacher_name,
        class_student_ids,
        _parse_answers_and_archive_rows(ans_rows, arch_rows),
    )


@st.cache_data(ttl=180)
def fetch_all_data() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Загружает полную историю ответов всех пользователей (вызывается ТОЛЬКО после авторизации администратора).
    """
    try:
        conn, is_postgres = get_db_connection()
    except Exception as e:
        st.error(f"Не удалось подключиться к базе данных: {e}")
        return (
            pd.DataFrame(columns=ANSWERS_COLUMNS),
            pd.DataFrame(columns=TEACHER_COLUMNS),
            pd.DataFrame(columns=ST_COLUMNS),
        )

    ans_rows = []
    arch_rows = []
    teachers_df = pd.DataFrame(columns=TEACHER_COLUMNS)
    st_teachers_df = pd.DataFrame(columns=ST_COLUMNS)
    cur = conn.cursor()

    try:
        try:
            cur.execute(
                "SELECT user_id, username, full_name, entry, created_at FROM user_answers ORDER BY created_at DESC;"
            )
            ans_rows = cur.fetchall()
        except Exception:
            try:
                cur.execute(
                    "SELECT user_id, username, NULL as full_name, entry, created_at FROM user_answers ORDER BY created_at DESC;"
                )
                ans_rows = cur.fetchall()
            except Exception:
                ans_rows = []

        try:
            cur.execute(
                """
                SELECT user_id, username, full_name, archive_date, mode, code, 
                       tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                FROM user_daily_archive ORDER BY archive_date DESC;
            """
            )
            arch_rows = cur.fetchall()
        except Exception:
            try:
                cur.execute(
                    """
                    SELECT user_id, NULL as username, NULL as full_name, archive_date, mode, code, 
                           tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                    FROM user_daily_archive ORDER BY archive_date DESC;
                """
                )
                arch_rows = cur.fetchall()
            except Exception:
                arch_rows = []

        try:
            query_teachers = "SELECT teacher_code, user_id, full_name, COALESCE(token_version, 1) AS token_version, created_at FROM teachers;"
            t_raw = pd.read_sql(query_teachers, conn)
            if not t_raw.empty:
                t_raw["teacher_code"] = t_raw["teacher_code"].astype(str).str.strip()
                t_raw["user_id"] = t_raw["user_id"].astype(str).str.strip()
                t_raw["full_name"] = (
                    t_raw["full_name"].fillna("").astype(str).str.strip()
                )
                t_raw["token_version"] = (
                    pd.to_numeric(t_raw["token_version"], errors="coerce")
                    .fillna(1)
                    .astype(int)
                )
                teachers_df = t_raw
        except Exception:
            teachers_df = pd.DataFrame(columns=TEACHER_COLUMNS)

        try:
            query_st = "SELECT student_id, teacher_code, joined_at FROM student_teachers;"
            st_raw = pd.read_sql(query_st, conn)
            if not st_raw.empty:
                st_raw["student_id"] = st_raw["student_id"].astype(str).str.strip()
                st_raw["teacher_code"] = (
                    st_raw["teacher_code"].astype(str).str.strip()
                )
                st_teachers_df = st_raw
        except Exception:
            st_teachers_df = pd.DataFrame(columns=ST_COLUMNS)
    finally:
        cur.close()
        conn.close()

    return (
        _parse_answers_and_archive_rows(ans_rows, arch_rows),
        teachers_df,
        st_teachers_df,
    )


# =====================================================================
# 4. КАЛЕНДАРЬ АКТИВНОСТИ И КОМПОНЕНТЫ ИНТЕРФЕЙСА
# =====================================================================

def render_activity_calendar(
    unfiltered_date_df: pd.DataFrame,
    default_mode: str = "Все режимы",
    key_prefix: str = "gh",
):
    st.divider()
    st.subheader("🟩 Календарь активности")

    if unfiltered_date_df.empty:
        st.info("ℹ️ Пока нет данных для отображения календаря.")
        return

    cal_data = build_calendar_data(unfiltered_date_df, counter_mode=default_mode)

    c_s1, c_s2, c_s3, c_s4 = st.columns(4)
    c_s1.metric("🔥 Текущая серия", f"{cal_data['current_streak']} дн.")
    c_s2.metric("🏆 Рекорд серии", f"{cal_data['best_streak']} дн.")
    c_s3.metric("🗓 Активных дней за год", f"{cal_data['active_days_year']} из 365")
    c_s4.metric(
        "📈 Темп (в среднем за день)",
        f"{cal_data['avg_actions_per_active_day']} / день",
    )

    components.html(build_calendar_html(cal_data), height=390, scrolling=False)


def build_user_avatar_html(
    full_name: str, username: Optional[str], user_id: str
) -> str:
    short_uid = html.escape(str(user_id or "").strip()[-4:] or "0000")
    safe_name = html.escape(str(full_name or "").strip() or f"Ученик #{short_uid}")
    raw_uname = str(username or "").replace("@", "").strip()
    clean_uname = raw_uname if re.match(r"^[A-Za-z0-9_]{3,32}$", raw_uname) else ""
    safe_uname = html.escape(clean_uname)

    alnum_words = [
        "".join(ch for ch in part if ch.isalnum())
        for part in str(full_name or "").split()
    ]
    alnum_words = [w for w in alnum_words if w]
    initials = "".join(w[0].upper() for w in alnum_words[:2]) if alnum_words else ""

    if not initials:
        raw_first = str(full_name or "").strip()
        initials = raw_first[:2] if raw_first else "УЧ"

    initials_safe = html.escape(initials)

    avatar_badge = (
        f'<div style="width: 48px; height: 48px; border-radius: 50%; '
        f"background: linear-gradient(135deg, #4f46e5, #6366f1); color: #ffffff; "
        f"display: flex; align-items: center; justify-content: center; "
        f"font-weight: 700; font-size: 1.05rem; border: 2px solid #818cf8; "
        f'flex-shrink: 0; box-shadow: 0 2px 8px rgba(79, 70, 229, 0.25);">'
        f"{initials_safe}</div>"
    )
    subtitle = f"@{safe_uname}" if safe_uname else f"Профиль #{short_uid}"
    return f"""
    <div class="user-avatar-card">
        {avatar_badge}
        <div>
            <div style="font-size: 1.15rem; font-weight: 600; line-height: 1.2;">{safe_name}</div>
            <div class="user-avatar-subtitle">{subtitle}</div>
        </div>
    </div>
    """


def render_user_avatar_badge(full_name: str, username: Optional[str], user_id: str):
    st.markdown(
        build_user_avatar_html(full_name, username, user_id), unsafe_allow_html=True
    )


def build_class_summary_df(
    class_student_ids: List[str], class_df: pd.DataFrame
) -> pd.DataFrame:
    summary_rows = []
    for uid in class_student_ids:
        u_df = (
            class_df[class_df["user_id"] == uid]
            if not class_df.empty
            else pd.DataFrame()
        )
        if not u_df.empty:
            r0 = u_df.iloc[0]
            u_total = len(u_df)
            u_err = int(u_df["is_problem"].sum())
            u_acc = round((1 - (u_err / u_total)) * 100, 1) if u_total > 0 else 0.0
            u_valid_time = u_df[
                (u_df["time_sec"] >= 0) & (u_df["time_sec"] <= 180.0)
            ]["time_sec"]
            u_time = (
                round(float(u_valid_time.mean()), 1)
                if not u_valid_time.empty
                else 0.0
            )
            uname_raw = str(r0["username"]).lstrip("@") if r0["username"] else ""
            safe_excel_name = (
                str(r0["full_name"] or "").strip().lstrip("=+-@")
                or f"Ученик #{str(uid)[-4:]}"
            )
            safe_excel_uname = f"@{uname_raw}" if uname_raw else "—"
            summary_rows.append(
                {
                    "Ученик": safe_excel_name,
                    "Юзернейм": safe_excel_uname,
                    "Всего ответов": u_total,
                    "Точность (%)": u_acc,
                    "Ошибок": u_err,
                    "Ср. время (сек)": u_time,
                }
            )
        else:
            summary_rows.append(
                {
                    "Ученик": f"Ученик #{str(uid)[-4:]}",
                    "Юзернейм": "—",
                    "Всего ответов": 0,
                    "Точность (%)": 0.0,
                    "Ошибок": 0,
                    "Ср. время (сек)": 0.0,
                }
            )
    return pd.DataFrame(summary_rows)


def _format_compact_terms_df(term_problems_df: pd.DataFrame) -> pd.DataFrame:
    """Форматирует таблицу терминов компактно (4 колонки без горизонтального скролла)."""
    if term_problems_df.empty:
        return pd.DataFrame(
            columns=["Термин", "Блок", "Ошибки / Попытки", "% ошибок"]
        )
    out = pd.DataFrame()
    out["Термин"] = term_problems_df["term"].astype(str)
    out["Блок"] = term_problems_df["module"].map(
        lambda m: MODULE_SHORT_BADGE.get(str(m), str(m))
    )
    out["Ошибки / Попытки"] = (
        term_problems_df["errors"].astype(int).astype(str)
        + " из "
        + term_problems_df["total_tries"].astype(int).astype(str)
        + " ("
        + term_problems_df["avg_seconds"].round(1).astype(str)
        + " с)"
    )
    out["% ошибок"] = term_problems_df["error_rate"].astype(float)
    return out


def _render_responsive_terms_table(term_problems_df: pd.DataFrame):
    """Рендерит адаптивную таблицу терминов, которая на 100% помещается на экране смартфона."""
    if term_problems_df.empty:
        st.info("Нет данных для отображения.")
        return

    rows_html = []
    for _, r in term_problems_df.iterrows():
        term_str = html.escape(str(r.get("term", "")))
        raw_mod = str(r.get("module", ""))
        mod_short = html.escape(MODULE_SHORT_BADGE.get(raw_mod, raw_mod))
        err_cnt = int(r.get("errors", 0))
        tries_cnt = int(r.get("total_tries", 0))
        avg_sec = round(float(r.get("avg_seconds", 0.0)), 1)
        err_rate = max(0.0, min(100.0, round(float(r.get("error_rate", 0.0)), 0)))

        rows_html.append(
            f"<tr>"
            f"<td class='terms-col-term'>{term_str}</td>"
            f"<td class='terms-col-mod'>{mod_short}</td>"
            f"<td class='terms-col-stat'>"
            f"<div class='terms-stat-top'>"
            f"<span>{err_cnt}/{tries_cnt} ({avg_sec}с)</span>"
            f"<span class='terms-pct-badge'>{int(err_rate)}%</span>"
            f"</div>"
            f"<div class='terms-bar-bg'><div class='terms-bar-fill' style='width:{err_rate}%;'></div></div>"
            f"</td>"
            f"</tr>"
        )

    table_html = (
        "<div class='terms-table-wrap'>"
        "<table class='terms-table'>"
        "<thead><tr>"
        "<th class='terms-col-term'>Термин</th>"
        "<th class='terms-col-mod'>Блок</th>"
        "<th class='terms-col-stat'>Ошибки • %</th>"
        "</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody>"
        "</table>"
        "</div>"
    )
    st.markdown(table_html, unsafe_allow_html=True)


def render_analytics_dashboard(
    df: pd.DataFrame,
    is_class_view: bool = False,
    prev_df: Optional[pd.DataFrame] = None,
    period_label: str = "За всё время",
    key_prefix: str = "main",
):
    if df.empty:
        st.warning("Нет данных за выбранный период.")
        return

    st.caption(f"📅 Отображаемый период: **{period_label}**")

    c1, c2, c3, c4 = st.columns(4)
    total_answers = len(df)
    valid_times = df[(df["time_sec"] >= 0) & (df["time_sec"] <= 180.0)]["time_sec"]
    avg_time = round(float(valid_times.mean()), 1) if not valid_times.empty else 0.0
    errors_count = int(df["is_problem"].sum())
    accuracy = (
        round((1 - (errors_count / total_answers)) * 100, 1)
        if total_answers > 0
        else 0.0
    )

    delta_total_str, delta_acc_str, delta_time_str, delta_err_str = (
        None,
        None,
        None,
        None,
    )
    if prev_df is not None and not prev_df.empty:
        p_total = len(prev_df)
        p_err = int(prev_df["is_problem"].sum())
        p_acc = round((1 - (p_err / p_total)) * 100, 1) if p_total > 0 else 0.0
        p_valid_t = prev_df[
            (prev_df["time_sec"] >= 0) & (prev_df["time_sec"] <= 180.0)
        ]["time_sec"]
        p_time = round(float(p_valid_t.mean()), 1) if not p_valid_t.empty else 0.0
        delta_total_str = f"{total_answers - p_total:+d} к прошл. периоду"
        delta_acc_str = f"{round(accuracy - p_acc, 1):+.1f}% к прошл. периоду"
        delta_time_str = f"{round(avg_time - p_time, 1):+.1f} сек"
        delta_err_str = f"{errors_count - p_err:+d}"

    c1.metric("Всего ответов", total_answers, delta=delta_total_str)
    c2.metric("Успешность / Точность", f"{accuracy}%", delta=delta_acc_str)
    c3.metric(
        "Среднее время", f"{avg_time} сек", delta=delta_time_str, delta_color="inverse"
    )
    c4.metric(
        "Ошибок / Затруднений",
        errors_count,
        delta=delta_err_str,
        delta_color="inverse",
    )

    st.divider()

    plotly_cfg = {
        "displayModeBar": False,
        "responsive": True,
        "staticPlot": True,
        "scrollZoom": False,
        "doubleClick": False,
        "showAxisDragHandles": False,
    }
    pie_common_layout = dict(
        height=235,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(t=6, b=36, l=8, r=8),
        hovermode=False,
        dragmode=False,
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.04,
            xanchor="center",
            x=0.5,
            font=dict(size=12),
            itemclick=False,
            itemdoubleclick=False,
        ),
    )

    col_pie1, col_pie2 = st.columns(2)
    with col_pie1:
        st.subheader("🥧 Качество ответов")
        fig_status = px.pie(
            df,
            names="status",
            color="status",
            color_discrete_map={
                "Верно / Знаю": "#2ecc71",
                "Неуверенно": "#f1c40f",
                "Ошибка / Не знаю": "#e74c3c",
            },
            hole=0.44,
        )
        fig_status.update_traces(
            domain=dict(x=[0.08, 0.92], y=[0.08, 0.96]),
            textposition="inside",
            textinfo="percent",
            hoverinfo="skip",
            hovertemplate=None,
        )
        fig_status.update_layout(**pie_common_layout)
        st.plotly_chart(fig_status, use_container_width=True, config=plotly_cfg)

    with col_pie2:
        title_suffix = "для класса" if is_class_view else "по блокам"
        st.subheader(f"🥧 Ошибки {title_suffix}")
        problem_df = df[df["is_problem"]].copy()
        if not problem_df.empty:
            problem_df["module_short"] = problem_df["module"].map(
                lambda m: MODULE_SHORT_BADGE.get(str(m), str(m))
            )
            fig_modules = px.pie(problem_df, names="module_short", hole=0.44)
            fig_modules.update_traces(
                domain=dict(x=[0.08, 0.92], y=[0.08, 0.96]),
                textposition="inside",
                textinfo="percent",
                hoverinfo="skip",
                hovertemplate=None,
            )
            fig_modules.update_layout(**pie_common_layout)
            st.plotly_chart(fig_modules, use_container_width=True, config=plotly_cfg)
        else:
            st.success(
                "🎉 Затруднений и ошибок за выбранный период нет — все ответы уверенные!"
            )

    st.divider()

    df_calc = df.copy()
    df_calc["time_sec_clamped"] = df_calc["time_sec"].clip(0, 180)
    term_problems = (
        df_calc.groupby(["code", "term", "module"])
        .agg(
            total_tries=("status", "count"),
            errors=("is_problem", "sum"),
            avg_seconds=("time_sec_clamped", "mean"),
        )
        .reset_index()
    )
    term_problems["error_rate"] = (
        term_problems["errors"] / term_problems["total_tries"] * 100
    ).round(1)
    term_problems["avg_seconds"] = term_problems["avg_seconds"].round(1)

    col_bar1, col_bar2 = st.columns([1.15, 0.85])

    with col_bar1:
        table_title = (
            "⚠️ Топ-10 трудных терминов для класса"
            if is_class_view
            else "⚠️ Ваши трудные термины"
        )
        st.subheader(table_title)
        top_problematic = term_problems.sort_values(
            by=["errors", "error_rate"], ascending=False
        ).head(10)
        _render_responsive_terms_table(top_problematic)

    with col_bar2:
        st.subheader("⏱ Скорость ответа по блокам")
        module_time = (
            df_calc.groupby("module")["time_sec_clamped"]
            .mean()
            .round(1)
            .reset_index()
        )
        module_time["module_short"] = module_time["module"].map(
            lambda m: MODULE_SHORT_BADGE.get(str(m), str(m))
        )
        fig_time = px.bar(
            module_time,
            x="module_short",
            y="time_sec_clamped",
            text="time_sec_clamped",
            labels={"module_short": "Блок", "time_sec_clamped": "Секунды"},
            color="time_sec_clamped",
            color_continuous_scale="Blues",
        )
        fig_time.update_traces(
            texttemplate="%{text}с",
            textposition="outside",
            cliponaxis=False,
            hoverinfo="skip",
            hovertemplate=None,
        )
        max_y = (
            float(module_time["time_sec_clamped"].max())
            if not module_time.empty
            else 10.0
        )
        fig_time.update_layout(
            height=250,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(t=22, b=20, l=10, r=10),
            coloraxis_showscale=False,
            hovermode=False,
            dragmode=False,
            xaxis=dict(fixedrange=True, title="Блок"),
            yaxis=dict(
                fixedrange=True, title="Секунды", range=[0, max(5.0, max_y * 1.22)]
            ),
        )
        st.plotly_chart(fig_time, use_container_width=True, config=plotly_cfg)

    # --- УМНЫЙ ПОИСК ТЕРМИНОВ ПО НАЗВАНИЮ (ОТ 3 БУКВ, С ЗАЩИТОЙ ОТ ОПЕЧАТОК) ---
    st.divider()
    st.subheader("🔎 Поиск статистики по конкретному термину")
    search_q = st.text_input(
        "Введите название термина:",
        key=f"{key_prefix}_term_fuzzy_search",
    ).strip()

    if search_q:
        clean_q = _normalize_search_str(search_q[:60]).strip()
        if len(clean_q) < 3:
            st.info("Введите минимум 3 буквы для запуска умного поиска.")
        else:
            scored_rows = []
            for idx_row, r in term_problems.iterrows():
                sc = compute_fuzzy_similarity(clean_q, str(r["term"]))
                if clean_q == str(r["code"]).lower():
                    sc = 1.0
                if sc >= 0.55:
                    row_copy = r.copy()
                    row_copy["_score"] = sc
                    scored_rows.append(row_copy)

            if scored_rows:
                matched_df = (
                    pd.DataFrame(scored_rows)
                    .sort_values(by=["_score", "total_tries"], ascending=False)
                    .head(10)
                )
                _render_responsive_terms_table(matched_df)
            else:
                cat_matches = []
                for c_code, c_info in TERMS_MAP.items():
                    sc = compute_fuzzy_similarity(clean_q, c_info["term"])
                    if sc >= 0.55:
                        cat_matches.append((c_info["term"], c_info["module"], sc))
                cat_matches.sort(key=lambda x: x[2], reverse=True)
                if cat_matches:
                    best_name, best_mod, _ = cat_matches[0]
                    st.info(
                        f"Термин **«{best_name}»** ({best_mod}) найден в кодификаторе, но за выбранный период ответов по нему ещё не было."
                    )
                else:
                    st.warning("Термин не найден. Проверьте написание.")


# =====================================================================
# 5. ИНТЕРФЕЙС И РОУТИНГ ПО РОЛЯМ (ВЕРСИЯ 7.6)
# =====================================================================

def run_dashboard_app():
    query_params = st.query_params
    student_param = _get_query_param(query_params, "student")
    token_param = _get_query_param(query_params, "token")
    teacher_param = _get_query_param(query_params, "teacher")

    # Очищаем открытый GET-параметр ?admin= из адресной строки, если он был передан
    if _get_query_param(query_params, "admin") is not None:
        try:
            if "admin" in st.query_params:
                del st.query_params["admin"]
        except Exception:
            pass

    # -----------------------------------------------------------------
    # СЦЕНАРИЙ 1: ПЕРСОНАЛЬНЫЙ КАБИНЕТ УЧЕНИКА (И TELEGRAM MINI APP)
    # -----------------------------------------------------------------
    if student_param:
        real_uid = decode_user_id(str(student_param).strip())
        if not real_uid:
            st.error("🔒 Ошибка расшифровки идентификатора: неверная ссылка.")
            st.stop()
            return

        student_id_str = str(real_uid)
        st.title("🎓 Личный кабинет ученика")

        if not verify_student_access(student_id_str, token_param):
            st.error("🔒 Доступ ограничен: ссылка устарела или была сброшена.")
            st.stop()
            return

        student_df = fetch_student_data(student_id_str)
        if student_df.empty:
            st.warning("Вы пока не совершили ни одного действия в тренажёре.")
            st.stop()
            return

        sample_row = student_df.iloc[0]
        render_user_avatar_badge(
            sample_row["full_name"], sample_row["username"], student_id_str
        )

        with st.popover("⚙️ Параметры и фильтры", use_container_width=True):
            st.subheader("Настройки отображения")
            mode_options = ["Все режимы", "Только карточки", "Только тесты"]
            selected_mode = st.selectbox(
                "Режим тренировки:", mode_options, key="student_mode"
            )

            st.divider()
            period_choice = st.selectbox(
                "Период для графиков:", TIME_WINDOW_OPTIONS, key="student_period"
            )
            custom_dates = None
            if period_choice == "Свой диапазон дат":
                today_d = datetime.now(TZ).date()
                custom_dates = st.date_input(
                    "Выберите даты:",
                    value=(today_d - timedelta(days=14), today_d),
                    key="student_custom_dates",
                )

            st.divider()
            if selected_mode == "Только карточки":
                mode_filtered_df = student_df[student_df["mode"] == "Карточки"]
            elif selected_mode == "Только тесты":
                mode_filtered_df = student_df[student_df["mode"] == "Тесты"]
            else:
                mode_filtered_df = student_df

            window_df, prev_window_df, period_label = filter_df_by_time_window(
                mode_filtered_df, period_choice, custom_dates
            )

            if st.button(
                "🔄 Обновить данные",
                key="student_refresh_btn",
                use_container_width=True,
            ):
                st.cache_data.clear()
                st.rerun()

        if window_df.empty:
            st.warning(
                f"За выбранный период («{period_label}») по режиму «{selected_mode}» у вас пока нет ответов."
            )
        else:
            render_analytics_dashboard(
                window_df,
                is_class_view=False,
                prev_df=prev_window_df,
                period_label=period_label,
                key_prefix="student",
            )

        render_activity_calendar(
            unfiltered_date_df=student_df,
            default_mode=selected_mode,
            key_prefix="student_gh",
        )
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

        teacher_name, class_student_ids, class_df = fetch_teacher_class_data(
            teacher_code
        )
        st.caption(f"Просмотр: **{teacher_name}**")

        if not class_student_ids:
            st.info(
                f"ℹ️ К классу `{teacher_code}` пока не прикрепился ни один ученик."
            )
            st.stop()
            return

        with st.popover("⚙️ Параметры и фильтры", use_container_width=True):
            st.subheader("Настройки отображения")

            student_labels = {"👥 Весь класс (Сводка)": "ALL"}
            for uid in class_student_ids:
                sub = class_df[class_df["user_id"] == uid]
                if not sub.empty:
                    row_s = sub.iloc[0]
                    uname_clean = (
                        str(row_s["username"]).lstrip("@")
                        if row_s["username"]
                        else ""
                    )
                    label = f"{row_s['full_name']} ({'@' + uname_clean if uname_clean else '#' + str(uid)[-4:]})"
                else:
                    label = f"Ученик #{str(uid)[-4:]} (0 ответов)"
                student_labels[label] = uid

            selected_student_label = st.selectbox(
                "Выберите ученика:",
                list(student_labels.keys()),
                key="teacher_student_sel",
            )
            selected_uid = student_labels[selected_student_label]

            mode_options = ["Все режимы", "Только карточки", "Только тесты"]
            selected_mode = st.selectbox(
                "Режим тренировки:", mode_options, key="teacher_mode"
            )

            st.divider()
            period_choice = st.selectbox(
                "Период для графиков:", TIME_WINDOW_OPTIONS, key="teacher_period"
            )
            custom_dates = None
            if period_choice == "Свой диапазон дат":
                today_d = datetime.now(TZ).date()
                custom_dates = st.date_input(
                    "Выберите даты:",
                    value=(today_d - timedelta(days=14), today_d),
                    key="teacher_custom_dates",
                )

            st.divider()
            entity_unfiltered_df = (
                class_df
                if selected_uid == "ALL"
                else class_df[class_df["user_id"] == selected_uid]
            )

            if selected_mode == "Только карточки":
                mode_entity_df = entity_unfiltered_df[
                    entity_unfiltered_df["mode"] == "Карточки"
                ]
            elif selected_mode == "Только тесты":
                mode_entity_df = entity_unfiltered_df[
                    entity_unfiltered_df["mode"] == "Тесты"
                ]
            else:
                mode_entity_df = entity_unfiltered_df

            active_window_df, prev_window_df, period_label = filter_df_by_time_window(
                mode_entity_df, period_choice, custom_dates
            )

            class_mode_df = (
                class_df[class_df["mode"] == "Карточки"]
                if selected_mode == "Только карточки"
                else (
                    class_df[class_df["mode"] == "Тесты"]
                    if selected_mode == "Только тесты"
                    else class_df
                )
            )
            class_window_df, _, _ = filter_df_by_time_window(
                class_mode_df, period_choice, custom_dates
            )
            report_df = build_class_summary_df(class_student_ids, class_window_df)
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                report_df.to_excel(writer, index=False, sheet_name="Ведомость класса")
            st.download_button(
                "📥 Скачать ведомость (.xlsx)",
                data=buffer.getvalue(),
                file_name=f"Класс_{teacher_code}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

            if st.button(
                "🔄 Обновить данные",
                key="teacher_refresh_btn",
                use_container_width=True,
            ):
                st.cache_data.clear()
                st.rerun()

        if selected_uid != "ALL":
            if not entity_unfiltered_df.empty:
                s_row = entity_unfiltered_df.iloc[0]
                render_user_avatar_badge(
                    s_row["full_name"], s_row["username"], selected_uid
                )
            else:
                render_user_avatar_badge(
                    f"Ученик #{str(selected_uid)[-4:]}", None, selected_uid
                )

        if active_window_df.empty:
            st.warning(f"За выбранный период («{period_label}») пока нет данных.")
        else:
            render_analytics_dashboard(
                active_window_df,
                is_class_view=(selected_uid == "ALL"),
                prev_df=prev_window_df,
                period_label=period_label,
                key_prefix="teacher",
            )

        render_activity_calendar(
            unfiltered_date_df=entity_unfiltered_df,
            default_mode=selected_mode,
            key_prefix="teacher_gh",
        )
        st.stop()
        return

    # -----------------------------------------------------------------
    # СЦЕНАРИЙ 3: АДМИН-ПАНЕЛЬ И ГЛАВНАЯ (ЗАЩИТА ОТ БРУТФОРСА)
    # -----------------------------------------------------------------
    else:
        if "is_admin_authed" not in st.session_state:
            st.session_state["is_admin_authed"] = False
        if "admin_failed_attempts" not in st.session_state:
            st.session_state["admin_failed_attempts"] = 0
        if "admin_lockout_until" not in st.session_state:
            st.session_state["admin_lockout_until"] = 0.0

        if not st.session_state["is_admin_authed"]:
            st.title("📊 Аналитический портал тренажера")
            st.write(
                "Добро пожаловать! Выберите свой раздел или войдите как администратор."
            )

            c_info1, c_info2 = st.columns(2)
            with c_info1:
                st.info(
                    "🎓 **Для учеников:**\nОткройте Telegram-бот и нажмите «📊 Моя статистика» → «📱 Открыть веб-аналитику»."
                )
            with c_info2:
                st.success(
                    "👨‍🏫 **Для преподавателей:**\nИспользуйте персональную ссылку, которую бот выдал по команде `/teacher`."
                )

            with st.popover("🔒 Вход для администратора", use_container_width=True):
                admin_input = st.text_input(
                    "Пароль:", type="password", key="admin_pass"
                )
                if st.button("Войти", use_container_width=True):
                    now_ts = time.time()
                    lock_until = float(
                        st.session_state.get("admin_lockout_until", 0.0)
                    )
                    if now_ts < lock_until:
                        wait_sec = int(lock_until - now_ts) + 1
                        st.error(
                            f"⏳ Слишком много попыток. Повторите через {wait_sec} сек."
                        )
                    elif verify_secret(admin_input, ADMIN_PASSWORD):
                        st.session_state["is_admin_authed"] = True
                        st.session_state["admin_failed_attempts"] = 0
                        st.session_state["admin_lockout_until"] = 0.0
                        st.rerun()
                    else:
                        time.sleep(2.0)
                        fails = int(st.session_state.get("admin_failed_attempts", 0)) + 1
                        st.session_state["admin_failed_attempts"] = fails
                        if fails >= 5:
                            st.session_state["admin_lockout_until"] = time.time() + 60.0
                            st.error(
                                "🔒 Превышен лимит попыток. Вход заблокирован на 60 секунд."
                            )
                        else:
                            st.error("Неверный пароль.")
            st.stop()
            return

        st.title("🛡 Глобальная панель администратора")
        if st.button("🚪 Выйти из панели", key="admin_logout_btn"):
            st.session_state["is_admin_authed"] = False
            try:
                st.query_params.clear()
            except Exception:
                pass
            st.rerun()
            return

        raw_df, teachers_df, st_teachers_df = fetch_all_data()

        if raw_df.empty:
            st.info("ℹ️ В базе пока нет ответов.")
            st.stop()
            return

        user_labels_map = {}
        for uid in raw_df["user_id"].unique():
            sub_df = raw_df[raw_df["user_id"] == uid]
            r0 = sub_df.iloc[0]
            fname = r0["full_name"]
            uname_clean = (
                str(r0["username"]).lstrip("@") if r0["username"] else ""
            )
            uname = f"@{uname_clean}" if uname_clean else f"Профиль #{str(uid)[-4:]}"
            user_labels_map[f"{fname} ({uname})"] = uid

        with st.popover("⚙️ Параметры и фильтры", use_container_width=True):
            st.subheader("Настройки отображения")

            users_options = ["Все пользователи"] + sorted(
                list(user_labels_map.keys())
            )
            selected_option = st.selectbox(
                "Выберите пользователя:", users_options, key="admin_user_sel"
            )

            mode_options = ["Все режимы", "Только карточки", "Только тесты"]
            selected_mode = st.selectbox(
                "Режим тренировки:", mode_options, key="admin_mode"
            )

            st.divider()
            period_choice = st.selectbox(
                "Период для графиков:", TIME_WINDOW_OPTIONS, key="admin_period"
            )
            custom_dates = None
            if period_choice == "Свой диапазон дат":
                today_d = datetime.now(TZ).date()
                custom_dates = st.date_input(
                    "Выберите даты:",
                    value=(today_d - timedelta(days=14), today_d),
                    key="admin_custom_dates",
                )

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

            window_df, prev_window_df, period_label = filter_df_by_time_window(
                mode_df, period_choice, custom_dates
            )

        if selected_option != "Все пользователи" and not entity_df.empty:
            s_row = entity_df.iloc[0]
            render_user_avatar_badge(
                s_row["full_name"], s_row["username"], s_row["user_id"]
            )

        if window_df.empty:
            st.warning(f"За выбранный период («{period_label}») пока нет данных.")
        else:
            render_analytics_dashboard(
                window_df,
                is_class_view=(selected_option == "Все пользователи"),
                prev_df=prev_window_df,
                period_label=period_label,
                key_prefix="admin",
            )

        render_activity_calendar(
            unfiltered_date_df=entity_df,
            default_mode=selected_mode,
            key_prefix="admin_gh",
        )


if __name__ == "__main__" or (
    "streamlit" in sys.modules if "sys" in globals() else True
):
    if os.getenv("DASHBOARD_SKIP_AUTORUN") != "1":
        run_dashboard_app()
