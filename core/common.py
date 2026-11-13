"""
Модуль общих утилит, конфигурации, безопасности, токенов и триграммного поиска (v7.6).
Используется совместно в bot.py и dashboard.py без дублирования кода.
"""
import os
import re
import json
import hmac
import base64
import hashlib
import difflib
import logging
import urllib.parse
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List, Tuple, Set

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =====================================================================
# 1. КОНФИГУРАЦИЯ И ЧАСОВОЙ ПОЯС
# =====================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH = os.getenv("DB_PATH", "social_bot.db")
TERMS_JSON_PATH = os.getenv("TERMS_JSON_PATH", "terms.json")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://obshaga.streamlit.app").rstrip("/")
SECRET_KEY = os.getenv("SECRET_KEY")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
TIMEZONE_STR = os.getenv("TIMEZONE", "Europe/Moscow")
LOG_CHAT_ID = os.getenv("LOG_CHAT_ID") or os.getenv("LIVE_LOG_CHAT_ID")
PRIVACY_URL = os.getenv(
    "PRIVACY_URL",
    "https://telegra.ph/Politika-konfidencialnosti-i-pravila-trenazhyora-11-03",
).strip()
TERMS_URL = os.getenv("TERMS_URL", PRIVACY_URL).strip()

logger = logging.getLogger("social_bot")

try:
    TZ = ZoneInfo(TIMEZONE_STR)
except Exception:
    logger.warning(f"Не удалось загрузить часовой пояс {TIMEZONE_STR}, используется UTC.")
    TZ = timezone.utc


def get_local_today() -> date:
    """Возвращает текущую дату в заданном часовом поясе (по умолчанию Europe/Moscow)."""
    return datetime.now(TZ).date()


def normalize_neon_pooler_url(db_url: Optional[str]) -> Optional[str]:
    """
    Автоматически конвертирует прямой URL Neon в адрес Connection Pooler (-pooler),
    чтобы обойти жёсткий лимит одновременных подключений на бесплатном тарифе Neon.
    """
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
            return urllib.parse.urlunsplit(
                (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
            )
    except Exception:
        pass
    return db_url


# =====================================================================
# 2. ТОКЕНЫ И КРИПТОГРАФИЧЕСКАЯ ПРОВЕРКА ДОСТУПА
# =====================================================================

def generate_student_token(
    user_id: Any, token_version: int = 1, secret_key: Optional[str] = None
) -> str:
    """Генерирует защищённый HMAC-SHA256 токен доступа ученика к веб-аналитике."""
    key = str(secret_key if secret_key is not None else (os.getenv("SECRET_KEY") or SECRET_KEY or ""))
    uid_str = str(user_id).strip()
    try:
        ver = max(1, int(token_version or 1))
    except (ValueError, TypeError):
        ver = 1
    msg = f"student:{uid_str}:v{ver}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]


def generate_teacher_token(
    teacher_code: Any, token_version: int = 1, secret_key: Optional[str] = None
) -> str:
    """Генерирует защищённый HMAC-SHA256 токен доступа к кабинету преподавателя с поддержкой ротации."""
    key = str(secret_key if secret_key is not None else (os.getenv("SECRET_KEY") or SECRET_KEY or ""))
    code_str = str(teacher_code).strip()
    try:
        ver = max(1, int(token_version or 1))
    except (ValueError, TypeError):
        ver = 1
    msg = f"teacher:{code_str}:v{ver}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]


def verify_token_constant_time(provided: Optional[str], expected: str) -> bool:
    """Защищённое от timing-атак сравнение токенов и паролей."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(
        str(provided).strip().encode("utf-8"),
        str(expected).strip().encode("utf-8"),
    )


# Алиас для обратной совместимости с dashboard.py
verify_secret = verify_token_constant_time


def encode_user_id(user_id: int, secret_key: Optional[str] = None) -> str:
    """Маскирует числовой user_id в компактную строку, безопасную для URL."""
    key = str(secret_key or os.getenv("SECRET_KEY") or SECRET_KEY or "")
    salt = hashlib.sha256(key.encode("utf-8")).digest()
    uid_bytes = str(user_id).encode("utf-8")
    masked = bytes([b ^ salt[i % len(salt)] for i, b in enumerate(uid_bytes)])
    return base64.urlsafe_b64encode(masked).decode("utf-8").rstrip("=")


def decode_user_id(encoded_str: str, secret_key: Optional[str] = None) -> Optional[int]:
    """Расшифровывает безопасный строковый токен обратно в Telegram user_id."""
    key = str(secret_key or os.getenv("SECRET_KEY") or SECRET_KEY or "")
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


def build_student_webapp_url(user_id: int, token_version: int = 1) -> str:
    token = generate_student_token(user_id, token_version)
    masked_uid = encode_user_id(user_id)
    safe_token = urllib.parse.quote(token)
    return f"{DASHBOARD_URL}/?student={masked_uid}&token={safe_token}"


def build_teacher_webapp_url(teacher_code: str, token_version: int = 1) -> str:
    """Формирует защищённую ссылку кабинета преподавателя."""
    t_token = generate_teacher_token(teacher_code, token_version)
    safe_code = urllib.parse.quote(str(teacher_code))
    safe_token = urllib.parse.quote(t_token)
    return f"{DASHBOARD_URL}/?teacher={safe_code}&token={safe_token}"


# =====================================================================
# 3. БЕЗОПАСНОЕ ФОРМАТИРОВАНИЕ ТЕКСТА И ПРОФИЛЕЙ
# =====================================================================

def escape_md(text: Any) -> str:
    """Экранирует спецсимволы Markdown v1 для безопасного вывода в Telegram."""
    if text is None:
        return ""
    s = str(text)
    for ch in ["\\", "_", "*", "`", "["]:
        s = s.replace(ch, f"\\{ch}")
    return s


def strip_md_delimiters(text: Any) -> str:
    """
    Очищает текст для безопасного размещения внутри курсива/жирного в Markdown v1.
    Заменяет переводы строк на пробелы, чтобы пустые строки внутри *...* или _..._
    не ломали парсер сущностей Telegram (Can't parse entities).
    """
    if text is None:
        return ""
    cleaned = re.sub(r"[\r\n\t]+", " ", str(text)).strip()
    return escape_md(cleaned)


def truncate_plain(text: str, max_len: int) -> str:
    """
    Безопасно обрезает сырой текст ДО применения Markdown-экранирования
    строго по границе целых слов (по пробелу перед лимитом), исключая обрывы на полуслове.
    """
    s = str(text or "").strip()
    if len(s) <= max_len:
        return s
    limit = max(0, max_len - 3)
    if limit <= 0:
        return "..."
    chunk = s[:limit]
    if len(s) > limit and not s[limit].isspace():
        last_space = chunk.rfind(" ")
        if last_space > 0:
            chunk = chunk[:last_space]
    return chunk.rstrip(" ,;:-—–.") + "..."


def get_safe_display_name(user: Any) -> str:
    """
    Формирует обезличенное имя (например, 'Данил К.' или 'дидригидрон'),
    чтобы полные паспортные фамилии не сохранялись в БД (Privacy by Design).
    Корректно обрабатывает смайлики, спецсимволы и двойные/тройные ФИО.
    """
    raw_first = (getattr(user, "first_name", "") or "").strip()
    raw_last = (getattr(user, "last_name", "") or "").strip()

    first_alnum = ["".join(ch for ch in w if ch.isalnum()) for w in raw_first.split()]
    first_alnum = [w for w in first_alnum if w]
    last_alnum = ["".join(ch for ch in w if ch.isalnum()) for w in raw_last.split()]
    last_alnum = [w for w in last_alnum if w]

    if first_alnum and last_alnum:
        return f"{first_alnum[0]} {last_alnum[0][0].upper()}."
    if len(first_alnum) >= 2:
        return f"{first_alnum[0]} {first_alnum[1][0].upper()}."
    if len(first_alnum) == 1:
        return first_alnum[0]
    if raw_first:
        cleaned_raw = re.sub(r"[\r\n\t=`*_\[\]]+", "", raw_first).lstrip("=+-@")[:24].strip()
        if cleaned_raw:
            return cleaned_raw
    if getattr(user, "username", None):
        return f"@{user.username}"
    uid_str = str(getattr(user, "id", "") or "")
    return f"Ученик #{uid_str[-4:]}" if uid_str else "Ученик"


def _safe_json_loads(raw_val: Any, default: Any) -> Any:
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


# =====================================================================
# 4. БЫСТРЫЙ НЕЧЁТКИЙ ПОИСК С ИНДЕКСОМ ТРИГРАММ
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
        if w == q_norm:
            return 0.98
        if w.startswith(q_norm):
            word_best = max(word_best, 0.92)
            eff_thresh = max(eff_thresh, word_best)
            continue

        w_inter = len(q_tri & w_tri) if (q_tri and w_tri) else 0
        w_union = len(q_tri) + len(w_tri) - w_inter
        w_tri_sc = w_inter / max(1, w_union)

        sm_w = difflib.SequenceMatcher(None, q_norm, w)
        if eff_thresh > 0.0 and (sm_w.real_quick_ratio() * 0.65 + w_tri_sc * 0.35) <= eff_thresh:
            continue
        if eff_thresh > 0.0 and (sm_w.quick_ratio() * 0.65 + w_tri_sc * 0.35) <= eff_thresh:
            continue

        sc = (sm_w.ratio() * 0.65) + (w_tri_sc * 0.35)
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
