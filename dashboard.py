import io
import os
import json
import html
import hashlib
import sqlite3
from typing import Optional, Dict, Any, List, Tuple
import pandas as pd
import streamlit as st
import plotly.express as px

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =====================================================================
# 1. НАСТРОЙКА СТРАНИЦЫ И ОКРУЖЕНИЯ
# =====================================================================

st.set_page_config(
    page_title="Аналитика терминов по обществознанию",
    page_icon="📊",
    layout="wide"
)

# Скрытие брендинга Streamlit, меню разработчика и ссылок на GitHub
st.markdown("""
<style>
#MainMenu {visibility: hidden; display: none !important;}
header {visibility: hidden; display: none !important;}
footer {visibility: hidden; display: none !important;}
[data-testid="stToolbar"] {visibility: hidden; display: none !important;}
[data-testid="stDecoration"] {visibility: hidden; display: none !important;}
[data-testid="stStatusWidget"] {visibility: hidden; display: none !important;}
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

# =====================================================================
# 2. МЕТАДАННЫЕ ТЕРМИНОВ (JSON ИЛИ РЕЗЕРВ ИЗ SQLITE)
# =====================================================================

@st.cache_data
def load_terms_metadata():
    modules_map = {
        1: "1. Человек и общество",
        2: "2. Экономика",
        3: "3. Социальные отношения",
        4: "4. Политика",
        5: "5. Право"
    }
    terms_dict = {}

    if os.path.exists(TERMS_JSON_PATH):
        try:
            with open(TERMS_JSON_PATH, "r", encoding="utf-8") as f:
                for item in json.load(f):
                    code = str(item.get("code", "")).strip()
                    try:
                        mod_num = int(code.split(".")[0])
                    except (ValueError, IndexError):
                        mod_num = 1
                    
                    terms_dict[code] = {
                        "term": item.get("term", f"Термин {code}"),
                        "module": modules_map.get(mod_num, f"Модуль {mod_num}"),
                        "genus": item.get("genus", "")
                    }
            if terms_dict:
                return terms_dict
        except Exception:
            pass

    if os.path.exists(DB_PATH):
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='terms';")
            if cur.fetchone():
                cur.execute("SELECT code, module_number, term, genus FROM terms;")
                for r in cur.fetchall():
                    code = str(r[0]).strip()
                    mod_num = int(r[1]) if r[1] else 1
                    terms_dict[code] = {
                        "term": str(r[2]).strip(),
                        "module": modules_map.get(mod_num, f"Модуль {mod_num}"),
                        "genus": str(r[3]).strip() if r[3] else ""
                    }
            conn.close()
        except Exception:
            pass

    return terms_dict

TERMS_MAP = load_terms_metadata()

# =====================================================================
# 3. ПОДКЛЮЧЕНИЕ К БАЗЕ ДАННЫХ
# =====================================================================

def get_db_connection():
    db_url = os.getenv("DATABASE_URL")
    if db_url and db_url.startswith("postgres"):
        import psycopg2
        return psycopg2.connect(db_url), True
    else:
        sqlite_path = os.getenv("DB_PATH", "social_bot.db")
        conn = sqlite3.connect(sqlite_path)
        return conn, False

@st.cache_data(ttl=15)
def fetch_all_data():
    """Загружает логи ответов, список учителей и связи ученик-учитель."""
    COLUMNS = [
        "user_id", "username", "full_name", "mode", "code", "term",
        "module", "delta_ans", "delta_time", "time_sec", "status",
        "is_problem", "created_at"
    ]

    try:
        conn, is_postgres = get_db_connection()
    except Exception as e:
        st.error(f"Не удалось подключиться к базе данных: {e}")
        return pd.DataFrame(columns=COLUMNS), pd.DataFrame(), pd.DataFrame()

    answers_df = pd.DataFrame()
    teachers_df = pd.DataFrame()
    st_teachers_df = pd.DataFrame()

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
            query_teachers = "SELECT teacher_code, user_id, full_name, created_at FROM teachers;"
            teachers_df = pd.read_sql(query_teachers, conn)
        except Exception:
            teachers_df = pd.DataFrame()

        try:
            query_st = "SELECT student_id, teacher_code, joined_at FROM student_teachers;"
            st_teachers_df = pd.read_sql(query_st, conn)
        except Exception:
            st_teachers_df = pd.DataFrame()

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
        entry_str = str(row["entry"])
        parts = entry_str.split(";")
        if len(parts) >= 4:
            code = parts[0].strip()
            try:
                d_ans = float(parts[1])
                d_time = float(parts[2])
                sec = float(parts[3])
            except ValueError:
                continue

            raw_mode = parts[4].strip() if len(parts) >= 5 else "card"
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
                "user_id": str(row["user_id"]),
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
# 4. КОМПОНЕНТЫ ИНТЕРФЕЙСА (ПОВТОРНО ИСПОЛЬЗУЕМЫЙ ВИЗУАЛ)
# =====================================================================

def render_user_avatar_badge(full_name: str, username: Optional[str], user_id: str):
    """Отображает компактный аватар из Telegram CDN с fallback на инициалы (защищено от XSS)."""
    safe_name = html.escape(str(full_name or ""))
    safe_uid = html.escape(str(user_id or ""))
    clean_uname = str(username).replace("@", "").strip() if username else ""
    safe_uname = html.escape(clean_uname)
    avatar_url = f"https://t.me/i/userpic/320/{safe_uname}.jpg" if safe_uname else ""
    
    name_parts = str(full_name or "").split()
    initials = "".join([p[0].upper() for p in name_parts[:2] if p]) if name_parts else "УЧ"
    safe_initials = html.escape(initials)

    html_code = f"""
    <div style="display: flex; align-items: center; gap: 14px; margin-bottom: 20px; padding: 12px 16px; background: rgba(255, 255, 255, 0.04); border-radius: 12px; border: 1px solid rgba(255, 255, 255, 0.1);">
        <img src="{avatar_url}" 
             onerror="this.onerror=null; this.src='https://ui-avatars.com/api/?name={safe_initials}&background=4f46e5&color=fff&size=128';"
             style="width: 48px; height: 48px; border-radius: 50%; object-fit: cover; border: 2px solid #6366f1;" />
        <div>
            <div style="font-size: 1.15rem; font-weight: 600; line-height: 1.2;">{safe_name}</div>
            <div style="font-size: 0.88rem; color: #94a3b8;">{'@' + safe_uname if safe_uname else 'ID: ' + safe_uid}</div>
        </div>
    </div>
    """
    st.markdown(html_code, unsafe_allow_html=True)


def render_analytics_dashboard(df: pd.DataFrame, is_class_view: bool = False):
    """
    Единый визуальный модуль аналитики:
    1. 4 ключевые KPI-метрики (Ответы, Точность, Время, Ошибки) с защитой от выбросов
    2. Круговая диаграмма качества ответов
    3. Круговая диаграмма распределения ошибок по 5 блокам
    4. Топ-10 трудных терминов (с процентом брака и средним временем)
    5. Столбчатая диаграмма скорости ответа по модулям
    """
    c1, c2, c3, c4 = st.columns(4)
    total_answers = len(df)
    
    # Защита от аномальных выбросов: исключаем время более 180с (когда ученик отвлекся)
    valid_times = df[(df["time_sec"] >= 0) & (df["time_sec"] <= 180.0)]["time_sec"]
    avg_time = round(valid_times.mean(), 1) if not valid_times.empty else 0.0
    
    errors_count = int(df["is_problem"].sum())
    accuracy = round((1 - (errors_count / total_answers)) * 100, 1) if total_answers > 0 else 0.0

    c1.metric("Всего ответов", total_answers)
    c2.metric("Успешность / Точность", f"{accuracy}%")
    c3.metric("Среднее время", f"{avg_time} сек")
    c4.metric("Ошибок / Затруднений", errors_count)

    st.divider()

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
                "Ошибка / Не знаю": "#e74c3c"
            },
            hole=0.4
        )
        st.plotly_chart(fig_status, use_container_width=True)

    with col_pie2:
        title_suffix = "для класса" if is_class_view else "по блокам"
        st.subheader(f"🥧 Ошибки {title_suffix}")
        problem_df = df[df["is_problem"]]
        if not problem_df.empty:
            fig_modules = px.pie(problem_df, names="module", hole=0.4)
            st.plotly_chart(fig_modules, use_container_width=True)
        else:
            st.success("🎉 Затруднений и ошибок пока нет — все ответы уверенные!")

    st.divider()

    col_bar1, col_bar2 = st.columns(2)
    
    # Клампим время до 180с для честного отображения в графиках без искажения масштаба
    df_calc = df.copy()
    df_calc["time_sec_clamped"] = df_calc["time_sec"].clip(0, 180)

    with col_bar1:
        table_title = "⚠️ Топ-10 трудных терминов для класса" if is_class_view else "⚠️ Твои трудные термины"
        st.subheader(table_title)
        term_problems = df_calc.groupby(["code", "term", "module"]).agg(
            total_tries=("status", "count"),
            errors=("is_problem", "sum"),
            avg_seconds=("time_sec_clamped", "mean")
        ).reset_index()
        term_problems["error_rate"] = (term_problems["errors"] / term_problems["total_tries"] * 100).round(1)
        term_problems["avg_seconds"] = term_problems["avg_seconds"].round(1)
        top_problematic = term_problems.sort_values(by=["errors", "error_rate"], ascending=False).head(10)

        st.dataframe(
            top_problematic[["code", "term", "module", "errors", "total_tries", "error_rate", "avg_seconds"]].rename(
                columns={
                    "code": "Код",
                    "term": "Термин",
                    "module": "Модуль",
                    "errors": "Ошибок",
                    "total_tries": "Попыток",
                    "error_rate": "% ошибок",
                    "avg_seconds": "Ср. время (с)"
                }
            ),
            use_container_width=True,
            hide_index=True
        )

    with col_bar2:
        st.subheader("⏱ Скорость ответа по блокам")
        module_time = df_calc.groupby("module")["time_sec_clamped"].mean().round(1).reset_index()
        fig_time = px.bar(
            module_time,
            x="module",
            y="time_sec_clamped",
            labels={"module": "Модуль", "time_sec_clamped": "Секунды"},
            color="time_sec_clamped",
            color_continuous_scale="Blues"
        )
        st.plotly_chart(fig_time, use_container_width=True)


# =====================================================================
# 5. ИНТЕРФЕЙС И РОУТИНГ ПО РОЛЯМ (БЕЗОПАСНЫЙ РЕЖИМ)
# =====================================================================

raw_df, teachers_df, st_teachers_df = fetch_all_data()

query_params = st.query_params
student_param = query_params.get("student")
token_param = query_params.get("token")
teacher_param = query_params.get("teacher")
admin_param = query_params.get("admin")

# ---------------------------------------------------------------------
# СЦЕНАРИЙ 1: ПЕРСОНАЛЬНЫЙ КАБИНЕТ УЧЕНИКА (?student=...&token=...)
# ---------------------------------------------------------------------
if student_param:
    st.title("🎓 Личный кабинет ученика")

    expected_token = hashlib.md5(f"{student_param}_{SECRET_KEY}".encode()).hexdigest()[:8]
    if token_param != expected_token:
        st.error("🔒 Доступ ограничен: отсутствует или не совпадает персональный токен.")
        st.info("Пожалуйста, открывайте аналитику по кнопке «🌐 Открыть подробную веб-аналитику» прямо из Telegram-бота (раздел «📊 Моя статистика»).")
        st.stop()

    if raw_df.empty:
        st.info("ℹ️ В базе пока нет данных об ответах. Запусти бота и пройди несколько карточек или тестов!")
        st.stop()

    student_df = raw_df[raw_df["user_id"] == str(student_param)]
    if student_df.empty:
        st.warning(f"Ученик с ID `{student_param}` пока не совершил ни одного действия в тренажере.")
        st.stop()

    sample_row = student_df.iloc[0]
    render_user_avatar_badge(sample_row["full_name"], sample_row["username"], str(student_param))

    mode_options = ["Все режимы", "Только карточки", "Только тесты"]
    selected_mode = st.sidebar.selectbox("Режим тренировки:", mode_options)
    if selected_mode == "Только карточки":
        df = student_df[student_df["mode"] == "Карточки"]
    elif selected_mode == "Только тесты":
        df = student_df[student_df["mode"] == "Тесты"]
    else:
        df = student_df

    if df.empty:
        st.warning(f"По режиму «{selected_mode}» у тебя пока нет ответов.")
        st.stop()

    # Отрисовка унифицированного визуального блока аналитики
    render_analytics_dashboard(df, is_class_view=False)

    if not st_teachers_df.empty and not teachers_df.empty:
        # Безопасное сравнение student_id как строк исключает ValueError при нечисловом значении
        my_t_codes = st_teachers_df[st_teachers_df["student_id"].astype(str) == str(student_param)]["teacher_code"].tolist()
        if my_t_codes:
            st.divider()
            st.subheader("👨‍🏫 Твои преподаватели")
            my_teachers = teachers_df[teachers_df["teacher_code"].isin(my_t_codes)]
            for _, tr in my_teachers.iterrows():
                st.write(f"• **{tr['full_name']}** (Код класса: `{tr['teacher_code']}`)")

    st.stop()


# ---------------------------------------------------------------------
# СЦЕНАРИЙ 2: КАБИНЕТ ПРЕПОДАВАТЕЛЯ (?teacher=...)
# ---------------------------------------------------------------------
elif teacher_param:
    teacher_code = str(teacher_param).strip()
    st.title("👨‍🏫 Кабинет преподавателя")

    teacher_name = f"Код класса: {teacher_code}"
    if not teachers_df.empty:
        t_match = teachers_df[teachers_df["teacher_code"] == teacher_code]
        if not t_match.empty:
            teacher_name = f"{t_match.iloc[0]['full_name']} (Класс `{teacher_code}`)"

    st.caption(f"Просмотр успеваемости класса: **{teacher_name}**")

    if st_teachers_df.empty:
        st.info(f"ℹ️ К классу `{teacher_code}` пока не прикрепился ни один ученик. Отправьте ученикам ссылку-приглашение из бота!")
        st.stop()

    class_student_ids = st_teachers_df[st_teachers_df["teacher_code"] == teacher_code]["student_id"].astype(str).unique().tolist()
    if not class_student_ids:
        st.info(f"ℹ️ К классу `{teacher_code}` пока не прикрепился ни один ученик. Отправьте ученикам ссылку-приглашение из бота!")
        st.stop()

    class_df = raw_df[raw_df["user_id"].isin(class_student_ids)]
    st.sidebar.header("Фильтры класса")
    st.sidebar.write(f"👥 Всего учеников в классе: **{len(class_student_ids)}**")

    # Обработка случая, когда ученики прикрепились, но ещё не успели дать ни одного ответа
    if class_df.empty:
        st.info("ℹ️ Ученики вашего класса уже прикрепились к вам, но пока не начали решать карточки и тесты в боте.")
        summary_rows = []
        for uid in class_student_ids:
            summary_rows.append({
                "Ученик": f"ID: {uid}",
                "Юзернейм": "—",
                "ID Telegram": uid,
                "Всего ответов": 0,
                "Точность (%)": 0.0,
                "Ошибок": 0,
                "Ср. время (сек)": 0.0
            })
        report_df = pd.DataFrame(summary_rows)
        st.dataframe(report_df, use_container_width=True, hide_index=True)
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            report_df.to_excel(writer, index=False, sheet_name="Ведомость класса")
        st.sidebar.download_button(
            label="📥 Скачать ведомость класса (.xlsx)",
            data=buffer.getvalue(),
            file_name=f"Класс_{teacher_code}_ведомость.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        st.stop()

    # Формируем список учеников с включением учеников с 0 ответов
    student_labels = {"👥 Весь класс (Сводка)": "ALL"}
    for uid in class_student_ids:
        sub = class_df[class_df["user_id"] == uid]
        if not sub.empty:
            row_s = sub.iloc[0]
            label = f"{row_s['full_name']} ({'@' + row_s['username'] if row_s['username'] else 'ID: ' + uid})"
        else:
            label = f"ID: {uid} (0 ответов)"
        student_labels[label] = uid

    selected_student_label = st.sidebar.selectbox("Выбери ученика:", list(student_labels.keys()))
    selected_uid = student_labels[selected_student_label]

    if selected_uid == "ALL":
        active_df = class_df
    else:
        active_df = class_df[class_df["user_id"] == selected_uid]
        if not active_df.empty:
            s_row = active_df.iloc[0]
            render_user_avatar_badge(s_row["full_name"], s_row["username"], selected_uid)
        else:
            st.info(f"Ученик с ID `{selected_uid}` пока не дал ни одного ответа в тренажере.")
            st.stop()

    mode_options = ["Все режимы", "Только карточки", "Только тесты"]
    selected_mode = st.sidebar.selectbox("Режим тренировки:", mode_options)
    if selected_mode == "Только карточки":
        active_df = active_df[active_df["mode"] == "Карточки"]
    elif selected_mode == "Только тесты":
        active_df = active_df[active_df["mode"] == "Тесты"]

    if active_df.empty:
        st.warning(f"По выбранному режиму ({selected_mode}) пока нет данных.")
        st.stop()

    st.sidebar.divider()
    summary_rows = []
    for uid in class_student_ids:
        u_df = class_df[class_df["user_id"] == uid]
        if not u_df.empty:
            r0 = u_df.iloc[0]
            u_total = len(u_df)
            u_err = int(u_df["is_problem"].sum())
            u_acc = round((1 - (u_err / u_total)) * 100, 1) if u_total > 0 else 0.0
            u_valid_time = u_df[(u_df["time_sec"] >= 0) & (u_df["time_sec"] <= 180.0)]["time_sec"]
            u_time = round(u_valid_time.mean(), 1) if not u_valid_time.empty else 0.0
            summary_rows.append({
                "Ученик": r0["full_name"],
                "Юзернейм": f"@{r0['username']}" if r0["username"] else "—",
                "ID Telegram": uid,
                "Всего ответов": u_total,
                "Точность (%)": u_acc,
                "Ошибок": u_err,
                "Ср. время (сек)": u_time
            })
        else:
            summary_rows.append({
                "Ученик": f"ID: {uid}",
                "Юзернейм": "—",
                "ID Telegram": uid,
                "Всего ответов": 0,
                "Точность (%)": 0.0,
                "Ошибок": 0,
                "Ср. время (сек)": 0.0
            })

    if summary_rows:
        report_df = pd.DataFrame(summary_rows)
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            report_df.to_excel(writer, index=False, sheet_name="Ведомость класса")
        st.sidebar.download_button(
            label="📥 Скачать ведомость класса (.xlsx)",
            data=buffer.getvalue(),
            file_name=f"Класс_{teacher_code}_ведомость.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    # Отрисовка унифицированного визуального блока аналитики
    render_analytics_dashboard(active_df, is_class_view=True)
    st.stop()


# ---------------------------------------------------------------------
# СЦЕНАРИЙ 3: ВХОД БЕЗ ПАРАМЕТРОВ (ПРИВЕТСТВИЕ + ЗАЩИТА АДМИНИСТРАТОРА)
# ---------------------------------------------------------------------
else:
    is_admin_authed = False
    if admin_param and admin_param == ADMIN_PASSWORD:
        is_admin_authed = True

    if not is_admin_authed:
        st.sidebar.header("🔒 Вход администратора")
        admin_input = st.sidebar.text_input("Пароль главного администратора:", type="password")
        if admin_input:
            if admin_input == ADMIN_PASSWORD:
                is_admin_authed = True
            else:
                st.sidebar.error("Неверный пароль администратора.")

    if not is_admin_authed:
        st.title("📊 Аналитический портал тренажера по обществознанию")
        st.write(
            "Добро пожаловать в веб-аналитику тренажера понятий кодификатора!\n\n"
            "В целях защиты персональных данных успеваемость разделена на изолированные разделы:"
        )

        c_info1, c_info2 = st.columns(2)
        with c_info1:
            st.info(
                "🎓 **Для учеников:**\n\n"
                "Чтобы посмотреть свою подробную статистику, графики и прогресс — "
                "открой Telegram-бот, нажми **«📊 Моя статистика»** и перейди по персональной кнопке "
                "**«🌐 Открыть подробную веб-аналитику»**."
            )
        with c_info2:
            st.success(
                "👨‍🏫 **Для преподавателей:**\n\n"
                "Чтобы увидеть ведомость и аналитику своего класса — "
                "используй персональную ссылку на класс, которую бот выдал при регистрации "
                "по команде `/teacher` (формата `?teacher=КОД_КЛАССА`)."
            )

        st.caption("🔒 Если вы главный администратор проекта — введите пароль в левой боковой панели для просмотра общей базы.")
        st.stop()

    st.title("🛡 Глобальная панель администратора")
    st.success("Авторизован как главный администратор проекта.")

    if raw_df.empty:
        st.info("ℹ️ В базе данных пока нет ответов. Запусти бота и пройди несколько карточек или тестов!")
        st.stop()

    st.sidebar.header("Фильтры базы")

    user_labels_map = {}
    for uid in raw_df["user_id"].unique():
        sub_df = raw_df[raw_df["user_id"] == uid]
        r0 = sub_df.iloc[0]
        fname = r0["full_name"]
        uname = f"@{r0['username']}" if r0["username"] else f"ID: {uid}"
        label = f"{fname} ({uname})"
        user_labels_map[label] = uid

    users_options = ["Все пользователи"] + sorted(list(user_labels_map.keys()))
    selected_option = st.sidebar.selectbox("Выбери пользователя:", users_options)

    if selected_option == "Все пользователи":
        df = raw_df
        st.sidebar.write(f"👥 Всего учеников в базе: **{raw_df['user_id'].nunique()}**")
    else:
        chosen_uid = user_labels_map[selected_option]
        df = raw_df[raw_df["user_id"] == chosen_uid]
        s_row = df.iloc[0]
        render_user_avatar_badge(s_row["full_name"], s_row["username"], chosen_uid)

    mode_options = ["Все режимы", "Только карточки", "Только тесты"]
    selected_mode = st.sidebar.selectbox("Режим тренировки:", mode_options)

    if selected_mode == "Только карточки":
        df = df[df["mode"] == "Карточки"]
    elif selected_mode == "Только тесты":
        df = df[df["mode"] == "Тесты"]

    if df.empty:
        st.warning(f"По выбранному режиму ({selected_mode}) пока нет данных.")
        st.stop()

    # Отрисовка унифицированного визуального блока аналитики
    render_analytics_dashboard(df, is_class_view=False)
