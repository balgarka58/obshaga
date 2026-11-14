"""
Модуль автономного ИИ-постинга для Telegram-канала «ОБЩАГА | ТЕРМИНЫ» (v3).
- Сбор свежих новостей через Google News RSS + прямые RSS-ленты.
- Сопоставление новостей с базой terms.json и генерация постов через Gemini API (gemini-4-hydrogen).
- Поиск реальных фотографий без вотермарок через бесплатный Wikimedia Commons API.
- Нативная вёрстка Telegram HTML с <blockquote> и жёстким анти-клише промптом.
"""
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

WIKIMEDIA_HEADERS: Dict[str, str] = {
    "User-Agent": "ObshagaEduBot/1.0 (https://t.me/obshaga; educational exam prep)",
    "Api-User-Agent": "ObshagaEduBot/1.0 (https://t.me/obshaga; educational exam prep)",
}

ANTI_CLICHE_RULES = (
    "СТРОГИЕ ПРАВИЛА СТИЛЯ И ВЁРСТКИ:\n"
    "1. Пиши чётко, живо и по делу — как сильный репетитор для 9–11 классов, без канцелярита и пафоса.\n"
    "2. КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНЫ нейросетевые штампы и лирика: 'это не просто X, а/это Y', "
    "'живёт в оболочке', 'в современном мире', 'давайте разберёмся', 'давайте посмотрим', "
    "'не секрет, что', 'таким образом', 'подводя итог', 'важно отметить', 'стоит подчеркнуть', 'каждый из нас'.\n"
    "3. Опирайся ТОЛЬКО на реальные факты из переданной новости и поля термина (определение, признаки, ловушки). "
    "Никаких выдуманных цифр, дат, статей законов и несуществующих критериев ФИПИ.\n"
    "4. Используй только разрешённые HTML-теги Telegram: <b>, <i>, <blockquote>. "
    "Блок <blockquote> используй ровно 1 раз в конце поста — для готового примера во 2-ю часть или главного правила для экзамена.\n"
    "5. Не ставь лишних эмодзи — максимум 2–3 на весь пост. Списки оформляй через '— '.\n"
    "6. Длина поста — строго 450–780 символов (должен влезать в подпись к фото и читаться за 20 секунд)."
)


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


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
    """Параллельно собирает свежие новости через Google News RSS и прямые RSS-ленты."""
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


async def find_wikimedia_image_url(queries: List[str]) -> Optional[str]:
    """
    Ищет реальное растровое фото (JPEG/PNG) без вотермарок через Wikimedia Commons API.
    Пробует список запросов по очереди и возвращает прямую ссылку на превью ~1200px.
    """
    clean_queries = [str(q).strip() for q in (queries or []) if str(q).strip()]
    if not clean_queries:
        return None

    api_url = "https://commons.wikimedia.org/w/api.php"
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout, headers=WIKIMEDIA_HEADERS) as session:
        for q in clean_queries[:3]:
            params = {
                "action": "query",
                "generator": "search",
                "gsrnamespace": "6",
                "gsrsearch": f"{q} filetype:bitmap",
                "gsrlimit": "6",
                "prop": "imageinfo",
                "iiprop": "url|mime|size",
                "iiurlwidth": "1200",
                "format": "json",
            }
            try:
                async with session.get(api_url, params=params) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
            except Exception as e:
                logger.debug(f"Wikimedia search error ({q}): {e}")
                continue

            pages = list((data.get("query") or {}).get("pages", {}).values())
            pages.sort(key=lambda p: p.get("index", 9999))
            for p in pages:
                infos = p.get("imageinfo") or []
                if not infos:
                    continue
                info = infos[0]
                mime = (info.get("mime") or "").lower()
                if mime not in ("image/jpeg", "image/png"):
                    continue
                w = int(info.get("width") or 0)
                h = int(info.get("height") or 0)
                if w < 500 or h < 350:
                    continue
                # Слишком вытянутые баннеры/схемы отсекаем
                ratio = w / max(1, h)
                if ratio > 2.6 or ratio < 0.45:
                    continue
                thumb_url = info.get("thumburl") or info.get("url")
                if thumb_url:
                    return str(thumb_url)
    return None


async def download_image_bytes(image_url: str) -> Optional[bytes]:
    """Скачивает найденное изображение по прямой ссылке."""
    if not image_url:
        return None
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=WIKIMEDIA_HEADERS) as session:
            async with session.get(image_url) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.read()
                if 10_000 <= len(raw) <= 6_000_000:
                    return raw
    except Exception as e:
        logger.debug(f"Image download error: {e}")
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
            "temperature": 0.6,
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
    Собирает новости, сопоставляет с базой terms.json, генерирует пост с <blockquote>
    через Gemini и подбирает реальное фото по теме на Wikimedia Commons.
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
            rss_fallback_note = "RSS-ленты не вернули свежих новостей — сгенерирован разбор по теории."
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
                "Ты методист ОГЭ и ЕГЭ по обществознанию. Выбери из списка ОДНУ самую наглядную и свежую новость, "
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

    img_query_hint = (
        '"image_queries": ["конкретный запрос на английском 2-3 слова для фото на Wikimedia Commons '
        '(например: Bank of Russia building, Russian ruble banknotes, State Duma Moscow, '
        'justice scales, university lecture, stock exchange)", "более общий запрос на английском 1-2 слова"]'
    )

    if format_type == "news_term" and chosen_news:
        gen_sys = (
            "Ты автор Telegram-канала «ОБЩАГА | ТЕРМИНЫ». Напиши пост строго по 3 блокам:\n"
            "1) <b>Что случилось:</b> 1–2 предложения про реальный факт из новости.\n"
            "2) <b>Как это работает в теории:</b> короткий разбор через выбранный термин из базы и 1–2 его признака.\n"
            "3) <blockquote><b>Пример для 2-й части:</b> готовая формулировка для заданий 19 или 25 ЕГЭ.</blockquote>\n"
            + ANTI_CLICHE_RULES
            + "\nВерни строго JSON:\n"
            '{"post_html": "текст поста (450-780 символов)", ' + img_query_hint + "}"
        )
        gen_user = (
            f"НОВОСТЬ ({chosen_news['source']}): {chosen_news['title']}\n{chosen_news['summary']}\n\n"
            f"ТЕРМИН ИЗ БАЗЫ:\n{json.dumps(term_context, ensure_ascii=False)}"
        )
    elif format_type == "traits_cheat":
        gen_sys = (
            "Ты автор Telegram-канала «ОБЩАГА | ТЕРМИНЫ». Формат: «3 признака для 18-го задания».\n"
            "Структура:\n"
            "1) Жирный заголовок термина и 1–2 предложения простым языком (без клише!).\n"
            "2) Список из 3 точных признаков (— ...) на основе поля traits.\n"
            "3) <blockquote><b>На чём ловят на экзамене:</b> 1–2 предложения из pitfalls.</blockquote>\n"
            + ANTI_CLICHE_RULES
            + "\nВерни строго JSON:\n"
            '{"post_html": "текст поста (450-750 символов)", ' + img_query_hint + "}"
        )
        gen_user = f"ТЕРМИН ИЗ БАЗЫ:\n{json.dumps(term_context, ensure_ascii=False)}"
    elif format_type == "pitfall_compare":
        gen_sys = (
            "Ты автор Telegram-канала «ОБЩАГА | ТЕРМИНЫ». Формат: «Главная ловушка экзамена».\n"
            "Структура:\n"
            "1) Название термина и суть частой ошибки выпускников.\n"
            "2) Чёткое сравнение:\n— <b>Как ошибаются:</b> ...\n— <b>Как нужно по критериям:</b> ...\n"
            "3) <blockquote><b>Правило для ЕГЭ/ОГЭ:</b> 1 короткий вывод, который спасёт балл.</blockquote>\n"
            + ANTI_CLICHE_RULES
            + "\nВерни строго JSON:\n"
            '{"post_html": "текст поста (450-750 символов)", ' + img_query_hint + "}"
        )
        gen_user = f"ТЕРМИН ИЗ БАЗЫ:\n{json.dumps(term_context, ensure_ascii=False)}"
    else:
        format_type = "blitz_poll"
        gen_sys = (
            "Ты автор Telegram-канала «ОБЩАГА | ТЕРМИНЫ». Формат: «Мини-шпаргалка + блиц-опрос».\n"
            "Структура поста:\n"
            "1) Сразу дай короткую шпаргалку со списками/различением, по которой идёт опрос ниже "
            "(никаких общих фраз про важность темы!).\n"
            "2) <blockquote><b>Подсказка перед опросом:</b> 1 точный критерий разграничения.</blockquote>\n"
            + ANTI_CLICHE_RULES
            + "\nВерни строго JSON:\n"
            '{"post_html": "мини-шпаргалка перед опросом (450-720 символов)", '
            '"poll_question": "вопрос опроса (до 200 симв.)", '
            '"poll_options": ["вариант 1 (до 85 симв.)", "вариант 2", "вариант 3"], '
            '"correct_option_id": 0, '
            '"poll_explanation": "почему так (до 160 симв.)", '
            + img_query_hint + "}"
        )
        gen_user = f"ТЕРМИН ИЗ БАЗЫ:\n{json.dumps(term_context, ensure_ascii=False)}"

    gen_res = await _call_gemini_json(gen_sys, gen_user, thinking_level="LOW")
    post_html = str(gen_res.get("post_html") or "").strip()
    if not post_html:
        raise RuntimeError("Gemini вернула пустой текст поста.")

    image_queries = gen_res.get("image_queries") or []
    if not isinstance(image_queries, list):
        image_queries = [str(image_queries)]
    image_url = await find_wikimedia_image_url(image_queries)

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
        "image_url": image_url or "",
        "post_html": post_html,
        "card_data": gen_res,
    }
    await asyncio.to_thread(db.save_autopost_draft, draft_id, payload)
    return payload
