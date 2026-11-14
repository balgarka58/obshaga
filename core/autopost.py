"""
Модуль автономного ИИ-постинга для Telegram-канала «ОБЩАГА | ТЕРМИНЫ» (v2).
- Сбор свежих новостей через Google News RSS (не блокируется с Render) + прямые RSS-ленты.
- Сопоставление новостей с базой terms.json и генерация постов через Gemini API (gemini-4-hydrogen) с авто-повтором при таймауте.
- Подтягивание реального фото новости (og:image) или генерация вертикальных 4:5 (1080x1350) карточек-шпаргалок на Pillow со шрифтом Inter.
"""
import io
import os
import re
import html
import json
import uuid
import random
import asyncio
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple, Set

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from core.common import GEMINI_API_KEY, GEMINI_MODEL, TZ

logger = logging.getLogger("social_bot")

GOOGLE_NEWS_RSS_FEEDS: List[Tuple[str, str]] = [
    (
        "Google News (Экономика)",
        "https://news.google.com/rss/search?q=%D1%8D%D0%BA%D0%BE%D0%BD%D0%BE%D0%BC%D0%B8%D0%BA%D0%B0+%D0%BD%D0%B0%D0%BB%D0%BE%D0%B3%D0%B8+%D0%A6%D0%91+%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D1%8F&hl=ru&gl=RU&ceid=RU:ru",
    ),
    (
        "Google News (Право)",
        "https://news.google.com/rss/search?q=%D0%B7%D0%B0%D0%BA%D0%BE%D0%BD+%D0%93%D0%BE%D1%81%D0%B4%D1%83%D0%BC%D0%B0+%D0%BF%D1%80%D0%B0%D0%B2%D0%BE+%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D1%8F&hl=ru&gl=RU&ceid=RU:ru",
    ),
    (
        "Google News (Общество)",
        "https://news.google.com/rss/search?q=%D1%80%D1%8B%D0%BD%D0%BE%D0%BA+%D1%82%D1%80%D1%83%D0%B4%D0%B0+%D0%BE%D0%B1%D1%80%D0%B0%D0%B7%D0%BE%D0%B2%D0%B0%D0%BD%D0%B8%D0%B5+%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D1%8F&hl=ru&gl=RU&ceid=RU:ru",
    ),
]

DIRECT_RSS_FEEDS: List[Tuple[str, str]] = [
    ("Коммерсантъ", "https://www.kommersant.ru/rss/news.xml"),
    ("Ведомости", "https://www.vedomosti.ru/rss/news"),
    ("ТАСС", "https://tass.ru/rss/v2.xml"),
    ("РИА Новости", "https://ria.ru/export/rss2/index.xml"),
]

RSS_STOP_KEYWORDS: Tuple[str, ...] = (
    "футбол", "хоккей", "теннис", "матч", "чемпионат", "лига чемпионов",
    "погода", "синоптик", "гороскоп", "шоу-бизнес", "кинопремьера", "трейлер",
    "дтп", "пожар", "убийств", "скончал", "погиб", "взрыв", "теракт",
    "арестован за наркотики", "бпла", "атака дрон",
)

FORMAT_TYPES: Tuple[str, ...] = (
    "news_term",
    "traits_cheat",
    "pitfall_compare",
    "blitz_poll",
)

# (ключевое слово раздела, основной акцент, мягкий фон акцента, тёмный акцент)
MODULE_PALETTES: List[
    Tuple[str, Tuple[int, int, int], Tuple[int, int, int], Tuple[int, int, int]]
] = [
    ("эконом", (5, 150, 105), (209, 250, 229), (6, 78, 59)),      # Изумрудный
    ("прав", (79, 70, 229), (224, 231, 255), (49, 46, 129)),       # Индиго
    ("полит", (220, 38, 38), (254, 226, 226), (127, 29, 29)),      # Алый
    ("социол", (217, 119, 6), (254, 243, 199), (120, 53, 15)),     # Янтарный
    ("человек", (13, 148, 136), (204, 251, 241), (17, 94, 89)),    # Бирюзовый
]
DEFAULT_PALETTE: Tuple[Tuple[int, int, int], Tuple[int, int, int], Tuple[int, int, int]] = (
    (37, 99, 235),
    (219, 234, 254),
    (30, 58, 138),
)

ANTI_CLICHE_RULES = (
    "СТРОГИЕ ПРАВИЛА СТИЛЯ:\n"
    "1. Пиши чётко, по делу и без лирики — как сильный репетитор или автор шпаргалок для 9–11 классов.\n"
    "2. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ любые нейросетевые штампы: 'это не просто X, а/это Y', "
    "'живёт в оболочке', 'в современном мире', 'давайте разберёмся', 'не секрет, что', "
    "'таким образом', 'подводя итог', 'важно отметить', 'стоит подчеркнуть', 'каждый из нас'.\n"
    "3. Опирайся ТОЛЬКО на реальные факты из переданной новости и поля термина (определение, признаки, ловушки). "
    "Не выдумывай несуществующие законы, цифры и статистику.\n"
    "4. Используй HTML-теги Telegram (<b>, <i>) для акцентов, максимум 2 эмодзи на весь пост.\n"
    "5. Длина поста — строго 450–780 символов (должен легко читаться с телефона за 20 секунд)."
)


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _get_module_colors(
    module_title: str,
) -> Tuple[Tuple[int, int, int], Tuple[int, int, int], Tuple[int, int, int]]:
    low = (module_title or "").lower()
    for key, accent, soft_bg, dark_accent in MODULE_PALETTES:
        if key in low:
            return accent, soft_bg, dark_accent
    return DEFAULT_PALETTE


def _load_font(size: int, weight: str = "regular") -> ImageFont.ImageFont:
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fonts_dir = os.path.join(base_dir, "assets", "fonts")
    weight_map = {
        "extrabold": [
            os.path.join(fonts_dir, "Inter-ExtraBold.otf"),
            os.path.join(fonts_dir, "Inter-Bold.otf"),
            "/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ],
        "bold": [
            os.path.join(fonts_dir, "Inter-Bold.otf"),
            "/usr/share/fonts/opentype/inter/Inter-Bold.otf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "C:\\Windows\\Fonts\\arialbd.ttf",
        ],
        "medium": [
            os.path.join(fonts_dir, "Inter-Medium.otf"),
            os.path.join(fonts_dir, "Inter-Regular.otf"),
            "/usr/share/fonts/opentype/inter/Inter-Medium.otf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ],
        "regular": [
            os.path.join(fonts_dir, "Inter-Regular.otf"),
            "/usr/share/fonts/opentype/inter/Inter-Regular.otf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "C:\\Windows\\Fonts\\arial.ttf",
        ],
    }
    for path in weight_map.get(weight, weight_map["regular"]):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return int(bbox[2] - bbox[0])


def _draw_pill(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    font: ImageFont.ImageFont,
    bg_color: Tuple[int, int, int],
    text_color: Tuple[int, int, int],
    pad_x: int = 24,
    height: int = 46,
    max_width: int = 950,
) -> int:
    """Рисует аккуратную капсулу строго по реальной ширине текста (textbbox)."""
    t = text.strip()
    while len(t) > 6 and _text_width(draw, t, font) + pad_x * 2 > max_width:
        t = t[:-2].rstrip() + "…"
    tw = _text_width(draw, t, font)
    w = tw + pad_x * 2
    draw.rounded_rectangle([x, y, x + w, y + height], radius=height // 2, fill=bg_color)
    bbox = draw.textbbox((0, 0), t, font=font)
    th = bbox[3] - bbox[1]
    ty = y + (height - th) // 2 - bbox[1]
    draw.text((x + pad_x, ty), t, font=font, fill=text_color)
    return w


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int = 4,
) -> List[str]:
    words = (text or "").strip().split()
    if not words:
        return []
    lines: List[str] = []
    cur = words[0]
    for w in words[1:]:
        test = f"{cur} {w}"
        if _text_width(draw, test, font) <= max_width:
            cur = test
        else:
            lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if len(lines) < max_lines:
        lines.append(cur)
    elif cur and lines:
        last = lines[-1]
        while len(last) > 3 and _text_width(draw, last + "…", font) > max_width:
            last = last[:-1].rstrip()
        lines[-1] = last + "…"
    return lines[:max_lines]


def render_post_card_png(
    format_type: str,
    term_obj: Dict[str, Any],
    card_data: Dict[str, Any],
) -> bytes:
    """
    Генерирует плотную вертикальную карточку-шпаргалку 1080x1350 (4:5)
    в светлой журнальной эстетике со шрифтом Inter.
    """
    W, H = 1080, 1350
    PAD = 64
    module_title = term_obj.get("module_title", "Обществознание")
    term_name = term_obj.get("term", "Термин")
    term_code = term_obj.get("code", "")
    accent, soft_bg, dark_accent = _get_module_colors(module_title)

    bg_paper = (247, 246, 242)
    card_white = (255, 255, 255)
    border_col = (226, 232, 240)
    ink_dark = (15, 23, 42)
    ink_body = (51, 65, 85)
    ink_muted = (100, 116, 139)

    font_header = _load_font(23, "bold")
    font_badge = _load_font(22, "bold")
    font_title = _load_font(56, "extrabold")
    font_title_sm = _load_font(46, "extrabold")
    font_sec = _load_font(25, "bold")
    font_body = _load_font(31, "medium")
    font_body_sm = _load_font(27, "regular")
    font_num = _load_font(28, "extrabold")
    font_footer = _load_font(23, "medium")

    img = Image.new("RGB", (W, H), bg_paper)
    draw = ImageDraw.Draw(img)

    # Верхняя акцентная полоса и шапка
    draw.rectangle([0, 0, W, 14], fill=accent)
    draw.text((PAD, 48), "ОБЩАГА  |  ЕГЭ И ОГЭ", font=font_header, fill=ink_muted)
    code_lbl = f"№ {term_code}"
    cw = _text_width(draw, code_lbl, font_header)
    draw.text((W - PAD - cw, 48), code_lbl, font=font_header, fill=accent)
    draw.line([(PAD, 92), (W - PAD, 92)], fill=border_col, width=2)

    if format_type == "traits_cheat":
        _draw_pill(draw, PAD, 118, f"ШПАРГАЛКА  •  {module_title.upper()}", font_badge, soft_bg, dark_accent)

        t_lines = _wrap_text(draw, term_name, font_title, W - PAD * 2, max_lines=2)
        y = 190
        for line in t_lines:
            lw = min(_text_width(draw, line, font_title), W - PAD * 2)
            draw.rectangle([PAD, y + 44, PAD + lw + 8, y + 60], fill=soft_bg)
            draw.text((PAD, y), line, font=font_title, fill=ink_dark)
            y += 72

        simple_def = str(card_data.get("simple_def") or term_obj.get("concept_explanation", "")).strip()
        y += 12
        def_lines = _wrap_text(draw, simple_def, font_body_sm, W - PAD * 2 - 48, max_lines=3)
        def_h = 64 + len(def_lines) * 36
        draw.rounded_rectangle([PAD, y, W - PAD, y + def_h], radius=22, fill=card_white, outline=border_col, width=2)
        draw.text((PAD + 24, y + 20), "СУТЬ ПОНЯТИЯ", font=font_badge, fill=accent)
        dy = y + 54
        for dl in def_lines:
            draw.text((PAD + 24, dy), dl, font=font_body_sm, fill=ink_body)
            dy += 36

        y += def_h + 28
        draw.text((PAD, y), "3 ПРИЗНАКА ДЛЯ 18-ГО ЗАДАНИЯ", font=font_sec, fill=ink_dark)
        y += 46

        points = card_data.get("card_points") or term_obj.get("traits") or []
        for idx in range(3):
            pt = str(points[idx]) if idx < len(points) else "Обязательный признак понятия из КИМ ФИПИ"
            p_lines = _wrap_text(draw, pt, font_body, W - PAD * 2 - 120, max_lines=3)
            box_h = max(132, 48 + len(p_lines) * 40)
            draw.rounded_rectangle([PAD, y, W - PAD, y + box_h], radius=24, fill=card_white, outline=border_col, width=2)
            draw.ellipse([PAD + 24, y + (box_h - 52) // 2, PAD + 76, y + (box_h + 52) // 2], fill=accent)
            num_s = str(idx + 1)
            nw = _text_width(draw, num_s, font_num)
            draw.text((PAD + 50 - nw // 2, y + (box_h - 34) // 2), num_s, font=font_num, fill=card_white)
            py = y + (box_h - len(p_lines) * 40) // 2
            for pl in p_lines:
                draw.text((PAD + 100, py), pl, font=font_body, fill=ink_dark)
                py += 40
            y += box_h + 20

    elif format_type == "pitfall_compare":
        _draw_pill(draw, PAD, 118, f"ЛОВУШКА ЭКЗАМЕНА  •  {module_title.upper()}", font_badge, (254, 226, 226), (153, 27, 27))

        t_lines = _wrap_text(draw, term_name, font_title, W - PAD * 2, max_lines=2)
        y = 190
        for line in t_lines:
            draw.text((PAD, y), line, font=font_title, fill=ink_dark)
            y += 70

        y += 22
        wrong_txt = str(card_data.get("wrong_way") or "Путают термин со смежными понятиями и теряют баллы во 2-й части.")
        w_lines = _wrap_text(draw, wrong_txt, font_body, W - PAD * 2 - 56, max_lines=5)
        w_h = max(250, 108 + len(w_lines) * 42)
        draw.rounded_rectangle([PAD, y, W - PAD, y + w_h], radius=26, fill=(255, 245, 245), outline=(252, 165, 165), width=2)
        _draw_pill(draw, PAD + 28, y + 26, "КАК ОШИБАЮТСЯ", font_badge, (239, 68, 68), card_white, height=40)
        wy = y + 88
        for wl in w_lines:
            draw.text((PAD + 28, wy), wl, font=font_body, fill=(127, 29, 29))
            wy += 42

        y += w_h + 26
        right_txt = str(card_data.get("right_way") or "Опираются на родовой признак и 2–3 отличительные черты из критериев.")
        r_lines = _wrap_text(draw, right_txt, font_body, W - PAD * 2 - 56, max_lines=5)
        r_h = max(250, 108 + len(r_lines) * 42)
        draw.rounded_rectangle([PAD, y, W - PAD, y + r_h], radius=26, fill=(240, 253, 244), outline=(110, 231, 183), width=2)
        _draw_pill(draw, PAD + 28, y + 26, "КАК НАДО ПО КРИТЕРИЯМ", font_badge, (16, 185, 129), card_white, height=40)
        ry = y + 88
        for rl in r_lines:
            draw.text((PAD + 28, ry), rl, font=font_body, fill=(6, 78, 59))
            ry += 42

        tip_txt = str(card_data.get("exam_tip") or "Сохрани карточку и проверь себя на этом термине в боте.")
        y += r_h + 26
        draw.rounded_rectangle([PAD, y, W - PAD, H - 108], radius=22, fill=card_white, outline=border_col, width=2)
        ty = y + 28
        for tl in _wrap_text(draw, f"Запомни: {tip_txt}", font_body_sm, W - PAD * 2 - 56, max_lines=3):
            draw.text((PAD + 28, ty), tl, font=font_body_sm, fill=ink_body)
            ty += 38

    elif format_type == "blitz_poll":
        _draw_pill(draw, PAD, 118, f"СХЕМА + БЛИЦ  •  {module_title.upper()}", font_badge, soft_bg, dark_accent)

        t_lines = _wrap_text(draw, term_name, font_title, W - PAD * 2, max_lines=2)
        y = 190
        for line in t_lines:
            lw = min(_text_width(draw, line, font_title), W - PAD * 2)
            draw.rectangle([PAD, y + 44, PAD + lw + 8, y + 60], fill=soft_bg)
            draw.text((PAD, y), line, font=font_title, fill=ink_dark)
            y += 72

        y += 18
        # Две колонки сравнения (чтобы карточка давала реальную шпаргалку перед опросом)
        col_w = (W - PAD * 2 - 24) // 2
        col_h = 830
        left_title = str(card_data.get("card_left_title") or "ЧТО ВХОДИТ")
        left_items = card_data.get("card_left_items") or ["Ключевой признак 1", "Ключевой признак 2", "Ключевой признак 3"]
        right_title = str(card_data.get("card_right_title") or "С ЧЕМ ПУТАЮТ")
        right_items = card_data.get("card_right_items") or ["Смежное понятие 1", "Смежное понятие 2", "Частая ловушка"]

        # Левая колонка
        draw.rounded_rectangle([PAD, y, PAD + col_w, y + col_h], radius=24, fill=card_white, outline=accent, width=2)
        _draw_pill(draw, PAD + 22, y + 24, left_title[:24].upper(), font_badge, soft_bg, dark_accent, height=40, max_width=col_w - 44)
        ly = y + 92
        for item in left_items[:4]:
            draw.ellipse([PAD + 24, ly + 12, PAD + 36, ly + 24], fill=accent)
            for il in _wrap_text(draw, str(item), font_body_sm, col_w - 72, max_lines=3):
                draw.text((PAD + 48, ly), il, font=font_body_sm, fill=ink_dark)
                ly += 36
            ly += 24

        # Правая колонка
        rx = PAD + col_w + 24
        draw.rounded_rectangle([rx, y, rx + col_w, y + col_h], radius=24, fill=card_white, outline=border_col, width=2)
        _draw_pill(draw, rx + 22, y + 24, right_title[:24].upper(), font_badge, (241, 245, 249), ink_body, height=40, max_width=col_w - 44)
        ry = y + 92
        for item in right_items[:4]:
            draw.ellipse([rx + 24, ry + 12, rx + 36, ry + 24], fill=ink_muted)
            for il in _wrap_text(draw, str(item), font_body_sm, col_w - 72, max_lines=3):
                draw.text((rx + 48, ry), il, font=font_body_sm, fill=ink_body)
                ry += 36
            ry += 24

        y += col_h + 24
        draw.rounded_rectangle([PAD, y, W - PAD, H - 104], radius=20, fill=soft_bg)
        draw.text((PAD + 28, y + 26), "Проверь себя в опросе сразу под карточкой ↓", font=font_sec, fill=dark_accent)

    else:
        # Макет 1: «Новость -> Термин -> Экзамен» (4:5)
        _draw_pill(draw, PAD, 118, f"ИНФОПОВОД  •  {module_title.upper()}", font_badge, soft_bg, dark_accent)

        t_lines = _wrap_text(draw, term_name, font_title, W - PAD * 2, max_lines=2)
        y = 190
        for line in t_lines:
            lw = min(_text_width(draw, line, font_title), W - PAD * 2)
            draw.rectangle([PAD, y + 44, PAD + lw + 8, y + 60], fill=soft_bg)
            draw.text((PAD, y), line, font=font_title, fill=ink_dark)
            y += 72

        y += 18
        news_fact = str(card_data.get("card_news_fact") or card_data.get("card_subtitle") or "Актуальный инфоповод из российских СМИ")
        nf_lines = _wrap_text(draw, news_fact, font_body, W - PAD * 2 - 56, max_lines=4)
        nf_h = max(210, 100 + len(nf_lines) * 42)
        draw.rounded_rectangle([PAD, y, W - PAD, y + nf_h], radius=24, fill=card_white, outline=border_col, width=2)
        draw.text((PAD + 28, y + 24), "ЧТО ПРОИЗОШЛО", font=font_badge, fill=accent)
        ny = y + 66
        for nl in nf_lines:
            draw.text((PAD + 28, ny), nl, font=font_body, fill=ink_dark)
            ny += 42

        y += nf_h + 22
        term_def = str(term_obj.get("concept_explanation") or "").strip()
        if term_def:
            td_lines = _wrap_text(draw, term_def, font_body_sm, W - PAD * 2 - 56, max_lines=4)
            td_h = max(160, 88 + len(td_lines) * 38)
            draw.rounded_rectangle([PAD, y, W - PAD, y + td_h], radius=24, fill=card_white, outline=border_col, width=2)
            draw.text((PAD + 28, y + 22), "СУТЬ ТЕРМИНА ДЛЯ ЭКЗАМЕНА", font=font_badge, fill=ink_muted)
            ty = y + 60
            for tdl in td_lines:
                draw.text((PAD + 28, ty), tdl, font=font_body_sm, fill=ink_body)
                ty += 38
            y += td_h + 22

        bridge = str(card_data.get("card_exam_takeaway") or "Как использовать этот факт в заданиях 19 и 25 на ЕГЭ/ОГЭ")
        br_lines = _wrap_text(draw, bridge, font_body, W - PAD * 2 - 56, max_lines=5)
        br_h = max(220, min(H - 110 - y, 120 + len(br_lines) * 44))
        draw.rounded_rectangle([PAD, y, W - PAD, H - 110], radius=24, fill=ink_dark)
        _draw_pill(draw, PAD + 28, y + 26, "КАК ЗАБРАТЬ БАЛЛЫ НА ЭКЗАМЕНЕ", font_badge, accent, card_white, height=40)
        by = y + 92
        for bl in br_lines:
            draw.text((PAD + 28, by), bl, font=font_body, fill=(241, 245, 249))
            by += 44

    # Футер
    draw.line([(PAD, H - 84), (W - PAD, H - 84)], fill=border_col, width=2)
    draw.text((PAD, H - 62), "Сохрани, чтобы не потерять перед экзаменом", font=font_footer, fill=ink_muted)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def _fetch_single_rss(
    session: aiohttp.ClientSession, source_name: str, url: str
) -> List[Dict[str, str]]:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return []
            xml_bytes = await resp.read()
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        logger.debug(f"RSS {source_name} недоступен: {e}")
        return []

    out: List[Dict[str, str]] = []
    for item in root.findall(".//item")[:20]:
        title = _strip_html(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        desc = _strip_html(item.findtext("description") or "")
        if not title or not link:
            continue
        low_blob = f"{title} {desc}".lower()
        if any(bad in low_blob for bad in RSS_STOP_KEYWORDS):
            continue
        # Для Google News вытаскиваем реальное имя СМИ из title ("Заголовок - РБК")
        real_source = source_name
        if "Google News" in source_name and " - " in title:
            parts = title.rsplit(" - ", 1)
            if len(parts) == 2 and len(parts[1]) <= 40:
                title, real_source = parts[0].strip(), parts[1].strip()
        out.append(
            {
                "source": real_source,
                "title": title[:220],
                "summary": desc[:320],
                "url": link,
            }
        )
    return out


async def fetch_rss_news(
    exclude_urls: Set[str], max_items: int = 15
) -> List[Dict[str, str]]:
    """
    Параллельно собирает свежие новости через Google News RSS (не блокируется на Render),
    при необходимости добирая из прямых RSS-лент.
    """
    collected: List[Dict[str, str]] = []
    seen_urls = set(exclude_urls)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
    }
    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for feed_group in (GOOGLE_NEWS_RSS_FEEDS, DIRECT_RSS_FEEDS):
            batches = await asyncio.gather(
                *[_fetch_single_rss(session, name, url) for name, url in feed_group],
                return_exceptions=True,
            )
            for batch in batches:
                if not isinstance(batch, list):
                    continue
                for item in batch:
                    if item["url"] in seen_urls:
                        continue
                    seen_urls.add(item["url"])
                    collected.append(item)
                    if len(collected) >= max_items:
                        return collected
            if len(collected) >= 6:
                break
    return collected


async def fetch_news_image(news_url: str) -> Optional[bytes]:
    """Пытается вытянуть реальную обложку статьи (og:image) при достаточном качестве."""
    if not news_url or "news.google.com" in news_url:
        # Для редирект-ссылок Google News сначала пробуем пройти редирект
        pass
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
    }
    timeout = aiohttp.ClientTimeout(total=6)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(news_url, allow_redirects=True) as resp:
                if resp.status != 200 or "html" not in (resp.content_type or ""):
                    return None
                html_text = await resp.text(errors="ignore")
            m = re.search(
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                html_text,
                re.IGNORECASE,
            )
            if not m:
                m = re.search(
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                    html_text,
                    re.IGNORECASE,
                )
            if not m:
                return None
            img_url = html.unescape(m.group(1).strip())
            if not img_url.startswith("http"):
                return None
            async with session.get(img_url) as img_resp:
                if img_resp.status != 200:
                    return None
                ctype = (img_resp.content_type or "").lower()
                if not any(t in ctype for t in ("jpeg", "jpg", "png", "webp")):
                    return None
                raw = await img_resp.read()
                if not (20_000 <= len(raw) <= 5_000_000):
                    return None
                # Проверяем, что картинка не слишком мелкая
                im = Image.open(io.BytesIO(raw))
                if im.width < 500 or im.height < 300:
                    return None
                return raw
    except Exception:
        return None


async def _call_gemini_json(
    system_prompt: str,
    user_prompt: str,
    thinking_level: str = "LOW",
) -> Dict[str, Any]:
    if not GEMINI_API_KEY:
        raise RuntimeError("Не задан GEMINI_API_KEY в переменных окружения.")

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }
    base_payload: Dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.65,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel": thinking_level},
        },
    }

    last_err: Optional[Exception] = None
    for attempt in range(2):
        timeout = aiohttp.ClientTimeout(total=80, connect=15)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=base_payload) as resp:
                    if resp.status == 400:
                        err_txt = await resp.text()
                        if "thinking" in err_txt.lower():
                            base_payload["generationConfig"].pop("thinkingConfig", None)
                            async with session.post(url, headers=headers, json=base_payload) as retry_resp:
                                if retry_resp.status != 200:
                                    raise RuntimeError(
                                        f"Gemini API {retry_resp.status}: {await retry_resp.text()}"
                                    )
                                data = await retry_resp.json()
                        else:
                            raise RuntimeError(f"Gemini API 400: {err_txt}")
                    elif resp.status >= 500 and attempt == 0:
                        last_err = RuntimeError(f"Gemini API {resp.status}: {await resp.text()}")
                        await asyncio.sleep(2)
                        continue
                    elif resp.status != 200:
                        raise RuntimeError(f"Gemini API {resp.status}: {await resp.text()}")
                    else:
                        data = await resp.json()
            break
        except (asyncio.TimeoutError, aiohttp.ClientError) as net_err:
            last_err = net_err
            if attempt == 0:
                await asyncio.sleep(2)
                continue
            raise
    else:
        if last_err:
            raise last_err

    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Пустой ответ от Gemini API: {data}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    raw_text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    if raw_text.startswith("```"):
        raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.DOTALL).strip()
    return json.loads(raw_text)


def _pick_fresh_term(
    all_terms: List[Dict[str, Any]], recent_terms: Set[str]
) -> Dict[str, Any]:
    fresh = [t for t in all_terms if t.get("code") not in recent_terms]
    pool = fresh if fresh else all_terms
    return random.choice(pool)


async def generate_autopost_draft(
    catalog: Any,
    db: Any,
    preferred_format: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Собирает новости, сопоставляет с базой terms.json, генерирует пост и данные
    для вертикальной карточки (или берёт фото новости) и сохраняет черновик.
    """
    if not catalog.all_terms:
        raise RuntimeError("Каталог терминов пуст.")

    recent_terms, recent_urls = await asyncio.to_thread(
        db.get_recent_autopost_history, 30, 100
    )

    if preferred_format in FORMAT_TYPES:
        format_type = preferred_format
    else:
        weekday = datetime.now(TZ).weekday()
        schedule_map = {
            0: "news_term",
            1: "blitz_poll",
            2: "news_term",
            3: "traits_cheat",
            4: "news_term",
            5: "blitz_poll",
            6: "pitfall_compare",
        }
        format_type = schedule_map.get(weekday, "news_term")

    chosen_news: Optional[Dict[str, str]] = None
    chosen_term: Optional[Dict[str, Any]] = None
    rss_fallback_note = ""

    if format_type == "news_term":
        news_list = await fetch_rss_news(recent_urls, max_items=15)
        if not news_list:
            format_type = random.choice(["traits_cheat", "pitfall_compare"])
            rss_fallback_note = "RSS-ленты не вернули свежих новостей — сгенерирована карточка по теории."
        else:
            available = [t for t in catalog.all_terms if t.get("code") not in recent_terms]
            if not available:
                available = catalog.all_terms
            compact_terms = [
                f"{t['code']}|{t['term']}|{t.get('module_title', '')}"
                for t in available[:200]
            ]
            compact_news = [
                f"[{idx}] {n['source']}: {n['title']} — {n['summary']}"
                for idx, n in enumerate(news_list)
            ]
            match_sys = (
                "Ты методист ОГЭ и ЕГЭ по обществознанию. Выбери из списка ОДНУ самую понятную и свежую новость, "
                "которую можно чётко объяснить через один термин из базы учеников. "
                "Верни строго JSON: {\"news_index\": int, \"term_code\": str, \"bridge\": str}."
            )
            match_user = (
                "НОВОСТИ:\n"
                + "\n".join(compact_news)
                + "\n\nТЕРМИНЫ (код|название|раздел):\n"
                + "\n".join(compact_terms)
            )
            match_res = await _call_gemini_json(match_sys, match_user, thinking_level="LOW")
            n_idx = int(match_res.get("news_index", 0))
            chosen_news = news_list[n_idx] if 0 <= n_idx < len(news_list) else news_list[0]
            t_code = str(match_res.get("term_code", "")).strip()
            chosen_term = catalog.get_by_code(t_code) or _pick_fresh_term(catalog.all_terms, recent_terms)

    if chosen_term is None:
        chosen_term = _pick_fresh_term(catalog.all_terms, recent_terms)

    term_context = {
        "code": chosen_term.get("code", ""),
        "module": chosen_term.get("module_title", ""),
        "term": chosen_term.get("term", ""),
        "explanation": chosen_term.get("concept_explanation", ""),
        "traits": chosen_term.get("traits", []),
        "ege_examples": chosen_term.get("ege_context_and_examples", ""),
        "pitfalls": chosen_term.get("common_pitfalls", ""),
    }

    if format_type == "news_term" and chosen_news:
        gen_sys = (
            "Ты автор Telegram-канала «ОБЩАГА | ТЕРМИНЫ». Сделай пост по строгой 3-частной схеме:\n"
            "1) <b>Что случилось:</b> 1–2 предложения про реальный факт из новости (никаких выдуманных цифр).\n"
            "2) <b>Как это работает в теории:</b> объясни факт через выбранный термин из базы и 1–2 его признака.\n"
            "3) <b>В копилку для 2-й части:</b> готовая формулировка примера для заданий 19 или 25 ЕГЭ.\n"
            + ANTI_CLICHE_RULES
            + "\nВерни строго JSON:\n"
            '{"post_html": "текст поста (450-780 символов)", '
            '"card_news_fact": "суть новости для карточки (до 135 симв.)", '
            '"card_exam_takeaway": "главный экзаменационный вывод на карточку (до 150 симв.)"}'
        )
        gen_user = (
            f"НОВОСТЬ ({chosen_news['source']}): {chosen_news['title']}\n{chosen_news['summary']}\n\n"
            f"ТЕРМИН ИЗ БАЗЫ:\n{json.dumps(term_context, ensure_ascii=False)}"
        )
    elif format_type == "traits_cheat":
        gen_sys = (
            "Ты автор Telegram-канала «ОБЩАГА | ТЕРМИНЫ». Формат: «Шпаргалка: 3 признака для 18-го задания».\n"
            "В посте сразу дай простое объяснение термина и 3 точных признака из базы без воды.\n"
            + ANTI_CLICHE_RULES
            + "\nВерни строго JSON:\n"
            '{"post_html": "текст поста (450-750 символов)", '
            '"simple_def": "суть понятия в 1-2 простых фразах для карточки (до 150 симв.)", '
            '"card_points": ["признак 1 (до 70 симв.)", "признак 2 (до 70 симв.)", "признак 3 (до 70 симв.)"]}'
        )
        gen_user = f"ТЕРМИН ИЗ БАЗЫ:\n{json.dumps(term_context, ensure_ascii=False)}"
    elif format_type == "pitfall_compare":
        gen_sys = (
            "Ты автор Telegram-канала «ОБЩАГА | ТЕРМИНЫ». Формат: «Главная ловушка экзамена».\n"
            "Покажи конкретную ошибку из-за которой снимают баллы, и как правильно писать по критериям.\n"
            + ANTI_CLICHE_RULES
            + "\nВерни строго JSON:\n"
            '{"post_html": "текст поста (450-750 символов)", '
            '"wrong_way": "конкретная ошибка ученика (до 135 симв.)", '
            '"right_way": "как нужно по критериям ФИПИ (до 135 симв.)", '
            '"exam_tip": "короткий совет для 2-й части (до 120 симв.)"}'
        )
        gen_user = f"ТЕРМИН ИЗ БАЗЫ:\n{json.dumps(term_context, ensure_ascii=False)}"
    else:
        format_type = "blitz_poll"
        gen_sys = (
            "Ты автор Telegram-канала «ОБЩАГА | ТЕРМИНЫ». Формат: «Шпаргалка-различение + блиц-опрос».\n"
            "В самом посте ОБЯЗАТЕЛЬНО дай конкретное сравнение или список, на который опирается опрос ниже "
            "(никаких общих фраз про важность темы!).\n"
            + ANTI_CLICHE_RULES
            + "\nВерни строго JSON:\n"
            '{"post_html": "чёткая мини-шпаргалка перед опросом (400-680 символов)", '
            '"card_left_title": "заголовок левой колонки (до 22 симв.)", '
            '"card_left_items": ["пункт 1 (до 45 симв.)", "пункт 2", "пункт 3"], '
            '"card_right_title": "заголовок правой колонки (до 22 симв.)", '
            '"card_right_items": ["пункт 1 (до 45 симв.)", "пункт 2", "пункт 3"], '
            '"poll_question": "вопрос опроса (до 200 симв.)", '
            '"poll_options": ["вариант 1 (до 85 симв.)", "вариант 2", "вариант 3"], '
            '"correct_option_id": 0, '
            '"poll_explanation": "почему так (до 160 симв.)"}'
        )
        gen_user = f"ТЕРМИН ИЗ БАЗЫ:\n{json.dumps(term_context, ensure_ascii=False)}"

    gen_res = await _call_gemini_json(gen_sys, gen_user, thinking_level="LOW")
    post_html = str(gen_res.get("post_html") or "").strip()
    if not post_html:
        raise RuntimeError("Gemini вернула пустой текст поста.")

    draft_id = uuid.uuid4().hex[:12]
    payload: Dict[str, Any] = {
        "draft_id": draft_id,
        "format_type": format_type,
        "term_code": chosen_term["code"],
        "term_name": chosen_term["term"],
        "module_title": chosen_term.get("module_title", ""),
        "news_url": chosen_news["url"] if chosen_news else "",
        "news_source": chosen_news["source"] if chosen_news else "",
        "rss_fallback_note": rss_fallback_note,
        "post_html": post_html,
        "card_data": gen_res,
    }
    await asyncio.to_thread(db.save_autopost_draft, draft_id, payload)
    return payload
