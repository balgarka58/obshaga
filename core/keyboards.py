"""
Модуль состояний FSM и Reply-клавиатур Telegram-бота (v7.6).
"""
from typing import Optional, Dict, Any, List
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from core.common import truncate_plain


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

# Набор служебных кнопок, которые НЕ должны запускать поиск термина
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
            input_field_placeholder="Выберите режим или введите термин...",
        )

    @staticmethod
    def exam_level_selection() -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text=BTN_EXAM_OGE), KeyboardButton(text=BTN_EXAM_EGE)],
            ],
            resize_keyboard=True,
            is_persistent=True,
            input_field_placeholder="Выберите экзамен (ОГЭ или ЕГЭ)...",
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
            input_field_placeholder="Выберите блок кодификатора...",
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
            input_field_placeholder="Нажмите 1, 2, 3 или 4...",
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
    def search_results_keyboard(
        matched_terms: List[Dict[str, Any]], show_detail_btn: bool = False
    ) -> ReplyKeyboardMarkup:
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
            input_field_placeholder="Введите название термина...",
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
            input_field_placeholder="Управление статистикой и аналитикой...",
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
            input_field_placeholder="Выберите действие или отправьте 8-значный код учителя...",
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
            input_field_placeholder="Введите запрос или вернитесь в меню...",
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
            input_field_placeholder="Нажмите «✅ Принять и начать»...",
        )
