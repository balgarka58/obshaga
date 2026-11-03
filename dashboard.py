import io
import os
import re
import sys
import json
import html
import hmac
import base64
import hashlib
import difflib
import sqlite3
import calendar
import warnings
import urllib.parse
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List, Tuple, Set
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
# 1. НАСТРОЙКА СТРАНИЦЫ, АВТО-ТЕМЫ УСТРОЙСТВА И БЕЗОПАСНОСТИ (ВЕРСИЯ 7.4)
# =====================================================================

st.set_page_config(
    page_title="Аналитика терминов по обществознанию",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Скрываем служебные элементы Streamlit + адаптивная вёрстка под Telegram Mini App и системную тему
st.markdown("""
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
    /* Если в горизонтальном блоке всего 2 колонки (графики/таблицы) — делаем на всю ширину */
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
""", unsafe_allow_html=True)

DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH = os.getenv("DB_PATH", "social_bot.db")
TERMS_JSON_PATH = os.getenv("TERMS_JSON_PATH", "terms.json")
SECRET_KEY = os.getenv("SECRET_KEY")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
if not SECRET_KEY:
    raise RuntimeError("Переменная окружения SECRET_KEY не задана!")
if not ADMIN_PASSWORD:
    raise RuntimeError("Переменная окружения ADMIN_PASSWORD не задана!")
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
MONTH_FULL_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
    7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
}
MODULE_SHORT_BADGE = {
    "1. Человек и общество": "Общество",
    "2. Экономика": "Экономика",
    "3. Социальные отношения": "Социум",
    "4. Политика": "Политика",
    "5. Право": "Право",
}


def normalize_neon_pooler_url(db_url: Optional[str]) -> Optional[str]:
    """Автоматически направляет подключения к Neon через Connection Pooler (-pooler)."""
    if not db_url or not db_url.startswith("postgres"):
        return db_url
    if os.getenv("NEON_USE_POOLER", "1") == "0":
        return db_url
    try:
        parsed = urllib.parse.urlsplit(db_url)
        host = parsed.hostname or ""
        if "neon.tech" in host and "-pooler." not in host and host.startswith("ep-"):
            parts = host.split(".", 1)
            new_host = f"{parts[0]}-pooler.{parts[1]}"
            netloc = parsed.netloc.replace(host, new_host, 1)
            return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    except Exception:
        pass
    return db_url


def verify_secret(provided: Optional[str], expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(
        str(provided).strip().encode("utf-8"),
        str(expected).strip().encode("utf-8")
    )


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


def generate_student_token(user_id: Any, token_version: int = 1, secret_key: Optional[str] = None) -> str:
    key = str(secret_key if secret_key is not None else os.getenv("SECRET_KEY", SECRET_KEY))
    uid_str = str(user_id).strip()
    try:
        ver = max(1, int(token_version or 1))
    except (ValueError, TypeError):
        ver = 1
    msg = f"student:{uid_str}:v{ver}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]


def generate_teacher_token(teacher_code: Any, token_version: int = 1, secret_key: Optional[str] = None) -> str:
    key = str(secret_key if secret_key is not None else os.getenv("SECRET_KEY", SECRET_KEY))
    code_str = str(teacher_code).strip()
    try:
        ver = max(1, int(token_version or 1))
    except (ValueError, TypeError):
        ver = 1
    msg = f"teacher:{code_str}:v{ver}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]


def decode_user_id(encoded_str: str, secret_key: Optional[str] = None) -> Optional[int]:
    """Расшифровывает безопасный строковый токен обратно в Telegram user_id."""
    key = secret_key or SECRET_KEY
    if not encoded_str or not key:
        return None
    try:
        salt = hashlib.sha256(key.encode("utf-8")).digest()
        padded = encoded_str + "=" * (-len(encoded_str) % 4)
        masked = base64.urlsafe_b64decode(padded.encode("utf-8"))
        raw_uid = bytes([b ^ salt[i % len(salt)] for i, b in enumerate(masked)]).decode("utf-8")
        val = int(raw_uid)
        return val if val > 0 else None
    except Exception:
        return None


# =====================================================================
# 2. МЕТАДАННЫЕ ТЕРМИНОВ И БЫСТРЫЙ НЕЧЁТКИЙ ПОИСК С ИНДЕКСОМ ТРИГРАММ
# =====================================================================

_SEARCH_CLEAN_RE = re.compile(r"[^a-zа-я0-9\s-]")
_MULTISPACE_RE = re.compile(r"\s+")
_WORD_SPLIT_RE = re.compile(r"[\s-]+")


def _normalize_search_str(s: str) -> str:
    s = str(s or "").lower().replace("ё", "е").strip()
    return _MULTISPACE_RE.sub(" ", _SEARCH_CLEAN_RE.sub(" ", s)).strip()


def _get_trigrams(s: str) -> Set[str]:
    clean = _normalize_search_str(s)
    if not clean:
        return set()
    padded = f"  {clean} "
    if len(padded) < 3:
        return {padded}
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


def _compute_similarity_preindexed(
    q_norm: str,
    q_tri: Set[str],
    t_norm: str,
    t_tri: Set[str],
    t_words_indexed: List[Tuple[str, Set[str]]],
    threshold: float = 0.0,
) -> float:
    """Сверхбыстрый расчёт нечёткого сходства с отсечением по верхней границе quick_ratio."""
    if not q_norm or not t_norm:
        return 0.0
    if q_norm == t_norm:
        return 1.0
    if t_norm.startswith(q_norm):
        return 0.96
    if q_norm in t_norm:
        return 0.90

    inter_len = len(q_tri & t_tri) if (q_tri and t_tri) else 0
    union_len = len(q_tri) + len(t_tri) - inter_len
    tri_score = inter_len / max(1, union_len)

    sm_full = difflib.SequenceMatcher(None, q_norm, t_norm)
    if threshold > 0.0 and (sm_full.real_quick_ratio() * 0.6 + tri_score * 0.4) < threshold:
        seq_full = 0.0
    elif threshold > 0.0 and (sm_full.quick_ratio() * 0.6 + tri_score * 0.4) < threshold:
        seq_full = 0.0
    else:
        seq_full = sm_full.ratio()

    full_combined = (seq_full * 0.6) + (tri_score * 0.4)
    word_best = 0.0
    eff_thresh = max(threshold, full_combined)

    for w, w_tri in t_words_indexed:
        if w.startswith(q_norm):
            word_best = max(word_best, 0.93)
            eff_thresh = max(eff_thresh, 0.93)
        else:
            w_inter = len(q_tri & w_tri) if (q_tri and w_tri) else 0
            w_union = len(q_tri) + len(w_tri) - w_inter
            w_tri_score = w_inter / max(1, w_union)
            if (0.65 + w_tri_score * 0.35) < eff_thresh:
                continue
            sm_w = difflib.SequenceMatcher(None, q_norm, w)
            if (sm_w.real_quick_ratio() * 0.65 + w_tri_score * 0.35) < eff_thresh:
                continue
            if (sm_w.quick_ratio() * 0.65 + w_tri_score * 0.35) < eff_thresh:
                continue
            r = sm_w.ratio()
            sc = (r * 0.65) + (w_tri_score * 0.35)
            if sc > word_best:
                word_best = sc
                if sc > eff_thresh:
                    eff_thresh = sc

    return round(max(full_combined, word_best), 4)


def compute_fuzzy_similarity(query: str, target: str) -> float:
    q_norm = _normalize_search_str(query)
    t_norm = _normalize_search_str(target)
    if not q_norm or not t_norm:
        return 0.0
    if q_norm == t_norm:
        return 1.0
    if t_norm.startswith(q_norm):
        return 0.96
    if q_norm in t_norm:
        return 0.90

    q_tri = _get_trigrams(q_norm)
    t_tri = _get_trigrams(t_norm)
    t_words = [(w, _get_trigrams(w)) for w in _WORD_SPLIT_RE.split(t_norm) if len(w) >= 3]
    return _compute_similarity_preindexed(q_norm, q_tri, t_norm, t_tri, t_words)


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


@st.cache_data(ttl=60)
def get_current_student_token_version(student_id: str) -> int:
    """Кэшированная на 60 сек проверка версии токена ученика (экономит Compute Hours Neon)."""
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


@st.cache_data(ttl=60)
def get_current_teacher_token_version(teacher_code: str) -> int:
    """Кэшированная на 60 сек проверка версии токена преподавателя."""
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
    "user_id", "username", "full_name", "mode", "code", "term",
    "module", "delta_ans", "delta_time", "time_sec", "status",
    "is_problem", "created_at"
]
TEACHER_COLUMNS = ["teacher_code", "user_id", "full_name", "token_version", "created_at"]
ST_COLUMNS = ["student_id", "teacher_code", "joined_at"]


def _parse_answers_and_archive_rows(
    ans_rows: List[Tuple[Any, ...]],
    arch_rows: List[Tuple[Any, ...]]
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

    def _clean_profile_fields(uid_str: str, raw_u: Any, raw_f: Any) -> Tuple[Optional[str], str]:
        u_val = str(raw_u).strip() if raw_u is not None and pd.notna(raw_u) else ""
        if u_val in ("None", "nan", ""):
            u_val = None
        f_val = str(raw_f).strip() if raw_f is not None and pd.notna(raw_f) else ""
        if f_val in ("None", "nan", ""):
            f_val = ""
        f_val = _anonymize_display_name(f_val)
        prev_u, prev_f = user_profiles.get(uid_str, (None, ""))
        # В выборке ORDER BY created_at DESC первой идёт самая свежая запись, сохраняем её приоритет
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

            parsed_rows.append({
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
                "created_at": created_at_val
            })

    for ar in arch_rows:
        uid_str = str(ar[0]).strip()
        username_val, fname_val = _clean_profile_fields(uid_str, ar[1], ar[2])
        arch_date = ar[3]
        raw_mode = str(ar[4] or "card").strip()
        code = str(ar[5] or "").strip()
        if not code:
            continue

        mode_display = "Тесты" if raw_mode == "test" else "Карточки"
        term_info = TERMS_MAP.get(code, {
            "term": f"Код {code}",
            "module": f"Модуль {code.split('.')[0] if '.' in code else '?'}"
        })

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
                created_ts = datetime(d_obj.year, d_obj.month, d_obj.day, 12, 0, 0, tzinfo=TZ).astimezone(timezone.utc).isoformat()
            except Exception:
                created_ts = f"{str(arch_date)[:10]} 12:00:00"
        else:
            created_ts = datetime.now(timezone.utc).isoformat()

        for _ in range(ok_cnt):
            parsed_rows.append({
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
            })

        err_status = "Неуверенно" if (0 < last_d_ans < 1.5 and raw_mode != "test") else "Ошибка / Не знаю"
        err_d_ans = last_d_ans if last_d_ans > 0 else 2.0
        for _ in range(err_cnt):
            parsed_rows.append({
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
            })

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


@st.cache_data(ttl=60)
def fetch_student_data(student_id: str) -> pd.DataFrame:
    """
    Точечно загружает из БД историю ТОЛЬКО одного конкретного ученика (WHERE user_id = ...).
    Снижает нагрузку на БД в сотни раз при открытии Mini App учеником.
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
                    (uid_int,)
                )
            else:
                cur.execute(
                    "SELECT user_id, username, full_name, entry, created_at FROM user_answers WHERE user_id = ? ORDER BY created_at DESC;",
                    (uid_int,)
                )
            ans_rows = cur.fetchall()
        except Exception:
            ans_rows = []

        try:
            if is_postgres:
                cur.execute("""
                    SELECT user_id, username, full_name, archive_date, mode, code, 
                           tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                    FROM user_daily_archive WHERE user_id = %s ORDER BY archive_date DESC;
                """, (uid_int,))
            else:
                cur.execute("""
                    SELECT user_id, username, full_name, archive_date, mode, code, 
                           tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                    FROM user_daily_archive WHERE user_id = ? ORDER BY archive_date DESC;
                """, (uid_int,))
            arch_rows = cur.fetchall()
        except Exception:
            arch_rows = []
    finally:
        cur.close()
        conn.close()

    return _parse_answers_and_archive_rows(ans_rows, arch_rows)


@st.cache_data(ttl=60)
def fetch_teacher_class_data(teacher_code: str) -> Tuple[str, List[str], pd.DataFrame]:
    """
    Точечно загружает из БД данные ТОЛЬКО одного класса преподавателя (WHERE user_id IN ...),
    не затрагивая ответы чужих учеников.
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
                cur.execute("SELECT full_name FROM teachers WHERE teacher_code = %s;", (code_clean,))
            else:
                cur.execute("SELECT full_name FROM teachers WHERE teacher_code = ?;", (code_clean,))
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
                cur.execute("SELECT student_id FROM student_teachers WHERE teacher_code = %s ORDER BY joined_at DESC;", (code_clean,))
            else:
                cur.execute("SELECT student_id FROM student_teachers WHERE teacher_code = ? ORDER BY joined_at DESC;", (code_clean,))
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
                        (uid_ints,)
                    )
                else:
                    placeholders = ",".join("?" for _ in uid_ints)
                    cur.execute(
                        f"SELECT user_id, username, full_name, entry, created_at FROM user_answers WHERE user_id IN ({placeholders}) ORDER BY created_at DESC;",
                        uid_ints
                    )
                ans_rows = cur.fetchall()
            except Exception:
                ans_rows = []

            try:
                if is_postgres:
                    cur.execute("""
                        SELECT user_id, username, full_name, archive_date, mode, code, 
                               tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                        FROM user_daily_archive WHERE user_id = ANY(%s) ORDER BY archive_date DESC;
                    """, (uid_ints,))
                else:
                    placeholders = ",".join("?" for _ in uid_ints)
                    cur.execute(f"""
                        SELECT user_id, username, full_name, archive_date, mode, code, 
                               tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                        FROM user_daily_archive WHERE user_id IN ({placeholders}) ORDER BY archive_date DESC;
                    """, uid_ints)
                arch_rows = cur.fetchall()
            except Exception:
                arch_rows = []
    finally:
        cur.close()
        conn.close()

    return teacher_name, class_student_ids, _parse_answers_and_archive_rows(ans_rows, arch_rows)


@st.cache_data(ttl=60)
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
            cur.execute("SELECT user_id, username, full_name, entry, created_at FROM user_answers ORDER BY created_at DESC;")
            ans_rows = cur.fetchall()
        except Exception:
            try:
                cur.execute("SELECT user_id, username, NULL as full_name, entry, created_at FROM user_answers ORDER BY created_at DESC;")
                ans_rows = cur.fetchall()
            except Exception:
                ans_rows = []

        try:
            cur.execute("""
                SELECT user_id, username, full_name, archive_date, mode, code, 
                       tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                FROM user_daily_archive ORDER BY archive_date DESC;
            """)
            arch_rows = cur.fetchall()
        except Exception:
            try:
                cur.execute("""
                    SELECT user_id, NULL as username, NULL as full_name, archive_date, mode, code, 
                           tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                    FROM user_daily_archive ORDER BY archive_date DESC;
                """)
                arch_rows = cur.fetchall()
            except Exception:
                arch_rows = []

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
        cur.close()
        conn.close()

    return _parse_answers_and_archive_rows(ans_rows, arch_rows), teachers_df, st_teachers_df


# =====================================================================
# 4. ФИЛЬТРАЦИЯ ПО ВРЕМЕННОМУ ОКНУ И КАЛЕНДАРЬ АКТИВНОСТИ (ВАРИАНТ Б + ГОД)
# =====================================================================

def _normalize_timestamps(series: pd.Series) -> pd.Series:
    try:
        parsed = pd.to_datetime(series, format="mixed", utc=True, errors="coerce")
    except TypeError:
        parsed = pd.to_datetime(series, utc=True, errors="coerce")
    return parsed.dt.tz_convert(TZ).dt.tz_localize(None)


def filter_df_by_time_window(
    df: pd.DataFrame,
    period_choice: str = "За всё время",
    custom_dates: Optional[Any] = None,
    reference_now: Optional[datetime] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, str]:
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
    """
    Формирует данные календаря активности:
    1. Точная разметка месяцев по дню недели, на который пришлось 1-е число месяца.
    2. Помесячные данные за последние 12 месяцев для мобильного вида (Вариант Б)
       со стрелочками переключения месяцев без горизонтального скролла.
    """
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
            if d_val <= today_d:
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
    elif (today_d - timedelta(days=1)) in daily_counts and daily_counts[(today_d - timedelta(days=1))] > 0:
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
    last_labeled_ym: Optional[Tuple[int, int]] = None
    last_label_w_idx = -10

    for w_idx in range(weeks_count):
        week_monday = start_monday + timedelta(weeks=w_idx)
        week_dates = [week_monday + timedelta(days=i) for i in range(7)]
        first_of_month = next((d for d in week_dates if d.day == 1), None)
        if w_idx == 0:
            ym = (week_monday.year, week_monday.month)
            month_labels[w_idx] = MONTH_SHORT_RU.get(week_monday.month, "")
            last_labeled_ym = ym
            last_label_w_idx = w_idx
        elif first_of_month is not None:
            ym = (first_of_month.year, first_of_month.month)
            if ym != last_labeled_ym:
                if w_idx - last_label_w_idx >= 2:
                    month_labels[w_idx] = MONTH_SHORT_RU.get(first_of_month.month, "")
                    last_label_w_idx = w_idx
                last_labeled_ym = ym

        for d_idx in range(7):
            cur_d = week_dates[d_idx]
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
                tooltip = f"{d_fmt} ({w_name}): 0 действий"
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

    months_data: List[Dict[str, Any]] = []
    cur_y, cur_m = today_d.year, today_d.month
    ym_list: List[Tuple[int, int]] = []
    for _ in range(12):
        ym_list.append((cur_y, cur_m))
        cur_m -= 1
        if cur_m == 0:
            cur_m = 12
            cur_y -= 1
    ym_list.reverse()

    cal_obj = calendar.Calendar(firstweekday=0)
    for y_val, m_val in ym_list:
        m_weeks = cal_obj.monthdatescalendar(y_val, m_val)
        m_rows = []
        m_total_actions = 0
        m_active_days = 0
        for wk in m_weeks:
            wk_cells = []
            for d_obj in wk:
                in_month = (d_obj.month == m_val)
                is_fut = d_obj > today_d
                cnt = 0 if (not in_month or is_fut) else daily_counts.get(d_obj, 0)
                c_cards = 0 if (not in_month or is_fut) else daily_cards.get(d_obj, 0)
                c_tests = 0 if (not in_month or is_fut) else daily_tests.get(d_obj, 0)
                c_err = 0 if (not in_month or is_fut) else daily_errors.get(d_obj, 0)
                acc = round((1 - c_err / cnt) * 100, 1) if cnt > 0 else 0.0
                if in_month and not is_fut:
                    m_total_actions += cnt
                    if cnt > 0:
                        m_active_days += 1
                lvl = _get_level(cnt) if (in_month and not is_fut) else (-2 if not in_month else -1)
                wk_cells.append({
                    "day": d_obj.day if in_month else "",
                    "date_fmt": d_obj.strftime("%d.%m.%Y"),
                    "in_month": in_month,
                    "is_today": (d_obj == today_d and in_month),
                    "count": cnt,
                    "cards": c_cards,
                    "tests": c_tests,
                    "acc": acc,
                    "level": lvl,
                })
            m_rows.append(wk_cells)
        months_data.append({
            "title": f"{MONTH_FULL_RU.get(m_val, '')} {y_val}",
            "total_actions": m_total_actions,
            "active_days": m_active_days,
            "weeks": m_rows,
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
        "months_data": months_data,
    }


def build_calendar_html(cal_data: Dict[str, Any]) -> str:
    """Генерирует адаптивный виджет календаря активности v7.4 с защитой от XSS."""
    weeks_count = cal_data["weeks_count"]
    month_labels = cal_data["month_labels"]
    safe_mode = html.escape(cal_data["mode_label"], quote=True)

    cell_size = 13
    gap = 3
    label_width = 28

    month_cells_html = [f'<div style="width:{label_width}px;flex-shrink:0;"></div>']
    for w_idx in range(weeks_count):
        m_txt = html.escape(month_labels.get(w_idx, ""))
        month_cells_html.append(
            f'<div class="gh-month-lbl" style="width:{cell_size}px;">{m_txt}</div>'
        )

    grid_by_pos = {(c["weekday_idx"], c["week_idx"]): c for c in cal_data["cells"]}
    rows_html = []
    for d_idx in range(7):
        day_label = WEEKDAY_SHORT_RU[d_idx] if d_idx in (0, 2, 4, 6) else ""
        row_squares = [
            f'<div class="gh-day-lbl" style="width:{label_width}px;height:{cell_size}px;line-height:{cell_size}px;">{day_label}</div>'
        ]
        for w_idx in range(weeks_count):
            cell = grid_by_pos[(d_idx, w_idx)]
            lvl = cell["level"]
            lvl_cls = f"lvl-{lvl}" if lvl >= 0 else "lvl-future"
            safe_tip = html.escape(cell["tooltip"], quote=True)
            click_js = (
                f"showCalDetail('{cell['date_formatted']}', {int(cell['count'])}, "
                f"{int(cell['cards_count'])}, {int(cell['tests_count'])}, '{cell['accuracy']}', '{safe_mode}')"
            )
            row_squares.append(
                f'<div class="gh-cell {lvl_cls}" title="{safe_tip}" '
                f'data-date="{cell["date"]}" data-count="{int(cell["count"])}" '
                f'onclick="{click_js}"></div>'
            )
        rows_html.append(f'<div class="gh-row">{"".join(row_squares)}</div>')

    legend_squares = "".join([
        f'<span class="gh-cell lvl-{lvl}" style="cursor:default;"></span>'
        for lvl in [0, 1, 2, 3, 4]
    ])

    months_json = json.dumps(cal_data.get("months_data", []), ensure_ascii=False).replace("</", "<\\/")

    return f"""
    <style>
        :root {{
            --cal-bg: #ffffff;
            --cal-border: #e2e8f0;
            --cal-text: #0f172a;
            --cal-muted: #64748b;
            --cal-bar-bg: #f8fafc;
            --cal-btn-bg: #f1f5f9;
            --cal-btn-hover: #e2e8f0;
            --lvl-future: rgba(148, 163, 184, 0.08);
            --lvl-0: #ebedf0;
            --lvl-1: #9be9a8;
            --lvl-2: #40c463;
            --lvl-3: #30a14e;
            --lvl-4: #216e39;
            --lvl-text-active: #ffffff;
        }}
        @media (prefers-color-scheme: dark) {{
            :root {{
                --cal-bg: #0f172a;
                --cal-border: rgba(255, 255, 255, 0.10);
                --cal-text: #f1f5f9;
                --cal-muted: #94a3b8;
                --cal-bar-bg: rgba(255, 255, 255, 0.05);
                --cal-btn-bg: rgba(255, 255, 255, 0.08);
                --cal-btn-hover: rgba(255, 255, 255, 0.15);
                --lvl-future: rgba(255, 255, 255, 0.02);
                --lvl-0: #161b22;
                --lvl-1: #0e4429;
                --lvl-2: #006d32;
                --lvl-3: #26a641;
                --lvl-4: #39d353;
                --lvl-text-active: #ffffff;
            }}
        }}
        * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
        body {{ margin: 0; padding: 0; background: transparent; color: var(--cal-text); }}
        .cal-card {{
            background: var(--cal-bg);
            border: 1px solid var(--cal-border);
            border-radius: 14px;
            padding: 16px;
            width: 100%;
        }}
        /* Десктопная годовая сетка */
        .desktop-year-wrap {{
            display: flex;
            flex-direction: column;
            align-items: center;
            width: 100%;
            overflow-x: auto;
        }}
        .gh-grid-inner {{
            display: inline-block;
        }}
        .gh-months-row {{
            display: flex;
            gap: {gap}px;
            margin-bottom: 6px;
            height: 14px;
        }}
        .gh-month-lbl {{
            font-size: 10px;
            font-weight: 500;
            color: var(--cal-muted);
            text-align: left;
            overflow: visible;
            white-space: nowrap;
            flex-shrink: 0;
        }}
        .gh-row {{
            display: flex;
            gap: {gap}px;
            align-items: center;
            margin-bottom: {gap}px;
        }}
        .gh-day-lbl {{
            font-size: 10px;
            color: var(--cal-muted);
            padding-right: 6px;
            flex-shrink: 0;
            text-align: right;
        }}
        .gh-cell {{
            width: {cell_size}px;
            height: {cell_size}px;
            border-radius: 3px;
            cursor: pointer;
            flex-shrink: 0;
            transition: transform 0.1s ease;
        }}
        .gh-cell:hover {{
            transform: scale(1.22);
            outline: 1.5px solid var(--cal-text);
            z-index: 2;
        }}
        .lvl-future {{ background: var(--lvl-future); opacity: 0.35; cursor: default; }}
        .lvl-0 {{ background: var(--lvl-0); }}
        .lvl-1 {{ background: var(--lvl-1); }}
        .lvl-2 {{ background: var(--lvl-2); }}
        .lvl-3 {{ background: var(--lvl-3); }}
        .lvl-4 {{ background: var(--lvl-4); }}

        .gh-footer {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            width: 100%;
            margin-top: 10px;
            font-size: 12px;
            color: var(--cal-muted);
            gap: 12px;
            flex-wrap: wrap;
        }}
        .gh-legend {{
            display: flex;
            align-items: center;
            gap: 4px;
        }}

        /* Мобильный помесячный календарь (Вариант Б) без горизонтального скролла */
        .mobile-month-wrap {{
            display: none;
            width: 100%;
            max-width: 420px;
            margin: 0 auto;
        }}
        .mob-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }}
        .mob-nav-btn {{
            background: var(--cal-btn-bg);
            color: var(--cal-text);
            border: 1px solid var(--cal-border);
            border-radius: 8px;
            width: 36px;
            height: 34px;
            font-size: 18px;
            font-weight: 600;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        .mob-nav-btn:hover {{ background: var(--cal-btn-hover); }}
        .mob-nav-btn:disabled {{ opacity: 0.35; cursor: default; }}
        .mob-title {{
            font-size: 15px;
            font-weight: 700;
            text-align: center;
        }}
        .mob-sub {{
            font-size: 11px;
            color: var(--cal-muted);
            text-align: center;
            margin-top: 2px;
        }}
        .mob-weekdays {{
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 5px;
            text-align: center;
            font-size: 11px;
            font-weight: 600;
            color: var(--cal-muted);
            margin-bottom: 6px;
        }}
        .mob-grid {{
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 4px;
        }}
        .mob-day {{
            height: 33px;
            border-radius: 7px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            user-select: none;
            border: 1px solid transparent;
        }}
        .mob-day.empty {{
            background: transparent;
            cursor: default;
        }}
        .mob-day.today {{
            border: 2px solid #6366f1;
        }}
        .mob-day.lvl-1, .mob-day.lvl-2, .mob-day.lvl-3, .mob-day.lvl-4 {{
            color: var(--lvl-text-active);
        }}

        .cal-detail-pill {{
            margin-top: 8px;
            padding: 8px 10px;
            min-height: 36px;
            display: block;
            background: var(--cal-bar-bg);
            border: 1px solid var(--cal-border);
            border-radius: 8px;
            font-size: 12px;
            line-height: 1.4;
            color: var(--cal-text);
            text-align: center;
            overflow-wrap: break-word;
        }}
        .cal-date-nowrap {{
            white-space: nowrap;
            font-weight: 700;
        }}

        @media (max-width: 720px) {{
            .cal-card {{ padding: 12px 10px; }}
            .desktop-year-wrap {{ display: none !important; }}
            .mobile-month-wrap {{ display: block !important; }}
        }}
    </style>

    <div class="cal-card">
        <!-- Десктопный годовой календарь -->
        <div class="desktop-year-wrap">
            <div class="gh-grid-inner">
                <div class="gh-months-row">
                    {"".join(month_cells_html)}
                </div>
                <div>
                    {"".join(rows_html)}
                </div>
                <div class="gh-footer">
                    <div id="cal-info-desktop">Наведите или нажмите на ячейку, чтобы увидеть статистику дня</div>
                    <div class="gh-legend">
                        <span>Меньше</span>
                        {legend_squares}
                        <span>Больше</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Мобильный помесячный календарь (Вариант Б) -->
        <div class="mobile-month-wrap">
            <div class="mob-header">
                <button class="mob-nav-btn" id="mob-prev" onclick="changeMobMonth(-1)">‹</button>
                <div>
                    <div class="mob-title" id="mob-month-title">Месяц</div>
                    <div class="mob-sub" id="mob-month-sub"></div>
                </div>
                <button class="mob-nav-btn" id="mob-next" onclick="changeMobMonth(1)">›</button>
            </div>
            <div class="mob-weekdays">
                <div>Пн</div><div>Вт</div><div>Ср</div><div>Чт</div><div>Пт</div><div>Сб</div><div>Вс</div>
            </div>
            <div class="mob-grid" id="mob-days-grid"></div>
            <div class="cal-detail-pill" id="cal-info-mobile">Нажмите на день для просмотра деталей</div>
        </div>
    </div>

    <script>
        var MONTHS_DATA = {months_json};
        var curMonthIdx = MONTHS_DATA.length > 0 ? MONTHS_DATA.length - 1 : 0;
        var MODE_LABEL = "{safe_mode}";

        function formatDayMessage(dateStr, count, cards, tests, acc, mode) {{
            if (Number(count) === 0) {{
                return "<span class='cal-date-nowrap'>📅 " + dateStr + "</span> — 0 действий (" + mode + ")";
            }}
            if (mode === "Все действия") {{
                return "<span class='cal-date-nowrap'>📅 " + dateStr + ": " + count + "</span> (Карточек: " + cards + ", Тестов: " + tests + " | Точность: " + acc + "%)";
            }}
            return "<span class='cal-date-nowrap'>📅 " + dateStr + ": " + count + "</span> (" + mode + " | Точность: " + acc + "%)";
        }}

        function showCalDetail(dateStr, count, cards, tests, acc, mode) {{
            var htmlMsg = formatDayMessage(dateStr, count, cards, tests, acc, mode);
            var dBox = document.getElementById("cal-info-desktop");
            if (dBox) dBox.innerHTML = htmlMsg;
            var mBox = document.getElementById("cal-info-mobile");
            if (mBox) mBox.innerHTML = htmlMsg;
        }}

        function renderMobMonth() {{
            if (!MONTHS_DATA || MONTHS_DATA.length === 0) return;
            var m = MONTHS_DATA[curMonthIdx];
            document.getElementById("mob-month-title").textContent = m.title;
            document.getElementById("mob-month-sub").textContent = "Активных дней: " + m.active_days + " | Ответов: " + m.total_actions;
            document.getElementById("mob-prev").disabled = (curMonthIdx <= 0);
            document.getElementById("mob-next").disabled = (curMonthIdx >= MONTHS_DATA.length - 1);

            var mBox = document.getElementById("cal-info-mobile");
            if (mBox) {{
                mBox.textContent = "Нажмите на день для просмотра деталей";
            }}

            var grid = document.getElementById("mob-days-grid");
            grid.innerHTML = "";
            m.weeks.forEach(function(wk) {{
                wk.forEach(function(cell) {{
                    var el = document.createElement("div");
                    if (!cell.in_month) {{
                        el.className = "mob-day empty";
                    }} else {{
                        var lvlClass = cell.level >= 0 ? ("lvl-" + cell.level) : "lvl-future";
                        el.className = "mob-day " + lvlClass + (cell.is_today ? " today" : "");
                        el.textContent = cell.day;
                        if (cell.level >= 0) {{
                            el.onclick = function() {{
                                showCalDetail(cell.date_fmt, cell.count, cell.cards, cell.tests, cell.acc, MODE_LABEL);
                            }};
                        }}
                    }}
                    grid.appendChild(el);
                }});
            }});
        }}

        function changeMobMonth(delta) {{
            var nextIdx = curMonthIdx + delta;
            if (nextIdx >= 0 && nextIdx < MONTHS_DATA.length) {{
                curMonthIdx = nextIdx;
                renderMobMonth();
            }}
        }}

        renderMobMonth();
    </script>
    """


def render_activity_calendar(
    unfiltered_date_df: pd.DataFrame,
    default_mode: str = "Все режимы",
    key_prefix: str = "gh"
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
    c_s4.metric(f"⚡️ Всего за год ({cal_data['mode_label']})", cal_data["total_actions_year"])

    components.html(build_calendar_html(cal_data), height=390, scrolling=False)


# =====================================================================
# 6. КОМПОНЕНТЫ ИНТЕРФЕЙСА (ТАБЛИЦА БЕЗ СКРОЛЛА + ПОИСК ТЕРМИНОВ)
# =====================================================================

def build_user_avatar_html(full_name: str, username: Optional[str], user_id: str) -> str:
    short_uid = html.escape(str(user_id or "").strip()[-4:] or "0000")
    safe_name = html.escape(str(full_name or "").strip() or f"Ученик #{short_uid}")
    raw_uname = str(username or "").replace("@", "").strip()
    clean_uname = raw_uname if re.match(r"^[A-Za-z0-9_]{3,32}$", raw_uname) else ""
    safe_uname = html.escape(clean_uname)

    alnum_words = ["".join(ch for ch in part if ch.isalnum()) for part in str(full_name or "").split()]
    alnum_words = [w for w in alnum_words if w]
    initials = "".join(w[0].upper() for w in alnum_words[:2]) if alnum_words else ""

    if not initials:
        raw_first = str(full_name or "").strip()
        initials = raw_first[:2] if raw_first else "УЧ"

    initials_safe = html.escape(initials)

    avatar_badge = (
        f'<div style="width: 48px; height: 48px; border-radius: 50%; '
        f'background: linear-gradient(135deg, #4f46e5, #6366f1); color: #ffffff; '
        f'display: flex; align-items: center; justify-content: center; '
        f'font-weight: 700; font-size: 1.05rem; border: 2px solid #818cf8; '
        f'flex-shrink: 0; box-shadow: 0 2px 8px rgba(79, 70, 229, 0.25);">'
        f'{initials_safe}</div>'
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
    st.markdown(build_user_avatar_html(full_name, username, user_id), unsafe_allow_html=True)


def build_class_summary_df(class_student_ids: List[str], class_df: pd.DataFrame) -> pd.DataFrame:
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
            safe_excel_name = str(r0["full_name"] or "").strip().lstrip("=+-@") or f"Ученик #{str(uid)[-4:]}"
            safe_excel_uname = f"@{uname_raw}" if uname_raw else "—"
            summary_rows.append({
                "Ученик": safe_excel_name,
                "Юзернейм": safe_excel_uname,
                "Всего ответов": u_total,
                "Точность (%)": u_acc,
                "Ошибок": u_err,
                "Ср. время (сек)": u_time
            })
        else:
            summary_rows.append({
                "Ученик": f"Ученик #{str(uid)[-4:]}",
                "Юзернейм": "—",
                "Всего ответов": 0,
                "Точность (%)": 0.0,
                "Ошибок": 0,
                "Ср. время (сек)": 0.0
            })
    return pd.DataFrame(summary_rows)


def _format_compact_terms_df(term_problems_df: pd.DataFrame) -> pd.DataFrame:
    """Форматирует таблицу терминов компактно (4 колонки без горизонтального скролла)."""
    if term_problems_df.empty:
        return pd.DataFrame(columns=["Термин", "Блок", "Ошибки / Попытки", "% ошибок"])
    out = pd.DataFrame()
    out["Термин"] = term_problems_df["term"].astype(str)
    out["Блок"] = term_problems_df["module"].map(lambda m: MODULE_SHORT_BADGE.get(str(m), str(m)))
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
    """Рендерит адаптивную таблицу терминов, которая на 100% помещается на экране смартфона без горизонтального скролла."""
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
    key_prefix: str = "main"
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
            color_discrete_map={"Верно / Знаю": "#2ecc71", "Неуверенно": "#f1c40f", "Ошибка / Не знаю": "#e74c3c"},
            hole=0.44
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
            problem_df["module_short"] = problem_df["module"].map(lambda m: MODULE_SHORT_BADGE.get(str(m), str(m)))
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
            st.success("🎉 Затруднений и ошибок за выбранный период нет — все ответы уверенные!")

    st.divider()

    df_calc = df.copy()
    df_calc["time_sec_clamped"] = df_calc["time_sec"].clip(0, 180)
    term_problems = df_calc.groupby(["code", "term", "module"]).agg(
        total_tries=("status", "count"),
        errors=("is_problem", "sum"),
        avg_seconds=("time_sec_clamped", "mean")
    ).reset_index()
    term_problems["error_rate"] = (term_problems["errors"] / term_problems["total_tries"] * 100).round(1)
    term_problems["avg_seconds"] = term_problems["avg_seconds"].round(1)

    col_bar1, col_bar2 = st.columns([1.15, 0.85])

    with col_bar1:
        table_title = "⚠️ Топ-10 трудных терминов для класса" if is_class_view else "⚠️ Ваши трудные термины"
        st.subheader(table_title)
        top_problematic = term_problems.sort_values(by=["errors", "error_rate"], ascending=False).head(10)
        _render_responsive_terms_table(top_problematic)

    with col_bar2:
        st.subheader("⏱ Скорость ответа по блокам")
        module_time = df_calc.groupby("module")["time_sec_clamped"].mean().round(1).reset_index()
        module_time["module_short"] = module_time["module"].map(lambda m: MODULE_SHORT_BADGE.get(str(m), str(m)))
        fig_time = px.bar(
            module_time,
            x="module_short",
            y="time_sec_clamped",
            text="time_sec_clamped",
            labels={"module_short": "Блок", "time_sec_clamped": "Секунды"},
            color="time_sec_clamped",
            color_continuous_scale="Blues"
        )
        fig_time.update_traces(
            texttemplate="%{text}с",
            textposition="outside",
            cliponaxis=False,
            hoverinfo="skip",
            hovertemplate=None,
        )
        max_y = float(module_time["time_sec_clamped"].max()) if not module_time.empty else 10.0
        fig_time.update_layout(
            height=250,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(t=22, b=20, l=10, r=10),
            coloraxis_showscale=False,
            hovermode=False,
            dragmode=False,
            xaxis=dict(fixedrange=True, title="Блок"),
            yaxis=dict(fixedrange=True, title="Секунды", range=[0, max(5.0, max_y * 1.22)]),
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
                if sc >= 0.45:
                    row_copy = r.copy()
                    row_copy["_score"] = sc
                    scored_rows.append(row_copy)

            if scored_rows:
                matched_df = pd.DataFrame(scored_rows).sort_values(by=["_score", "total_tries"], ascending=False).head(10)
                _render_responsive_terms_table(matched_df)
            else:
                cat_matches = []
                for c_code, c_info in TERMS_MAP.items():
                    sc = compute_fuzzy_similarity(clean_q, c_info["term"])
                    if sc >= 0.50:
                        cat_matches.append((c_info["term"], c_info["module"], sc))
                cat_matches.sort(key=lambda x: x[2], reverse=True)
                if cat_matches:
                    best_name, best_mod, _ = cat_matches[0]
                    st.info(f"Термин **«{best_name}»** ({best_mod}) найден в кодификаторе, но за выбранный период ответов по нему ещё не было.")
                else:
                    st.warning("Термин не найден. Проверьте написание.")


# =====================================================================
# 7. ИНТЕРФЕЙС И РОУТИНГ ПО РОЛЯМ (ВЕРСИЯ 7.4)
# =====================================================================

def run_dashboard_app():
    query_params = st.query_params
    student_param = _get_query_param(query_params, "student")
    token_param = _get_query_param(query_params, "token")
    teacher_param = _get_query_param(query_params, "teacher")
    admin_param = _get_query_param(query_params, "admin")

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
        render_user_avatar_badge(sample_row["full_name"], sample_row["username"], student_id_str)

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

            if st.button("🔄 Обновить данные", key="student_refresh_btn", use_container_width=True):
                st.cache_data.clear()
                st.rerun()

        if window_df.empty:
            st.warning(f"За выбранный период («{period_label}») по режиму «{selected_mode}» у вас пока нет ответов.")
        else:
            render_analytics_dashboard(window_df, is_class_view=False, prev_df=prev_window_df, period_label=period_label, key_prefix="student")

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

        teacher_name, class_student_ids, class_df = fetch_teacher_class_data(teacher_code)
        st.caption(f"Просмотр: **{teacher_name}**")

        if not class_student_ids:
            st.info(f"ℹ️ К классу `{teacher_code}` пока не прикрепился ни один ученик.")
            st.stop()
            return

        with st.popover("⚙️ Параметры и фильтры", use_container_width=True):
            st.subheader("Настройки отображения")

            student_labels = {"👥 Весь класс (Сводка)": "ALL"}
            for uid in class_student_ids:
                sub = class_df[class_df["user_id"] == uid]
                if not sub.empty:
                    row_s = sub.iloc[0]
                    uname_clean = str(row_s["username"]).lstrip("@") if row_s["username"] else ""
                    label = f"{row_s['full_name']} ({'@' + uname_clean if uname_clean else '#' + str(uid)[-4:]})"
                else:
                    label = f"Ученик #{str(uid)[-4:]} (0 ответов)"
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

            class_mode_df = (
                class_df[class_df["mode"] == "Карточки"] if selected_mode == "Только карточки"
                else (class_df[class_df["mode"] == "Тесты"] if selected_mode == "Только тесты" else class_df)
            )
            class_window_df, _, _ = filter_df_by_time_window(class_mode_df, period_choice, custom_dates)
            report_df = build_class_summary_df(class_student_ids, class_window_df)
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                report_df.to_excel(writer, index=False, sheet_name="Ведомость класса")
            st.download_button("📥 Скачать ведомость (.xlsx)", data=buffer.getvalue(), file_name=f"Класс_{teacher_code}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)

            if st.button("🔄 Обновить данные", key="teacher_refresh_btn", use_container_width=True):
                st.cache_data.clear()
                st.rerun()

        if selected_uid != "ALL":
            if not entity_unfiltered_df.empty:
                s_row = entity_unfiltered_df.iloc[0]
                render_user_avatar_badge(s_row["full_name"], s_row["username"], selected_uid)
            else:
                render_user_avatar_badge(f"Ученик #{str(selected_uid)[-4:]}", None, selected_uid)

        if active_window_df.empty:
            st.warning(f"За выбранный период («{period_label}») пока нет данных.")
        else:
            render_analytics_dashboard(active_window_df, is_class_view=(selected_uid == "ALL"), prev_df=prev_window_df, period_label=period_label, key_prefix="teacher")

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
            try:
                if "admin" in st.query_params:
                    del st.query_params["admin"]
            except Exception:
                pass

        if not st.session_state["is_admin_authed"]:
            st.title("📊 Аналитический портал тренажера")
            st.write("Добро пожаловать! Выберите свой раздел или войдите как администратор.")

            c_info1, c_info2 = st.columns(2)
            with c_info1:
                st.info("🎓 **Для учеников:**\nОткройте Telegram-бот и нажмите «📊 Моя статистика» → «📱 Открыть веб-аналитику».")
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
            uname_clean = str(r0["username"]).lstrip("@") if r0["username"] else ""
            uname = f"@{uname_clean}" if uname_clean else f"Профиль #{str(uid)[-4:]}"
            user_labels_map[f"{fname} ({uname})"] = uid

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

        if selected_option != "Все пользователи" and not entity_df.empty:
            s_row = entity_df.iloc[0]
            render_user_avatar_badge(s_row["full_name"], s_row["username"], s_row["user_id"])

        if window_df.empty:
            st.warning(f"За выбранный период («{period_label}») пока нет данных.")
        else:
            render_analytics_dashboard(window_df, is_class_view=(selected_option == "Все пользователи"), prev_df=prev_window_df, period_label=period_label, key_prefix="admin")

        render_activity_calendar(unfiltered_date_df=entity_df, default_mode=selected_mode, key_prefix="admin_gh")


if __name__ == "__main__" or ("streamlit" in sys.modules if "sys" in globals() else True):
    if os.getenv("DASHBOARD_SKIP_AUTORUN") != "1":
        run_dashboard_app()
