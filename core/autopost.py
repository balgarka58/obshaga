"""
Модуль автономного ИИ-постинга для Telegram-канала «ОБЩАГА | ТЕРМИНЫ».
- Сбор свежих новостей из российских деловых и общественных RSS-лент.
- Сопоставление новостей с базой terms.json и генерация постов через Gemini API (gemini-4-hydrogen).
- Генерация 4 типов брендированных карточек-обложек (1200x630 PNG) на Pillow.
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

RSS_FEEDS: List[Tuple[str, str]] = [
    ("Коммерсантъ", "https://www.kommersant.ru/rss/news.xml"),
    ("Ведомости", "https://www.vedomosti.ru/rss/news"),
    ("ТАСС", "https://tass.ru/rss/v2.xml"),
    ("РИА Новости", "https://ria.ru/export/rss2/index.xml"),
]

RSS_STOP_KEYWORDS: Tuple[str, ...] = (
    "футбол", "хоккей", "теннис", "матч", "чемпионат", "олимпиад", "лига чемпионов",
    "погода", "синоптик", "гороскоп", "шоу-бизнес", "кинопремьера", "трейлер",
    "дтп", "пожар", "убийств", "скончал", "погиб", "взрыв", "теракт", "арестован за наркотики",
)

FORMAT_TYPES: Tuple[str, ...] = (
    "news_term",
    "traits_cheat",
    "pitfall_compare",
    "blitz_poll",
)

MODULE_PALETTES: List[Tuple[str, Tuple[int, int, int], Tuple[int, int, int]]] = [
    ("эконом", (16, 185, 129), (6, 78, 59)),      # Изумрудный
    ("прав", (99, 102, 241), (49, 46, 129)),       # Индиго
    ("полит", (239, 68, 68), (127, 29, 29)),       # Алый
    ("социол", (245, 158, 11), (120, 53, 15)),     # Янтарный
    ("человек", (20, 184, 166), (17, 94, 89)),     # Бирюзовый
]
DEFAULT_PALETTE: Tuple[Tuple[int, int, int], Tuple[int, int, int]] = (
    (59, 130, 246),
    (30, 58, 138),
)

ANTI_CLICHE_RULES = (
    "СТРОГИЕ ПРАВИЛА СТИЛЯ:\n"
    "1. Пиши живым, ясным языком для школьников 9–11 классов, без канцелярита и воды.\n"
    "2. ЗАПРЕЩЕНЫ шаблонные ИИ-фразы: 'В современном мире', 'Давайте разберёмся', "
    "'Не секрет, что', 'Таким образом', 'Подводя итог', 'Важно отметить', "
    "'Стоит подчеркнуть', 'В эпоху цифровизации', 'Каждый из нас сталкивался'.\n"
    "3. Опирайся ТОЛЬКО на факты из переданного термина (определение, признаки, ловушки) "
    "и самой новости. Ничего не выдумывай про законы и критерии ФИПИ.\n"
    "4. Используй HTML-теги Telegram (<b>, <i>) умеренно, максимум 2–3 эмодзи на весь пост.\n"
    "5. Длина текста поста — строго 400–720 символов (чтобы целиком влезал в подпись к картинке)."
)


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _get_module_colors(module_title: str) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    low = (module_title or "").lower()
    for key, accent, dark_accent in MODULE_PALETTES:
        if key in low:
            return accent, dark_accent
    return DEFAULT_PALETTE


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "C:\\Windows\\Fonts\\arialbd.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "C:\\Windows\\Fonts\\arial.ttf",
        ]
    )
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


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
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            cur = test
        else:
            lines.append(cur)
            cur = w
            if len(lines) >= max_lines:
                break
    if len(lines) < max_lines:
        lines.append(cur)
    elif cur and lines:
        # Если текст не влез, аккуратно обрезаем последнюю строку
        last = lines[-1]
        while len(last) > 3:
            bbox = draw.textbbox((0, 0), last + "…", font=font)
            if bbox[2] - bbox[0] <= max_width:
                lines[-1] = last + "…"
                break
            last = last[:-1].rstrip()
    return lines[:max_lines]


def render_post_card_png(
    format_type: str,
    term_obj: Dict[str, Any],
    card_data: Dict[str, Any],
) -> bytes:
    """Генерирует брендированную PNG-карточку 1200x630 по одному из 4 макетов."""
    W, H = 1200, 630
    module_title = term_obj.get("module_title", "Обществознание")
    term_name = term_obj.get("term", "Термин")
    term_code = term_obj.get("code", "")
    accent, dark_accent = _get_module_colors(module_title)

    font_badge = _load_font(22, bold=True)
    font_title = _load_font(48, bold=True)
    font_title_sm = _load_font(38, bold=True)
    font_sub = _load_font(27, bold=False)
    font_bold_md = _load_font(26, bold=True)
    font_footer = _load_font(22, bold=False)

    if format_type == "traits_cheat":
        # Макет 2: Светлая шпаргалка «3 признака для задания 18»
        img = Image.new("RGB", (W, H), (247, 248, 251))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, 22, H], fill=accent)

        badge_text = f"3 ПРИЗНАКА  •  {module_title.upper()}"
        draw.rounded_rectangle([64, 44, 64 + min(900, len(badge_text) * 15 + 44), 88], radius=22, fill=accent)
        draw.text((86, 53), badge_text[:55], font=font_badge, fill=(255, 255, 255))

        t_lines = _wrap_text(draw, term_name, font_title, W - 130, max_lines=2)
        y = 112
        for line in t_lines:
            draw.text((64, y), line, font=font_title, fill=(17, 24, 39))
            y += 56

        points = card_data.get("card_points") or []
        if not isinstance(points, list) or not points:
            points = [
                "Ключевой родовой признак понятия",
                "Отличительная характеристика из КИМ",
                "Практическое проявление в жизни",
            ]
        y_box = max(y + 18, 232)
        box_h = 96
        for idx, pt in enumerate(points[:3]):
            draw.rounded_rectangle(
                [64, y_box, W - 64, y_box + box_h],
                radius=18,
                fill=(255, 255, 255),
                outline=(226, 232, 240),
                width=2,
            )
            draw.ellipse([86, y_box + 26, 130, y_box + 70], fill=accent)
            draw.text((100, y_box + 34), str(idx + 1), font=font_bold_md, fill=(255, 255, 255))
            p_lines = _wrap_text(draw, str(pt), font_sub, W - 240, max_lines=2)
            py = y_box + (18 if len(p_lines) > 1 else 32)
            for pl in p_lines:
                draw.text((152, py), pl, font=font_sub, fill=(31, 41, 55))
                py += 32
            y_box += box_h + 14

        draw.text((64, H - 48), f"Термин №{term_code}  •  ОБЩАГА | ТЕРМИНЫ", font=font_footer, fill=(100, 116, 139))

    elif format_type == "pitfall_compare":
        # Макет 3: Сравнение «Как ошибаются vs Как правильно»
        img = Image.new("RGB", (W, H), (24, 24, 27))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, W, 12], fill=accent)

        badge_text = f"ЛОВУШКА ЭКЗАМЕНА  •  {module_title.upper()}"
        draw.rounded_rectangle([60, 40, 60 + min(900, len(badge_text) * 15 + 44), 84], radius=22, fill=dark_accent)
        draw.text((82, 49), badge_text[:55], font=font_badge, fill=(255, 255, 255))

        t_lines = _wrap_text(draw, term_name, font_title_sm, W - 120, max_lines=2)
        y = 108
        for line in t_lines:
            draw.text((60, y), line, font=font_title_sm, fill=(250, 250, 250))
            y += 46

        box_y = max(y + 24, 210)
        box_w = 524
        box_h = 330

        # Левая плашка — ошибка
        draw.rounded_rectangle(
            [60, box_y, 60 + box_w, box_y + box_h],
            radius=22,
            fill=(48, 24, 28),
            outline=(239, 68, 68),
            width=2,
        )
        draw.text((88, box_y + 26), "КАК ОШИБАЮТСЯ", font=font_bold_md, fill=(252, 165, 165))
        wrong_txt = str(card_data.get("wrong_way") or "Путают термин со смежным понятием и теряют баллы.")
        wy = box_y + 78
        for wl in _wrap_text(draw, wrong_txt, font_sub, box_w - 56, max_lines=6):
            draw.text((88, wy), wl, font=font_sub, fill=(244, 244, 245))
            wy += 36

        # Правая плашка — правильно
        rx = 60 + box_w + 32
        draw.rounded_rectangle(
            [rx, box_y, rx + box_w, box_y + box_h],
            radius=22,
            fill=(20, 44, 35),
            outline=(16, 185, 129),
            width=2,
        )
        draw.text((rx + 28, box_y + 26), "КАК ПО КРИТЕРИЯМ", font=font_bold_md, fill=(110, 231, 183))
        right_txt = str(card_data.get("right_way") or "Выделяют 3 точных признака и проверяют контекст.")
        ry = box_y + 78
        for rl in _wrap_text(draw, right_txt, font_sub, box_w - 56, max_lines=6):
            draw.text((rx + 28, ry), rl, font=font_sub, fill=(244, 244, 245))
            ry += 36

        draw.text((60, H - 48), f"Термин №{term_code}  •  ОБЩАГА | ТЕРМИНЫ", font=font_footer, fill=(161, 161, 170))

    elif format_type == "blitz_poll":
        # Макет 4: Блиц-проверка
        img = Image.new("RGB", (W, H), (17, 24, 39))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, W, 14], fill=accent)

        badge_text = f"БЛИЦ-ПРОВЕРКА  •  {module_title.upper()}"
        draw.rounded_rectangle([64, 52, 64 + min(900, len(badge_text) * 15 + 44), 96], radius=22, fill=accent)
        draw.text((86, 61), badge_text[:55], font=font_badge, fill=(255, 255, 255))

        t_lines = _wrap_text(draw, term_name, font_title, W - 128, max_lines=2)
        y = 148
        for line in t_lines:
            draw.text((64, y), line, font=font_title, fill=(255, 255, 255))
            y += 60

        hook = str(card_data.get("card_subtitle") or "Проверь себя в опросе ниже — большинство здесь ошибается.")
        draw.rounded_rectangle(
            [64, 310, W - 64, 495],
            radius=24,
            fill=(31, 41, 55),
            outline=accent,
            width=2,
        )
        hy = 342
        for hl in _wrap_text(draw, hook, font_sub, W - 180, max_lines=3):
            draw.text((92, hy), hl, font=font_sub, fill=(229, 231, 235))
            hy += 38

        draw.text((64, H - 56), f"Термин №{term_code}  •  ОБЩАГА | ТЕРМИНЫ", font=font_footer, fill=(156, 163, 175))

    else:
        # Макет 1 (default): «Новость -> Термин»
        img = Image.new("RGB", (W, H), (15, 23, 42))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, W, 14], fill=accent)

        badge_text = f"ИНФОПОВОД  •  {module_title.upper()}"
        draw.rounded_rectangle([64, 48, 64 + min(900, len(badge_text) * 15 + 44), 92], radius=22, fill=accent)
        draw.text((86, 57), badge_text[:55], font=font_badge, fill=(255, 255, 255))

        t_lines = _wrap_text(draw, term_name, font_title, W - 128, max_lines=2)
        y = 124
        for line in t_lines:
            draw.text((64, y), line, font=font_title, fill=(248, 250, 252))
            y += 58

        bridge = str(card_data.get("card_subtitle") or "Как реальная новость работает на экзамене")
        draw.rounded_rectangle(
            [64, 286, W - 64, 514],
            radius=24,
            fill=(30, 41, 59),
            outline=(51, 65, 85),
            width=2,
        )
        draw.text((92, 310), "СВЯЗЬ С ЭКЗАМЕНОМ", font=font_badge, fill=accent)
        by = 352
        for bl in _wrap_text(draw, bridge, font_sub, W - 184, max_lines=3):
            draw.text((92, by), bl, font=font_sub, fill=(226, 232, 240))
            by += 38

        draw.text((64, H - 52), f"Термин №{term_code}  •  ОБЩАГА | ТЕРМИНЫ", font=font_footer, fill=(148, 163, 184))

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
        out.append(
            {
                "source": source_name,
                "title": title[:220],
                "summary": desc[:320],
                "url": link,
            }
        )
    return out


async def fetch_rss_news(
    exclude_urls: Set[str], max_items: int = 15
) -> List[Dict[str, str]]:
    """Параллельно собирает свежие новости по экономике, обществу и праву из RSS."""
    collected: List[Dict[str, str]] = []
    seen_urls = set(exclude_urls)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ObshagaBot/1.0)"}
    timeout = aiohttp.ClientTimeout(total=8)

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        batches = await asyncio.gather(
            *[_fetch_single_rss(session, name, url) for name, url in RSS_FEEDS],
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
    return collected


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
            "temperature": 0.7,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel": thinking_level},
        },
    }

    timeout = aiohttp.ClientTimeout(total=90, connect=15)
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
            elif resp.status != 200:
                raise RuntimeError(f"Gemini API {resp.status}: {await resp.text()}")
            else:
                data = await resp.json()

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
    Собирает свежие новости (если нужен новостной формат), подбирает термин из terms.json,
    генерирует текст поста через Gemini и возвращает словарь черновика.
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

    if format_type == "news_term":
        news_list = await fetch_rss_news(recent_urls, max_items=15)
        if not news_list:
            format_type = random.choice(["traits_cheat", "pitfall_compare"])
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
                "Ты методист ОГЭ и ЕГЭ по обществознанию. Выбери одну новость из списка, "
                "которая лучше всего иллюстрирует один термин из базы учеников. "
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
            if 0 <= n_idx < len(news_list):
                chosen_news = news_list[n_idx]
            else:
                chosen_news = news_list[0]
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
            "Ты автор Telegram-канала «ОБЩАГА | ТЕРМИНЫ». Формат поста: «Новость -> Термин -> Вывод для ЕГЭ».\n"
            + ANTI_CLICHE_RULES
            + "\nВерни строго JSON:\n"
            '{"post_html": "текст поста (400-700 символов)", '
            '"card_subtitle": "короткая фраза для карточки (до 95 символов)"}'
        )
        gen_user = (
            f"НОВОСТЬ ({chosen_news['source']}): {chosen_news['title']}\n{chosen_news['summary']}\n\n"
            f"ТЕРМИН ИЗ БАЗЫ:\n{json.dumps(term_context, ensure_ascii=False)}"
        )
    elif format_type == "traits_cheat":
        gen_sys = (
            "Ты автор Telegram-канала «ОБЩАГА | ТЕРМИНЫ». Формат поста: «С канцелярского на человеческий + 3 признака для 18 задания ЕГЭ».\n"
            + ANTI_CLICHE_RULES
            + "\nВерни строго JSON:\n"
            '{"post_html": "текст поста (400-700 символов)", '
            '"card_points": ["признак 1 (до 65 симв.)", "признак 2 (до 65 симв.)", "признак 3 (до 65 симв.)"]}'
        )
        gen_user = f"ТЕРМИН ИЗ БАЗЫ:\n{json.dumps(term_context, ensure_ascii=False)}"
    elif format_type == "pitfall_compare":
        gen_sys = (
            "Ты автор Telegram-канала «ОБЩАГА | ТЕРМИНЫ». Формат поста: «Главная ловушка на экзамене» (сравни частую ошибку и верный подход).\n"
            + ANTI_CLICHE_RULES
            + "\nВерни строго JSON:\n"
            '{"post_html": "текст поста (400-700 символов)", '
            '"wrong_way": "коротко как ошибаются (до 110 симв.)", '
            '"right_way": "коротко как правильно (до 110 симв.)"}'
        )
        gen_user = f"ТЕРМИН ИЗ БАЗЫ:\n{json.dumps(term_context, ensure_ascii=False)}"
    else:
        format_type = "blitz_poll"
        gen_sys = (
            "Ты автор Telegram-канала «ОБЩАГА | ТЕРМИНЫ». Формат поста: «Блиц-капкан» — короткая подводка + нативный Telegram-опрос (quiz) с подвохом по термину.\n"
            + ANTI_CLICHE_RULES
            + "\nВерни строго JSON:\n"
            '{"post_html": "короткая подводка перед опросом (300-550 символов)", '
            '"card_subtitle": "короткий зацеп на картинку (до 95 симв.)", '
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
        "post_html": post_html,
        "card_data": gen_res,
    }
    await asyncio.to_thread(db.save_autopost_draft, draft_id, payload)
    return payload
