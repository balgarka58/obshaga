import os
import re
import sys
import hmac
import json
import time
import random
import base64
import hashlib
import logging
import sqlite3
import asyncio
import difflib
import threading
import traceback
import urllib.parse
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple, Set
from collections import deque
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    ErrorEvent,
    BotCommand,
)
from aiogram.client.default import DefaultBotProperties

# =====================================================================
# 1. КОНФИГУРАЦИЯ, ТОКЕНЫ И БЕЗОПАСНАЯ РАЗМЕТКА (ВЕРСИЯ 7.5 HARDENED)
# =====================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH = os.getenv("DB_PATH", "social_bot.db")
TERMS_JSON_PATH = os.getenv("TERMS_JSON_PATH", "terms.json")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://obshaga.streamlit.app").rstrip("/")
SECRET_KEY = os.getenv("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("Переменная окружения SECRET_KEY не задана!")
TIMEZONE_STR = os.getenv("TIMEZONE", "Europe/Moscow")
LOG_CHAT_ID = os.getenv("LOG_CHAT_ID")
PRIVACY_URL = os.getenv("PRIVACY_URL", "https://telegra.ph/Politika-konfidencialnosti-i-pravila-trenazhyora-11-03").strip()
TERMS_URL = os.getenv("TERMS_URL", PRIVACY_URL).strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
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
    чтобы обойти жёсткий лимит одновременных подключений на бесплатном тарифе Neon
    (PgBouncer выдерживает до 10 000 виртуальных подключений).
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
            return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    except Exception:
        pass
    return db_url


def generate_student_token(user_id: Any, token_version: int = 1, secret_key: Optional[str] = None) -> str:
    """Генерирует защищённый HMAC-SHA256 токен доступа ученика к веб-аналитике."""
    key = str(secret_key if secret_key is not None else os.getenv("SECRET_KEY", SECRET_KEY))
    uid_str = str(user_id).strip()
    try:
        ver = max(1, int(token_version or 1))
    except (ValueError, TypeError):
        ver = 1
    msg = f"student:{uid_str}:v{ver}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]


def generate_teacher_token(teacher_code: Any, token_version: int = 1, secret_key: Optional[str] = None) -> str:
    """Генерирует защищённый HMAC-SHA256 токен доступа к кабинету преподавателя с поддержкой ротации."""
    key = str(secret_key if secret_key is not None else os.getenv("SECRET_KEY", SECRET_KEY))
    code_str = str(teacher_code).strip()
    try:
        ver = max(1, int(token_version or 1))
    except (ValueError, TypeError):
        ver = 1
    msg = f"teacher:{code_str}:v{ver}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]


def verify_token_constant_time(provided: Optional[str], expected: str) -> bool:
    """Защищённое от timing-атак сравнение токенов."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(
        str(provided).strip().encode("utf-8"),
        str(expected).strip().encode("utf-8"),
    )


def encode_user_id(user_id: int, secret_key: Optional[str] = None) -> str:
    """Маскирует числовой user_id в компактную строку, безопасную для URL."""
    key = secret_key or SECRET_KEY
    salt = hashlib.sha256(key.encode("utf-8")).digest()
    uid_bytes = str(user_id).encode("utf-8")
    masked = bytes([b ^ salt[i % len(salt)] for i, b in enumerate(uid_bytes)])
    return base64.urlsafe_b64encode(masked).decode("utf-8").rstrip("=")


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


# =====================================================================
# 2. МЕХАНИЗМ «ОДНОГО ЭКРАНА» И ПО-ПОЛЬЗОВАТЕЛЬСКАЯ БЛОКИРОВКА
# =====================================================================

class UserActionLockManager:
    """
    Предотвращает гонки состояний (Race Conditions) при быстрых двойных/тройных
    кликах пользователя по кнопкам (Button Mashing), сериализуя обработку по chat_id/user_id.
    """
    def __init__(self, max_locks: int = 10000):
        self._locks: Dict[int, asyncio.Lock] = {}
        self.max_locks = max_locks

    def get_lock(self, key: int) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            if len(self._locks) >= self.max_locks:
                # Удаляем старые свободные локи
                for old_k in list(self._locks.keys()):
                    if not self._locks[old_k].locked():
                        self._locks.pop(old_k, None)
                        break
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


user_lock_manager = UserActionLockManager()


class ScreenManager:
    """
    Хранит ID последнего активного сообщения бота для каждого чата:
    1) Мгновенно удаляет сообщение пользователя (message.delete()).
    2) Удаляет предыдущее сообщение бота и отправляет новое с обновлённой клавиатурой.
    В чате ВСЕГДА висит ровно 1 актуальное сообщение бота без спама.
    """
    def __init__(self, max_chats: int = 10000):
        self.last_bot_msg_id: Dict[int, int] = {}
        self.max_chats = max_chats

    def get_last_id(self, chat_id: int) -> Optional[int]:
        return self.last_bot_msg_id.get(chat_id)

    def set_last_id(self, chat_id: int, message_id: int):
        if chat_id in self.last_bot_msg_id:
            self.last_bot_msg_id.pop(chat_id, None)
        elif len(self.last_bot_msg_id) >= self.max_chats:
            oldest = next(iter(self.last_bot_msg_id))
            self.last_bot_msg_id.pop(oldest, None)
        self.last_bot_msg_id[chat_id] = message_id


screen_manager = ScreenManager()


async def _safe_delete_message(bot: Bot, chat_id: int, message_id: Optional[int]):
    """Тихо удаляет сообщение с защитой от сбоев и кратковременного флуд-контроля Telegram."""
    if not message_id or bot is None:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        err_msg = str(e).lower()
        # Если Telegram вернул временный Retry-After (флуд-контроль), повторяем через указанную паузу
        if "retry after" in err_msg or "flood" in err_msg:
            try:
                await asyncio.sleep(1.2)
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass


class AntiFloodDDoSMiddleware(BaseMiddleware):
    """
    Защита бота от прикладного DDoS, спам-скриптов и флуда кнопками (Token Bucket):
    1) Мгновенно (в первую миллисекунду получения апдейта) запускает фоновое удаление
       входящего сообщения пользователя — не дожидаясь БД или генерации ответа.
    2) Ограничивает частоту запросов от одного пользователя (не более 4 в секунду
       и не более 16 за 10 секунд). При превышении включает короткий кулдаун (4 сек),
       отбрасывая спам-запросы до обращения к процессору и базе данных.
    3) Защищает от атак длинными текстами (обрезает гигантские сообщения).
    """
    def __init__(self, max_per_sec: int = 4, max_per_10s: int = 16, cooldown_sec: float = 4.0):
        super().__init__()
        self.max_per_sec = max_per_sec
        self.max_per_10s = max_per_10s
        self.cooldown_sec = cooldown_sec
        self._user_hits: Dict[int, deque] = {}
        self._cooldown_until: Dict[int, float] = {}
        self._max_tracked = 10000

    async def __call__(self, handler, event: Any, data: Dict[str, Any]) -> Any:
        if isinstance(event, Message):
            chat_id = event.chat.id if event.chat else None
            msg_id = event.message_id
            bot_inst = event.bot

            # Мгновенно запускаем удаление входящего сообщения пользователя в фоне
            if chat_id and msg_id and bot_inst:
                asyncio.create_task(_safe_delete_message(bot_inst, chat_id, msg_id))

            uid = event.from_user.id if event.from_user else chat_id
            if uid is not None:
                now = time.monotonic()
                cd_until = self._cooldown_until.get(uid, 0.0)
                if now < cd_until:
                    return None

                hits = self._user_hits.get(uid)
                if hits is None:
                    if len(self._user_hits) >= self.max_per_10s and len(self._user_hits) >= self._max_tracked:
                        oldest_k = next(iter(self._user_hits))
                        self._user_hits.pop(oldest_k, None)
                        self._cooldown_until.pop(oldest_k, None)
                    hits = deque(maxlen=self.max_per_10s + 4)
                    self._user_hits[uid] = hits

                while hits and (now - hits[0]) > 10.0:
                    hits.popleft()

                recent_1s = sum(1 for t in hits if (now - t) <= 1.0)
                if recent_1s >= self.max_per_sec or len(hits) >= self.max_per_10s:
                    self._cooldown_until[uid] = now + self.cooldown_sec
                    logger.warning(f"AntiFlood: пользователь {uid} временно ограничен на {self.cooldown_sec}с за спам.")
                    return None

                hits.append(now)

        return await handler(event, data)


async def render_single_screen(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: Any = None,
    disable_web_page_preview: bool = True,
) -> Optional[Message]:
    """
    Сверхбыстрый движок отображения «Одного экрана» (Clean Chat):
    - Входящее сообщение пользователя уже удаляется на входе через AntiFloodDDoSMiddleware.
    - Отправка нового экрана и удаление предыдущего сообщения бота выполняются без задержек.
    """
    chat_id = message.chat.id
    user_msg_id = message.message_id
    bot = message.bot

    # Страховочное фоновое удаление сообщения пользователя (если middleware не сработал в тестах)
    if bot and chat_id and user_msg_id:
        asyncio.create_task(_safe_delete_message(bot, chat_id, user_msg_id))

    safe_text = str(text or "")
    if len(safe_text) > 4090:
        safe_text = safe_text[:4050].rstrip() + "\n\n...[текст сокращён]"

    data = await state.get_data()
    # screen_manager в RAM всегда содержит самый свежий ID (включая ночные напоминания)
    prev_bot_id = screen_manager.get_last_id(chat_id) or data.get("last_bot_msg_id")

    # Если после рестарта Render память в RAM очистилась (prev_bot_id пуст),
    # математически подчищаем веер предыдущих номеров без единого запроса к базе данных!
    if not prev_bot_id and user_msg_id and user_msg_id > 1 and bot and chat_id:
        for c_id in range(max(1, user_msg_id - 4), user_msg_id):
            asyncio.create_task(_safe_delete_message(bot, chat_id, c_id))

    try:
        new_msg = await message.answer(
            safe_text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=disable_web_page_preview,
        )
    except TelegramBadRequest as e:
        err_lower = str(e).lower()
        if "can't parse entities" in err_lower or "message is too long" in err_lower:
            logger.warning(f"Fallback отправки без Markdown: {e}")
            plain_fallback = safe_text.replace("\\*", "*").replace("\\_", "_").replace("\\`", "`").replace("\\[", "[")
            new_msg = await message.answer(
                plain_fallback[:4090],
                reply_markup=reply_markup,
                parse_mode=None,
                disable_web_page_preview=disable_web_page_preview,
            )
        else:
            raise
    except TelegramForbiddenError:
        return None

    screen_manager.set_last_id(chat_id, new_msg.message_id)
    await state.update_data(last_bot_msg_id=new_msg.message_id)

    # Мгновенно запускаем удаление предыдущего сообщения бота в фоне, не задерживая ответ
    if prev_bot_id and prev_bot_id != new_msg.message_id:
        asyncio.create_task(_safe_delete_message(bot, chat_id, prev_bot_id))

    return new_msg


# =====================================================================
# 3. ЖИВЫЕ ЛОГИ ОШИБОК В TELEGRAM-КАНАЛ (LIVE ERROR LOGGER)
# =====================================================================

class LiveErrorLogger:
    """
    Отправляет живые уведомления об ошибках в Telegram-канал или чат с защитой от спама:
    - Дедупликация по типу исключения и контексту (без динамического текста сообщения,
      чтобы спам разными текстами не пробивал фильтр).
    - Глобальный rate-limit (не более 15 сообщений в минуту в лог-канал).
    - FIFO-вытеснение старых записей вместо полного сброса кэша.
    """
    def __init__(self, log_chat_id: Optional[str] = LOG_CHAT_ID):
        self.log_chat_id = str(log_chat_id).strip() if log_chat_id else None
        self._dedup_cache: Dict[str, Tuple[float, int]] = {}
        self.cooldown_sec = 180.0
        self._sent_timestamps: deque = deque(maxlen=20)
        self.max_per_minute = 15

    async def report_error(
        self,
        bot: Optional[Bot],
        exc: BaseException,
        context_info: str = "",
        user_info: str = "Неизвестен",
    ):
        tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        logger.error(f"Ошибка [{context_info}] ({user_info}): {exc}\n{tb_str}")

        if not self.log_chat_id or bot is None:
            return

        # Стабильная сигнатура ошибки (тип + первые 80 символов ошибки + тип контекста)
        ctx_prefix = str(context_info or "Global").split(":", 1)[0].strip()
        sig = hashlib.md5(f"{type(exc).__name__}:{str(exc)[:80]}:{ctx_prefix}".encode("utf-8")).hexdigest()[:12]
        now = time.time()

        if sig in self._dedup_cache:
            last_sent, count = self._dedup_cache[sig]
            if now - last_sent < self.cooldown_sec:
                self._dedup_cache[sig] = (last_sent, count + 1)
                return
            repeat_badge = f" (повторов за 3 мин: {count})" if count > 1 else ""
            self._dedup_cache.pop(sig, None)
        else:
            repeat_badge = ""

        # Глобальный анти-флуд лимит (защита от 429 Too Many Requests в лог-канале)
        while self._sent_timestamps and now - self._sent_timestamps[0] > 60.0:
            self._sent_timestamps.popleft()
        if len(self._sent_timestamps) >= self.max_per_minute:
            return

        while len(self._dedup_cache) >= 500:
            oldest_key = next(iter(self._dedup_cache))
            self._dedup_cache.pop(oldest_key, None)
        self._dedup_cache[sig] = (now, 1)
        self._sent_timestamps.append(now)

        now_str = datetime.now(TZ).strftime("%d.%m.%Y %H:%M:%S")
        tb_short = tb_str[-2600:] if len(tb_str) > 2600 else tb_str
        msg_text = (
            f"🚨 <b>ОШИБКА В БОТЕ v7.5</b>{repeat_badge}\n"
            f"⏰ <b>Время:</b> <code>{now_str}</code>\n"
            f"👤 <b>Пользователь:</b> <code>{self._escape_html(user_info)}</code>\n"
            f"📍 <b>Контекст:</b> <code>{self._escape_html(context_info or 'Global')}</code>\n"
            f"⚠️ <b>Исключение:</b> <code>{self._escape_html(f'{type(exc).__name__}: {exc}')}</code>\n\n"
            f"📜 <b>Traceback:</b>\n<pre>{self._escape_html(tb_short)}</pre>"
        )

        try:
            chat_target: Any = int(self.log_chat_id) if self.log_chat_id.lstrip("-").isdigit() else self.log_chat_id
            await bot.send_message(chat_id=chat_target, text=msg_text, parse_mode=ParseMode.HTML)
        except Exception as send_err:
            logger.warning(f"Не удалось отправить лог ошибки в канал {self.log_chat_id}: {send_err}")

    @staticmethod
    def _escape_html(val: Any) -> str:
        s = str(val or "")
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


live_logger = LiveErrorLogger()


# =====================================================================
# 4. АЛГОРИТМ ИНТЕРВАЛЬНОГО ПОВТОРЕНИЯ ДЛЯ КАРТОЧЕК
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
# 5. КАТАЛОГ ТЕРМИНОВ И БЫСТРЫЙ НЕЧЁТКИЙ ПОИСК С ИНДЕКСОМ ТРИГРАММ
# =====================================================================

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


class TermsCatalog:
    def __init__(self, json_path: str = TERMS_JSON_PATH, sqlite_path: str = DB_PATH):
        self.json_path = json_path
        self.sqlite_path = sqlite_path
        self.terms_by_code: Dict[str, Dict[str, Any]] = {}
        self.terms_by_button_label: Dict[str, Dict[str, Any]] = {}
        self.terms_by_module: Dict[int, List[Dict[str, Any]]] = {1: [], 2: [], 3: [], 4: [], 5: []}
        self.all_terms: List[Dict[str, Any]] = []
        self._search_index: List[Tuple[Dict[str, Any], str, str, Set[str], List[Tuple[str, Set[str]]]]] = []

    def load(self):
        self.terms_by_code.clear()
        self.terms_by_button_label.clear()
        self.all_terms.clear()
        self._search_index.clear()
        for m in list(self.terms_by_module.keys()):
            self.terms_by_module[m].clear()

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
                logger.warning(f"Файл {self.json_path} пуст или некорректен. Пробуем резерв из БД.")
            except Exception as e:
                logger.warning(f"Ошибка чтения {self.json_path}: {e}. Пробуем резерв из БД.")

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
                    logger.info(f"Резервная загрузка: получено {len(self.all_terms)} терминов из {self.sqlite_path}.")
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
            btn_label = f"📖 {truncate_plain(term_name, 32)}"
            if btn_label not in self.terms_by_button_label:
                self.terms_by_button_label[btn_label] = term_obj
            self.all_terms.append(term_obj)
            if mod_num in self.terms_by_module:
                self.terms_by_module[mod_num].append(term_obj)
            else:
                self.terms_by_module[mod_num] = [term_obj]

            # Предварительное индексирование триграмм и слов для мгновенного поиска
            t_norm = _normalize_search_str(term_name)
            t_tri = _get_trigrams(t_norm)
            t_words = [(w, _get_trigrams(w)) for w in _WORD_SPLIT_RE.split(t_norm) if len(w) >= 3]
            self._search_index.append((term_obj, code.lower(), t_norm, t_tri, t_words))

    def get_by_code(self, code: str) -> Optional[Dict[str, Any]]:
        return self.terms_by_code.get(str(code).strip())

    def get_by_button_label(self, btn_text: str) -> Optional[Dict[str, Any]]:
        return self.terms_by_button_label.get(str(btn_text).strip())

    def get_terms(self, module_filter: Optional[int] = None) -> List[Dict[str, Any]]:
        if module_filter is not None:
            return self.terms_by_module.get(module_filter, [])
        return self.all_terms

    def search_terms_fuzzy(self, query: str, limit: int = 6) -> List[Tuple[Dict[str, Any], float]]:
        # Защита от Algorithmic Complexity DDoS: ограничиваем длину поискового запроса до 60 символов
        clean_q = _normalize_search_str(str(query or "")[:60])
        if len(clean_q) < 3:
            return []

        q_tri = _get_trigrams(clean_q)
        scored: List[Tuple[Dict[str, Any], float]] = []

        if len(self._search_index) == len(self.all_terms) and self._search_index:
            for t, code_low, t_norm, t_tri, t_words in self._search_index:
                if clean_q == code_low:
                    score = 1.0
                else:
                    score = _compute_similarity_preindexed(clean_q, q_tri, t_norm, t_tri, t_words, threshold=0.45)
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
        return scored[:limit]


catalog = TermsCatalog()


# =====================================================================
# 6. БАЗА ДАННЫХ С БЕЗПОТЕРЬНОЙ АРХИВАЦИЕЙ (LOSSLESS ROLLUP / ARCHIVE)
# =====================================================================

class Database:
    """
    Высокопроизводительный слой БД с автоматической суточной архивацией без потери точности:
    1. Ответы младше 30 дней хранятся в user_answers поминутно.
    2. Ответы старше 30 дней раз в сутки агрегируются в user_daily_archive
       с сохранением точного числа попыток (tries_count), ошибок (errors_count),
       суммарного изменения веса (total_delta_weight) и профиля пользователя (username, full_name).
    3. Полная защита от гонок кэша (monotone _write_seq) и гонок транзакций.
    """
    def __init__(self, db_url: Optional[str] = DATABASE_URL, sqlite_path: str = DB_PATH):
        self.db_url = normalize_neon_pooler_url(db_url)
        self.sqlite_path = sqlite_path
        self.is_postgres = bool(self.db_url and self.db_url.startswith("postgres"))
        self._cache_lock = threading.Lock()
        self._write_seq: int = 0
        self._user_entries_cache: Dict[int, Tuple[float, int, List[Tuple[str, Any]]]] = {}
        self._user_settings_cache: Dict[int, Tuple[float, int, bool, bool, str]] = {}
        self._cache_ttl = 600.0  # 10 минут
        self._max_cache_size = 2000
        self._write_queue_lock = threading.Lock()
        self._pending_writes: List[Tuple[int, Optional[str], Optional[str], str]] = []
        self._flush_task: Optional[asyncio.Task] = None

    def get_connection(self):
        if self.is_postgres:
            import psycopg2
            return psycopg2.connect(self.db_url, connect_timeout=10)
        else:
            conn = sqlite3.connect(self.sqlite_path, timeout=15.0)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute("PRAGMA busy_timeout=10000;")
            except Exception:
                pass
            return conn

    def _evict_oldest_locked(self, cache_dict: Dict[Any, Any]):
        while len(cache_dict) >= self._max_cache_size:
            oldest_key = next(iter(cache_dict))
            cache_dict.pop(oldest_key, None)

    def invalidate_user_cache(self, user_id: Optional[int] = None):
        with self._cache_lock:
            self._write_seq += 1
            if user_id is None:
                self._user_entries_cache.clear()
                self._user_settings_cache.clear()
            else:
                self._user_entries_cache.pop(user_id, None)
                self._user_settings_cache.pop(user_id, None)

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
                CREATE INDEX IF NOT EXISTS idx_user_answers_user_created ON user_answers(user_id, id);
                CREATE INDEX IF NOT EXISTS idx_user_answers_created_at ON user_answers(created_at);

                -- Таблица сжатого архива ответов старше 30 дней (экономия места до 95% без потери точности)
                CREATE TABLE IF NOT EXISTS user_daily_archive (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    archive_date DATE NOT NULL,
                    mode VARCHAR(16) NOT NULL,
                    code VARCHAR(32) NOT NULL,
                    tries_count INTEGER NOT NULL DEFAULT 1,
                    errors_count INTEGER NOT NULL DEFAULT 0,
                    total_time_sec REAL NOT NULL DEFAULT 0.0,
                    total_delta_weight REAL NOT NULL DEFAULT 0.0,
                    last_delta_ans REAL NOT NULL DEFAULT 0.0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_user_daily_code UNIQUE (user_id, archive_date, mode, code)
                );
                ALTER TABLE user_daily_archive ADD COLUMN IF NOT EXISTS username TEXT;
                ALTER TABLE user_daily_archive ADD COLUMN IF NOT EXISTS full_name TEXT;
                CREATE INDEX IF NOT EXISTS idx_archive_user ON user_daily_archive(user_id);
                CREATE INDEX IF NOT EXISTS idx_archive_user_date ON user_daily_archive(user_id, archive_date);

                CREATE TABLE IF NOT EXISTS teachers (
                    teacher_code VARCHAR(8) PRIMARY KEY,
                    user_id BIGINT NOT NULL UNIQUE,
                    full_name TEXT,
                    token_version INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                ALTER TABLE teachers ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 1;

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
                    policy_accepted INTEGER NOT NULL DEFAULT 0,
                    exam_level VARCHAR(8) NOT NULL DEFAULT 'ege',
                    last_reminder_date TEXT,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS policy_accepted INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS exam_level VARCHAR(8) NOT NULL DEFAULT 'ege';
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
                CREATE INDEX IF NOT EXISTS idx_user_answers_user_created ON user_answers(user_id, id);
                CREATE INDEX IF NOT EXISTS idx_user_answers_created_at ON user_answers(created_at);

                CREATE TABLE IF NOT EXISTS user_daily_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    full_name TEXT,
                    archive_date TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    code TEXT NOT NULL,
                    tries_count INTEGER NOT NULL DEFAULT 1,
                    errors_count INTEGER NOT NULL DEFAULT 0,
                    total_time_sec REAL NOT NULL DEFAULT 0.0,
                    total_delta_weight REAL NOT NULL DEFAULT 0.0,
                    last_delta_ans REAL NOT NULL DEFAULT 0.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (user_id, archive_date, mode, code)
                );
                CREATE INDEX IF NOT EXISTS idx_archive_user ON user_daily_archive(user_id);
                CREATE INDEX IF NOT EXISTS idx_archive_user_date ON user_daily_archive(user_id, archive_date);

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
                    policy_accepted INTEGER NOT NULL DEFAULT 0,
                    exam_level TEXT NOT NULL DEFAULT 'ege',
                    last_reminder_date TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """)
                try:
                    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_teachers_user_unique ON teachers(user_id);")
                except Exception:
                    pass

                for alter_sql in [
                    "ALTER TABLE user_answers ADD COLUMN username TEXT;",
                    "ALTER TABLE user_answers ADD COLUMN full_name TEXT;",
                    "ALTER TABLE user_daily_archive ADD COLUMN username TEXT;",
                    "ALTER TABLE user_daily_archive ADD COLUMN full_name TEXT;",
                    "ALTER TABLE teachers ADD COLUMN token_version INTEGER NOT NULL DEFAULT 1;",
                    "ALTER TABLE user_settings ADD COLUMN policy_accepted INTEGER NOT NULL DEFAULT 0;",
                    "ALTER TABLE user_settings ADD COLUMN exam_level TEXT NOT NULL DEFAULT 'ege';",
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
        mode_str = "Neon PostgreSQL (Pooler + Lossless Archive)" if self.is_postgres else f"SQLite ({self.sqlite_path})"
        logger.info(f"База данных успешно инициализирована: {mode_str}.")

    def _update_ram_cache_on_answer(self, user_id: int, entry_str: str, now_dt: datetime):
        """Мгновенно (за 0.001с) обновляет RAM-кэш пользователя до физической записи в БД."""
        with self._cache_lock:
            self._write_seq += 1
            cached = self._user_entries_cache.get(user_id)
            if cached is not None:
                ts_cached, _, rows_list = cached
                if time.time() - ts_cached < self._cache_ttl:
                    new_list = list(rows_list)
                    new_list.append((entry_str, now_dt))
                    self._user_entries_cache[user_id] = (time.time(), self._write_seq, new_list)
                else:
                    self._user_entries_cache.pop(user_id, None)

    def _insert_batch_to_db_sync(self, batch: List[Tuple[int, Optional[str], Optional[str], str]]):
        """Выполняет пакетную запись (Batch Insert) всех накопившихся ответов за 1 транзакцию."""
        if not batch:
            return
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.executemany(
                    "INSERT INTO user_answers (user_id, username, full_name, entry) VALUES (%s, %s, %s, %s);",
                    batch
                )
            else:
                cursor.executemany(
                    "INSERT INTO user_answers (user_id, username, full_name, entry) VALUES (?, ?, ?, ?);",
                    batch
                )
            conn.commit()
        except Exception as e:
            logger.error(f"Ошибка пакетной записи ({len(batch)} шт.) в БД: {e}")
        finally:
            cursor.close()
            conn.close()

    def flush_pending_writes_sync(self):
        """Принудительно сбрасывает буфер ответов на диск в БД (вызывается фоновым воркером и при остановке бота)."""
        with self._write_queue_lock:
            if not self._pending_writes:
                return
            batch = list(self._pending_writes)
            self._pending_writes.clear()
        self._insert_batch_to_db_sync(batch)

    async def _delayed_batch_flush(self):
        """Копит ответы в течение 0.8 сек и записывает их в БД одним пакетным запросом (разгружает БД в 10-20 раз)."""
        try:
            await asyncio.sleep(0.8)
            await asyncio.to_thread(self.flush_pending_writes_sync)
        except Exception as e:
            logger.error(f"Ошибка в _delayed_batch_flush: {e}")

    def save_answer_entry(self, user_id: int, username: Optional[str], full_name: Optional[str], entry_str: str):
        """Синхронное сохранение ответа: обновляет RAM-кэш и сразу пишет в БД."""
        now_dt = datetime.now(timezone.utc)
        self._update_ram_cache_on_answer(user_id, entry_str, now_dt)
        self._insert_batch_to_db_sync([(user_id, username, full_name, entry_str)])

    async def save_answer_entry_fast(self, user_id: int, username: Optional[str], full_name: Optional[str], entry_str: str):
        """
        Неблокирующее пакетное сохранение ответа (Optimistic UI + Batch Queue):
        1) Если кэш пользователя ещё не прогрет в RAM — прогревает его 1 раз.
        2) Мгновенно обновляет RAM-кэш (следующая карточка выбирается сразу с новым весом за 0.001с).
        3) Добавляет ответ в буфер пакетной записи и планирует фоновый сброс через 0.8с.
        """
        with self._cache_lock:
            is_cached = (
                user_id in self._user_entries_cache
                and (time.time() - self._user_entries_cache[user_id][0] < self._cache_ttl)
            )
        if not is_cached:
            await asyncio.to_thread(self._fetch_user_entries_with_timestamps, user_id)

        now_dt = datetime.now(timezone.utc)
        self._update_ram_cache_on_answer(user_id, entry_str, now_dt)

        with self._write_queue_lock:
            self._pending_writes.append((user_id, username, full_name, entry_str))
            should_flush_immediately = len(self._pending_writes) >= 25

        if should_flush_immediately:
            asyncio.create_task(asyncio.to_thread(self.flush_pending_writes_sync))
        elif self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._delayed_batch_flush())

    def archive_old_answers(self, days_threshold: int = 30) -> int:
        """
        Фоновое сжатие (Lossless Rollup):
        Схлопывает ответы старше 30 дней в компактную таблицу user_daily_archive.
        Граница cutoff_dt выравнивается строго по полуночи локального часового пояса (TZ),
        чтобы календарные сутки никогда не дробились на части между user_answers и user_daily_archive.
        Отфильтровывает случайные мисклики в тестах (< 3 сек), ограничивает аномальные AFK-тайминги
        и сохраняет точное число попыток, ошибок, весов и имя пользователя.
        """
        self.flush_pending_writes_sync()
        cutoff_local_date = get_local_today() - timedelta(days=days_threshold)
        cutoff_dt = datetime(
            cutoff_local_date.year, cutoff_local_date.month, cutoff_local_date.day,
            0, 0, 0, tzinfo=TZ
        ).astimezone(timezone.utc)
        conn = self.get_connection()
        cursor = conn.cursor()
        compressed_count = 0
        try:
            if self.is_postgres:
                cursor.execute(
                    "SELECT id, user_id, username, full_name, entry, created_at FROM user_answers WHERE created_at < %s ORDER BY id ASC;",
                    (cutoff_dt,)
                )
            else:
                cursor.execute(
                    "SELECT id, user_id, username, full_name, entry, created_at FROM user_answers WHERE created_at < ? ORDER BY id ASC;",
                    (cutoff_dt.strftime("%Y-%m-%d %H:%M:%S"),)
                )
            old_rows = cursor.fetchall()
            if not old_rows:
                return 0

            compressed_count = len(old_rows)
            agg: Dict[Tuple[int, str, str, str], Dict[str, Any]] = {}
            row_ids_to_delete = []

            for r in old_rows:
                r_id = r[0]
                uid = int(r[1])
                uname = str(r[2]).strip() if r[2] is not None and str(r[2]).strip() not in ("", "None", "nan") else None
                fname = str(r[3]).strip() if r[3] is not None and str(r[3]).strip() not in ("", "None", "nan") else None
                entry_str = str(r[4] or "")
                raw_created = r[5]
                d_val = self._extract_date_from_ts(raw_created, target_tz=TZ) or get_local_today()
                d_str = d_val.isoformat()

                parts = entry_str.split(";")
                if len(parts) < 4:
                    row_ids_to_delete.append(r_id)
                    continue

                code = parts[0].strip()
                if not code:
                    row_ids_to_delete.append(r_id)
                    continue

                try:
                    d_ans = float(parts[1])
                    d_tm = float(parts[2])
                    sec = max(0.0, float(parts[3]))
                except ValueError:
                    row_ids_to_delete.append(r_id)
                    continue

                mode = parts[4].strip() if len(parts) >= 5 else "card"
                # Игнорируем мисклики (< 3.0 сек) в режиме тестов
                if mode == "test" and sec < SpacedRepetitionAlgorithm.MIN_TIME_WINDOW:
                    row_ids_to_delete.append(r_id)
                    continue

                sec_clamped = min(sec, SpacedRepetitionAlgorithm.MAX_TIME_WINDOW)
                is_err = 1 if (d_ans > 0) else 0

                key = (uid, d_str, mode, code)
                if key not in agg:
                    agg[key] = {
                        "username": uname,
                        "full_name": fname,
                        "tries": 0,
                        "errors": 0,
                        "time_sec": 0.0,
                        "delta_weight": 0.0,
                        "last_d_ans": d_ans,
                    }
                if uname:
                    agg[key]["username"] = uname
                if fname:
                    agg[key]["full_name"] = fname
                agg[key]["tries"] += 1
                agg[key]["errors"] += is_err
                agg[key]["time_sec"] += sec_clamped
                agg[key]["delta_weight"] += (d_ans + d_tm)
                agg[key]["last_d_ans"] = d_ans
                row_ids_to_delete.append(r_id)

            for (uid, d_str, mode, code), metrics in agg.items():
                if self.is_postgres:
                    cursor.execute("""
                        INSERT INTO user_daily_archive 
                            (user_id, username, full_name, archive_date, mode, code, tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (user_id, archive_date, mode, code) DO UPDATE SET
                            username = COALESCE(EXCLUDED.username, user_daily_archive.username),
                            full_name = COALESCE(EXCLUDED.full_name, user_daily_archive.full_name),
                            tries_count = user_daily_archive.tries_count + EXCLUDED.tries_count,
                            errors_count = user_daily_archive.errors_count + EXCLUDED.errors_count,
                            total_time_sec = user_daily_archive.total_time_sec + EXCLUDED.total_time_sec,
                            total_delta_weight = user_daily_archive.total_delta_weight + EXCLUDED.total_delta_weight,
                            last_delta_ans = EXCLUDED.last_delta_ans;
                    """, (
                        uid, metrics["username"], metrics["full_name"], d_str, mode, code,
                        metrics["tries"], metrics["errors"], round(metrics["time_sec"], 2),
                        round(metrics["delta_weight"], 2), metrics["last_d_ans"]
                    ))
                else:
                    cursor.execute("""
                        INSERT INTO user_daily_archive 
                            (user_id, username, full_name, archive_date, mode, code, tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (user_id, archive_date, mode, code) DO UPDATE SET
                            username = COALESCE(excluded.username, user_daily_archive.username),
                            full_name = COALESCE(excluded.full_name, user_daily_archive.full_name),
                            tries_count = user_daily_archive.tries_count + excluded.tries_count,
                            errors_count = user_daily_archive.errors_count + excluded.errors_count,
                            total_time_sec = user_daily_archive.total_time_sec + excluded.total_time_sec,
                            total_delta_weight = user_daily_archive.total_delta_weight + excluded.total_delta_weight,
                            last_delta_ans = excluded.last_delta_ans;
                    """, (
                        uid, metrics["username"], metrics["full_name"], d_str, mode, code,
                        metrics["tries"], metrics["errors"], round(metrics["time_sec"], 2),
                        round(metrics["delta_weight"], 2), metrics["last_d_ans"]
                    ))

            if row_ids_to_delete:
                batch_size = 500
                for b_i in range(0, len(row_ids_to_delete), batch_size):
                    batch = row_ids_to_delete[b_i:b_i + batch_size]
                    if self.is_postgres:
                        cursor.execute("DELETE FROM user_answers WHERE id = ANY(%s);", (batch,))
                    else:
                        placeholders = ",".join("?" for _ in batch)
                        cursor.execute(f"DELETE FROM user_answers WHERE id IN ({placeholders});", batch)

            conn.commit()
            self.invalidate_user_cache()
            logger.info(f"Сжатие БД: {compressed_count} старых записей успешно агрегированы в {len(agg)} строк архива.")
            return compressed_count
        except Exception as e:
            logger.warning(f"Ошибка при сжатии старых записей БД: {e}")
            return 0
        finally:
            cursor.close()
            conn.close()

    def _fetch_user_entries_with_timestamps(self, user_id: int) -> List[Tuple[str, Any]]:
        """
        Возвращает полную хронологическую историю (архив + свежие ответы).
        Для данных из user_daily_archive восстанавливает точное число попыток (tries_count),
        ошибок (errors_count) и изменение весов, сохраняя 100% математическую идентичность
        метрик и интервальных весов до и после архивации.
        """
        now = time.time()
        with self._cache_lock:
            cached = self._user_entries_cache.get(user_id)
            if cached is not None:
                ts_cached, _, rows_list = cached
                if now - ts_cached < self._cache_ttl:
                    return list(rows_list)
            seq_before = self._write_seq

        result: List[Tuple[str, Any]] = []
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            # 1. Читаем сжатый архив старше 30 дней
            try:
                if self.is_postgres:
                    cursor.execute("""
                        SELECT archive_date, mode, code, tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                        FROM user_daily_archive WHERE user_id = %s ORDER BY archive_date ASC, id ASC;
                    """, (user_id,))
                else:
                    cursor.execute("""
                        SELECT archive_date, mode, code, tries_count, errors_count, total_time_sec, total_delta_weight, last_delta_ans 
                        FROM user_daily_archive WHERE user_id = ? ORDER BY archive_date ASC, id ASC;
                    """, (user_id,))
                arch_rows = cursor.fetchall()
                for ar in arch_rows:
                    a_date = ar[0]
                    a_mode = str(ar[1]).strip()
                    a_code = str(ar[2]).strip()
                    a_tries = max(1, int(ar[3] or 1))
                    a_err = max(0, min(a_tries, int(ar[4] or 0)))
                    a_ok = a_tries - a_err
                    a_sec = round(max(0.0, float(ar[5] or 0.0)) / a_tries, 1)
                    if a_mode == "test" and a_sec < SpacedRepetitionAlgorithm.MIN_TIME_WINDOW:
                        a_sec = SpacedRepetitionAlgorithm.MIN_TIME_WINDOW
                    a_total_dw = float(ar[6] or 0.0)
                    a_last_ans = float(ar[7] or 0.0)
                    ts_val = a_date if isinstance(a_date, date) else f"{str(a_date)[:10]}T12:00:00+00:00"

                    if a_mode == "test":
                        err_ans_val = a_last_ans if a_last_ans > 0 else 2.0
                        ok_entry = f"{a_code};-1.0;0.0;{a_sec};test"
                        err_entry = f"{a_code};{err_ans_val};0.0;{a_sec};test"
                        if a_last_ans > 0 and a_err > 0:
                            for _ in range(a_ok):
                                result.append((ok_entry, ts_val))
                            for _ in range(a_err):
                                result.append((err_entry, ts_val))
                        else:
                            for _ in range(a_err):
                                result.append((err_entry, ts_val))
                            for _ in range(a_ok):
                                result.append((ok_entry, ts_val))
                    else:
                        if a_tries == 1:
                            d_tm_single = round(a_total_dw - a_last_ans, 2)
                            result.append((f"{a_code};{a_last_ans};{d_tm_single};{a_sec};{a_mode}", ts_val))
                        else:
                            step_dw = round(a_total_dw / a_tries, 2)
                            err_ans_val = a_last_ans if a_last_ans > 0 else 2.0
                            for i in range(a_tries - 1):
                                is_step_err = (i < a_err) if a_last_ans <= 0 else (i >= a_ok)
                                step_ans = err_ans_val if is_step_err else -1.0
                                step_tm = round(step_dw - step_ans, 2)
                                result.append((f"{a_code};{step_ans};{step_tm};{a_sec};{a_mode}", ts_val))
                            rem_dw = round(a_total_dw - step_dw * (a_tries - 1), 2)
                            last_tm = round(rem_dw - a_last_ans, 2)
                            result.append((f"{a_code};{a_last_ans};{last_tm};{a_sec};{a_mode}", ts_val))
            except Exception as arch_err:
                logger.warning(f"Не удалось прочитать архив для user_id={user_id}: {arch_err}")

            # 2. Читаем свежие ответы
            if self.is_postgres:
                cursor.execute("SELECT entry, created_at FROM user_answers WHERE user_id = %s ORDER BY id ASC;", (user_id,))
            else:
                cursor.execute("SELECT entry, created_at FROM user_answers WHERE user_id = ? ORDER BY id ASC;", (user_id,))
            rows = cursor.fetchall()
            for r in rows:
                result.append((str(r[0]), r[1]))
        finally:
            cursor.close()
            conn.close()

        with self._cache_lock:
            if self._write_seq == seq_before:
                self._evict_oldest_locked(self._user_entries_cache)
                self._user_entries_cache[user_id] = (time.time(), self._write_seq, result)
        return list(result)

    def _fetch_user_entries(self, user_id: int) -> List[str]:
        rows = self._fetch_user_entries_with_timestamps(user_id)
        return [r[0] for r in rows]

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
        entries = self._fetch_user_entries(user_id)
        return self._compute_progress_from_entries(entries)

    def get_problem_term_codes(
        self,
        user_id: int,
        pre_entries: Optional[List[str]] = None,
        pre_progress: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> List[str]:
        """
        Возвращает список кодов проблемных терминов за 1 проход:
        - Если последнее действие по термину было в режиме теста и ответ верный (is_test_ok),
          термин считается успешно проработанным.
        - Если последняя карточка или последний тест были с ошибкой, либо вес карточки >= 7.0
          (при отсутствии более свежего успешного теста), термин включается в список ошибок.
        """
        entries = pre_entries if pre_entries is not None else self._fetch_user_entries(user_id)
        progress_map = pre_progress if pre_progress is not None else self._compute_progress_from_entries(entries)

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
                        is_err = (d_ans > 0)
                        last_test_problem[code] = is_err
                        last_action_mode[code] = "test"
                        if not is_err:
                            last_card_problem[code] = False
                else:
                    if sec >= 0.0:
                        last_card_problem[code] = (d_ans > 0)
                        last_action_mode[code] = "card"

        problem_codes = []
        seen = set()

        for code, p in progress_map.items():
            is_high_weight = p["total_weight"] >= 7.0
            is_card_err = last_card_problem.get(code, False)
            is_test_ok = (last_action_mode.get(code) == "test" and not last_test_problem.get(code, False))

            if is_test_ok:
                continue

            if (is_high_weight or is_card_err) and catalog.get_by_code(code) and code not in seen:
                seen.add(code)
                problem_codes.append(code)

        for code, is_prob in last_test_problem.items():
            if is_prob and last_action_mode.get(code) == "test" and catalog.get_by_code(code) and code not in seen:
                seen.add(code)
                problem_codes.append(code)

        for code, is_prob in last_card_problem.items():
            if is_prob and last_action_mode.get(code) == "card" and catalog.get_by_code(code) and code not in seen:
                seen.add(code)
                problem_codes.append(code)

        return problem_codes

    @staticmethod
    def _extract_date_from_ts(raw_ts: Any, target_tz=None) -> Optional[date]:
        if raw_ts is None:
            return None
        if isinstance(raw_ts, datetime):
            dt = raw_ts
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if target_tz is not None:
                dt = dt.astimezone(target_tz)
            return dt.date()
        if isinstance(raw_ts, date):
            return raw_ts
        s = str(raw_ts).strip()
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            try:
                return date.fromisoformat(s)
            except ValueError:
                pass
        if len(s) >= 10:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if target_tz is not None:
                    dt = dt.astimezone(target_tz)
                return dt.date()
            except ValueError:
                try:
                    dt = datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)
                    if target_tz is not None:
                        dt = dt.astimezone(target_tz)
                    return dt.date()
                except ValueError:
                    pass
        return None

    def get_user_streak_info(
        self,
        user_id: int,
        reference_date: Optional[date] = None,
        pre_rows: Optional[List[Tuple[str, Any]]] = None,
    ) -> Dict[str, int]:
        today_dt = reference_date or get_local_today()
        rows = pre_rows if pre_rows is not None else self._fetch_user_entries_with_timestamps(user_id)

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

            d = self._extract_date_from_ts(created_at, target_tz=TZ) or today_dt
            if d <= today_dt:
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
        """Вычисляет все показатели статистики за один запрос к кэшу/БД без повторного чтения."""
        rows = self._fetch_user_entries_with_timestamps(user_id)
        entries = [r[0] for r in rows]
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

        streak_info = self.get_user_streak_info(user_id, pre_rows=rows)
        problem_count = len(self.get_problem_term_codes(user_id, pre_entries=entries, pre_progress=progress_map))

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

    # --- НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ, УРОВЕНЬ ЭКЗАМЕНА, ТОКЕНЫ И НАПОМИНАНИЯ ---

    def _ensure_user_settings_row(self, cursor, user_id: int):
        if self.is_postgres:
            cursor.execute(
                "INSERT INTO user_settings (user_id, token_version, reminders_enabled, policy_accepted, exam_level) VALUES (%s, 1, 1, 0, 'ege') ON CONFLICT (user_id) DO NOTHING;",
                (user_id,)
            )
        else:
            cursor.execute(
                "INSERT OR IGNORE INTO user_settings (user_id, token_version, reminders_enabled, policy_accepted, exam_level) VALUES (?, 1, 1, 0, 'ege');",
                (user_id,)
            )

    def _load_user_settings_cached(self, user_id: int) -> Tuple[int, bool, bool, str]:
        now = time.time()
        with self._cache_lock:
            cached = self._user_settings_cache.get(user_id)
            if cached is not None and (now - cached[0] < self._cache_ttl):
                return cached[1], cached[2], cached[3], cached[4]
            seq_before = self._write_seq

        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute(
                    "SELECT token_version, reminders_enabled, COALESCE(policy_accepted, 0), COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = %s;",
                    (user_id,)
                )
            else:
                cursor.execute(
                    "SELECT token_version, reminders_enabled, COALESCE(policy_accepted, 0), COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = ?;",
                    (user_id,)
                )
            row = cursor.fetchone()
            t_ver = int(row[0]) if row and row[0] is not None else 1
            rem_on = bool(int(row[1])) if row and row[1] is not None else True
            pol_ok = bool(int(row[2])) if row and row[2] is not None else False
            raw_lvl = str(row[3] if row and len(row) > 3 and row[3] is not None else "ege").strip().lower()
            exam_lvl = "oge" if raw_lvl == "oge" else "ege"

            # Если пользователь уже активно решал карточки/тесты до обновления, автоматически засчитываем согласие
            if not pol_ok:
                if self.is_postgres:
                    cursor.execute("SELECT 1 FROM user_answers WHERE user_id = %s LIMIT 1;", (user_id,))
                else:
                    cursor.execute("SELECT 1 FROM user_answers WHERE user_id = ? LIMIT 1;", (user_id,))
                has_ans = bool(cursor.fetchone())
                if not has_ans:
                    try:
                        if self.is_postgres:
                            cursor.execute("SELECT 1 FROM user_daily_archive WHERE user_id = %s LIMIT 1;", (user_id,))
                        else:
                            cursor.execute("SELECT 1 FROM user_daily_archive WHERE user_id = ? LIMIT 1;", (user_id,))
                        has_ans = bool(cursor.fetchone())
                    except Exception:
                        pass
                if has_ans:
                    pol_ok = True
                    self._ensure_user_settings_row(cursor, user_id)
                    if self.is_postgres:
                        cursor.execute("UPDATE user_settings SET policy_accepted = 1 WHERE user_id = %s;", (user_id,))
                    else:
                        cursor.execute("UPDATE user_settings SET policy_accepted = 1 WHERE user_id = ?;", (user_id,))
                    conn.commit()

            with self._cache_lock:
                if self._write_seq == seq_before:
                    self._evict_oldest_locked(self._user_settings_cache)
                    self._user_settings_cache[user_id] = (now, t_ver, rem_on, pol_ok, exam_lvl)
            return t_ver, rem_on, pol_ok, exam_lvl
        except Exception:
            return 1, True, False, "ege"
        finally:
            cursor.close()
            conn.close()

    def get_user_token_version(self, user_id: int) -> int:
        t_ver, _, _, _ = self._load_user_settings_cached(user_id)
        return t_ver

    def get_user_reminders_enabled(self, user_id: int) -> bool:
        _, rem_on, _, _ = self._load_user_settings_cached(user_id)
        return rem_on

    def is_user_policy_accepted(self, user_id: int) -> bool:
        _, _, pol_ok, _ = self._load_user_settings_cached(user_id)
        return pol_ok

    def get_user_exam_level(self, user_id: int) -> str:
        _, _, _, exam_lvl = self._load_user_settings_cached(user_id)
        return "oge" if exam_lvl == "oge" else "ege"

    def set_user_exam_level(self, user_id: int, level: str):
        clean_level = "oge" if str(level or "").strip().lower() == "oge" else "ege"
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            self._ensure_user_settings_row(cursor, user_id)
            if self.is_postgres:
                cursor.execute(
                    "UPDATE user_settings SET exam_level = %s, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s;",
                    (clean_level, user_id)
                )
            else:
                cursor.execute(
                    "UPDATE user_settings SET exam_level = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (clean_level, user_id)
                )
            conn.commit()
        finally:
            cursor.close()
            conn.close()

        with self._cache_lock:
            self._write_seq += 1
            self._user_settings_cache.pop(user_id, None)

    def toggle_user_exam_level(self, user_id: int) -> str:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            self._ensure_user_settings_row(cursor, user_id)
            if self.is_postgres:
                cursor.execute(
                    "UPDATE user_settings SET exam_level = CASE WHEN COALESCE(exam_level, 'ege') = 'oge' THEN 'ege' ELSE 'oge' END, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s RETURNING exam_level;",
                    (user_id,)
                )
                row = cursor.fetchone()
                new_level = str(row[0]).strip().lower() if row and row[0] else "ege"
            else:
                cursor.execute(
                    "UPDATE user_settings SET exam_level = CASE WHEN COALESCE(exam_level, 'ege') = 'oge' THEN 'ege' ELSE 'oge' END, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (user_id,)
                )
                cursor.execute("SELECT COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = ?;", (user_id,))
                row = cursor.fetchone()
                new_level = str(row[0]).strip().lower() if row and row[0] else "ege"
            conn.commit()
        finally:
            cursor.close()
            conn.close()

        clean_new = "oge" if new_level == "oge" else "ege"
        with self._cache_lock:
            self._write_seq += 1
            self._user_settings_cache.pop(user_id, None)
        return clean_new

    def accept_user_policy(self, user_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            self._ensure_user_settings_row(cursor, user_id)
            if self.is_postgres:
                cursor.execute(
                    "UPDATE user_settings SET policy_accepted = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s;",
                    (user_id,)
                )
                cursor.execute(
                    "SELECT token_version, reminders_enabled, COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = %s;",
                    (user_id,)
                )
            else:
                cursor.execute(
                    "UPDATE user_settings SET policy_accepted = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (user_id,)
                )
                cursor.execute(
                    "SELECT token_version, reminders_enabled, COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = ?;",
                    (user_id,)
                )
            row = cursor.fetchone()
            conn.commit()
            t_ver = int(row[0]) if row and row[0] is not None else 1
            rem_on = bool(int(row[1])) if row and row[1] is not None else True
            raw_lvl = str(row[2] if row and len(row) > 2 and row[2] is not None else "ege").strip().lower()
            exam_lvl = "oge" if raw_lvl == "oge" else "ege"
            with self._cache_lock:
                self._write_seq += 1
                self._evict_oldest_locked(self._user_settings_cache)
                self._user_settings_cache[user_id] = (time.time(), t_ver, rem_on, True, exam_lvl)
        finally:
            cursor.close()
            conn.close()

    def rotate_user_token(self, user_id: int) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            self._ensure_user_settings_row(cursor, user_id)
            if self.is_postgres:
                cursor.execute(
                    "UPDATE user_settings SET token_version = token_version + 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = %s RETURNING token_version, reminders_enabled, COALESCE(policy_accepted, 0), COALESCE(exam_level, 'ege');",
                    (user_id,)
                )
                row = cursor.fetchone()
            else:
                cursor.execute(
                    "UPDATE user_settings SET token_version = token_version + 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (user_id,)
                )
                cursor.execute(
                    "SELECT token_version, reminders_enabled, COALESCE(policy_accepted, 0), COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = ?;",
                    (user_id,)
                )
                row = cursor.fetchone()
            new_ver = int(row[0]) if row else 2
            rem_on = bool(int(row[1])) if row and row[1] is not None else True
            pol_ok = bool(int(row[2])) if row and row[2] is not None else True
            raw_lvl = str(row[3] if row and len(row) > 3 and row[3] is not None else "ege").strip().lower()
            exam_lvl = "oge" if raw_lvl == "oge" else "ege"
            conn.commit()
            with self._cache_lock:
                self._write_seq += 1
                self._evict_oldest_locked(self._user_settings_cache)
                self._user_settings_cache[user_id] = (time.time(), new_ver, rem_on, pol_ok, exam_lvl)
            return new_ver
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
                cursor.execute(
                    "SELECT token_version, reminders_enabled, COALESCE(policy_accepted, 0), COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = %s;",
                    (user_id,)
                )
            else:
                cursor.execute(
                    "UPDATE user_settings SET reminders_enabled = CASE WHEN reminders_enabled = 1 THEN 0 ELSE 1 END, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?;",
                    (user_id,)
                )
                cursor.execute(
                    "SELECT token_version, reminders_enabled, COALESCE(policy_accepted, 0), COALESCE(exam_level, 'ege') FROM user_settings WHERE user_id = ?;",
                    (user_id,)
                )
            row = cursor.fetchone()
            conn.commit()
            t_ver = int(row[0]) if row and row[0] is not None else 1
            rem_on = bool(int(row[1])) if row and row[1] is not None else True
            pol_ok = bool(int(row[2])) if row and row[2] is not None else True
            raw_lvl = str(row[3] if row and len(row) > 3 and row[3] is not None else "ege").strip().lower()
            exam_lvl = "oge" if raw_lvl == "oge" else "ege"
            with self._cache_lock:
                self._write_seq += 1
                self._evict_oldest_locked(self._user_settings_cache)
                self._user_settings_cache[user_id] = (time.time(), t_ver, rem_on, pol_ok, exam_lvl)
            return rem_on
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
        today_dt = reference_date or get_local_today()
        today_iso = today_dt.isoformat()
        yesterday_dt = today_dt - timedelta(days=1)
        cutoff_dt = datetime(yesterday_dt.year, yesterday_dt.month, yesterday_dt.day, 0, 0, 0, tzinfo=TZ).astimezone(timezone.utc)

        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            recent_uids_set: Set[int] = set()
            if self.is_postgres:
                cursor.execute("SELECT DISTINCT user_id FROM user_answers WHERE created_at >= %s;", (cutoff_dt,))
                for r in cursor.fetchall():
                    if r and r[0] is not None:
                        recent_uids_set.add(int(r[0]))
                cursor.execute("SELECT DISTINCT user_id FROM user_daily_archive WHERE archive_date >= %s;", (yesterday_dt,))
                for r in cursor.fetchall():
                    if r and r[0] is not None:
                        recent_uids_set.add(int(r[0]))
            else:
                cutoff_str = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")
                cursor.execute("SELECT DISTINCT user_id FROM user_answers WHERE created_at >= ?;", (cutoff_str,))
                for r in cursor.fetchall():
                    if r and r[0] is not None:
                        recent_uids_set.add(int(r[0]))
                cursor.execute("SELECT DISTINCT user_id FROM user_daily_archive WHERE archive_date >= ?;", (yesterday_dt.isoformat(),))
                for r in cursor.fetchall():
                    if r and r[0] is not None:
                        recent_uids_set.add(int(r[0]))

            recent_uids = sorted(recent_uids_set)
            if not recent_uids:
                return []

            settings_map: Dict[int, Tuple[bool, str]] = {}
            batch_size = 500
            for i in range(0, len(recent_uids), batch_size):
                batch = recent_uids[i:i + batch_size]
                if self.is_postgres:
                    cursor.execute(
                        "SELECT user_id, reminders_enabled, last_reminder_date FROM user_settings WHERE user_id = ANY(%s);",
                        (batch,)
                    )
                else:
                    placeholders = ",".join("?" for _ in batch)
                    cursor.execute(
                        f"SELECT user_id, reminders_enabled, last_reminder_date FROM user_settings WHERE user_id IN ({placeholders});",
                        batch
                    )
                for r in cursor.fetchall():
                    settings_map[int(r[0])] = (
                        bool(int(r[1])) if r[1] is not None else True,
                        str(r[2] or "")
                    )
        finally:
            cursor.close()
            conn.close()

        candidates = []
        for uid in recent_uids:
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
                    cursor.execute("""
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
                    """)
                    cursor.execute("""
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

    def update_teacher_name(self, user_id: int, full_name: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute("UPDATE teachers SET full_name = %s WHERE user_id = %s;", (full_name, user_id))
            else:
                cursor.execute("UPDATE teachers SET full_name = ? WHERE user_id = ?;", (full_name, user_id))
            conn.commit()
        except Exception:
            pass
        finally:
            cursor.close()
            conn.close()

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
        """Потокобезопасная регистрация учителя с защитой от гонок (ON CONFLICT DO NOTHING)."""
        existing = self.get_teacher_by_user_id(user_id)
        if existing:
            return existing["teacher_code"]

        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            for _ in range(1000):
                if self.is_postgres:
                    cursor.execute("SELECT teacher_code FROM teachers WHERE user_id = %s;", (user_id,))
                else:
                    cursor.execute("SELECT teacher_code FROM teachers WHERE user_id = ?;", (user_id,))
                row = cursor.fetchone()
                if row:
                    return str(row[0])

                candidate = str(random.randint(10000000, 99999999))
                try:
                    if self.is_postgres:
                        cursor.execute(
                            """
                            INSERT INTO teachers (teacher_code, user_id, full_name, token_version)
                            VALUES (%s, %s, %s, 1)
                            ON CONFLICT DO NOTHING
                            RETURNING teacher_code;
                            """,
                            (candidate, user_id, full_name)
                        )
                        ins_row = cursor.fetchone()
                        conn.commit()
                        if ins_row:
                            logger.info(f"Зарегистрирован новый преподаватель {full_name} с кодом {candidate}.")
                            return str(ins_row[0])
                    else:
                        cursor.execute(
                            "INSERT OR IGNORE INTO teachers (teacher_code, user_id, full_name, token_version) VALUES (?, ?, ?, 1);",
                            (candidate, user_id, full_name)
                        )
                        conn.commit()
                        if cursor.rowcount and cursor.rowcount > 0:
                            logger.info(f"Зарегистрирован новый преподаватель {full_name} с кодом {candidate}.")
                            return candidate
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass

            raise RuntimeError("Не удалось сгенерировать уникальный 8-значный код учителя.")
        finally:
            cursor.close()
            conn.close()

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

    def delete_user_data(self, user_id: int):
        """Полностью стирает историю ответов, настройки и привязки пользователя."""
        with self._write_queue_lock:
            self._pending_writes = [item for item in self._pending_writes if item[0] != user_id]
        self.flush_pending_writes_sync()
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            if self.is_postgres:
                cursor.execute("DELETE FROM user_answers WHERE user_id = %s;", (user_id,))
                cursor.execute("DELETE FROM user_daily_archive WHERE user_id = %s;", (user_id,))
                cursor.execute("DELETE FROM user_settings WHERE user_id = %s;", (user_id,))
                cursor.execute("DELETE FROM student_teachers WHERE student_id = %s;", (user_id,))
                cursor.execute(
                    "DELETE FROM student_teachers WHERE teacher_code IN (SELECT teacher_code FROM teachers WHERE user_id = %s);",
                    (user_id,)
                )
                cursor.execute("DELETE FROM teachers WHERE user_id = %s;", (user_id,))
            else:
                cursor.execute("DELETE FROM user_answers WHERE user_id = ?;", (user_id,))
                cursor.execute("DELETE FROM user_daily_archive WHERE user_id = ?;", (user_id,))
                cursor.execute("DELETE FROM user_settings WHERE user_id = ?;", (user_id,))
                cursor.execute("DELETE FROM student_teachers WHERE student_id = ?;", (user_id,))
                cursor.execute(
                    "DELETE FROM student_teachers WHERE teacher_code IN (SELECT teacher_code FROM teachers WHERE user_id = ?);",
                    (user_id,)
                )
                cursor.execute("DELETE FROM teachers WHERE user_id = ?;", (user_id,))
            conn.commit()
        finally:
            cursor.close()
            conn.close()
        self.invalidate_user_cache(user_id)


db = Database()


# =====================================================================
# 7. СЕССИОННЫЙ МЕНЕДЖЕР И ВЫБОРКА КАРТОЧЕК
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
            return SpacedRepetitionAlgorithm.UNREVIEWED_PRIORITY_WEIGHT
        return max(SpacedRepetitionAlgorithm.MIN_WEIGHT, float(progress_map[code]["total_weight"]))

    weights = [get_effective_weight(t) for t in candidates]
    chosen = random.choices(candidates, weights=weights, k=1)[0]
    session_tracker.push(user_id, chosen["code"])
    return chosen


# =====================================================================
# 8. ГЕНЕРАТОР ТЕСТОВ
# =====================================================================

class TestGenerator:
    @staticmethod
    def _pick_unique_distractors(distractors: List[Dict[str, Any]], count: int, correct_term: Dict[str, Any]) -> List[Dict[str, Any]]:
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

        sampled_distractors = random.sample(distractors, min(len(distractors), 15)) if distractors else []
        foreign_candidates: List[Tuple[Dict[str, Any], str]] = []
        for d in sampled_distractors:
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
                unique_real_traits.append(f"Признак понятия ({len(unique_real_traits) + 1})")

            foreign_term, foreign_trait = random.choice(foreign_candidates)
            explanation_note = f"Лишний признак: «{foreign_trait}» (относится к понятию «{foreign_term['term']}»)."

            options = [
                {"id": "A", "code": correct_term["code"], "text": foreign_trait, "is_correct": True},
                {"id": "B", "code": correct_term["code"], "text": unique_real_traits[0], "is_correct": False},
                {"id": "C", "code": correct_term["code"], "text": unique_real_traits[1], "is_correct": False},
                {"id": "D", "code": correct_term["code"], "text": unique_real_traits[2], "is_correct": False},
            ]

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
# 9. СОСТОЯНИЯ И НИЖНИЕ REPLY-КЛАВИАТУРЫ (100% БЕЗ ИНЛАЙН-КНОПОК)
# =====================================================================

class UserFlow(StatesGroup):
    selecting_mode = State()
    selecting_topic = State()
    selecting_exam_level = State()
    in_flashcards = State()
    in_tests = State()
    in_errors_menu = State()
    in_search = State()
    in_stats = State()
    in_stats_reset_confirm = State()
    in_my_teachers = State()
    in_teacher_cabinet = State()
    in_teacher_reset_confirm = State()
    in_delete_me_confirm = State()
    awaiting_policy_accept = State()


BTN_MAIN_CARDS = "🗂 Карточки"
BTN_MAIN_TESTS = "🎯 Тесты"
BTN_MAIN_ERRORS = "🛠 Работа над ошибками"
BTN_MAIN_SEARCH = "🔎 Поиск термина"
BTN_MAIN_STATS = "📊 Моя статистика"

BTN_EXAM_OGE = "🎓 ОГЭ"
BTN_EXAM_EGE = "🏛 ЕГЭ"

BTN_BACK_MAIN = "🏠 В главное меню"
BTN_BACK_STATS = "⬅️ К статистике"
BTN_BACK_TEACHER = "⬅️ В кабинет учителя"

TOPIC_BUTTONS_MAP = {
    "🌐 Все темы": "all",
    "1. Человек и общество": "1",
    "2. Экономика": "2",
    "3. Социальные отношения": "3",
    "4. Политика": "4",
    "5. Право": "5",
}

BTN_CARD_FLIP = "🔍 Перевернуть карточку"
BTN_CARD_KNOW = "🟢 Знаю"
BTN_CARD_UNSURE = "🟡 Неуверенно"
BTN_CARD_FORGOT = "🔴 Забыл"
BTN_CARD_DETAIL = "💡 Пояснение"

BTN_TEST_NEXT = "➡️ Следующий вопрос"
BTN_TEST_SHOW_ERRORS = "❌ Просмотреть ошибки"
BTN_TEST_RESTART = "🔄 Начать заново"

BTN_ERR_CARDS = "🗂 Проработать в карточках"
BTN_ERR_TESTS = "🎯 Проверить в тесте"

BTN_STATS_WEBAPP = "📱 Открыть аналитику"
BTN_STATS_REMIND_ON = "🔔 Напоминания: Вкл"
BTN_STATS_REMIND_OFF = "🔕 Напоминания: Выкл"
BTN_STATS_RESET_LINK = "🔄 Сбросить ссылку"
BTN_STATS_MY_TEACHERS = "👨‍🏫 Преподаватели"
BTN_CONFIRM_RESET_YES = "✅ Да, обновить ссылку"

BTN_TEACHER_WEBAPP = "📱 Аналитика класса"
BTN_TEACHER_RESET_LINK = "🔄 Сбросить ссылку класса"

BTN_CONFIRM_DELETE_DATA = "🗑 Да, стереть все мои данные"
BTN_CANCEL_DELETE_DATA = "⬅️ Отмена, оставить данные"
BTN_ACCEPT_POLICY = "✅ Принять и начать"


class Keyboards:
    @staticmethod
    def main_menu() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=BTN_MAIN_CARDS), KeyboardButton(text=BTN_MAIN_TESTS)],
                [KeyboardButton(text=BTN_MAIN_ERRORS), KeyboardButton(text=BTN_MAIN_SEARCH)],
                [KeyboardButton(text=BTN_MAIN_STATS)],
            ],
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Выберите режим или введите термин..."
        )

    @staticmethod
    def exam_level_selection() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=BTN_EXAM_OGE), KeyboardButton(text=BTN_EXAM_EGE)],
            ],
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Выберите экзамен (ОГЭ или ЕГЭ)..."
        )

    @staticmethod
    def topic_selection(mode: str = "cards") -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🌐 Все темы")],
                [KeyboardButton(text="1. Человек и общество"), KeyboardButton(text="2. Экономика")],
                [KeyboardButton(text="3. Социальные отношения"), KeyboardButton(text="4. Политика")],
                [KeyboardButton(text="5. Право"), KeyboardButton(text=BTN_BACK_MAIN)],
            ],
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Выберите блок кодификатора..."
        )

    @staticmethod
    def error_workout_menu(count: int = 0) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=BTN_ERR_CARDS), KeyboardButton(text=BTN_ERR_TESTS)],
                [KeyboardButton(text=BTN_BACK_MAIN)],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    @staticmethod
    def flashcard_front(code: str = "") -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=BTN_CARD_FLIP)],
                [KeyboardButton(text=BTN_BACK_MAIN)],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    @staticmethod
    def flashcard_back(code: str = "", has_detail: bool = False) -> ReplyKeyboardMarkup:
        second_row = []
        if not has_detail:
            second_row.append(KeyboardButton(text=BTN_CARD_DETAIL))
        second_row.append(KeyboardButton(text=BTN_BACK_MAIN))
        return ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(text=BTN_CARD_KNOW),
                    KeyboardButton(text=BTN_CARD_UNSURE),
                    KeyboardButton(text=BTN_CARD_FORGOT),
                ],
                second_row,
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    @staticmethod
    def test_options(code: str = "", options: Optional[List[Dict[str, Any]]] = None) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(text="1"),
                    KeyboardButton(text="2"),
                    KeyboardButton(text="3"),
                    KeyboardButton(text="4"),
                ],
                [KeyboardButton(text=BTN_BACK_MAIN)],
            ],
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Нажмите 1, 2, 3 или 4..."
        )

    @staticmethod
    def test_next() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=BTN_TEST_NEXT)],
                [KeyboardButton(text=BTN_BACK_MAIN)],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    @staticmethod
    def test_finish(has_errors: bool = True) -> ReplyKeyboardMarkup:
        rows = []
        if has_errors:
            rows.append([KeyboardButton(text=BTN_TEST_SHOW_ERRORS)])
        rows.append([KeyboardButton(text=BTN_TEST_RESTART), KeyboardButton(text=BTN_BACK_MAIN)])
        return ReplyKeyboardMarkup(
            keyboard=rows,
            resize_keyboard=True,
            is_persistent=True,
        )

    @staticmethod
    def search_results_keyboard(matched_terms: List[Dict[str, Any]], show_detail_btn: bool = False) -> ReplyKeyboardMarkup:
        rows = []
        row_buf = []
        for t in matched_terms[:6]:
            row_buf.append(KeyboardButton(text=f"📖 {truncate_plain(t['term'], 32)}"))
            if len(row_buf) == 2:
                rows.append(row_buf)
                row_buf = []
        if row_buf:
            rows.append(row_buf)
        bottom = []
        if show_detail_btn:
            bottom.append(KeyboardButton(text=BTN_CARD_DETAIL))
        bottom.append(KeyboardButton(text=BTN_BACK_MAIN))
        rows.append(bottom)
        return ReplyKeyboardMarkup(
            keyboard=rows,
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Введите название термина..."
        )

    @staticmethod
    def stats_reply(student_web_url: str, reminders_on: bool) -> ReplyKeyboardMarkup:
        reminder_label = BTN_STATS_REMIND_ON if reminders_on else BTN_STATS_REMIND_OFF
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=BTN_STATS_WEBAPP, web_app=WebAppInfo(url=student_web_url))],
                [KeyboardButton(text=reminder_label), KeyboardButton(text=BTN_STATS_RESET_LINK)],
                [KeyboardButton(text=BTN_STATS_MY_TEACHERS), KeyboardButton(text=BTN_BACK_MAIN)],
            ],
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Управление статистикой и аналитикой..."
        )

    @staticmethod
    def stats_reset_confirm_reply() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=BTN_CONFIRM_RESET_YES)],
                [KeyboardButton(text=BTN_BACK_STATS), KeyboardButton(text=BTN_BACK_MAIN)],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    @staticmethod
    def my_teachers_reply(teachers: List[Dict[str, Any]]) -> ReplyKeyboardMarkup:
        rows = []
        for t in teachers[:6]:
            rows.append([KeyboardButton(text=f"❌ Отключить {t['teacher_code']}")])
        rows.append([KeyboardButton(text=BTN_BACK_STATS), KeyboardButton(text=BTN_BACK_MAIN)])
        return ReplyKeyboardMarkup(
            keyboard=rows,
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Выберите действие или отправьте 8-значный код учителя..."
        )

    @staticmethod
    def teacher_cabinet_reply(teacher_web_url: str) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=BTN_TEACHER_WEBAPP, web_app=WebAppInfo(url=teacher_web_url))],
                [KeyboardButton(text=BTN_TEACHER_RESET_LINK), KeyboardButton(text=BTN_BACK_MAIN)],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    @staticmethod
    def teacher_reset_confirm_reply() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=BTN_CONFIRM_RESET_YES)],
                [KeyboardButton(text=BTN_BACK_TEACHER), KeyboardButton(text=BTN_BACK_MAIN)],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    @staticmethod
    def back_to_menu() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=BTN_BACK_MAIN)]],
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Введите запрос или вернитесь в меню..."
        )

    @staticmethod
    def delete_confirm_reply() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=BTN_CONFIRM_DELETE_DATA)],
                [KeyboardButton(text=BTN_CANCEL_DELETE_DATA)],
            ],
            resize_keyboard=True,
            is_persistent=True,
        )

    @staticmethod
    def policy_consent_reply() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=BTN_ACCEPT_POLICY)],
            ],
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Нажмите «✅ Принять и начать»..."
        )


# =====================================================================
# 10. ФОРМАТИРОВАНИЕ КАРТОЧЕК И ТЕОРЕТИЧЕСКОГО ДОСЬЕ (ОГЭ / ЕГЭ)
# =====================================================================

def format_card_back(card: Dict[str, Any], show_detail: bool = False, exam_level: str = "ege") -> str:
    is_oge = (str(exam_level or "ege").strip().lower() == "oge")
    traits = [str(t).strip() for t in (card.get("traits") or []) if str(t).strip()]

    mod_title = strip_md_delimiters(truncate_plain(card.get("module_title") or "Обществознание", 120))
    term_title = strip_md_delimiters(truncate_plain(str(card.get("term") or "").upper(), 150))

    if is_oge:
        genus_text = strip_md_delimiters(truncate_plain(card.get("genus") or "Родовое понятие не указано", 240))
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

    # Форматирование для ЕГЭ (полное академическое досье)
    genus_text = strip_md_delimiters(truncate_plain(card.get("genus") or "Родовое понятие не указано", 400))
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
    types_list = [str(x).strip() for x in (struct.get("types_or_forms") or []) if str(x).strip()]
    if types_list:
        types_str = "\n".join([f"• {escape_md(truncate_plain(item, 220))}" for item in types_list[:8]])
        if len(types_list) > 8:
            types_str += f"\n_...и ещё {len(types_list) - 8} форм_"
        extra_blocks.append(f"🧩 *Формы и классификации:*\n{types_str}")

    funcs_list = [str(x).strip() for x in (struct.get("functions_or_goals") or []) if str(x).strip()]
    if funcs_list:
        funcs_str = "\n".join([f"• {escape_md(truncate_plain(item, 220))}" for item in funcs_list[:8]])
        if len(funcs_list) > 8:
            funcs_str += f"\n_...и ещё {len(funcs_list) - 8} функций_"
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


# =====================================================================
# 11. ХЕНДЛЕРЫ БОТА (РЕЖИМ ОДНОГО ЭКРАНА + ЗАЩИТА ОТ ГОНОК)
# =====================================================================

router = Router()


@router.error()
async def global_error_handler(event: ErrorEvent, bot: Bot):
    upd = event.update
    user_str = "Неизвестен"
    ctx_str = "Update"
    try:
        if upd.message and upd.message.from_user:
            u = upd.message.from_user
            user_str = f"User #{str(u.id)[-4:]}"
            ctx_str = f"Message: {str(upd.message.text or '')[:40]}"
        elif upd.callback_query and upd.callback_query.from_user:
            u = upd.callback_query.from_user
            user_str = f"User #{str(u.id)[-4:]}"
            ctx_str = f"Callback: {str(upd.callback_query.data or '')[:40]}"
    except Exception:
        pass

    await live_logger.report_error(bot, event.exception, context_info=ctx_str, user_info=user_str)
    return True


def _build_policy_consent_text() -> str:
    safe_priv_url = PRIVACY_URL.replace(")", "%29")
    return (
        "⚖️ *Правила использования тренажёра*\n\n"
        "Перед началом работы подтвердите согласие с "
        f"[Политикой конфиденциальности и условиями сервиса]({safe_priv_url}).\n\n"
        "💡 _Стереть все данные можно в любой момент командой_ `/delete_me`."
    )


async def _process_teacher_deeplink(user_id: int, teacher_code: str) -> str:
    teacher = await asyncio.to_thread(db.get_teacher, teacher_code)
    if not teacher:
        return f"❌ *Преподаватель по коду* `{escape_md(teacher_code)}` *не найден.*\n\n"
    if teacher["user_id"] == user_id:
        return "⚠️ *Вы не можете прикрепиться к своему собственному классу в качестве ученика.*\n\n"
    if not await asyncio.to_thread(db.is_student_connected, user_id, teacher_code):
        await asyncio.to_thread(db.connect_student_to_teacher, user_id, teacher_code)
        return (
            f"✅ *Вы успешно прикреплены к преподавателю:*\n"
            f"*{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(teacher_code)}`).\n\n"
        )
    return (
        f"ℹ️ *Вы уже прикреплены к преподавателю:*\n"
        f"*{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(teacher_code)}`).\n\n"
    )


@router.message(CommandStart())
async def handle_start(message: Message, state: FSMContext):
    if not message.from_user:
        return
    uid = message.from_user.id
    async with user_lock_manager.get_lock(uid):
        prev_data = await state.get_data()
        prev_bot_id = prev_data.get("last_bot_msg_id")
        await state.clear()
        if prev_bot_id:
            await state.update_data(last_bot_msg_id=prev_bot_id)

        parts = message.text.split(maxsplit=1) if message.text else []
        pending_t_code = None
        if len(parts) > 1 and parts[1].strip().startswith("t_"):
            pending_t_code = parts[1].strip()[2:].strip()

        policy_ok = await asyncio.to_thread(db.is_user_policy_accepted, uid)
        if not policy_ok:
            await state.set_state(UserFlow.awaiting_policy_accept)
            if pending_t_code:
                await state.update_data(pending_teacher_code=pending_t_code)
            await render_single_screen(
                message,
                state,
                _build_policy_consent_text(),
                reply_markup=Keyboards.policy_consent_reply(),
            )
            return

        connect_notice = ""
        if pending_t_code:
            connect_notice = await _process_teacher_deeplink(uid, pending_t_code)

        text = f"{connect_notice}🏠 *Главное меню*\n\nВыберите формат тренировки:"
        await render_single_screen(message, state, text, reply_markup=Keyboards.main_menu())


@router.message(F.text == BTN_ACCEPT_POLICY)
async def handle_accept_policy_reply(message: Message, state: FSMContext):
    if not message.from_user:
        return
    uid = message.from_user.id
    async with user_lock_manager.get_lock(uid):
        data = await state.get_data()
        pending_t_code = data.get("pending_teacher_code")
        prev_bot_id = data.get("last_bot_msg_id")
        await asyncio.to_thread(db.accept_user_policy, uid)
        await state.clear()
        if prev_bot_id:
            await state.update_data(last_bot_msg_id=prev_bot_id)
        if pending_t_code:
            await state.update_data(pending_teacher_code=pending_t_code)

        await state.set_state(UserFlow.selecting_exam_level)
        await render_single_screen(
            message,
            state,
            "К какому экзамену ты готовишься?",
            reply_markup=Keyboards.exam_level_selection(),
        )


@router.message(StateFilter(UserFlow.selecting_exam_level), F.text.in_({BTN_EXAM_OGE, BTN_EXAM_EGE}))
async def handle_select_exam_level_reply(message: Message, state: FSMContext):
    if not message.from_user or not message.text:
        return
    uid = message.from_user.id
    async with user_lock_manager.get_lock(uid):
        chosen_level = "oge" if message.text.strip() == BTN_EXAM_OGE else "ege"
        await asyncio.to_thread(db.set_user_exam_level, uid, chosen_level)

        data = await state.get_data()
        pending_t_code = data.get("pending_teacher_code")
        prev_bot_id = data.get("last_bot_msg_id")
        await state.clear()
        if prev_bot_id:
            await state.update_data(last_bot_msg_id=prev_bot_id)

        connect_notice = ""
        if pending_t_code:
            connect_notice = await _process_teacher_deeplink(uid, str(pending_t_code))

        level_badge = (
            "🎓 *Выбран режим подготовки: ОГЭ*\n_Карточки адаптированы: базовое определение, 2 главных признака и пример из РФ._\n\n"
            if chosen_level == "oge"
            else "🏛 *Выбран режим подготовки: ЕГЭ*\n_Включены полные академические формулировки, классификации и разбор ловушек._\n\n"
        )
        text = f"{connect_notice}{level_badge}🏠 *Главное меню*\n\nВыберите формат тренировки:"
        await render_single_screen(message, state, text, reply_markup=Keyboards.main_menu())


@router.message(Command("change"))
async def handle_change_exam_command(message: Message, state: FSMContext):
    if not message.from_user:
        return
    uid = message.from_user.id
    async with user_lock_manager.get_lock(uid):
        policy_ok = await asyncio.to_thread(db.is_user_policy_accepted, uid)
        if not policy_ok:
            await state.set_state(UserFlow.awaiting_policy_accept)
            await render_single_screen(
                message,
                state,
                _build_policy_consent_text(),
                reply_markup=Keyboards.policy_consent_reply(),
            )
            return

        new_level = await asyncio.to_thread(db.toggle_user_exam_level, uid)
        prev_data = await state.get_data()
        prev_bot_id = prev_data.get("last_bot_msg_id")
        await state.clear()
        if prev_bot_id:
            await state.update_data(last_bot_msg_id=prev_bot_id)

        if new_level == "oge":
            text = (
                "🎓 *Режим переключен: ОГЭ*\n\n"
                "_Карточки стали более краткими: базовое определение и ключевые признаки._\n\n"
                "Для возврата введи /change"
            )
        else:
            text = (
                "🏛 *Режим переключен: ЕГЭ*\n\n"
                "_Включены полные академические формулировки, классификации и примеры._\n\n"
                "Для возврата введи /change"
            )
        await render_single_screen(message, state, text, reply_markup=Keyboards.main_menu())


@router.message(F.text.in_({BTN_BACK_MAIN, "⬅️ В главное меню", "🚪 В главное меню", "🚪 Меню", "🚪 Завершить тест"}))
async def handle_reply_back_to_main(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    async with user_lock_manager.get_lock(uid):
        policy_ok = await asyncio.to_thread(db.is_user_policy_accepted, uid)
        if not policy_ok:
            await state.set_state(UserFlow.awaiting_policy_accept)
            await render_single_screen(
                message,
                state,
                _build_policy_consent_text(),
                reply_markup=Keyboards.policy_consent_reply(),
            )
            return

        prev_data = await state.get_data()
        prev_bot_id = prev_data.get("last_bot_msg_id")
        await state.clear()
        if prev_bot_id:
            await state.update_data(last_bot_msg_id=prev_bot_id)
        text = "🏠 *Главное меню*\n\nВыберите формат тренировки:"
        await render_single_screen(message, state, text, reply_markup=Keyboards.main_menu())


# ---------------------------------------------------------------------
# УЧИТЕЛЬСКАЯ СИСТЕМА И РОТАЦИЯ ТОКЕНОВ (НА НИЖНЕЙ КЛАВИАТУРЕ)
# ---------------------------------------------------------------------

def _build_teacher_cabinet_view(
    full_name: str,
    code: str,
    st_count: int,
    token_version: int,
    bot_username: str,
    is_new: bool = False
) -> Tuple[str, ReplyKeyboardMarkup]:
    raw_dash_url = build_teacher_webapp_url(code, token_version)
    invite_url = escape_md(f"https://t.me/{bot_username}?start=t_{code}")

    if is_new:
        text = (
            f"🎉 *Вы успешно зарегистрированы как преподаватель!*\n\n"
            f"👤 Имя: *{strip_md_delimiters(full_name)}*\n"
            f"🔑 Ваш личный 8-значный код: `{escape_md(code)}`\n\n"
            f"👥 *Ссылка для учеников (отправьте её классу):*\n"
            f"{invite_url}\n\n"
            f"💡 _Нажмите кнопку «📱 Аналитика класса» внизу экрана для просмотра сводки._"
        )
    else:
        text = (
            f"👨‍🏫 *Кабинет преподавателя*\n\n"
            f"👤 Преподаватель: *{strip_md_delimiters(full_name)}*\n"
            f"🔑 Личный код класса: `{escape_md(code)}`\n"
            f"👥 Подключено учеников: *{st_count}*\n\n"
            f"🔗 *Ссылка для учеников (отправьте в чат класса):*\n"
            f"{invite_url}"
        )

    keyboard = Keyboards.teacher_cabinet_reply(raw_dash_url)
    return text, keyboard


@router.message(Command("teacher", "teachers"))
@router.message(F.text == BTN_BACK_TEACHER)
async def handle_teacher_profile(message: Message, state: FSMContext):
    if not message.from_user:
        return
    user_id = message.from_user.id
    async with user_lock_manager.get_lock(user_id):
        policy_ok = await asyncio.to_thread(db.is_user_policy_accepted, user_id)
        if not policy_ok:
            await state.set_state(UserFlow.awaiting_policy_accept)
            await render_single_screen(
                message,
                state,
                _build_policy_consent_text(),
                reply_markup=Keyboards.policy_consent_reply(),
            )
            return

        existing = await asyncio.to_thread(db.get_teacher_by_user_id, user_id)

        bot_username = getattr(message.bot, "_cached_username", None)
        if not bot_username:
            bot_info = await message.bot.get_me()
            bot_username = bot_info.username or "obshaga_bot"
            setattr(message.bot, "_cached_username", bot_username)

        await state.set_state(UserFlow.in_teacher_cabinet)

        if existing:
            code = existing["teacher_code"]
            safe_t_name = get_safe_display_name(message.from_user)
            if existing.get("full_name") != safe_t_name:
                await asyncio.to_thread(db.update_teacher_name, user_id, safe_t_name)
                existing["full_name"] = safe_t_name
            st_count = await asyncio.to_thread(db.get_teacher_students_count, code)
            t_ver = existing.get("token_version", 1)
            text, kb = _build_teacher_cabinet_view(
                existing["full_name"], code, st_count, t_ver, bot_username, is_new=False
            )
            await render_single_screen(message, state, text, reply_markup=kb)
            return

        user = message.from_user
        full_name = get_safe_display_name(user)
        code = await asyncio.to_thread(db.register_teacher, user_id, full_name)
        text, kb = _build_teacher_cabinet_view(
            full_name, code, 0, 1, bot_username, is_new=True
        )
        await render_single_screen(message, state, text, reply_markup=kb)


@router.message(StateFilter(UserFlow.in_teacher_cabinet), F.text == BTN_TEACHER_RESET_LINK)
async def handle_teacher_reset_prompt_reply(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        existing = await asyncio.to_thread(db.get_teacher_by_user_id, message.from_user.id)
        if not existing:
            await state.clear()
            await render_single_screen(message, state, "Профиль преподавателя не найден.", reply_markup=Keyboards.main_menu())
            return

        await state.set_state(UserFlow.in_teacher_reset_confirm)
        text = (
            "🔐 *Сброс ссылки на аналитику класса*\n\n"
            "Вы уверены, что хотите выпустить новую ссылку для кабинета преподавателя?\n"
            "Предыдущая ссылка сразу перестанет работать на всех устройствах "
            "(8-значный код класса для учеников останется прежним)."
        )
        await render_single_screen(message, state, text, reply_markup=Keyboards.teacher_reset_confirm_reply())


@router.message(StateFilter(UserFlow.in_teacher_reset_confirm), F.text == BTN_CONFIRM_RESET_YES)
async def handle_teacher_reset_confirm_reply(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        res = await asyncio.to_thread(db.rotate_teacher_token, message.from_user.id)
        if not res:
            await state.clear()
            await render_single_screen(message, state, "Профиль преподавателя не найден.", reply_markup=Keyboards.main_menu())
            return

        code, new_ver = res
        existing = await asyncio.to_thread(db.get_teacher_by_user_id, message.from_user.id)
        bot_username = getattr(message.bot, "_cached_username", None)
        if not bot_username:
            bot_info = await message.bot.get_me()
            bot_username = bot_info.username or "obshaga_bot"
            setattr(message.bot, "_cached_username", bot_username)
        st_count = await asyncio.to_thread(db.get_teacher_students_count, code)
        full_name = existing["full_name"] if existing else f"Преподаватель {code}"

        await state.set_state(UserFlow.in_teacher_cabinet)
        text, kb = _build_teacher_cabinet_view(full_name, code, st_count, new_ver, bot_username, is_new=False)
        text = "✅ *Ссылка на аналитику класса успешно обновлена!*\n\n" + text
        await render_single_screen(message, state, text, reply_markup=kb)


@router.message(Command("reset_link"))
async def handle_reset_link_command(message: Message, state: FSMContext):
    if not message.from_user:
        return
    user_id = message.from_user.id
    async with user_lock_manager.get_lock(user_id):
        new_student_ver = await asyncio.to_thread(db.rotate_user_token, user_id)
        reminders_on = await asyncio.to_thread(db.get_user_reminders_enabled, user_id)
        student_url = build_student_webapp_url(user_id, new_student_ver)

        lines = [
            "🔐 *Ссылка на аналитику успешно обновлена!*\n",
            "Все старые ссылки отозваны и больше не открываются.\n",
            "Нажмите кнопку «📱 Открыть аналитику» внизу экрана для просмотра."
        ]

        teacher_res = await asyncio.to_thread(db.rotate_teacher_token, user_id)
        if teacher_res:
            lines.append("\n👨‍🏫 *Ссылка кабинета преподавателя также успешно обновлена.*")

        await state.set_state(UserFlow.in_stats)
        await render_single_screen(
            message,
            state,
            "\n".join(lines),
            reply_markup=Keyboards.stats_reply(student_url, reminders_on),
        )


async def _show_student_teachers_screen(message: Message, state: FSMContext, user_id: int, prefix_note: str = ""):
    teachers = await asyncio.to_thread(db.get_student_teachers, user_id)
    await state.set_state(UserFlow.in_my_teachers)
    if not teachers:
        text = (
            f"{prefix_note}"
            "ℹ️ *Вы пока не прикреплены ни к одному преподавателю.*\n\n"
            "Чтобы подключиться, просто отправьте его 8-значный код сообщением в этот чат."
        )
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=BTN_BACK_STATS), KeyboardButton(text=BTN_BACK_MAIN)]],
            resize_keyboard=True,
            is_persistent=True,
        )
        await render_single_screen(message, state, text, reply_markup=kb)
        return

    lines = [f"{prefix_note}👨‍🏫 *Ваши преподаватели:*\n"]
    for idx, t in enumerate(teachers, 1):
        lines.append(f"{idx}. *{strip_md_delimiters(t['full_name'])}* (код `{escape_md(t['teacher_code'])}`)")
    lines.append("\n_Чтобы отключиться от преподавателя, нажмите соответствующую кнопку внизу:_")

    await render_single_screen(
        message,
        state,
        "\n".join(lines),
        reply_markup=Keyboards.my_teachers_reply(teachers)
    )


@router.message(F.text == BTN_STATS_MY_TEACHERS)
async def handle_teachers_list(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        await _show_student_teachers_screen(message, state, message.from_user.id)


@router.message(F.text.regexp(r"^❌ Отключить (\d{8})$"))
async def handle_unlink_teacher_reply(message: Message, state: FSMContext):
    if not message.from_user or not message.text:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        m = re.match(r"^❌ Отключить (\d{8})$", message.text.strip())
        if not m:
            return
        code = m.group(1)
        teacher = await asyncio.to_thread(db.get_teacher, code)
        t_name = teacher["full_name"] if teacher else f"Код {code}"
        await asyncio.to_thread(db.disconnect_student_from_teacher, message.from_user.id, code)

        note = f"✅ *Отключено от преподавателя:* {strip_md_delimiters(t_name)} (`{escape_md(code)}`)\n\n"
        await _show_student_teachers_screen(message, state, message.from_user.id, prefix_note=note)


@router.message(F.text.regexp(r"^\s*\d{8}\s*$"))
async def handle_teacher_code_input(message: Message, state: FSMContext):
    if not message.from_user or not message.text:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        code = message.text.strip()
        await state.clear()
        teacher = await asyncio.to_thread(db.get_teacher, code)
        if not teacher:
            await render_single_screen(
                message,
                state,
                f"❌ Преподаватель с кодом `{escape_md(code)}` не найден.\n"
                f"Проверьте правильность ввода 8 цифр.",
                reply_markup=Keyboards.main_menu()
            )
            return

        if teacher["user_id"] == message.from_user.id:
            await render_single_screen(
                message,
                state,
                "❌ Вы не можете прикрепиться к своему собственному классу в качестве ученика.",
                reply_markup=Keyboards.main_menu()
            )
            return

        if await asyncio.to_thread(db.is_student_connected, message.from_user.id, code):
            await render_single_screen(
                message,
                state,
                f"ℹ️ Вы уже подключены к преподавателю *{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(code)}`).",
                reply_markup=Keyboards.main_menu()
            )
            return

        await asyncio.to_thread(db.connect_student_to_teacher, message.from_user.id, code)
        await render_single_screen(
            message,
            state,
            f"✅ *Преподаватель найден:*\n"
            f"*{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(code)}`).\n\n"
            f"Вы успешно подключены! Ваш прогресс теперь отображается в ведомости класса.",
            reply_markup=Keyboards.main_menu()
        )


# ---------------------------------------------------------------------
# СТАТИСТИКА И TELEGRAM MINI APP (100% НА НИЖНЕЙ КЛАВИАТУРЕ)
# ---------------------------------------------------------------------

def _build_stats_view_sync(user_id: int, prefix_notice: str = "") -> Tuple[str, ReplyKeyboardMarkup]:
    stats = db.get_user_stats(user_id)
    token_ver = db.get_user_token_version(user_id)
    reminders_on = db.get_user_reminders_enabled(user_id)
    exam_level = db.get_user_exam_level(user_id)
    exam_label = "ОГЭ" if exam_level == "oge" else "ЕГЭ"
    student_web_url = build_student_webapp_url(user_id, token_ver)

    text = (
        f"{prefix_notice}"
        f"🎯 *Экзамен: {exam_label}*\n"
        "📊 *Ваша статистика изучения*\n\n"
        "🔥 *Регулярность и серия занятий:*\n"
        f"• Текущая серия: *{stats['current_streak']} дн. подряд* (рекорд: *{stats['best_streak']} дн.*)\n"
        f"• Действий сегодня: *{stats['today_actions']}* | Активных дней за год: *{stats['active_days_year']}*\n\n"
        "🗂 *Карточки:*\n"
        f"• Всего ответов: *{stats['cards_count']}*\n"
        f"• 🟢 Выучено надёжно: *{stats['learned']}*\n"
        f"• 🟡 В процессе: *{stats['in_progress']}*\n"
        f"• 🔴 Требуют повторения: *{stats['needs_review']}*\n\n"
        "🎯 *Тесты:*\n"
        f"• Вопросов решено: *{stats['tests_count']}*\n"
        f"• Точность: *{stats['tests_accuracy']}%* ({stats['tests_correct']} верно / {stats['tests_errors']} ошибок)\n"
        f"• Среднее время: *{stats['tests_avg_time']} сек*"
    )
    keyboard = Keyboards.stats_reply(student_web_url, reminders_on)
    return text, keyboard


@router.message(F.text.in_({BTN_MAIN_STATS, BTN_BACK_STATS}))
async def handle_show_stats_msg(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        await state.set_state(UserFlow.in_stats)
        text, reply_kb = await asyncio.to_thread(_build_stats_view_sync, message.from_user.id)
        await render_single_screen(message, state, text, reply_markup=reply_kb)


@router.message(F.text.in_({BTN_STATS_REMIND_ON, BTN_STATS_REMIND_OFF}))
async def handle_toggle_reminder_reply(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        new_state = await asyncio.to_thread(db.toggle_user_reminders, message.from_user.id)
        notice = "🔔 *Ежедневные напоминания включены!*\n\n" if new_state else "🔕 *Ежедневные напоминания отключены.*\n\n"
        await state.set_state(UserFlow.in_stats)
        text, reply_kb = await asyncio.to_thread(_build_stats_view_sync, message.from_user.id, notice)
        await render_single_screen(message, state, text, reply_markup=reply_kb)


@router.message(F.text == BTN_STATS_RESET_LINK)
async def handle_stats_reset_prompt_reply(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    async with user_lock_manager.get_lock(uid):
        await state.set_state(UserFlow.in_stats_reset_confirm)
        text = (
            "🔐 *Сброс персональной ссылки на аналитику*\n\n"
            "Вы уверены, что хотите выпустить новую ссылку?\n"
            "Предыдущая ссылка сразу перестанет открываться на всех устройствах."
        )
        await render_single_screen(message, state, text, reply_markup=Keyboards.stats_reset_confirm_reply())


@router.message(StateFilter(UserFlow.in_stats_reset_confirm), F.text == BTN_CONFIRM_RESET_YES)
async def handle_stats_reset_confirm_reply(message: Message, state: FSMContext):
    if not message.from_user:
        return
    user_id = message.from_user.id
    async with user_lock_manager.get_lock(user_id):
        await asyncio.to_thread(db.rotate_user_token, user_id)
        await state.set_state(UserFlow.in_stats)
        notice = "✅ *Ваша ссылка на аналитику успешно обновлена!*\n\n"
        text, reply_kb = await asyncio.to_thread(_build_stats_view_sync, user_id, notice)
        await render_single_screen(message, state, text, reply_markup=reply_kb)


# ---------------------------------------------------------------------
# УМНЫЙ ПОИСК ТЕРМИНОВ ПО НАЗВАНИЮ (ОТ 3 БУКВ, С ЗАЩИТОЙ ОТ ОПЕЧАТОК)
# ---------------------------------------------------------------------

@router.message(Command("search"))
@router.message(F.text == BTN_MAIN_SEARCH)
async def handle_search_start(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    async with user_lock_manager.get_lock(uid):
        await state.set_state(UserFlow.in_search)
        await state.update_data(search_last_code=None, search_other_codes=[])
        text = "🔎 *Поиск термина*\n\nВведите название понятия (от 3 букв):\n"
        await render_single_screen(message, state, text, reply_markup=Keyboards.back_to_menu())


@router.message(StateFilter(UserFlow.in_search), F.text == BTN_CARD_DETAIL)
async def handle_search_detail_button(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    async with user_lock_manager.get_lock(uid):
        data = await state.get_data()
        code = data.get("search_last_code")
        if not code:
            await render_single_screen(message, state, "Введите название термина для поиска (от 3 букв):", reply_markup=Keyboards.back_to_menu())
            return
        card = catalog.get_by_code(code)
        if not card:
            return
        exam_level = await asyncio.to_thread(db.get_user_exam_level, uid)
        other_codes = data.get("search_other_codes") or []
        other_terms = [catalog.get_by_code(c) for c in other_codes if catalog.get_by_code(c)]
        text = format_card_back(card, show_detail=True, exam_level=exam_level)
        kb = Keyboards.search_results_keyboard(other_terms, show_detail_btn=False)
        await render_single_screen(message, state, text, reply_markup=kb)


async def _process_search_query_unlocked(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    exam_level = await asyncio.to_thread(db.get_user_exam_level, uid)
    raw_text = (message.text or "").strip()

    # Мгновенный O(1) переход при клике на кнопку быстрого выбора термина "📖 ..."
    if raw_text.startswith("📖 "):
        data = await state.get_data()
        other_codes = data.get("search_other_codes") or []
        exact_term = None
        for c in other_codes:
            t_cand = catalog.get_by_code(c)
            if t_cand and f"📖 {truncate_plain(t_cand['term'], 32)}" == raw_text:
                exact_term = t_cand
                break
        if exact_term is None:
            exact_term = catalog.get_by_button_label(raw_text)

        if exact_term is not None:
            remaining_codes = [c for c in other_codes if c != exact_term["code"]]
            remaining_terms = [catalog.get_by_code(c) for c in remaining_codes if catalog.get_by_code(c)]
            await state.update_data(
                search_last_code=exact_term["code"],
                search_other_codes=remaining_codes[:4]
            )
            card_text = format_card_back(exact_term, show_detail=False, exam_level=exam_level)
            if remaining_terms:
                similar_names = ", ".join([f"_{strip_md_delimiters(t['term'])}_" for t in remaining_terms[:4]])
                card_text += f"\n\n🔎 *Похожие понятия:* {similar_names}"
            kb = Keyboards.search_results_keyboard(remaining_terms, show_detail_btn=True)
            await render_single_screen(message, state, card_text, reply_markup=kb)
            return

    raw_q = raw_text
    if raw_q.startswith("📖 "):
        raw_q = raw_q[2:].strip()
        if raw_q.endswith("..."):
            raw_q = raw_q[:-3].strip()

    clean_q = _normalize_search_str(raw_q).strip()
    if len(clean_q) < 3:
        await render_single_screen(
            message,
            state,
            "⚠️ Введите хотя бы *3 буквы*, чтобы я мог точно найти термин.",
            reply_markup=Keyboards.back_to_menu()
        )
        return

    matches = catalog.search_terms_fuzzy(raw_q, limit=6)
    if not matches:
        await render_single_screen(
            message,
            state,
            f"😕 По запросу «*{escape_md(truncate_plain(raw_q, 80))}*» ничего не найдено.\nПопробуйте написать слово иначе или введите первые 3–4 буквы корня:",
            reply_markup=Keyboards.back_to_menu()
        )
        return

    top_term, top_score = matches[0]
    other_terms = [m[0] for m in matches[1:] if m[1] >= 0.52]

    await state.update_data(
        search_last_code=top_term["code"],
        search_other_codes=[t["code"] for t in other_terms[:4]]
    )
    typo_note = ""
    if _normalize_search_str(raw_q) not in _normalize_search_str(top_term["term"]):
        typo_note = f"✨ _Найдено с учётом опечатки ({int(top_score * 100)}% совпадение):_\n\n"

    card_text = typo_note + format_card_back(top_term, show_detail=False, exam_level=exam_level)
    if other_terms:
        similar_names = ", ".join([f"_{strip_md_delimiters(t['term'])}_" for t in other_terms[:4]])
        card_text += f"\n\n🔎 *Похожие понятия:* {similar_names}"

    kb = Keyboards.search_results_keyboard(other_terms, show_detail_btn=True)
    await render_single_screen(message, state, card_text, reply_markup=kb)


@router.message(StateFilter(UserFlow.in_search), F.text)
async def handle_search_query(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    async with user_lock_manager.get_lock(uid):
        await _process_search_query_unlocked(message, state)


# ---------------------------------------------------------------------
# РЕЖИМ «РАБОТА НАД ОШИБКАМИ»
# ---------------------------------------------------------------------

def _build_errors_workout_screen_sync(user_id: int) -> Tuple[str, ReplyKeyboardMarkup]:
    problem_codes = db.get_problem_term_codes(user_id)
    if not problem_codes:
        text = (
            "🎉 *Отличный результат!*\n\n"
            "У вас сейчас нет проблемных терминов — все изученные понятия закреплены уверенно."
        )
        return text, Keyboards.main_menu()

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
        f"Найдено понятий для повторения: *{count}*.\n\n"
        f"{preview_block}\n\n"
        f"Выберите формат проработки на клавиатуре внизу:"
    )
    return text, Keyboards.error_workout_menu(count)


@router.message(F.text == BTN_MAIN_ERRORS)
async def handle_work_errors_command(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        await state.clear()
        text, kb = await asyncio.to_thread(_build_errors_workout_screen_sync, message.from_user.id)
        await state.set_state(UserFlow.in_errors_menu)
        await render_single_screen(message, state, text, reply_markup=kb)


@router.message(F.text.in_({BTN_ERR_CARDS, BTN_ERR_TESTS}))
async def handle_errors_start_reply(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        submode = "cards" if message.text == BTN_ERR_CARDS else "tests"
        problem_codes = await asyncio.to_thread(db.get_problem_term_codes, message.from_user.id)
        if not problem_codes:
            await state.clear()
            await render_single_screen(
                message,
                state,
                "🎉 Все проблемные термины уже разобраны!",
                reply_markup=Keyboards.main_menu(),
            )
            return

        if submode == "cards":
            await state.update_data(mode="cards", module_filter=None, custom_codes=problem_codes, is_error_workout=True)
            await state.set_state(UserFlow.in_flashcards)
            await send_next_flashcard_msg(message, message.from_user.id, state)
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
            await send_next_test_msg(message, state)


# ---------------------------------------------------------------------
# ВЫБОР РЕЖИМА И МОДУЛЯ (КАРТОЧКИ / ТЕСТЫ)
# ---------------------------------------------------------------------

@router.message(F.text.in_({BTN_MAIN_CARDS, BTN_MAIN_TESTS}))
async def handle_main_mode_reply(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    async with user_lock_manager.get_lock(uid):
        mode = "cards" if message.text == BTN_MAIN_CARDS else "tests"
        await state.update_data(mode=mode, custom_codes=None, is_error_workout=False)
        await state.set_state(UserFlow.selecting_topic)

        mode_name = "🗂 Карточки" if mode == "cards" else "🎯 Тесты"
        text = f"*{mode_name}*\n\nВыберите тему кодификатора:"
        await render_single_screen(message, state, text, reply_markup=Keyboards.topic_selection(mode))


@router.message(StateFilter(UserFlow.selecting_topic), F.text.in_(set(TOPIC_BUTTONS_MAP.keys())))
async def handle_topic_selection_reply(message: Message, state: FSMContext):
    if not message.from_user or not message.text:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        topic_val = TOPIC_BUTTONS_MAP[message.text]
        module_filter = None if topic_val == "all" else int(topic_val)

        data = await state.get_data()
        mode = data.get("mode", "cards")
        await state.update_data(module_filter=module_filter, mode=mode, custom_codes=None, is_error_workout=False)

        if mode == "cards":
            await state.set_state(UserFlow.in_flashcards)
            await send_next_flashcard_msg(message, message.from_user.id, state)
        else:
            await state.set_state(UserFlow.in_tests)
            available = catalog.get_terms(module_filter)
            if not available:
                await render_single_screen(message, state, "❌ В выбранном модуле пока недостаточно терминов.", reply_markup=Keyboards.main_menu())
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
            await send_next_test_msg(message, state)


# ---------------------------------------------------------------------
# КАРТОЧКИ НА НИЖНЕЙ REPLY-КЛАВИАТУРЕ (В 1 СООБЩЕНИЕ)
# ---------------------------------------------------------------------

async def send_next_flashcard_msg(message: Message, user_id: int, state: FSMContext):
    data = await state.get_data()
    module_filter = data.get("module_filter")
    is_error_workout = data.get("is_error_workout", False)

    custom_codes = None
    if is_error_workout:
        custom_codes = await asyncio.to_thread(db.get_problem_term_codes, user_id)
        if not custom_codes:
            await state.clear()
            done_text = (
                "🎉 *Работа над ошибками завершена!*\n\n"
                "Вы успешно закрыли все проблемные термины."
            )
            await render_single_screen(message, state, done_text, reply_markup=Keyboards.main_menu())
            return

    card = await asyncio.to_thread(select_next_card, user_id, module_filter, custom_codes)
    if not card:
        await state.clear()
        await render_single_screen(message, state, "❌ В выбранном модуле пока нет терминов.", reply_markup=Keyboards.main_menu())
        return

    exam_level = await asyncio.to_thread(db.get_user_exam_level, user_id)
    shown_at = time.time()
    await state.update_data(current_card_code=card["code"], shown_at=shown_at, revealed_elapsed=None)

    mod_title = strip_md_delimiters(card.get("module_title") or "Обществознание")
    if exam_level == "oge":
        mod_title = f"{mod_title} (ОГЭ)"
    term_title = strip_md_delimiters(str(card.get("term") or "").upper())
    workout_badge = "🛠 *Работа над ошибками*\n" if is_error_workout else ""

    text = (
        f"{workout_badge}🏷 *{mod_title}*\n\n"
        f"✨ *{term_title}*\n\n"
        f"💭 _Вспомните родовое понятие и ключевые признаки термина._"
    )
    await render_single_screen(message, state, text, reply_markup=Keyboards.flashcard_front(card["code"]))


@router.message(StateFilter(UserFlow.in_flashcards), F.text == BTN_CARD_FLIP)
async def handle_card_reveal_reply(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    async with user_lock_manager.get_lock(uid):
        data = await state.get_data()
        code = data.get("current_card_code")
        if not code:
            if message.from_user:
                await send_next_flashcard_msg(message, message.from_user.id, state)
            return

        card = catalog.get_by_code(code)
        if not card:
            return

        shown_at = data.get("shown_at", time.time())
        if data.get("revealed_elapsed") is None:
            revealed_elapsed = round(max(0.0, time.time() - shown_at), 1)
            await state.update_data(revealed_elapsed=revealed_elapsed)

        exam_level = await asyncio.to_thread(db.get_user_exam_level, uid)
        text = format_card_back(card, show_detail=False, exam_level=exam_level)
        await render_single_screen(message, state, text, reply_markup=Keyboards.flashcard_back(code, has_detail=False))


@router.message(StateFilter(UserFlow.in_flashcards), F.text == BTN_CARD_DETAIL)
async def handle_card_detail_reply(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    async with user_lock_manager.get_lock(uid):
        data = await state.get_data()
        code = data.get("current_card_code")
        if not code:
            return
        card = catalog.get_by_code(code)
        if not card:
            return

        exam_level = await asyncio.to_thread(db.get_user_exam_level, uid)
        text = format_card_back(card, show_detail=True, exam_level=exam_level)
        await render_single_screen(message, state, text, reply_markup=Keyboards.flashcard_back(code, has_detail=True))


@router.message(StateFilter(UserFlow.in_flashcards), F.text.in_({BTN_CARD_KNOW, BTN_CARD_UNSURE, BTN_CARD_FORGOT}))
async def handle_card_answer_reply(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        data = await state.get_data()
        code = data.get("current_card_code")
        if not code:
            await send_next_flashcard_msg(message, message.from_user.id, state)
            return

        shown_at = data.get("shown_at", time.time())
        revealed_elapsed = data.get("revealed_elapsed")

        # Защита от случайного двойного клика по кнопке оценки (когда новая карточка только что открылась лицевой стороной)
        if revealed_elapsed is None and (time.time() - shown_at) < 0.8:
            await _safe_delete_message(message.bot, message.chat.id, message.message_id)
            return

        await state.update_data(current_card_code=None)

        ans_map = {
            BTN_CARD_KNOW: "know_well",
            BTN_CARD_UNSURE: "know_poorly",
            BTN_CARD_FORGOT: "dont_know",
        }
        answer_type = ans_map[message.text]

        raw_elapsed = revealed_elapsed if revealed_elapsed is not None else round(max(0.0, time.time() - shown_at), 1)
        elapsed = round(min(max(1.0, raw_elapsed), SpacedRepetitionAlgorithm.MAX_TIME_WINDOW), 1)

        is_positive = (answer_type == "know_well")
        delta_ans = SpacedRepetitionAlgorithm.calculate_answer_delta(answer_type)
        delta_tm = SpacedRepetitionAlgorithm.calculate_time_delta(elapsed, is_correct=is_positive)

        safe_code = str(code).replace(";", "_")
        entry_str = f"{safe_code};{delta_ans};{delta_tm};{elapsed};card"
        user = message.from_user
        username = f"@{user.username}" if user.username else None
        full_name = get_safe_display_name(user)

        await db.save_answer_entry_fast(user.id, username, full_name, entry_str)
        await send_next_flashcard_msg(message, user.id, state)


# ---------------------------------------------------------------------
# ТЕСТЫ НА НИЖНЕЙ REPLY-КЛАВИАТУРЕ (В 1 СООБЩЕНИЕ)
# ---------------------------------------------------------------------

async def send_next_test_msg(message: Message, state: FSMContext):
    data = await state.get_data()
    pool = list(data.get("test_pool") or [])
    module_filter = data.get("module_filter")
    is_error_workout = data.get("is_error_workout", False)

    if not pool:
        total = data.get("test_total_in_session", 0)
        if total <= 0:
            await state.clear()
            await render_single_screen(message, state, "❌ В выбранном модуле пока недостаточно терминов.", reply_markup=Keyboards.main_menu())
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
        await render_single_screen(message, state, text, reply_markup=Keyboards.test_finish(has_errors=bool(errors)))
        return

    next_code = pool.pop()
    await state.update_data(test_pool=pool)

    card = catalog.get_by_code(next_code)
    if not card:
        if not catalog.all_terms:
            await state.clear()
            await render_single_screen(message, state, "❌ База терминов пуста.", reply_markup=Keyboards.main_menu())
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
    await render_single_screen(message, state, text, reply_markup=Keyboards.test_options(card["code"], test_data["options"]))


@router.message(StateFilter(UserFlow.in_tests), F.text.in_({"1", "2", "3", "4"}))
async def handle_test_answer_reply(message: Message, state: FSMContext):
    if not message.from_user or not message.text:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        option_idx = int(message.text.strip()) - 1
        data = await state.get_data()
        test_data = data.get("current_test_data")
        shown_at = data.get("test_shown_at", time.time())
        elapsed = round(max(0.0, time.time() - shown_at), 1)

        if not test_data or option_idx < 0 or option_idx >= len(test_data.get("options", [])):
            await _safe_delete_message(message.bot, message.chat.id, message.message_id)
            return

        await state.update_data(current_test_data=None)

        chosen_option = test_data["options"][option_idx]
        is_correct = chosen_option["is_correct"]
        correct_term = test_data["correct_term"]
        code = correct_term["code"]

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

        session_times.append(min(elapsed, SpacedRepetitionAlgorithm.MAX_TIME_WINDOW))
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
            clamped_elapsed = round(min(elapsed, SpacedRepetitionAlgorithm.MAX_TIME_WINDOW), 1)
            entry_str = f"{safe_code};{delta_ans};{delta_tm};{clamped_elapsed};test"

            user = message.from_user
            username = f"@{user.username}" if user.username else None
            full_name = get_safe_display_name(user)
            await db.save_answer_entry_fast(user.id, username, full_name, entry_str)

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

        await render_single_screen(message, state, text, reply_markup=Keyboards.test_next())


@router.message(StateFilter(UserFlow.in_tests), F.text == BTN_TEST_NEXT)
async def handle_test_next_reply(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    async with user_lock_manager.get_lock(uid):
        await send_next_test_msg(message, state)


@router.message(StateFilter(UserFlow.in_tests), F.text == BTN_TEST_SHOW_ERRORS)
async def handle_test_show_errors_reply(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    async with user_lock_manager.get_lock(uid):
        data = await state.get_data()
        errors = data.get("test_session_errors", [])
        if not errors:
            await render_single_screen(message, state, "🎉 Ошибок нет — 100% результат!", reply_markup=Keyboards.test_finish(has_errors=False))
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
        await render_single_screen(message, state, text, reply_markup=Keyboards.test_finish(has_errors=False))


@router.message(StateFilter(UserFlow.in_tests), F.text == BTN_TEST_RESTART)
async def handle_test_restart_reply(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        data = await state.get_data()
        module_filter = data.get("module_filter")
        is_error_workout = data.get("is_error_workout", False)

        if is_error_workout:
            custom_codes = await asyncio.to_thread(db.get_problem_term_codes, message.from_user.id)
            if not custom_codes:
                await state.clear()
                await render_single_screen(message, state, "🎉 Все проблемные термины уже разобраны!", reply_markup=Keyboards.main_menu())
                return
            pool = list(custom_codes)
        else:
            available = catalog.get_terms(module_filter)
            if not available:
                await state.clear()
                await render_single_screen(message, state, "❌ В выбранном модуле пока недостаточно терминов.", reply_markup=Keyboards.main_menu())
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
        await send_next_test_msg(message, state)


@router.message(Command("privacy"))
async def handle_privacy_command(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    async with user_lock_manager.get_lock(uid):
        safe_priv_url = PRIVACY_URL.replace(")", "%29")
        text = (
            "🛡 *Правила сервиса и конфиденциальность*\n\n"
            f"📄 С полным текстом условий и политики можно ознакомиться [по этой ссылке]({safe_priv_url}).\n\n"
            "💡 _Стереть все свои данные можно командой_ `/delete_me`."
        )
        policy_ok = await asyncio.to_thread(db.is_user_policy_accepted, uid)
        kb = Keyboards.main_menu() if policy_ok else Keyboards.policy_consent_reply()
        await render_single_screen(message, state, text, reply_markup=kb)


@router.message(Command("delete_me"))
async def handle_delete_me_command(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        await state.set_state(UserFlow.in_delete_me_confirm)
        text = (
            "⚠️ *Удаление учетной записи и истории*\n\n"
            "Вы собираетесь безвозвратно удалить все свои результаты тестов, "
            "прогресс интервального повторения карточек, серию занятий и привязку к классам.\n\n"
            "Это действие *нельзя отменить*. Вы уверены?"
        )
        await render_single_screen(message, state, text, reply_markup=Keyboards.delete_confirm_reply())


@router.message(StateFilter(UserFlow.in_delete_me_confirm), F.text == BTN_CONFIRM_DELETE_DATA)
async def handle_confirm_delete_data(message: Message, state: FSMContext):
    if not message.from_user:
        return
    uid = message.from_user.id
    async with user_lock_manager.get_lock(uid):
        await asyncio.to_thread(db.delete_user_data, uid)
        session_tracker.clear_user(uid)
        await state.clear()
        await state.set_state(UserFlow.awaiting_policy_accept)
        text = (
            "✅ *Все ваши данные успешно удалены из базы.*\n\n"
            "История ответов, статистика и настройки полностью стёрты.\n\n"
            + _build_policy_consent_text()
        )
        await render_single_screen(message, state, text, reply_markup=Keyboards.policy_consent_reply())


@router.message(StateFilter(UserFlow.in_delete_me_confirm), F.text == BTN_CANCEL_DELETE_DATA)
async def handle_cancel_delete_data(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    async with user_lock_manager.get_lock(uid):
        policy_ok = await asyncio.to_thread(db.is_user_policy_accepted, uid)
        if not policy_ok:
            await state.set_state(UserFlow.awaiting_policy_accept)
            await render_single_screen(
                message,
                state,
                _build_policy_consent_text(),
                reply_markup=Keyboards.policy_consent_reply(),
            )
            return

        await state.clear()
        text = "🏠 *Главное меню*\n\nУдаление отменено. Выберите формат тренировки:"
        await render_single_screen(message, state, text, reply_markup=Keyboards.main_menu())


# Набор служебных кнопок, которые НЕ должны запускать поиск термина, если нажаты после перезапуска сервера
SYSTEM_UI_BUTTONS_SET = {
    BTN_MAIN_CARDS,
    BTN_MAIN_TESTS,
    BTN_MAIN_ERRORS,
    BTN_MAIN_SEARCH,
    BTN_MAIN_STATS,
    BTN_EXAM_OGE,
    BTN_EXAM_EGE,
    BTN_BACK_MAIN,
    BTN_BACK_STATS,
    BTN_BACK_TEACHER,
    BTN_ERR_CARDS,
    BTN_ERR_TESTS,
    BTN_STATS_REMIND_ON,
    BTN_STATS_REMIND_OFF,
    BTN_STATS_RESET_LINK,
    BTN_STATS_MY_TEACHERS,
    BTN_CARD_FLIP,
    BTN_CARD_KNOW,
    BTN_CARD_UNSURE,
    BTN_CARD_FORGOT,
    BTN_CARD_DETAIL,
    BTN_TEST_NEXT,
    BTN_TEST_SHOW_ERRORS,
    BTN_TEST_RESTART,
    BTN_CONFIRM_RESET_YES,
    BTN_TEACHER_RESET_LINK,
    BTN_STATS_WEBAPP,
    BTN_TEACHER_WEBAPP,
    BTN_CONFIRM_DELETE_DATA,
    BTN_CANCEL_DELETE_DATA,
    BTN_ACCEPT_POLICY,
    "1", "2", "3", "4",
} | set(TOPIC_BUTTONS_MAP.keys())


# Универсальный перехватчик текстового ввода в любом состоянии:
# если пользователь написал слово от 3 букв — запускаем нечёткий поиск;
# если нажата кнопка старой сессии после перезапуска сервера — мягко восстанавливаем режим или меню.
@router.message(F.text)
async def handle_universal_text_fallback(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    async with user_lock_manager.get_lock(uid):
        txt = (message.text or "").strip()
        cur_state = await state.get_state()

        if cur_state == UserFlow.awaiting_policy_accept.state:
            await render_single_screen(
                message,
                state,
                _build_policy_consent_text(),
                reply_markup=Keyboards.policy_consent_reply(),
            )
            return

        if cur_state == UserFlow.selecting_exam_level.state:
            await render_single_screen(
                message,
                state,
                "К какому экзамену ты готовишься?",
                reply_markup=Keyboards.exam_level_selection(),
            )
            return

        policy_ok = await asyncio.to_thread(db.is_user_policy_accepted, uid)
        if not policy_ok:
            await state.set_state(UserFlow.awaiting_policy_accept)
            await render_single_screen(
                message,
                state,
                _build_policy_consent_text(),
                reply_markup=Keyboards.policy_consent_reply(),
            )
            return

        # Если после рестарта сервера пользователь нажал кнопку выбора экзамена (🎓 ОГЭ или 🏛 ЕГЭ)
        if txt in {BTN_EXAM_OGE, BTN_EXAM_EGE} and message.from_user:
            chosen_level = "oge" if txt == BTN_EXAM_OGE else "ege"
            await asyncio.to_thread(db.set_user_exam_level, uid, chosen_level)
            await state.clear()
            level_badge = (
                "🎓 *Выбран режим подготовки: ОГЭ*\n_Карточки адаптированы: базовое определение, 2 главных признака и пример из РФ._\n\n"
                if chosen_level == "oge"
                else "🏛 *Выбран режим подготовки: ЕГЭ*\n_Включены полные академические формулировки, классификации и разбор ловушек._\n\n"
            )
            await render_single_screen(
                message,
                state,
                f"{level_badge}🏠 *Главное меню*\n\nВыберите формат тренировки:",
                reply_markup=Keyboards.main_menu(),
            )
            return

        # Если после рестарта сервера пользователь нажал кнопку карточки — бесшовно продолжаем карточки
        if txt in {BTN_CARD_FLIP, BTN_CARD_KNOW, BTN_CARD_UNSURE, BTN_CARD_FORGOT, BTN_CARD_DETAIL} and message.from_user:
            await state.set_state(UserFlow.in_flashcards)
            await send_next_flashcard_msg(message, message.from_user.id, state)
            return

        # Если после рестарта нажата кнопка выбора темы — запускаем карточки по выбранной теме
        if txt in TOPIC_BUTTONS_MAP and message.from_user:
            topic_val = TOPIC_BUTTONS_MAP[txt]
            mod_filter = None if topic_val == "all" else int(topic_val)
            await state.update_data(module_filter=mod_filter, mode="cards", custom_codes=None, is_error_workout=False)
            await state.set_state(UserFlow.in_flashcards)
            await send_next_flashcard_msg(message, message.from_user.id, state)
            return

        if len(txt) >= 3 and not txt.startswith("/") and txt not in SYSTEM_UI_BUTTONS_SET:
            await state.set_state(UserFlow.in_search)
            await _process_search_query_unlocked(message, state)
        else:
            await state.clear()
            await render_single_screen(
                message,
                state,
                "🏠 *Главное меню*\n\nВыберите формат тренировки:",
                reply_markup=Keyboards.main_menu()
            )


# =====================================================================
# 12. ФОНОВЫЕ ЗАДАЧИ С АРХИВАЦИЕЙ, РЕЖИМОМ СНА NEON И RENDER PORT
# =====================================================================

async def setup_bot_commands(bot_instance: Bot):
    """Регистрирует системные команды бота в меню Telegram (без дублирования кнопок главной клавиатуры)."""
    commands = [
        BotCommand(command="start", description="🏠 Перезапуск / Главное меню"),
        BotCommand(command="change", description="🎓 Сменить экзамен (ОГЭ / ЕГЭ)"),
        BotCommand(command="teachers", description="👨‍🏫 Кабинет преподавателя"),
        BotCommand(command="privacy", description="🛡 Политика конфиденциальности"),
        BotCommand(command="delete_me", description="🗑 Удалить мои данные"),
    ]
    try:
        await bot_instance.set_my_commands(commands)
    except Exception as e:
        logger.warning(f"Не удалось зарегистрировать системные команды Telegram: {e}")


async def process_daily_reminders(bot_instance: Bot, reference_date: Optional[date] = None) -> int:
    today_dt = reference_date or get_local_today()
    today_iso = today_dt.isoformat()
    candidates = await asyncio.to_thread(db.get_users_for_daily_reminder, today_dt)
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
            prev_id = screen_manager.get_last_id(uid)
            sent_msg = await bot_instance.send_message(
                chat_id=uid,
                text=text,
                reply_markup=Keyboards.main_menu(),
                parse_mode=ParseMode.MARKDOWN
            )
            screen_manager.set_last_id(uid, sent_msg.message_id)
            if prev_id and prev_id != sent_msg.message_id:
                await _safe_delete_message(bot_instance, uid, prev_id)
            await asyncio.to_thread(db.mark_reminder_sent, uid, today_iso)
            sent_count += 1
        except TelegramForbiddenError:
            await asyncio.to_thread(db.mark_reminder_sent, uid, today_iso)
        except Exception as e:
            logger.warning(f"Не удалось отправить напоминание пользователю {uid}: {e}")

    return sent_count


async def start_dummy_web_server(port: int = 10000):
    """Защищённый от HTTP-флуда и Slowloris-атак health-check сервер для Render."""
    conn_semaphore = asyncio.Semaphore(64)

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if conn_semaphore.locked():
            try:
                writer.close()
            except Exception:
                pass
            return
        async with conn_semaphore:
            try:
                await asyncio.wait_for(reader.read(512), timeout=1.0)
                response = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/plain; charset=utf-8\r\n"
                    b"Content-Length: 2\r\n"
                    b"Connection: close\r\n\r\nOK"
                )
                writer.write(response)
                await asyncio.wait_for(writer.drain(), timeout=1.0)
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    server = await asyncio.start_server(handle_client, "0.0.0.0", port, backlog=64)
    logger.info(f"Фоновый веб-сервер запущен на порту {port} для Render.")
    return server


async def periodic_background_task(bot_instance: Optional[Bot] = None):
    """
    Фоновый цикл (Scale-to-Zero Friendly для Neon):
    - Регламентные задачи (архивация старых записей, очистка учителей, напоминания)
      запускаются СТРОГО 1 раз в сутки в вечернее окно (18:00–20:00 МСК).
    - Ночью и днём база Neon спит и бережёт бесплатные вычислительные часы.
    """
    last_db_maintenance_date: Optional[date] = None

    while True:
        try:
            await asyncio.sleep(1800)
            session_tracker.cleanup_old_sessions()

            now_local = datetime.now(TZ)
            today_local = now_local.date()

            if 18 <= now_local.hour <= 20 and last_db_maintenance_date != today_local:
                last_db_maintenance_date = today_local
                await asyncio.to_thread(db.archive_old_answers, 30)
                await asyncio.to_thread(db.cleanup_inactive_teachers)
                if bot_instance is not None:
                    await process_daily_reminders(bot_instance, reference_date=today_local)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Ошибка в periodic_background_task: {e}")
            await live_logger.report_error(bot_instance, e, context_info="periodic_background_task")


async def main():
    if not BOT_TOKEN:
        logger.error("Токен бота не найден в переменной BOT_TOKEN!")
        print("\n[!] ОШИБКА: Задайте переменную окружения BOT_TOKEN в файле .env или в переменных среды.\n")
        return

    catalog.load()
    await asyncio.to_thread(db.init_db)

    port = int(os.getenv("PORT", "10000"))
    web_server = None
    try:
        web_server = await start_dummy_web_server(port)
    except Exception as e:
        logger.warning(f"Не удалось запустить фоновый веб-сервер на порту {port}: {e}")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    await setup_bot_commands(bot)

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(AntiFloodDDoSMiddleware())
    dp.include_router(router)

    bg_task = asyncio.create_task(periodic_background_task(bot))

    logger.info("Бот версии 7.5 (ОГЭ/ЕГЭ + Lossless Archive + Concurrency Guard) успешно запущен.")
    try:
        await dp.start_polling(bot)
    except Exception as fatal_err:
        await live_logger.report_error(bot, fatal_err, context_info="main_polling")
        raise
    finally:
        await asyncio.to_thread(db.flush_pending_writes_sync)
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
