"""
Модуль фильтрации по временному окну, расчёта и HTML/CSS-рендеринга
календаря активности для дашборда (v7.6).
"""
import html
import json
import calendar
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List, Tuple
import pandas as pd

from core.common import TZ

TIME_WINDOW_OPTIONS = [
    "За всё время",
    "Сегодня",
    "Последние 7 дней",
    "Последние 30 дней",
    "Этот учебный год",
    "Свой диапазон дат",
]

WEEKDAY_NAMES_RU = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]
WEEKDAY_SHORT_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTH_SHORT_RU = {
    1: "Янв",
    2: "Фев",
    3: "Мар",
    4: "Апр",
    5: "Май",
    6: "Июн",
    7: "Июл",
    8: "Авг",
    9: "Сен",
    10: "Окт",
    11: "Ноя",
    12: "Дек",
}
MONTH_FULL_RU = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}
MODULE_SHORT_BADGE = {
    "1. Человек и общество": "Общество",
    "2. Экономика": "Экономика",
    "3. Социальные отношения": "Социология",
    "4. Политика": "Политика",
    "5. Право": "Право",
}


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
    end_date: Optional[date] = None,
) -> Dict[str, Any]:
    """
    Формирует данные календаря активности:
    1. Точная разметка месяцев по дню недели, на который пришлось 1-е число месяца.
    2. Помесячные данные за последние 12 месяцев для мобильного вида (Вариант Б).
    3. Расчёт среднего темпа за активный день (avg_actions_per_active_day) без деления на ноль.
    """
    today_d = end_date or datetime.now(TZ).date()
    if isinstance(today_d, datetime):
        today_d = today_d.date()

    mode_norm = str(counter_mode or "Все режимы").strip()
    if mode_norm in ["Только карточки", "Карточки"]:
        mode_label = "Только карточки"
        mode_df = (
            unfiltered_df[unfiltered_df["mode"] == "Карточки"].copy()
            if not unfiltered_df.empty
            else pd.DataFrame()
        )
    elif mode_norm in ["Только тесты", "Тесты"]:
        mode_label = "Только тесты"
        mode_df = (
            unfiltered_df[unfiltered_df["mode"] == "Тесты"].copy()
            if not unfiltered_df.empty
            else pd.DataFrame()
        )
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
        ts_series = _normalize_timestamps(mode_df["created_at"]).fillna(
            pd.Timestamp(today_d)
        )
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
    total_actions_year = sum(
        c for d, c in daily_counts.items() if year_start_365 <= d <= today_d
    )
    avg_actions_per_active_day = (
        round(total_actions_year / active_days_year, 1)
        if active_days_year > 0
        else 0.0
    )

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
    elif (
        (today_d - timedelta(days=1)) in daily_counts
        and daily_counts[(today_d - timedelta(days=1))] > 0
    ):
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

            cells.append(
                {
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
                }
            )

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
                in_month = d_obj.month == m_val
                is_fut = d_obj > today_d
                cnt = (
                    0
                    if (not in_month or is_fut)
                    else daily_counts.get(d_obj, 0)
                )
                c_cards = (
                    0
                    if (not in_month or is_fut)
                    else daily_cards.get(d_obj, 0)
                )
                c_tests = (
                    0
                    if (not in_month or is_fut)
                    else daily_tests.get(d_obj, 0)
                )
                c_err = (
                    0
                    if (not in_month or is_fut)
                    else daily_errors.get(d_obj, 0)
                )
                acc = round((1 - c_err / cnt) * 100, 1) if cnt > 0 else 0.0
                if in_month and not is_fut:
                    m_total_actions += cnt
                    if cnt > 0:
                        m_active_days += 1
                lvl = (
                    _get_level(cnt)
                    if (in_month and not is_fut)
                    else (-2 if not in_month else -1)
                )
                wk_cells.append(
                    {
                        "day": d_obj.day if in_month else "",
                        "date_fmt": d_obj.strftime("%d.%m.%Y"),
                        "in_month": in_month,
                        "is_today": (d_obj == today_d and in_month),
                        "count": cnt,
                        "cards": c_cards,
                        "tests": c_tests,
                        "acc": acc,
                        "level": lvl,
                    }
                )
            m_rows.append(wk_cells)
        months_data.append(
            {
                "title": f"{MONTH_FULL_RU.get(m_val, '')} {y_val}",
                "total_actions": m_total_actions,
                "active_days": m_active_days,
                "weeks": m_rows,
            }
        )

    return {
        "mode_label": mode_label,
        "start_date": start_monday.isoformat(),
        "end_date": today_d.isoformat(),
        "weeks_count": weeks_count,
        "current_streak": current_streak,
        "best_streak": best_streak,
        "active_days_year": active_days_year,
        "total_actions_year": total_actions_year,
        "avg_actions_per_active_day": avg_actions_per_active_day,
        "z_matrix": z_matrix,
        "hover_matrix": hover_matrix,
        "cells": cells,
        "month_labels": month_labels,
        "months_data": months_data,
    }


def build_calendar_html(cal_data: Dict[str, Any]) -> str:
    """Генерирует адаптивный виджет календаря активности v7.6 с защитой от XSS."""
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

    legend_squares = "".join(
        [
            f'<span class="gh-cell lvl-{lvl}" style="cursor:default;"></span>'
            for lvl in [0, 1, 2, 3, 4]
        ]
    )

    months_json = json.dumps(
        cal_data.get("months_data", []), ensure_ascii=False
    ).replace("</", "<\\/")

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
