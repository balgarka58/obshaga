import os
import re
import time
import random
import hashlib
import logging
import asyncio
import traceback
from datetime import datetime, date
from typing import Optional, Dict, Any, Tuple
from collections import deque

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ErrorEvent,
    BotCommand,
)
from aiogram.client.default import DefaultBotProperties

# =====================================================================
# 1. ИМПОРТ МОДУЛЕЙ ЯДРА (v7.6 MODULAR HARDENED)
# =====================================================================

from core.common import (
    BOT_TOKEN,
    DATABASE_URL,
    DB_PATH,
    TERMS_JSON_PATH,
    DASHBOARD_URL,
    SECRET_KEY,
    TIMEZONE_STR,
    LOG_CHAT_ID,
    PRIVACY_URL,
    TERMS_URL,
    TZ,
    logger,
    get_local_today,
    normalize_neon_pooler_url,
    generate_student_token,
    generate_teacher_token,
    verify_token_constant_time,
    encode_user_id,
    decode_user_id,
    build_student_webapp_url,
    build_teacher_webapp_url,
    escape_md,
    strip_md_delimiters,
    truncate_plain,
    get_safe_display_name,
    _safe_json_loads,
    _normalize_search_str,
    _get_trigrams,
    _compute_similarity_preindexed,
    compute_fuzzy_similarity,
)
from core.learning import (
    SpacedRepetitionAlgorithm,
    TermsCatalog,
    catalog,
    SessionTracker,
    session_tracker,
    TestGenerator,
    format_card_back,
)
from core.keyboards import (
    UserFlow,
    Keyboards,
    TOPIC_BUTTONS_MAP,
    SYSTEM_UI_BUTTONS_SET,
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
    BTN_CARD_FLIP,
    BTN_CARD_KNOW,
    BTN_CARD_UNSURE,
    BTN_CARD_FORGOT,
    BTN_CARD_DETAIL,
    BTN_TEST_NEXT,
    BTN_TEST_SHOW_ERRORS,
    BTN_TEST_RESTART,
    BTN_ERR_CARDS,
    BTN_ERR_TESTS,
    BTN_STATS_WEBAPP,
    BTN_STATS_REMIND_ON,
    BTN_STATS_REMIND_OFF,
    BTN_STATS_RESET_LINK,
    BTN_STATS_MY_TEACHERS,
    BTN_CONFIRM_RESET_YES,
    BTN_TEACHER_WEBAPP,
    BTN_TEACHER_RESET_LINK,
    BTN_CONFIRM_DELETE_DATA,
    BTN_CANCEL_DELETE_DATA,
    BTN_ACCEPT_POLICY,
)
from core.database import Database, db, select_next_card

if not SECRET_KEY:
    raise RuntimeError("Переменная окружения SECRET_KEY не задана!")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)


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
    except TelegramRetryAfter as retry_err:
        try:
            await asyncio.sleep(min(float(retry_err.retry_after), 3.0))
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
    except Exception as e:
        err_msg = str(e).lower()
        if "retry after" in err_msg or "flood" in err_msg:
            try:
                await asyncio.sleep(1.2)
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass


class AntiFloodDDoSMiddleware(BaseMiddleware):
    """
    Защита бота от прикладного DDoS, спам-скриптов и флуда кнопками (Token Bucket):
    1) Мгновенно запускает фоновое удаление входящего сообщения пользователя
       и проставляет флаг _user_msg_deleted в data, чтобы render_single_screen не слал дубль.
    2) Ограничивает частоту запросов от одного пользователя (не более 4 в секунду
       и не более 16 за 10 секунд).
    """

    def __init__(
        self, max_per_sec: int = 4, max_per_10s: int = 16, cooldown_sec: float = 4.0
    ):
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

            if chat_id and msg_id and bot_inst:
                asyncio.create_task(_safe_delete_message(bot_inst, chat_id, msg_id))
                try:
                    setattr(event, "_user_msg_deleted", True)
                except Exception:
                    pass
                data["_user_msg_deleted"] = True

            uid = event.from_user.id if event.from_user else chat_id
            if uid is not None:
                now = time.monotonic()
                cd_until = self._cooldown_until.get(uid, 0.0)
                if now < cd_until:
                    return None

                hits = self._user_hits.get(uid)
                if hits is None:
                    if (
                        len(self._user_hits) >= self.max_per_10s
                        and len(self._user_hits) >= self._max_tracked
                    ):
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
                    logger.warning(
                        f"AntiFlood: пользователь {uid} временно ограничен на {self.cooldown_sec}с за спам."
                    )
                    return None

                hits.append(now)

        return await handler(event, data)


class PolicyConsentMiddleware(BaseMiddleware):
    """
    Закрывает обход согласия с политикой конфиденциальности:
    проверяет db.is_user_policy_accepted(uid) для всех входящих сообщений,
    кроме команд /start, /privacy, /delete_me и текста кнопки BTN_ACCEPT_POLICY
    (а также подтверждения/отмены удаления в состоянии in_delete_me_confirm).
    """

    async def __call__(self, handler, event: Any, data: Dict[str, Any]) -> Any:
        if isinstance(event, Message) and event.from_user:
            txt = (event.text or "").strip()
            cmd_token = (
                txt.split(maxsplit=1)[0].split("@", 1)[0].lower()
                if txt.startswith("/")
                else ""
            )
            if cmd_token in {"/start", "/privacy", "/delete_me"} or txt == BTN_ACCEPT_POLICY:
                return await handler(event, data)

            state: Optional[FSMContext] = data.get("state")
            if state is not None and txt in {BTN_CONFIRM_DELETE_DATA, BTN_CANCEL_DELETE_DATA}:
                cur_state = await state.get_state()
                if cur_state == UserFlow.in_delete_me_confirm.state:
                    return await handler(event, data)

            uid = event.from_user.id
            async with user_lock_manager.get_lock(uid):
                policy_ok = await asyncio.to_thread(db.is_user_policy_accepted, uid)
                if not policy_ok:
                    if state is not None:
                        await state.set_state(UserFlow.awaiting_policy_accept)
                        await render_single_screen(
                            event,
                            state,
                            _build_policy_consent_text(),
                            reply_markup=Keyboards.policy_consent_reply(),
                        )
                    else:
                        await event.answer(
                            _build_policy_consent_text(),
                            reply_markup=Keyboards.policy_consent_reply(),
                            parse_mode=ParseMode.MARKDOWN,
                            disable_web_page_preview=True,
                        )
                    return None

        return await handler(event, data)


class TeacherCodeBruteforceGuard:
    """
    Защита от перебора 8-значного кода преподавателя по user_id (5 ошибок -> 15 мин блокировки)
    и защита преподавателей от спама повторными заявками (кулдаун 5 мин на пару ученик+код).
    """

    def __init__(
        self,
        max_attempts: int = 5,
        block_seconds: int = 900,
        join_cooldown_seconds: int = 300,
        max_tracked: int = 10000,
    ):
        self.max_attempts = max_attempts
        self.block_seconds = block_seconds
        self.join_cooldown_seconds = join_cooldown_seconds
        self._max_tracked = max_tracked
        self._attempts: Dict[int, int] = {}
        self._blocked_until: Dict[int, float] = {}
        self._join_sent_until: Dict[Tuple[int, str], float] = {}

    def is_blocked(self, user_id: int) -> Tuple[bool, int]:
        now = time.monotonic()
        until = self._blocked_until.get(user_id, 0.0)
        if now < until:
            return True, int(until - now) + 1
        if user_id in self._blocked_until:
            self._blocked_until.pop(user_id, None)
        return False, 0

    def record_failure(self, user_id: int) -> Tuple[bool, int, int]:
        now = time.monotonic()
        if user_id not in self._attempts and len(self._attempts) >= self._max_tracked:
            evict_k = next(
                (k for k in self._attempts if self._blocked_until.get(k, 0.0) <= now),
                next(iter(self._attempts)),
            )
            self._attempts.pop(evict_k, None)
            self._blocked_until.pop(evict_k, None)

        fails = self._attempts.get(user_id, 0) + 1
        self._attempts[user_id] = fails
        if fails >= self.max_attempts:
            self._blocked_until[user_id] = now + self.block_seconds
            self._attempts[user_id] = 0
            return True, fails, self.block_seconds
        return False, fails, 0

    def reset(self, user_id: int) -> None:
        self._attempts.pop(user_id, None)
        self._blocked_until.pop(user_id, None)

    def can_send_join_request(self, user_id: int, teacher_code: str) -> Tuple[bool, int]:
        now = time.monotonic()
        key = (user_id, str(teacher_code))
        until = self._join_sent_until.get(key, 0.0)
        if now < until:
            return False, int(until - now) + 1
        self._join_sent_until.pop(key, None)
        return True, 0

    def mark_join_request_sent(self, user_id: int, teacher_code: str) -> None:
        now = time.monotonic()
        key = (user_id, str(teacher_code))
        if key not in self._join_sent_until and len(self._join_sent_until) >= self._max_tracked:
            oldest_k = next(iter(self._join_sent_until))
            self._join_sent_until.pop(oldest_k, None)
        self._join_sent_until[key] = now + self.join_cooldown_seconds


teacher_code_guard = TeacherCodeBruteforceGuard()


async def render_single_screen(
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup: Any = None,
    disable_web_page_preview: bool = True,
) -> Optional[Message]:
    """
    Движок отображения «Одного экрана» (Clean Chat) без дублирующих вызовов delete_message:
    - Удаляет входящее сообщение пользователя ТОЛЬКО если его ещё не удалил AntiFloodDDoSMiddleware.
    """
    chat_id = message.chat.id
    user_msg_id = message.message_id
    bot = message.bot

    if (
        bot
        and chat_id
        and user_msg_id
        and not getattr(message, "_user_msg_deleted", False)
    ):
        asyncio.create_task(_safe_delete_message(bot, chat_id, user_msg_id))

    safe_text = str(text or "")
    if len(safe_text) > 4090:
        safe_text = safe_text[:4050].rstrip() + "\n\n...[текст сокращён]"

    data = await state.get_data()
    prev_bot_id = screen_manager.get_last_id(chat_id) or data.get("last_bot_msg_id")

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
            plain_fallback = (
                safe_text.replace("\\*", "*")
                .replace("\\_", "_")
                .replace("\\`", "`")
                .replace("\\[", "[")
            )
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

    if prev_bot_id and prev_bot_id != new_msg.message_id:
        asyncio.create_task(_safe_delete_message(bot, chat_id, prev_bot_id))

    return new_msg


# =====================================================================
# 3. ЖИВЫЕ ЛОГИ ОШИБОК И СУТОЧНЫЕ СВОДКИ В TELEGRAM-КАНАЛ (LIVE LOGGER)
# =====================================================================

class LiveErrorLogger:
    """
    Отправляет живые уведомления об ошибках и суточные сводки в Telegram-канал или чат
    с дедупликацией и защитой от спама.
    """

    def __init__(self, log_chat_id: Optional[str] = LOG_CHAT_ID):
        self.log_chat_id = str(log_chat_id).strip() if log_chat_id else None
        self._dedup_cache: Dict[str, Tuple[float, int]] = {}
        self.cooldown_sec = 180.0
        self._sent_timestamps: deque = deque(maxlen=20)
        self.max_per_minute = 15

    def _get_target_chat(self) -> Any:
        if not self.log_chat_id:
            return None
        return (
            int(self.log_chat_id)
            if self.log_chat_id.lstrip("-").isdigit()
            else self.log_chat_id
        )

    async def report_error(
        self,
        bot: Optional[Bot],
        exc: BaseException,
        context_info: str = "",
        user_info: str = "Неизвестен",
    ):
        tb_str = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        logger.error(f"Ошибка [{context_info}] ({user_info}): {exc}\n{tb_str}")

        if not self.log_chat_id or bot is None:
            return

        ctx_prefix = str(context_info or "Global").split(":", 1)[0].strip()
        sig = hashlib.md5(
            f"{type(exc).__name__}:{str(exc)[:80]}:{ctx_prefix}".encode("utf-8")
        ).hexdigest()[:12]
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
            f"🚨 <b>ОШИБКА В БОТЕ v7.6</b>{repeat_badge}\n"
            f"⏰ <b>Время:</b> <code>{now_str}</code>\n"
            f"👤 <b>Пользователь:</b> <code>{self._escape_html(user_info)}</code>\n"
            f"📍 <b>Контекст:</b> <code>{self._escape_html(context_info or 'Global')}</code>\n"
            f"⚠️ <b>Исключение:</b> <code>{self._escape_html(f'{type(exc).__name__}: {exc}')}</code>\n\n"
            f"📜 <b>Traceback:</b>\n<pre>{self._escape_html(tb_short)}</pre>"
        )

        try:
            chat_target = self._get_target_chat()
            await bot.send_message(
                chat_id=chat_target, text=msg_text, parse_mode=ParseMode.HTML
            )
        except Exception as send_err:
            logger.warning(
                f"Не удалось отправить лог ошибки в канал {self.log_chat_id}: {send_err}"
            )

    async def send_daily_digest(self, bot: Optional[Bot], digest: Dict[str, Any]):
        """Отправляет суточную сводку по пользователям и активности в канал логов."""
        if not self.log_chat_id or bot is None:
            return
        msg_text = (
            f"📊 <b>Суточная сводка тренажёра ({self._escape_html(digest.get('date_str', ''))})</b>\n\n"
            f"👥 <b>Аудитория:</b>\n"
            f"• Всего пользователей в базе: <b>{digest.get('total_users', 0)}</b>\n"
            f"• Новых за сегодня: <b>+{digest.get('new_users_today', 0)}</b>\n"
            f"• Активно сегодня (DAU): <b>{digest.get('dau', 0)}</b>\n\n"
            f"⚡️ <b>Практика за день:</b>\n"
            f"• Всего ответов: <b>{digest.get('total_actions_today', 0)}</b> "
            f"(карточки: {digest.get('cards_today', 0)}, тесты: {digest.get('tests_today', 0)})\n"
            f"• Средняя точность: <b>{digest.get('accuracy_today', 0.0)}%</b>\n\n"
            f"👨‍🏫 <b>Учителя и классы:</b>\n"
            f"• Кабинетов преподавателей: <b>{digest.get('teachers_count', 0)}</b>\n"
            f"• Учеников в классах: <b>{digest.get('connected_students_count', 0)}</b>"
        )
        try:
            chat_target = self._get_target_chat()
            await bot.send_message(
                chat_id=chat_target,
                text=msg_text,
                parse_mode=ParseMode.HTML,
                disable_notification=True,
            )
            logger.info("Суточная сводка успешно отправлена в канал логов.")
        except Exception as e:
            logger.warning(f"Не удалось отправить суточную сводку в лог-канал: {e}")

    @staticmethod
    def _escape_html(val: Any) -> str:
        s = str(val or "")
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


live_logger = LiveErrorLogger()


# =====================================================================
# 4. ХЕНДЛЕРЫ БОТА (РЕЖИМ ОДНОГО ЭКРАНА + ЗАЩИТА ОТ ГОНОК)
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

    await live_logger.report_error(
        bot, event.exception, context_info=ctx_str, user_info=user_str
    )
    return True


def _build_policy_consent_text() -> str:
    safe_priv_url = PRIVACY_URL.replace(")", "%29")
    return (
        "⚖️ *Правила использования тренажёра*\n\n"
        "Перед началом работы подтвердите согласие с "
        f"[Политикой конфиденциальности и условиями сервиса]({safe_priv_url}).\n\n"
        "💡 _Стереть все данные можно в любой момент командой_ `/delete_me`."
    )


async def _send_teacher_join_request(
    bot: Optional[Bot],
    student_user: Any,
    teacher: Dict[str, Any],
    teacher_code: str,
) -> bool:
    if not bot:
        return False
    student_id = student_user.id
    student_name = get_safe_display_name(student_user)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Принять в класс",
                    callback_data=f"t_approve:{student_id}:{teacher_code}",
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"t_reject:{student_id}:{teacher_code}",
                ),
            ]
        ]
    )
    msg_text = (
        f"📩 *Заявка на вступление в класс* (код `{escape_md(teacher_code)}`)\n\n"
        f"👤 Ученик: *{strip_md_delimiters(student_name)}* (`{student_id}`)"
    )
    try:
        await bot.send_message(
            chat_id=teacher["user_id"],
            text=msg_text,
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN,
        )
        return True
    except Exception as e:
        logger.warning(f"Не удалось отправить заявку преподавателю {teacher['user_id']}: {e}")
        return False


async def _process_teacher_deeplink(
    bot: Optional[Bot], student_user: Any, teacher_code: str
) -> str:
    user_id = student_user.id
    blocked, wait_sec = teacher_code_guard.is_blocked(user_id)
    if blocked:
        return f"⏳ *Слишком много неверных попыток ввода кода.* Повторите через {wait_sec} сек.\n\n"

    teacher = await asyncio.to_thread(db.get_teacher, teacher_code)
    if not teacher:
        now_blocked, _, block_sec = teacher_code_guard.record_failure(user_id)
        if now_blocked:
            return (
                f"❌ *Преподаватель по коду* `{escape_md(teacher_code)}` *не найден.*\n"
                f"🔒 Ввод кодов заблокирован на {block_sec // 60} мин.\n\n"
            )
        return f"❌ *Преподаватель по коду* `{escape_md(teacher_code)}` *не найден.*\n\n"

    teacher_code_guard.reset(user_id)
    if teacher["user_id"] == user_id:
        return "⚠️ *Вы не можете прикрепиться к своему собственному классу в качестве ученика.*\n\n"
    if await asyncio.to_thread(db.is_student_connected, user_id, teacher_code):
        return (
            f"ℹ️ *Вы уже прикреплены к преподавателю:*\n"
            f"*{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(teacher_code)}`).\n\n"
        )

    can_send, cd_sec = teacher_code_guard.can_send_join_request(user_id, teacher_code)
    if not can_send:
        return (
            f"⏳ *Заявка преподавателю* *{strip_md_delimiters(teacher['full_name'])}* "
            f"*уже отправлена.* Повторная отправка возможна через {cd_sec} сек.\n\n"
        )

    sent = await _send_teacher_join_request(bot, student_user, teacher, teacher_code)
    if sent:
        teacher_code_guard.mark_join_request_sent(user_id, teacher_code)
        return (
            f"📩 *Заявка отправлена преподавателю на подтверждение:*\n"
            f"*{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(teacher_code)}`).\n\n"
        )
    return "⚠️ *Не удалось отправить заявку преподавателю.* Попробуйте позже.\n\n"


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
            connect_notice = await _process_teacher_deeplink(message.bot, message.from_user, pending_t_code)

        text = f"{connect_notice}🏠 *Главное меню*\n\nВыберите формат тренировки:"
        await render_single_screen(
            message, state, text, reply_markup=Keyboards.main_menu()
        )


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


@router.message(
    StateFilter(UserFlow.selecting_exam_level),
    F.text.in_({BTN_EXAM_OGE, BTN_EXAM_EGE}),
)
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
            connect_notice = await _process_teacher_deeplink(message.bot, message.from_user, str(pending_t_code))

        level_badge = (
            "🎓 *Выбран режим подготовки: ОГЭ*\n_Карточки адаптированы: базовое определение, 2 главных признака и пример из РФ._\n\n"
            if chosen_level == "oge"
            else "🏛 *Выбран режим подготовки: ЕГЭ*\n_Включены полные академические формулировки, классификации и разбор ловушек._\n\n"
        )
        text = f"{connect_notice}{level_badge}🏠 *Главное меню*\n\nВыберите формат тренировки:"
        await render_single_screen(
            message, state, text, reply_markup=Keyboards.main_menu()
        )


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
        await render_single_screen(
            message, state, text, reply_markup=Keyboards.main_menu()
        )


@router.message(
    F.text.in_(
        {
            BTN_BACK_MAIN,
            "⬅️ В главное меню",
            "🚪 В главное меню",
            "🚪 Меню",
            "🚪 Завершить тест",
        }
    )
)
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
        await render_single_screen(
            message, state, text, reply_markup=Keyboards.main_menu()
        )


# ---------------------------------------------------------------------
# УЧИТЕЛЬСКАЯ СИСТЕМА И РОТАЦИЯ ТОКЕНОВ (НА НИЖНЕЙ КЛАВИАТУРЕ)
# ---------------------------------------------------------------------

def _build_teacher_cabinet_view(
    full_name: str,
    code: str,
    st_count: int,
    token_version: int,
    bot_username: str,
    is_new: bool = False,
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
                existing["full_name"],
                code,
                st_count,
                t_ver,
                bot_username,
                is_new=False,
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


@router.message(
    StateFilter(UserFlow.in_teacher_cabinet), F.text == BTN_TEACHER_RESET_LINK
)
async def handle_teacher_reset_prompt_reply(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        existing = await asyncio.to_thread(
            db.get_teacher_by_user_id, message.from_user.id
        )
        if not existing:
            await state.clear()
            await render_single_screen(
                message,
                state,
                "Профиль преподавателя не найден.",
                reply_markup=Keyboards.main_menu(),
            )
            return

        await state.set_state(UserFlow.in_teacher_reset_confirm)
        text = (
            "🔐 *Сброс ссылки на аналитику класса*\n\n"
            "Вы уверены, что хотите выпустить новую ссылку для кабинета преподавателя?\n"
            "Предыдущая ссылка сразу перестанет работать на всех устройствах "
            "(8-значный код класса для учеников останется прежним)."
        )
        await render_single_screen(
            message, state, text, reply_markup=Keyboards.teacher_reset_confirm_reply()
        )


@router.message(
    StateFilter(UserFlow.in_teacher_reset_confirm), F.text == BTN_CONFIRM_RESET_YES
)
async def handle_teacher_reset_confirm_reply(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        res = await asyncio.to_thread(db.rotate_teacher_token, message.from_user.id)
        if not res:
            await state.clear()
            await render_single_screen(
                message,
                state,
                "Профиль преподавателя не найден.",
                reply_markup=Keyboards.main_menu(),
            )
            return

        code, new_ver = res
        existing = await asyncio.to_thread(
            db.get_teacher_by_user_id, message.from_user.id
        )
        bot_username = getattr(message.bot, "_cached_username", None)
        if not bot_username:
            bot_info = await message.bot.get_me()
            bot_username = bot_info.username or "obshaga_bot"
            setattr(message.bot, "_cached_username", bot_username)
        st_count = await asyncio.to_thread(db.get_teacher_students_count, code)
        full_name = existing["full_name"] if existing else f"Преподаватель {code}"

        await state.set_state(UserFlow.in_teacher_cabinet)
        text, kb = _build_teacher_cabinet_view(
            full_name, code, st_count, new_ver, bot_username, is_new=False
        )
        text = "✅ *Ссылка на аналитику класса успешно обновлена!*\n\n" + text
        await render_single_screen(message, state, text, reply_markup=kb)


@router.message(Command("reset_link"))
async def handle_reset_link_command(message: Message, state: FSMContext):
    if not message.from_user:
        return
    user_id = message.from_user.id
    async with user_lock_manager.get_lock(user_id):
        new_student_ver = await asyncio.to_thread(db.rotate_user_token, user_id)
        reminders_on = await asyncio.to_thread(
            db.get_user_reminders_enabled, user_id
        )
        student_url = build_student_webapp_url(user_id, new_student_ver)

        lines = [
            "🔐 *Ссылка на аналитику успешно обновлена!*\n",
            "Все старые ссылки отозваны и больше не открываются.\n",
            "Нажмите кнопку «📱 Открыть аналитику» внизу экрана для просмотра.",
        ]

        teacher_res = await asyncio.to_thread(db.rotate_teacher_token, user_id)
        if teacher_res:
            lines.append(
                "\n👨‍🏫 *Ссылка кабинета преподавателя также успешно обновлена.*"
            )

        await state.set_state(UserFlow.in_stats)
        await render_single_screen(
            message,
            state,
            "\n".join(lines),
            reply_markup=Keyboards.stats_reply(student_url, reminders_on),
        )


async def _show_student_teachers_screen(
    message: Message, state: FSMContext, user_id: int, prefix_note: str = ""
):
    teachers = await asyncio.to_thread(db.get_student_teachers, user_id)
    await state.set_state(UserFlow.in_my_teachers)
    if not teachers:
        text = (
            f"{prefix_note}"
            "ℹ️ *Вы пока не прикреплены ни к одному преподавателю.*\n\n"
            "Чтобы подключиться, просто отправьте его 8-значный код сообщением в этот чат."
        )
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(text=BTN_BACK_STATS),
                    KeyboardButton(text=BTN_BACK_MAIN),
                ]
            ],
            resize_keyboard=True,
            is_persistent=True,
        )
        await render_single_screen(message, state, text, reply_markup=kb)
        return

    lines = [f"{prefix_note}👨‍🏫 *Ваши преподаватели:*\n"]
    for idx, t in enumerate(teachers, 1):
        lines.append(
            f"{idx}. *{strip_md_delimiters(t['full_name'])}* (код `{escape_md(t['teacher_code'])}`)"
        )
    lines.append(
        "\n_Чтобы отключиться от преподавателя, нажмите соответствующую кнопку внизу:_"
    )

    await render_single_screen(
        message,
        state,
        "\n".join(lines),
        reply_markup=Keyboards.my_teachers_reply(teachers),
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
        await asyncio.to_thread(
            db.disconnect_student_from_teacher, message.from_user.id, code
        )

        note = f"✅ *Отключено от преподавателя:* {strip_md_delimiters(t_name)} (`{escape_md(code)}`)\n\n"
        await _show_student_teachers_screen(
            message, state, message.from_user.id, prefix_note=note
        )


@router.message(F.text.regexp(r"^\s*\d{8}\s*$"))
async def handle_teacher_code_input(message: Message, state: FSMContext):
    if not message.from_user or not message.text:
        return
    uid = message.from_user.id
    async with user_lock_manager.get_lock(uid):
        code = message.text.strip()
        prev_data = await state.get_data()
        prev_bot_id = prev_data.get("last_bot_msg_id")
        await state.clear()
        if prev_bot_id:
            await state.update_data(last_bot_msg_id=prev_bot_id)

        blocked, wait_sec = teacher_code_guard.is_blocked(uid)
        if blocked:
            await render_single_screen(
                message,
                state,
                f"⏳ Слишком много неверных попыток ввода кода.\n"
                f"Попробуйте снова через {wait_sec} сек.",
                reply_markup=Keyboards.main_menu(),
            )
            return

        teacher = await asyncio.to_thread(db.get_teacher, code)
        if not teacher:
            now_blocked, fails, block_sec = teacher_code_guard.record_failure(uid)
            if now_blocked:
                err_txt = (
                    f"❌ Преподаватель с кодом `{escape_md(code)}` не найден.\n"
                    f"🔒 Превышен лимит попыток. Ввод кодов заблокирован на {block_sec // 60} мин."
                )
            else:
                left = teacher_code_guard.max_attempts - fails
                err_txt = (
                    f"❌ Преподаватель с кодом `{escape_md(code)}` не найден.\n"
                    f"Проверьте правильность ввода 8 цифр (осталось попыток: {left})."
                )
            await render_single_screen(
                message,
                state,
                err_txt,
                reply_markup=Keyboards.main_menu(),
            )
            return

        teacher_code_guard.reset(uid)

        if teacher["user_id"] == uid:
            await render_single_screen(
                message,
                state,
                "❌ Вы не можете прикрепиться к своему собственному классу в качестве ученика.",
                reply_markup=Keyboards.main_menu(),
            )
            return

        if await asyncio.to_thread(db.is_student_connected, uid, code):
            await render_single_screen(
                message,
                state,
                f"ℹ️ Вы уже подключены к преподавателю *{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(code)}`).",
                reply_markup=Keyboards.main_menu(),
            )
            return

        can_send, cd_sec = teacher_code_guard.can_send_join_request(uid, code)
        if not can_send:
            await render_single_screen(
                message,
                state,
                f"⏳ *Заявка преподавателю* *{strip_md_delimiters(teacher['full_name'])}* "
                f"(код `{escape_md(code)}`) *уже отправлена.*\n"
                f"Повторная отправка возможна через {cd_sec} сек.",
                reply_markup=Keyboards.main_menu(),
            )
            return

        sent = await _send_teacher_join_request(
            message.bot, message.from_user, teacher, code
        )
        if sent:
            teacher_code_guard.mark_join_request_sent(uid, code)
            await render_single_screen(
                message,
                state,
                f"📩 *Заявка отправлена преподавателю на подтверждение:*\n"
                f"*{strip_md_delimiters(teacher['full_name'])}* (код `{escape_md(code)}`).\n\n"
                f"Вы получите уведомление, как только преподаватель примет решение.",
                reply_markup=Keyboards.main_menu(),
            )
        else:
            await render_single_screen(
                message,
                state,
                "⚠️ Не удалось отправить заявку преподавателю. Попробуйте позже.",
                reply_markup=Keyboards.main_menu(),
            )


@router.callback_query(F.data.startswith(("t_approve:", "t_reject:")))
async def handle_teacher_join_decision(callback: CallbackQuery):
    if not callback.data or not callback.from_user:
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Некорректные данные заявки.", show_alert=True)
        return
    action, student_id_str, code = parts
    if not re.fullmatch(r"\d{8}", code):
        await callback.answer("Некорректный код класса.", show_alert=True)
        return
    try:
        student_id = int(student_id_str)
    except ValueError:
        await callback.answer("Некорректный ID ученика.", show_alert=True)
        return
    if student_id <= 0 or student_id == callback.from_user.id:
        await callback.answer("Некорректный ID ученика.", show_alert=True)
        return

    async with user_lock_manager.get_lock(callback.from_user.id):
        teacher = await asyncio.to_thread(db.get_teacher, code)
        if not teacher or teacher["user_id"] != callback.from_user.id:
            await callback.answer("⛔ У вас нет прав для обработки этой заявки.", show_alert=True)
            return

        if action == "t_approve":
            already_connected = await asyncio.to_thread(
                db.is_student_connected, student_id, code
            )
            if already_connected:
                if callback.message:
                    try:
                        await callback.message.edit_text(
                            f"✅ *Заявка уже была принята.* Ученик (`{student_id}`) в классе `{escape_md(code)}`.",
                            reply_markup=None,
                            parse_mode=ParseMode.MARKDOWN,
                        )
                    except TelegramBadRequest:
                        pass
                await callback.answer("Ученик уже состоит в классе.")
                return

            await asyncio.to_thread(db.connect_student_to_teacher, student_id, code)
            if callback.message:
                try:
                    await callback.message.edit_text(
                        f"✅ *Заявка принята.* Ученик (`{student_id}`) добавлен в класс `{escape_md(code)}`.",
                        reply_markup=None,
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except TelegramBadRequest:
                    pass
            if callback.bot:
                try:
                    await callback.bot.send_message(
                        chat_id=student_id,
                        text=(
                            f"✅ *Преподаватель {strip_md_delimiters(teacher['full_name'])}* "
                            f"(код `{escape_md(code)}`) *принял вашу заявку!*\n\n"
                            f"Ваш прогресс теперь отображается в ведомости класса."
                        ),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass
            await callback.answer("Ученик принят в класс!")
        else:
            if callback.message:
                try:
                    await callback.message.edit_text(
                        f"❌ *Заявка отклонена.* Ученик (`{student_id}`) не добавлен в класс `{escape_md(code)}`.",
                        reply_markup=None,
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except TelegramBadRequest:
                    pass
            if callback.bot:
                try:
                    await callback.bot.send_message(
                        chat_id=student_id,
                        text=(
                            f"❌ *Преподаватель {strip_md_delimiters(teacher['full_name'])}* "
                            f"(код `{escape_md(code)}`) *отклонил вашу заявку на вступление в класс.*"
                        ),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass
            await callback.answer("Заявка отклонена.")


# ---------------------------------------------------------------------
# СТАТИСТИКА И TELEGRAM MINI APP (100% НА НИЖНЕЙ КЛАВИАТУРЕ)
# ---------------------------------------------------------------------

def _build_stats_view_sync(
    user_id: int, prefix_notice: str = ""
) -> Tuple[str, ReplyKeyboardMarkup]:
    if getattr(db, "_pending_writes", None):
        db.flush_pending_writes_sync()
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
        text, reply_kb = await asyncio.to_thread(
            _build_stats_view_sync, message.from_user.id
        )
        await render_single_screen(message, state, text, reply_markup=reply_kb)


@router.message(F.text.in_({BTN_STATS_REMIND_ON, BTN_STATS_REMIND_OFF}))
async def handle_toggle_reminder_reply(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        new_state = await asyncio.to_thread(
            db.toggle_user_reminders, message.from_user.id
        )
        notice = (
            "🔔 *Ежедневные напоминания включены!*\n\n"
            if new_state
            else "🔕 *Ежедневные напоминания отключены.*\n\n"
        )
        await state.set_state(UserFlow.in_stats)
        text, reply_kb = await asyncio.to_thread(
            _build_stats_view_sync, message.from_user.id, notice
        )
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
        await render_single_screen(
            message, state, text, reply_markup=Keyboards.stats_reset_confirm_reply()
        )


@router.message(
    StateFilter(UserFlow.in_stats_reset_confirm), F.text == BTN_CONFIRM_RESET_YES
)
async def handle_stats_reset_confirm_reply(message: Message, state: FSMContext):
    if not message.from_user:
        return
    user_id = message.from_user.id
    async with user_lock_manager.get_lock(user_id):
        await asyncio.to_thread(db.rotate_user_token, user_id)
        await state.set_state(UserFlow.in_stats)
        notice = "✅ *Ваша ссылка на аналитику успешно обновлена!*\n\n"
        text, reply_kb = await asyncio.to_thread(
            _build_stats_view_sync, user_id, notice
        )
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
        await render_single_screen(
            message, state, text, reply_markup=Keyboards.back_to_menu()
        )


@router.message(StateFilter(UserFlow.in_search), F.text == BTN_CARD_DETAIL)
async def handle_search_detail_button(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    async with user_lock_manager.get_lock(uid):
        data = await state.get_data()
        code = data.get("search_last_code")
        if not code:
            await render_single_screen(
                message,
                state,
                "Введите название термина для поиска (от 3 букв):",
                reply_markup=Keyboards.back_to_menu(),
            )
            return
        card = catalog.get_by_code(code)
        if not card:
            return
        exam_level = await asyncio.to_thread(db.get_user_exam_level, uid)
        other_codes = data.get("search_other_codes") or []
        other_terms = [
            catalog.get_by_code(c) for c in other_codes if catalog.get_by_code(c)
        ]
        text = format_card_back(card, show_detail=True, exam_level=exam_level)
        kb = Keyboards.search_results_keyboard(other_terms, show_detail_btn=False)
        await render_single_screen(message, state, text, reply_markup=kb)


async def _process_search_query_unlocked(message: Message, state: FSMContext):
    uid = message.from_user.id if message.from_user else message.chat.id
    exam_level = await asyncio.to_thread(db.get_user_exam_level, uid)
    raw_text = (message.text or "").strip()

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
            remaining_terms = [
                catalog.get_by_code(c)
                for c in remaining_codes
                if catalog.get_by_code(c)
            ]
            await state.update_data(
                search_last_code=exact_term["code"],
                search_other_codes=remaining_codes[:4],
            )
            card_text = format_card_back(
                exact_term, show_detail=False, exam_level=exam_level
            )
            if remaining_terms:
                similar_names = ", ".join(
                    [f"_{strip_md_delimiters(t['term'])}_" for t in remaining_terms[:4]]
                )
                card_text += f"\n\n🔎 *Похожие понятия:* {similar_names}"
            kb = Keyboards.search_results_keyboard(
                remaining_terms, show_detail_btn=True
            )
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
            reply_markup=Keyboards.back_to_menu(),
        )
        return

    matches = await asyncio.to_thread(catalog.search_terms_fuzzy, raw_q, 6)
    if not matches:
        await render_single_screen(
            message,
            state,
            f"😕 По запросу «*{escape_md(truncate_plain(raw_q, 80))}*» ничего не найдено.\nПопробуйте написать слово иначе или введите первые 3–4 буквы корня:",
            reply_markup=Keyboards.back_to_menu(),
        )
        return

    top_term, top_score = matches[0]
    other_terms = [m[0] for m in matches[1:] if m[1] >= 0.52]

    await state.update_data(
        search_last_code=top_term["code"],
        search_other_codes=[t["code"] for t in other_terms[:4]],
    )
    typo_note = ""
    if _normalize_search_str(raw_q) not in _normalize_search_str(top_term["term"]):
        typo_note = f"✨ _Найдено с учётом опечатки ({int(top_score * 100)}% совпадение):_\n\n"

    card_text = typo_note + format_card_back(
        top_term, show_detail=False, exam_level=exam_level
    )
    if other_terms:
        similar_names = ", ".join(
            [f"_{strip_md_delimiters(t['term'])}_" for t in other_terms[:4]]
        )
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

def _build_errors_workout_screen_sync(
    user_id: int,
) -> Tuple[str, ReplyKeyboardMarkup]:
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
        text, kb = await asyncio.to_thread(
            _build_errors_workout_screen_sync, message.from_user.id
        )
        await state.set_state(UserFlow.in_errors_menu)
        await render_single_screen(message, state, text, reply_markup=kb)


@router.message(F.text.in_({BTN_ERR_CARDS, BTN_ERR_TESTS}))
async def handle_errors_start_reply(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        submode = "cards" if message.text == BTN_ERR_CARDS else "tests"
        problem_codes = await asyncio.to_thread(
            db.get_problem_term_codes, message.from_user.id
        )
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
            await state.update_data(
                mode="cards",
                module_filter=None,
                custom_codes=problem_codes,
                is_error_workout=True,
            )
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
                test_session_times=[],
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
        await render_single_screen(
            message, state, text, reply_markup=Keyboards.topic_selection(mode)
        )


@router.message(
    StateFilter(UserFlow.selecting_topic), F.text.in_(set(TOPIC_BUTTONS_MAP.keys()))
)
async def handle_topic_selection_reply(message: Message, state: FSMContext):
    if not message.from_user or not message.text:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        topic_val = TOPIC_BUTTONS_MAP[message.text]
        module_filter = None if topic_val == "all" else int(topic_val)

        data = await state.get_data()
        mode = data.get("mode", "cards")
        await state.update_data(
            module_filter=module_filter,
            mode=mode,
            custom_codes=None,
            is_error_workout=False,
        )

        if mode == "cards":
            await state.set_state(UserFlow.in_flashcards)
            await send_next_flashcard_msg(message, message.from_user.id, state)
        else:
            await state.set_state(UserFlow.in_tests)
            available = catalog.get_terms(module_filter)
            if not available:
                await render_single_screen(
                    message,
                    state,
                    "❌ В выбранном модуле пока недостаточно терминов.",
                    reply_markup=Keyboards.main_menu(),
                )
                return

            pool = [t["code"] for t in available]
            random.shuffle(pool)
            await state.update_data(
                test_pool=pool,
                test_total_in_session=len(pool),
                test_session_correct=0,
                test_session_errors=[],
                test_session_times=[],
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
            await render_single_screen(
                message, state, done_text, reply_markup=Keyboards.main_menu()
            )
            return

    card = await asyncio.to_thread(
        select_next_card, user_id, module_filter, custom_codes
    )
    if not card:
        await state.clear()
        await render_single_screen(
            message,
            state,
            "❌ В выбранном модуле пока нет терминов.",
            reply_markup=Keyboards.main_menu(),
        )
        return

    exam_level = await asyncio.to_thread(db.get_user_exam_level, user_id)
    shown_at = time.time()
    await state.update_data(
        current_card_code=card["code"], shown_at=shown_at, revealed_elapsed=None
    )

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
    await render_single_screen(
        message, state, text, reply_markup=Keyboards.flashcard_front(card["code"])
    )


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
        await render_single_screen(
            message,
            state,
            text,
            reply_markup=Keyboards.flashcard_back(code, has_detail=False),
        )


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
        await render_single_screen(
            message,
            state,
            text,
            reply_markup=Keyboards.flashcard_back(code, has_detail=True),
        )


@router.message(
    StateFilter(UserFlow.in_flashcards),
    F.text.in_({BTN_CARD_KNOW, BTN_CARD_UNSURE, BTN_CARD_FORGOT}),
)
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

        if revealed_elapsed is None and (time.time() - shown_at) < 0.8:
            if not getattr(message, "_user_msg_deleted", False):
                await _safe_delete_message(
                    message.bot, message.chat.id, message.message_id
                )
            return

        await state.update_data(current_card_code=None)

        ans_map = {
            BTN_CARD_KNOW: "know_well",
            BTN_CARD_UNSURE: "know_poorly",
            BTN_CARD_FORGOT: "dont_know",
        }
        answer_type = ans_map[message.text]

        raw_elapsed = (
            revealed_elapsed
            if revealed_elapsed is not None
            else round(max(0.0, time.time() - shown_at), 1)
        )
        elapsed = round(
            min(max(1.0, raw_elapsed), SpacedRepetitionAlgorithm.MAX_TIME_WINDOW), 1
        )

        is_positive = answer_type == "know_well"
        delta_ans = SpacedRepetitionAlgorithm.calculate_answer_delta(answer_type)
        delta_tm = SpacedRepetitionAlgorithm.calculate_time_delta(
            elapsed, is_correct=is_positive
        )

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

    card = None
    while pool:
        next_code = pool.pop()
        candidate = catalog.get_by_code(next_code)
        if candidate:
            card = candidate
            break

    await state.update_data(test_pool=pool)

    if not card:
        total = data.get("test_total_in_session", 0)
        if total <= 0:
            await state.clear()
            await render_single_screen(
                message,
                state,
                "❌ В выбранном модуле пока недостаточно терминов.",
                reply_markup=Keyboards.main_menu(),
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
        await render_single_screen(
            message,
            state,
            text,
            reply_markup=Keyboards.test_finish(has_errors=bool(errors)),
        )
        return

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
    await render_single_screen(
        message,
        state,
        text,
        reply_markup=Keyboards.test_options(card["code"], test_data["options"]),
    )


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

        if (
            not test_data
            or option_idx < 0
            or option_idx >= len(test_data.get("options", []))
        ):
            if not getattr(message, "_user_msg_deleted", False):
                await _safe_delete_message(
                    message.bot, message.chat.id, message.message_id
                )
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
            session_errors.append(
                {
                    "term": correct_term["term"],
                    "genus": correct_term["genus"],
                    "traits": correct_term.get("traits", []),
                    "explanation": test_data.get("explanation_note", "")
                    or f"Правильный ответ: {correct_term['term']}",
                }
            )

        session_times.append(min(elapsed, SpacedRepetitionAlgorithm.MAX_TIME_WINDOW))
        await state.update_data(
            test_session_correct=correct_count,
            test_session_errors=session_errors,
            test_session_times=session_times,
        )

        if elapsed >= SpacedRepetitionAlgorithm.MIN_TIME_WINDOW:
            answer_type = "correct" if is_correct else "incorrect"
            delta_ans = SpacedRepetitionAlgorithm.calculate_answer_delta(answer_type)
            delta_tm = SpacedRepetitionAlgorithm.calculate_time_delta(
                elapsed, is_correct=is_correct
            )
            safe_code = str(code).replace(";", "_")
            clamped_elapsed = round(
                min(elapsed, SpacedRepetitionAlgorithm.MAX_TIME_WINDOW), 1
            )
            entry_str = f"{safe_code};{delta_ans};{delta_tm};{clamped_elapsed};test"

            user = message.from_user
            username = f"@{user.username}" if user.username else None
            full_name = get_safe_display_name(user)
            await db.save_answer_entry_fast(user.id, username, full_name, entry_str)

        if is_correct:
            status_text = "🎉 *Верно!* Отличная работа."
        else:
            if test_data.get("explanation_note"):
                status_text = (
                    f"❌ *Неверно.*\n💡 {escape_md(test_data['explanation_note'])}"
                )
            else:
                status_text = f"❌ *Неверно.* Правильный термин: *{strip_md_delimiters(correct_term['term'])}*."

        traits = correct_term.get("traits") or []
        traits_hint = (
            f"• {escape_md(traits[0])}"
            if traits
            else f"• {escape_md(correct_term.get('genus') or '')}"
        )
        term_upper = strip_md_delimiters(str(correct_term.get("term") or "").upper())
        genus_safe = strip_md_delimiters(
            correct_term.get("genus") or "Родовое понятие"
        )
        text = (
            f"{status_text}\n\n"
            f"🎓 *{term_upper}*\n"
            f"🏛 _{genus_safe}_\n"
            f"📌 {traits_hint}"
        )

        if elapsed < SpacedRepetitionAlgorithm.MIN_TIME_WINDOW:
            text += "\n\n⚡️ _Ответ < 3 сек (мисклик) — не учтён в общей статистике._"

        await render_single_screen(
            message, state, text, reply_markup=Keyboards.test_next()
        )


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
            await render_single_screen(
                message,
                state,
                "🎉 Ошибок нет — 100% результат!",
                reply_markup=Keyboards.test_finish(has_errors=False),
            )
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
        await render_single_screen(
            message, state, text, reply_markup=Keyboards.test_finish(has_errors=False)
        )


@router.message(StateFilter(UserFlow.in_tests), F.text == BTN_TEST_RESTART)
async def handle_test_restart_reply(message: Message, state: FSMContext):
    if not message.from_user:
        return
    async with user_lock_manager.get_lock(message.from_user.id):
        data = await state.get_data()
        module_filter = data.get("module_filter")
        is_error_workout = data.get("is_error_workout", False)

        if is_error_workout:
            custom_codes = await asyncio.to_thread(
                db.get_problem_term_codes, message.from_user.id
            )
            if not custom_codes:
                await state.clear()
                await render_single_screen(
                    message,
                    state,
                    "🎉 Все проблемные термины уже разобраны!",
                    reply_markup=Keyboards.main_menu(),
                )
                return
            pool = list(custom_codes)
        else:
            available = catalog.get_terms(module_filter)
            if not available:
                await state.clear()
                await render_single_screen(
                    message,
                    state,
                    "❌ В выбранном модуле пока недостаточно терминов.",
                    reply_markup=Keyboards.main_menu(),
                )
                return
            pool = [t["code"] for t in available]

        random.shuffle(pool)
        await state.update_data(
            test_pool=pool,
            test_total_in_session=len(pool),
            test_session_correct=0,
            test_session_errors=[],
            test_session_times=[],
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
        kb = (
            Keyboards.main_menu()
            if policy_ok
            else Keyboards.policy_consent_reply()
        )
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
        await render_single_screen(
            message, state, text, reply_markup=Keyboards.delete_confirm_reply()
        )


@router.message(
    StateFilter(UserFlow.in_delete_me_confirm), F.text == BTN_CONFIRM_DELETE_DATA
)
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
        await render_single_screen(
            message, state, text, reply_markup=Keyboards.policy_consent_reply()
        )


@router.message(
    StateFilter(UserFlow.in_delete_me_confirm), F.text == BTN_CANCEL_DELETE_DATA
)
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
        await render_single_screen(
            message, state, text, reply_markup=Keyboards.main_menu()
        )


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

        if (
            txt
            in {
                BTN_CARD_FLIP,
                BTN_CARD_KNOW,
                BTN_CARD_UNSURE,
                BTN_CARD_FORGOT,
                BTN_CARD_DETAIL,
            }
            and message.from_user
        ):
            await state.set_state(UserFlow.in_flashcards)
            await send_next_flashcard_msg(message, message.from_user.id, state)
            return

        if txt in TOPIC_BUTTONS_MAP and message.from_user:
            topic_val = TOPIC_BUTTONS_MAP[txt]
            mod_filter = None if topic_val == "all" else int(topic_val)
            await state.update_data(
                module_filter=mod_filter,
                mode="cards",
                custom_codes=None,
                is_error_workout=False,
            )
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
                reply_markup=Keyboards.main_menu(),
            )


# =====================================================================
# 5. ФОНОВЫЕ ЗАДАЧИ С АРХИВАЦИЕЙ, СВОДКОЙ В ЛОГИ И RENDER PORT
# =====================================================================

async def setup_bot_commands(bot_instance: Bot):
    """Регистрирует системные команды бота в меню Telegram."""
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


async def process_daily_reminders(
    bot_instance: Bot, reference_date: Optional[date] = None
) -> int:
    """
    Отправляет вечерние напоминания с безопасной паузой 0.08с и защитой от TelegramRetryAfter.
    """
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
            try:
                sent_msg = await bot_instance.send_message(
                    chat_id=uid,
                    text=text,
                    reply_markup=Keyboards.main_menu(),
                    parse_mode=ParseMode.MARKDOWN,
                )
            except TelegramRetryAfter as retry_err:
                await asyncio.sleep(min(float(retry_err.retry_after) + 0.5, 10.0))
                sent_msg = await bot_instance.send_message(
                    chat_id=uid,
                    text=text,
                    reply_markup=Keyboards.main_menu(),
                    parse_mode=ParseMode.MARKDOWN,
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
        await asyncio.sleep(0.08)

    return sent_count


async def start_dummy_web_server(port: int = 10000):
    """Защищённый от HTTP-флуда и Slowloris-атак health-check сервер для Render."""
    conn_semaphore = asyncio.Semaphore(64)

    async def handle_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
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
    - Регламентные задачи (архивация старых записей, очистка учителей, напоминания,
      а также суточная сводка по пользователям в LOG_CHAT_ID)
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
                    await process_daily_reminders(
                        bot_instance, reference_date=today_local
                    )
                    try:
                        digest = await asyncio.to_thread(
                            db.get_daily_admin_digest, today_local
                        )
                        await live_logger.send_daily_digest(bot_instance, digest)
                    except Exception as dig_err:
                        logger.warning(f"Ошибка формирования суточной сводки: {dig_err}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"Ошибка в periodic_background_task: {e}")
            await live_logger.report_error(
                bot_instance, e, context_info="periodic_background_task"
            )


async def _on_bot_shutdown(*args, **kwargs):
    """Гарантированный сброс буфера ответов при получении SIGTERM / остановке диспетчера."""
    try:
        await asyncio.to_thread(db.flush_pending_writes_sync)
        logger.info("Буфер ответов успешно сброшен в БД при остановке бота.")
    except Exception as e:
        logger.error(f"Ошибка сброса буфера при остановке: {e}")


async def main():
    if not BOT_TOKEN:
        logger.error("Токен бота не найден в переменной BOT_TOKEN!")
        print(
            "\n[!] ОШИБКА: Задайте переменную окружения BOT_TOKEN в файле .env или в переменных среды.\n"
        )
        return

    catalog.load()
    await asyncio.to_thread(db.init_db)

    port = int(os.getenv("PORT", "10000"))
    web_server = None
    try:
        web_server = await start_dummy_web_server(port)
    except Exception as e:
        logger.warning(f"Не удалось запустить фоновый веб-сервер на порту {port}: {e}")

    bot = Bot(
        token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN)
    )
    await setup_bot_commands(bot)

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(AntiFloodDDoSMiddleware())
    dp.message.outer_middleware(PolicyConsentMiddleware())
    dp.shutdown.register(_on_bot_shutdown)
    dp.include_router(router)

    bg_task = asyncio.create_task(periodic_background_task(bot))

    logger.info(
        "Бот версии 7.6 (Modular Core + Lossless Archive + Daily Log Digest) успешно запущен."
    )
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
