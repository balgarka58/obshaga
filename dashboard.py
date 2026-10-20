import os
import json
import sqlite3
import pandas as pd
import streamlit as st
import plotly.express as px

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# =====================================================================
# 1. НАСТРОЙКА СТРАНИЦЫ
# =====================================================================

st.set_page_config(
    page_title="Аналитика терминов ЕГЭ",
    page_icon="📊",
    layout="wide"
)

DATABASE_URL = os.getenv("DATABASE_URL")
DB_PATH = os.getenv("DB_PATH", "social_bot.db")
TERMS_JSON_PATH = os.getenv("TERMS_JSON_PATH", "terms.json")

# =====================================================================
# 2. ЗАГРУЗКА МЕТАДАННЫХ ТЕРМИНОВ (JSON ИЛИ РЕЗЕРВ ИЗ SQLITE)
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

    # Способ А: Загрузка из terms.json
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

    # Способ Б: Резервная загрузка напрямую из SQLite social_bot.db
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
# 3. ПОДКЛЮЧЕНИЕ К БАЗЕ ДАННЫХ (NEON POSTGRESQL / SQLITE FALLBACK)
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

@st.cache_data(ttl=30)
def fetch_data():
    try:
        conn, is_postgres = get_db_connection()
    except Exception as e:
        st.error(f"Не удалось подключиться к базе данных: {e}")
        return pd.DataFrame()

    query = """
        SELECT user_id, entry, created_at 
        FROM user_answers 
        ORDER BY created_at DESC;
    """
    try:
        df = pd.read_sql(query, conn)
    except Exception:
        # Если таблица user_answers еще не создана
        df = pd.DataFrame()
    finally:
        conn.close()

    if df.empty:
        return pd.DataFrame()

    # Парсим строку формата "code;delta_answer;delta_time;time_seconds"
    parsed_rows = []
    for _, row in df.iterrows():
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

            term_info = TERMS_MAP.get(code, {
                "term": f"Код {code}",
                "module": f"Модуль {code.split('.')[0] if '.' in code else '?'}"
            })

            # Статус ответа по величине дельты:
            # <= 0: уверенный верный ответ (-1.0)
            # 0.5: неуверенно / знаю плохо
            # >= 1.5: ошибка / не знаю (+2.0)
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
                "user_id": str(row["user_id"]),
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

    return pd.DataFrame(parsed_rows)

# =====================================================================
# 4. ИНТЕРФЕЙС АНАЛИТИЧЕСКОЙ ПАНЕЛИ
# =====================================================================

st.title("📊 Аналитическая панель тренажера ЕГЭ")

raw_df = fetch_data()

if raw_df.empty:
    st.info("ℹ️ В базе данных пока нет ответов. Запусти бота и пройди несколько карточек или тестов!")
    st.stop()

# Боковая панель: фильтр по ученикам
st.sidebar.header("Фильтры")
users_list = ["Все пользователи"] + sorted(raw_df["user_id"].unique().tolist())
selected_user = st.sidebar.selectbox("Выбери пользователя:", users_list)

if selected_user == "Все пользователи":
    df = raw_df
    st.sidebar.write(f"👥 Всего учеников в базе: **{raw_df['user_id'].nunique()}**")
else:
    df = raw_df[raw_df["user_id"] == selected_user]
    st.sidebar.write(f"👤 Карточка ученика ID: `{selected_user}`")

# =====================================================================
# 5. КЛЮЧЕВЫЕ МЕТРИКИ (KPI)
# =====================================================================

c1, c2, c3, c4 = st.columns(4)
total_answers = len(df)
avg_time = round(df["time_sec"].mean(), 1) if total_answers > 0 else 0.0
errors_count = int(df["is_problem"].sum())
accuracy = round((1 - (errors_count / total_answers)) * 100, 1) if total_answers > 0 else 0.0

c1.metric("Всего ответов", total_answers)
c2.metric("Успешность", f"{accuracy}%")
c3.metric("Среднее время", f"{avg_time} сек")
c4.metric("Ошибок / Затруднений", errors_count)

st.divider()

# =====================================================================
# 6. КРУГОВЫЕ ДИАГРАММЫ
# =====================================================================

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
    st.subheader("🥧 Проблемные места по модулям")
    problem_df = df[df["is_problem"]]
    if not problem_df.empty:
        fig_modules = px.pie(
            problem_df,
            names="module",
            title="Распределение затруднений по блокам",
            hole=0.4
        )
        st.plotly_chart(fig_modules, use_container_width=True)
    else:
        st.success("🎉 Затруднений и ошибок пока нет — все ответы уверенные!")

st.divider()

# =====================================================================
# 7. ДЕТАЛЬНЫЙ РАЗБОР: ПРОБЛЕМНЫЕ ТЕРМИНЫ И СКОРОСТЬ
# =====================================================================

col_bar1, col_bar2 = st.columns(2)

with col_bar1:
    st.subheader("⚠️ Топ-10 трудных терминов")
    term_problems = df.groupby(["code", "term", "module"]).agg(
        total_tries=("status", "count"),
        errors=("is_problem", "sum"),
        avg_seconds=("time_sec", "mean")
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
    st.subheader("⏱ Скорость ответа по блокам кодификатора")
    module_time = df.groupby("module")["time_sec"].mean().round(1).reset_index()
    fig_time = px.bar(
        module_time,
        x="module",
        y="time_sec",
        labels={"module": "Модуль", "time_sec": "Секунды"},
        color="time_sec",
        color_continuous_scale="Blues"
    )
    st.plotly_chart(fig_time, use_container_width=True)