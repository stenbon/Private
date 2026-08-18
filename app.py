
"""
app.py — автоматическая публикация статей в WordPress для Яндекс Дзен.
Пайплайн: Google Sheets (тема) -> Gemini 2.5 Flash с Google Search grounding
(текст + встроенный факт-чек через веб-поиск) -> fal.ai FLUX.1 schnell
(обложка + 3 иллюстрации) -> мультик из листа "Мультики" -> WordPress
(REST API, Application Password) -> RSS -> Дзен.

12.08.2026: написан заново для запуска ПРЯМО НА СЕРВЕРЕ (000l.ru) через уже
существующий системный cron (crontab -l показал 3 записи на app.py --article
1/2/3, ссылавшиеся на несуществующий файл — этот файл реализует то, что,
судя по всему, задумывалось изначально). Отличия от параллельной попытки
через GitHub Actions (dzen_publisher.py в этом же репозитории):
- LLM — Gemini 2.5 Flash с grounding (google_search), а не groq/compound.
  Причина: groq/compound на этом аккаунте упирается в 413 request_too_large
  независимо от max_tokens (проверено вплоть до max_tokens=2000) — похоже,
  тариф/квота аккаунта Groq не тянет служебный расход токенов на встроенный
  поиск. Бесплатный тариф Gemini 2.5 Flash: 250 запросов/день, grounding —
  5000 запросов/месяц бесплатно; при 3 статьях/день с 2-3 поисками на
  факт-чек это ~270 запросов/месяц — на два порядка меньше лимита.
- Запускается НЕ через GitHub Actions, а системным cron прямо на сервере —
  значит публикация полностью независима от компьютера пользователя И от
  GitHub Secrets/сети песочницы. Секреты читаются из локального .env файла
  рядом со скриптом (см. .env.example).
- Логика картинок (fal.ai FLUX.1 schnell, обложка + 3 иллюстрации) и
  мультиков (лист "Мультики", обёртка wp:html + отдельный wp:video блок)
  идентична dzen_publisher.py — методология не менялась, менялся только
  генератор текста.

Требования:
    pip install google-genai requests python-dotenv gspread google-auth

Настройка .env (в той же папке, что и этот файл):
    WP_URL, WP_USER, WP_APP_PASS, WP_CATEGORY, GEMINI_API_KEY, FAL_API_KEY,
    и ЛИБО GOOGLE_CREDENTIALS (JSON сервис-аккаунта одной строкой),
    ЛИБО GOOGLE_CREDENTIALS_FILE (путь к .json файлу — проще для сервера).

Запуск: python3 app.py --article 1   (аргумент только для логов/различения
    трёх дневных слотов в выводе cron, на выбор темы не влияет — тема всегда
    берётся как первая незаполненная строка в Google Sheets).
"""

import os
import re
import io
import csv
import sys
import json
import time
import argparse
import urllib.parse
import requests
import hashlib
from datetime import datetime
from dotenv import load_dotenv
from google import genai
from google.genai import types
from anthropic import Anthropic
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

# ─── Конфигурация ────────────────────────────────────────────────────────────

WP_URL          = os.getenv("WP_URL", "").rstrip("/")
WP_USER         = os.getenv("WP_USER")
WP_APP_PASS     = os.getenv("WP_APP_PASS")
WP_CATEGORY     = int(os.getenv("WP_CATEGORY", "1"))
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL    = "gemini-3.5-flash"
FAL_API_KEY     = os.getenv("FAL_API_KEY")
# 14.08.2026: фактчек через Claude Haiku + web_search (НЕ Sonnet — тот использовался
# для полной генерации статьи и стоил ~$3/ночь, отсюда решение отключить 12.08.2026).
# Haiku применяется ТОЛЬКО к узкому шагу проверки цифр/фактов, не к генерации —
# по оценке в разы дешевле (центы/день на объёме 3 статьи/день). Если ключ не задан
# в .env — фактчек тихо пропускается (см. self_check_facts_haiku ниже), пайплайн
# не падает, но публикует без проверки, как раньше.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_HAIKU_MODEL = "claude-haiku-4-5-20251001"

SHEET_ID     = "1d8VS3BmMAZUWCXG0Ha2I-R1b7gdXiVEO_p8RssyaXME"
MULTIKI_SHEET_NAME = "Мультики"

parser = argparse.ArgumentParser()
parser.add_argument("--article", type=int, default=0,
                     help="Только для логов/различения слотов cron (01:00/05:00/19:00 МСК) — на выбор темы не влияет.")
ARGS, _ = parser.parse_known_args()

AUTHOR_BANNER = (
    '<img src="https://000l.ru/wp-content/uploads/2026/06/'
    'ChatGPT-Image-5-июн.-2026-г.-19_59_37.png" '
    'alt="Иллюстрация: искусственный интеллект в повседневных задачах" '
    'title="Иллюстрация: искусственный интеллект в повседневных задачах" '
    'style="width:100%;display:block;margin:20px 0;" />'
)

wp_auth      = (WP_USER, WP_APP_PASS)
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
import os as _os_vertex
_os_vertex.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", _os_vertex.getenv("GOOGLE_CREDENTIALS_FILE", ""))
VERTEX_PROJECT_ID = "gen-lang-client-0706505662"
VERTEX_LOCATION = "us-central1"
vertex_client = genai.Client(vertexai=True, project=VERTEX_PROJECT_ID, location=VERTEX_LOCATION)


def _gemini_complete(system_prompt, user_content, max_tokens=8000):
    """Единая обёртка над Gemini 3.5 Flash БЕЗ grounding (google_search)
    через Interactions API. ЧЕСТНО: обычная генерация по знаниям модели,
    БЕЗ реального веб-поиска — см. причину ниже.

    12.08.2026, хронология решений:
    1) groq/compound — 413 request_too_large независимо от max_tokens,
       похоже квота/тариф аккаунта не тянет встроенный поиск.
    2) Gemini 2.5 Flash — модель отключена для новых ключей (404).
    3) Gemini 3.5 Flash + google_search (grounding) — 429 при любом вызове
       с tools=[google_search]; выяснилось, что grounding требует ОТДЕЛЬНО
       активного (не просто "привязанного") биллинг-аккаунта Google Cloud.
       Первая попытка это включить не прошла (карта не прошла авторизацию
       нового биллинг-аккаунта). Обычная генерация БЕЗ grounding на том же
       ключе работает нормально (бесплатный тариф).
    4) Вариант вернуться на Claude Sonnet 5 + web_search (проверенная 2
       месяца схема, см. _CLAUDE_DIGEST.md в E:\ЯД ФГ 1.0) — отклонён
       Владимиром по цене (~$3/ночь на Anthropic API).

    ИТОГ: grounding убран. generate_article()/self_check_facts() ниже
    переписаны с учётом того, что реального веб-поиска для фактчека
    больше НЕТ — вместо липового "проверено" модель прямо просит не
    придумывать конкретные цифры, которые не может подтвердить."""
    last_err = None
    for attempt in range(3):
        try:
            interaction = gemini_client.interactions.create(
                model=GEMINI_MODEL,
                system_instruction=system_prompt,
                input=user_content,
                generation_config={
                    "max_output_tokens": min(max_tokens, 65536),
                    # 13.08.2026: gemini-3.5-flash — think-модель, по умолчанию
                    # тратит бюджет ответа на видимые рассуждения-черновик
                    # ("Section 4:", "Total Word Count check... Perfect!" и т.п.)
                    # вместо готового текста, из-за чего парсинг тегов падает.
                    # thinking_level="low" резко снижает этот шум.
                    "thinking_level": "low",
                },
                store=False,
            )
            return interaction.output_text or ""
        except Exception as e:
            last_err = e
            msg = str(e)
            is_quota = "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()
            if is_quota and attempt < 2:
                print(f"[gemini] похоже на лимит квоты/RPM ({msg[:150]}), "
                      f"повтор через 25с (попытка {attempt + 2}/3)...")
                time.sleep(25)
                continue
            raise
    raise last_err


# ─── Google Sheets ────────────────────────────────────────────────────────────

def get_sheets_client():
    """Поддерживает два способа передать сервис-аккаунт: GOOGLE_CREDENTIALS
    (JSON одной строкой в .env, как в GitHub Secrets) ИЛИ GOOGLE_CREDENTIALS_FILE
    (путь к .json файлу — проще для локального запуска на сервере, не нужно
    экранировать JSON внутри .env)."""
    creds_json = os.getenv("GOOGLE_CREDENTIALS")
    creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE")
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    if creds_file:
        creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
    elif creds_json:
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    else:
        raise RuntimeError("Не задан ни GOOGLE_CREDENTIALS, ни GOOGLE_CREDENTIALS_FILE в .env")
    return gspread.authorize(creds)


def get_next_topic():
    """Возвращает (тема, номер_строки) для первой незаполненной строки Статус"""
    gc = get_sheets_client()
    ws = gc.open_by_key(SHEET_ID).sheet1
    rows = ws.get_all_values()
    for i, row in enumerate(rows[1:], start=2):   # строка 1 — заголовки
        topic  = row[0].strip() if len(row) > 0 else ""
        status = row[1].strip() if len(row) > 1 else ""
        if topic and not status:
            return topic, i
    return None, None


def mark_published(row_index, url):
    """Записывает 'Опубликовано' и URL поста в колонку Статус"""
    gc = get_sheets_client()
    ws = gc.open_by_key(SHEET_ID).sheet1
    ws.update_cell(row_index, 2, f"Опубликовано: {url}")


# ─── Вспомогательные функции ─────────────────────────────────────────────────

def make_slug(title):
    translit = {
        'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo',
        'ж':'zh','з':'z','и':'i','й':'y','к':'k','л':'l','м':'m',
        'н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u',
        'ф':'f','х':'kh','ц':'ts','ч':'ch','ш':'sh','щ':'sch',
        'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
    }
    result = ''
    for ch in title.lower():
        result += translit.get(ch, ch)
    result = re.sub(r'[^a-z0-9]+', '-', result)
    return result.strip('-')[:60]


FOREIGN_SCRIPT_PATTERN = re.compile(
    "[" +
    "一-鿿" +   # CJK Unified Ideographs
    "㐀-䶿" +   # CJK Extension A
    "豈-﫿" +   # CJK Compatibility Ideographs
    "぀-ヿ" +   # Hiragana + Katakana
    "가-힣" +   # Hangul Syllables
    "]+"
)


def strip_foreign_scripts(text):
    """Удаляет случайные вкрапления китайских/японских/корейских иероглифов,
    которые модель иногда подмешивает в кириллический текст (наблюдалось у groq/compound на
    практике: «ИИ<CJK> начал проникать...»). Без этой очистки такие
    артефакты нарушают правила Дзена и требуют ручной правки постфактум."""
    cleaned = FOREIGN_SCRIPT_PATTERN.sub('', text)
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
    cleaned = re.sub(r'[ \t]+([,.!?;:])', r'\1', cleaned)
    return cleaned


def insert_banner(html):
    """Вставляет баннер автора после 3-го раздела статьи (после 3-го заголовка
    <h2>/<h3> — первый раздел на канале идёт как <h2>, остальные как <h3>)."""
    parts = re.split(r'(<h[23]\b[^>]*>.*?</h[23]>)', html, flags=re.DOTALL)
    heading_count = 0
    result = []
    inserted = False
    for part in parts:
        result.append(part)
        if re.match(r'<h[23]\b', part) and not inserted:
            heading_count += 1
            if heading_count == 3:
                result.append(AUTHOR_BANNER)
                inserted = True
    if not inserted:
        result.append(AUTHOR_BANNER)
    return ''.join(result)


# ─── 1. Генерация текста статьи ──────────────────────────────────────────────

ARTICLE_SYSTEM = """Ты — опытный автор для Яндекс Дзен.
Пиши живым разговорным языком, от первого лица или нейтрально.

СТРУКТУРА (строго соблюдать):
- Вступление: 3 абзаца <p>
- 6 смысловых разделов на одном уровне вложенности:
  * ПЕРВЫЙ раздел — заголовок <h2>
  * ВСЕ ОСТАЛЬНЫЕ разделы (2–6) — заголовок <h3>
  * под каждым заголовком — 2–4 абзаца <p>, при необходимости <ul><li>
  * КАЖДЫЙ абзац <p> — не длиннее 3–4 строк (примерно 40–60 слов); длинные мысли
    дробить на несколько отдельных <p>, а не писать один длинный абзац
  * подразделов внутри разделов не делать (без <h4>, <h5>, <h6>) — Дзен поддерживает
    только h1–h4, и по принятой на канале схеме ниже первого h2 идут только h3
- Заключение: 2 абзаца <p>

ОБЪЁМ: не менее 1500 слов — это критически важно. Пиши развёрнуто, с примерами и деталями.
ЗАПРЕЩЕНО: <h1>, <h4>, <h5>, <h6>, markdown, заголовок статьи в начале текста.
ЗАПРЕЩЕНО подменять метрику похожей по смыслу (если источник про рост выручки — не пиши про рост лояльности/продаж/доверия, это разные метрики). ЗАПРЕЩЕНО округлять или "причёсывать" цифры для звучности (нашёл 33% — пиши 33%, не 30% и не "около трети"). Копируй число и название метрики из источника дословно.
Разрешён <b> точечно — не более 2–3 раз на статью, только для самой важной мысли раздела, не злоупотреблять."""


def generate_article(topic):
    print(f"[1/4] Генерирую текст (без веб-поиска, по знаниям модели): «{topic}»...")

    system_prompt = ARTICLE_SYSTEM + """

КРИТИЧЕСКИ ВАЖНО — правила работы с фактами (12.08.2026: у тебя больше НЕТ
инструмента веб-поиска в этом пайплайне — см. app.py, решение принято
Владимиром из-за стоимости платного фактчека):
- НЕ придумывай конкретные цифры, проценты, суммы или статистику, которые не можешь
  подтвердить из широко известных, стабильных фактов (устоявшихся за годы, не свежих
  новостей). Если не уверен — пиши без цифры, обычным текстом ("значительная часть",
  "заметно выросло" и т.п.) — это ЧЕСТНЕЕ выдуманного числа.
- Категорически ЗАПРЕЩЕНО писать "по данным опросов", "по оценкам экспертов",
  "исследования показывают", "согласно РБК/Habr/..." — у тебя нет способа это
  проверить, такая фраза сейчас была бы ложью о несуществующем источнике.
- Цифры разрешены только для общеизвестных, давно стабильных фактов (например,
  количество дней в году) — не для актуальной статистики рынков/технологий/опросов."""

    def _request():
        user_content = f"""Напиши статью для Яндекс Дзен на тему: {topic}

Пиши по своим знаниям, БЕЗ веб-поиска (его больше нет в этом пайплайне) —
следуй правилам работы с фактами выше строго.

КРИТИЧЕСКИ ВАЖНО ПРО ФОРМАТ ОТВЕТА (13.08.2026: добавлено после того, как
gemini-3.5-flash вместо тегов начала выдавать черновик-план по разделам —
"Section 4 (<h3>):", "Paragraph 1:" и т.п. — и упиралась в лимит токенов,
не успевая дойти до закрывающих тегов): НЕ пиши план, наброски, промежуточные
рассуждения, нумерацию разделов или что-либо ДО первого тега. Первым символом
твоего ответа должен быть символ "<" тега <title>. Сразу пиши готовый финальный
текст в трёх тегах ниже, без черновика и без слов до/после них.

Верни ответ строго в таком формате (без лишних слов до и после):
<title>заголовок 40–60 символов, конкретный, отражает суть темы. ЗАПРЕЩЕНО: восклицательный или вопросительный знак в конце, троеточие, КАПС, слова «шок»/«сенсация», приманки-императивы («смотри», «узнаешь только тут», «не поверишь»), преувеличения без конкретики («невероятный», «сумасшедший»)</title>
<html>полный HTML текст статьи — строго не менее 1500 слов, развёрнуто с примерами</html>
<image_prompt>описание обложки на английском, фотореализм, без текста, без красных обводок/стрелок/восклицательных знаков, без гипертрофированной мимики лиц, 16:9</image_prompt>"""
        return _gemini_complete(system_prompt, user_content, max_tokens=24000)

    raw = _request()
    title_m = re.search(r"<title>(.*?)</title>", raw, re.DOTALL)
    html_m  = re.search(r"<html>(.*?)</html>", raw, re.DOTALL)
    img_m   = re.search(r"<image_prompt>(.*?)</image_prompt>", raw, re.DOTALL)

    retry_count = 0
    while not (title_m and html_m and img_m) and retry_count < 2:
        retry_count += 1
        print(f"    ⚠️ Модель не вернула нужный формат, повторяю запрос ({retry_count}/2)...")
        print("    --- RAW OTVET (первые 1500 симв.) ---")
        print(raw[:1500])
        print("    --------------------------------------")
        raw = _request()
        title_m = re.search(r"<title>(.*?)</title>", raw, re.DOTALL)
        html_m  = re.search(r"<html>(.*?)</html>", raw, re.DOTALL)
        img_m   = re.search(r"<image_prompt>(.*?)</image_prompt>", raw, re.DOTALL)

    if not (title_m and html_m and img_m):
        print("    ❌ Все попытки не дали нужный формат:")
        print(raw[:2000])
        raise ValueError(f"Не удалось распарсить ответ модели для темы: {topic}")

    title        = title_m.group(1).strip()
    html         = html_m.group(1).strip()
    image_prompt = img_m.group(1).strip()
    title = strip_foreign_scripts(title)
    html  = strip_foreign_scripts(html)

    # Расширяем статью, пока не наберётся нужный объём по Дзену (макс. 3 попытки)
    text_only  = re.sub(r'<[^>]+>', '', html)
    char_count = len(text_only)
    word_count = len(text_only.split())
    attempts = 0
    while char_count < 3500 and attempts < 3:
        attempts += 1
        print(f"    Объём {word_count} слов / {char_count} знаков — дописываю (попытка {attempts}/3)...")
        expand_user = f"Продолжи и расширь следующую статью. Если нужны новые цифры или примеры — сначала найди их через веб-поиск. Добавь 3–4 новых раздела с заголовком <h3> и 2–4 абзацами <p> под каждым (без подразделов, без <h4>/<h5>/<h6>). Верни только новые HTML разделы без вступления и заключения:\n\n{html}"
        extra = strip_foreign_scripts(_gemini_complete(system_prompt, expand_user, max_tokens=4000).strip())
        html = html + "\n" + extra
        text_only  = re.sub(r'<[^>]+>', '', html)
        char_count = len(text_only)
        word_count = len(text_only.split())

    if char_count < 3500:
        print(f"    ⚠️ После {attempts} попыток объём всё ещё мал: {word_count} слов / {char_count} знаков. Публикую как есть.")
    else:
        print(f"    ✅ Объём достаточный: {word_count} слов / {char_count} знаков")

    html = insert_banner(html)
    print(f"    Заголовок: {title}")
    print(f"    Объём: {word_count} слов")
    return {"title": title, "html": html, "image_prompt": image_prompt}

# ─── 2. Генерация обложки и иллюстраций (fal.ai FLUX.1 schnell) ─────────────

def generate_fal_image(prompt):
    """Генерирует одну картинку через fal.ai FLUX.1 schnell и возвращает
    прямую URL на CDN fal.media. Заголовок X-Fal-Object-Lifecycle-Preference
    с expiration_duration_seconds=null обязателен — без него файл удаляется
    примерно через 7 дней и картинка на сайте пропадёт (см. media-expiration
    в официальной документации fal.ai)."""
    response = requests.post(
        "https://fal.run/fal-ai/flux/schnell",
        headers={
            "Authorization": f"Key {FAL_API_KEY}",
            "Content-Type": "application/json",
            "X-Fal-Object-Lifecycle-Preference": json.dumps({"expiration_duration_seconds": None}),
        },
        json={"prompt": prompt},
        timeout=90,
    )
    response.raise_for_status()
    result = response.json()
    return result["images"][0]["url"]


def generate_cover_image_tag(prompt, title):
    """Обложка вставляется ПЕРВОЙ картинкой в теле статьи (не как Featured
    Image — см. примечание в шапке файла про визуальный дубль обложки)."""
    print("[2/5] Генерирую обложку (fal.ai FLUX.1 schnell)...")
    full_prompt = f"{prompt}. Bright vivid colors, dynamic composition, illustration/cartoon style, no text, no watermarks."
    url = generate_fal_image(full_prompt)
    print(f"    Обложка: {url}")
    return f'<img src="{url}" alt="{title}" style="max-width:100%;height:auto;">'


def insert_illustrations(html, base_prompt):
    """Вставляет до 3 иллюстраций в тело статьи: выбирает 3 самых
    содержательных смысловых блока между заголовками h2/h3 (исключая
    заключительный блок, если блоков больше трёх), и в каждом вставляет
    картинку сразу после первого абзаца <p> этого блока. Промпт каждой
    иллюстрации строится из заголовка блока — не абстрактный промпт
    обложки на всю статью."""
    print("[3/5] Генерирую 3 иллюстрации по смысловым блокам (fal.ai)...")
    headings = list(re.finditer(r'<h[23]\b[^>]*>(.*?)</h[23]>', html, re.DOTALL))
    if len(headings) < 2:
        print("    ⚠️ Недостаточно заголовков для смысловой разбивки — иллюстрации пропущены.")
        return html

    blocks = []
    for i, h in enumerate(headings):
        start = h.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(html)
        heading_text = re.sub(r'<[^>]+>', '', h.group(1)).strip()
        blocks.append({"heading": heading_text, "start": start, "end": end, "length": end - start})

    candidate_blocks = blocks[:-1] if len(blocks) > 3 else blocks
    chosen = sorted(candidate_blocks, key=lambda b: -b["length"])[:3]
    chosen = sorted(chosen, key=lambda b: b["start"])

    offset = 0
    inserted = 0
    for b in chosen:
        prompt = f"{b['heading']}. {base_prompt}. Bright vivid colors, dynamic composition, illustration/cartoon style, no text, no watermarks."
        try:
            url = generate_fal_image(prompt)
        except Exception as e:
            print(f"    ⚠️ Не удалось сгенерировать иллюстрацию для «{b['heading']}»: {str(e)[:150]}")
            continue
        block_start = b["start"] + offset
        block_end = b["end"] + offset
        block_html = html[block_start:block_end]
        p_match = re.search(r'</p>', block_html)
        img_tag = f'\n<img src="{url}" alt="{b["heading"]}" style="max-width:100%;height:auto;">\n'
        if p_match:
            insert_pos = block_start + p_match.end()
        else:
            insert_pos = block_start
        html = html[:insert_pos] + img_tag + html[insert_pos:]
        offset += len(img_tag)
        inserted += 1
        print(f"    Иллюстрация вставлена в блок «{b['heading']}»: {url}")

    print(f"    Итого вставлено иллюстраций: {inserted}/3")
    return html


# ─── 2b. Мультик (видео) в конец статьи ──────────────────────────────────────
# Методология отработана и подтверждена вручную 11.08.2026 (Cowork-сессия):
# официальный формат Дзена для видео в RSS — <video><source src="URL"
# type="video/mp4"></video> в content:encoded, без привязки к каталогу
# "Ролики" Дзена. Плагин ProZen на сайте ищет такой тег только если контент
# распознан как Gutenberg-блоки — значит нужен обычный блок core/video
# (не кастомный prozen-dzen/dzen-video) со ссылкой на /mp4/{id}.mp4-прокси
# (mu-plugin dzen-multiki.php, редиректит на прямую ссылку Google Drive).
#
# ЛОВУШКА (баг ProZen ContentProcessor::transform_block(), найден 11.08.2026):
# если в контенте есть Gutenberg-блок, а остальной текст — свободный HTML без
# блочной разметки, PHP нестрого сравнивает NULL == false как true в switch,
# весь свободный текст уходит в transform_embed_block() и пропадает из RSS,
# остаётся только видео. ОБХОД: весь текст статьи (всё, что не является
# отдельным Gutenberg-блоком) оборачивается в <!-- wp:html -->...<!-- /wp:html -->,
# это превращает blockName из NULL в строку 'core/html', баг не срабатывает.

def get_multiki_worksheet():
    gc = get_sheets_client()
    return gc.open_by_key(SHEET_ID).worksheet(MULTIKI_SHEET_NAME)


def get_next_multik():
    """Возвращает (номер_строки, file_id, название) для первого по возрастанию
    номера (колонка C, только для ориентации) подтверждённого (колонка D —
    Название — не пустая) и ещё не использованного (колонка B пустая)
    мультика. Если подходящих строк нет — (None, None, None), это ОЖИДАЕМАЯ
    ситуация (пользователь ещё не назвал следующие по очереди мультики), а
    не ошибка — статья в этом случае публикуется без видео."""
    try:
        ws = get_multiki_worksheet()
        rows = ws.get_all_values()
    except Exception as e:
        print(f"    ⚠️ Не удалось открыть лист «{MULTIKI_SHEET_NAME}»: {str(e)[:150]}")
        return None, None, None

    candidates = []
    for i, row in enumerate(rows[1:], start=2):
        link   = row[0].strip() if len(row) > 0 else ""
        status = row[1].strip() if len(row) > 1 else ""
        num    = row[2].strip() if len(row) > 2 else ""
        title  = row[3].strip() if len(row) > 3 else ""
        if not link or status or not title:
            continue
        m = re.search(r'id=([a-zA-Z0-9_-]+)', link)
        if not m:
            continue
        try:
            sort_key = float(num)
        except ValueError:
            sort_key = float(i)
        candidates.append((sort_key, i, m.group(1), title))

    if not candidates:
        return None, None, None

    candidates.sort(key=lambda x: x[0])
    _, row_index, file_id, title = candidates[0]
    return row_index, file_id, title


def mark_multik_used(row_index, url):
    try:
        ws = get_multiki_worksheet()
        ws.update_cell(row_index, 2, f"Использовано: {url}")
    except Exception as e:
        print(f"    ⚠️ Не удалось отметить мультик как использованный: {str(e)[:150]}")


def build_video_fragment(file_id, caption):
    """Возвращает (caption_html, iframe_html, video_gutenberg_block).
    iframe — для проигрывания НА САЙТЕ (Google Drive не отдаёт нужные
    заголовки для <video src> напрямую чужому домену). video_gutenberg —
    ОТДЕЛЬНЫЙ Gutenberg-блок для Дзена (Дзен перекачивает файл на своей
    стороне при импорте RSS, кросс-доменные ограничения браузера его не
    касаются)."""
    mp4_url = f"{WP_URL}/mp4/{file_id}.mp4"
    caption_html = f'<p style="text-align:center;font-weight:bold;">{caption}</p>'
    iframe_html = (
        '<div style="position:relative;padding-top:56.25%;max-width:50%;margin:0 auto;">'
        f'<iframe src="https://drive.google.com/file/d/{file_id}/preview" '
        'style="position:absolute;top:0;left:0;width:100%;height:100%;border:0;" '
        'allow="autoplay" allowfullscreen></iframe></div>'
    )
    video_block = (
        f'\n<!-- wp:video {{"src":"{mp4_url}"}} -->\n'
        f'<figure class="wp-block-video"><video controls src="{mp4_url}"></video></figure>\n'
        '<!-- /wp:video -->'
    )
    return caption_html, iframe_html, video_block


def assemble_final_content(html):
    """Собирает финальный #content поста. Если найден подтверждённый
    мультик — оборачивает ВЕСЬ текст (включая подпись и iframe мультика) в
    <!-- wp:html -->, а видео-блок для Дзена добавляет ОТДЕЛЬНО в конце
    (см. ловушку ProZen выше). Если мультика нет — контент остаётся обычным
    HTML без блочной обёртки (в нём нет ни одного Gutenberg-блока, ловушка
    не применима). Возвращает (final_html, multik_row_index_or_None)."""
    row_index, file_id, title = get_next_multik()
    if not file_id:
        print("    Нет подтверждённых (названных) мультиков в очереди — статья без видео.")
        return html, None

    caption_html, iframe_html, video_block = build_video_fragment(file_id, title)
    wrapped = "<!-- wp:html -->\n" + html + "\n" + caption_html + "\n" + iframe_html + "\n<!-- /wp:html -->"
    final = wrapped + video_block
    print(f"    Мультик вставлен: «{title}» (строка {row_index})")
    return final, row_index


# ─── 5. Публикация поста в WordPress ─────────────────────────────────────────
def publish_post(title, html, status="publish"):
    """Обложка теперь ВНУТРИ html (первой картинкой), Featured Image
    сознательно не выставляется — см. примечание в шапке файла."""
    print("[5/5] Публикую пост в WordPress...")
    slug = make_slug(title)
    print(f"    Slug: {slug}")
    response = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        json={
            "title":          title,
            "content":        html,
            "slug":           slug,
            "status":         status,
            "categories":     [WP_CATEGORY],
            "comment_status": "closed",
        },
        auth=wp_auth,
        timeout=30,
    )
    if response.status_code >= 400:
        print(f"    WP post publish FAILED: HTTP {response.status_code}")
        print(f"    Response body: {response.text[:2000]}")
    response.raise_for_status()
    post = response.json()
    if status == "draft":
        print(f"    Пост сохранён как черновик: {post['link']}")
    else:
        print(f"    Пост опубликован: {post['link']}")
    return post

# ─── Главная функция ──────────────────────────────────────────────────────────
# ─── Самопроверка фактов ──────────────────────────────────────────────────────
def _extract_anthropic_text(response):
    """Собирает финальный текст ответа Anthropic из всех text-блоков
    (между ними могут быть блоки web_search_tool_result — их пропускаем)."""
    return "".join(block.text for block in response.content if block.type == "text")


def self_check_facts(html):
    """15.08.2026: переписано на Vertex AI (Gemini 2.5 Flash + google_search
    grounding) вместо версии на Claude Haiku (14.08.2026) — Anthropic, Groq и
    Tavily заблокированы на сетевом уровне с этого сервера (403, подтверждено
    curl). Обычный Gemini API (generativelanguage.googleapis.com) с grounding
    отдаёт 429 RESOURCE_EXHAUSTED даже на оплаченном Tier 1 (известный баг
    Google на стороне платформы) — но Vertex AI (aiplatform.googleapis.com),
    тот же проект и биллинг, работает без 429 и без сетевой блокировки
    (проверено test_vertex.py на этом сервере 15.08.2026). Возвращает
    (needs_review, problems_text): needs_review=True, если найдены проблемы."""
    print("[доп] Проверяю факты в статье через Vertex AI (Gemini + google_search)...")
    text_only = re.sub(r'<[^>]+>', '', html)
    try:
        response = vertex_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                "Ты — строгий фактчекер. В тексте есть конкретные цифры, проценты, "
                "статистика. Для каждой такой цифры используй поиск и проверь, "
                "существует ли реально такое исследование/данные с такими значениями, "
                "и что метрика (название показателя) в тексте совпадает с метрикой "
                "в источнике, а не подменена похожей по смыслу.\n"
                "Не проверяй общие утверждения без цифр.\n"
                "Если в тексте вообще нет ни одной проверяемой цифры, процента или "
                "статистики — это НЕ проблема, ответь ровно: OK (не пиши пояснений "
                "вроде \"нет данных для проверки\" — это тоже считается OK).\n"
                "Если все цифры, которые есть в тексте, подтвердились реальными "
                "источниками, и метрики не перепутаны — тоже ответь ровно: OK\n"
                "Если хотя бы одна цифра не подтвердилась, выдумана, сильно искажена "
                "или метрика подменена другой (например, вместо \"рост выручки\" "
                "написано \"рост лояльности\") — перечисли проблемные места списком, "
                "каждая проблема с новой строки, кратко, с указанием, что именно не "
                "так и какое верное значение/метрика нашлась в источнике.\n\n"
                f"Текст статьи:\n{text_only[:6000]}"
            ),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
        result = (response.text or "").strip()
        if not result:
            print("    ⚠️ Фактчек не дал текстового ответа. Публикую как черновик — нужна ручная проверка.")
            return True, ""
        if result.upper().startswith("OK"):
            print("    ✅ Фактчек через Vertex AI не выявил проблем")
            return False, ""
        else:
            print("    ⚠️ Фактчек нашёл непроверенные/неверные цифры:")
            for line in result.splitlines():
                if line.strip():
                    print(f"       - {line.strip()}")
            return True, result
    except Exception as e:
        print(f"    ⚠️ Фактчек не сработал — публикую как черновик на всякий случай: {str(e)[:120]}")
        return True, ""  # при ошибке фактчека — лучше перестраховаться и не публиковать сразу


def fix_flagged_issues(html, problems):
    """Правит проблемы, найденные self_check_facts, через Vertex AI (Gemini +
    google_search), вместо того чтобы сразу отправлять пост в черновики.
    Возвращает исправленный HTML (или исходный html, если исправление
    не удалось / выглядит подозрительно)."""
    print("[доп] Пробую исправить найденные фактчеком проблемы (1 попытка)...")
    try:
        response = vertex_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                "Ты редактируешь готовую статью для Дзен, чтобы исправить "
                "конкретные фактические проблемы, которые нашёл фактчекер. "
                "Для каждой проблемы: используй поиск, найди точное значение "
                "метрики (число И название метрики дословно, не заменяй "
                "похожим по смыслу словом) и исправь текст на месте. Если "
                "подтвердить цифру не получается вообще — убери её из текста, "
                "перепиши фразу без конкретного числа, сохранив смысл абзаца. "
                "Не трогай части текста, к которым нет претензий. Верни ПОЛНЫЙ "
                "исправленный HTML статьи целиком (все разделы от вступления "
                "до заключения), без комментариев до/после, в тех же тегах "
                "<h2>/<h3>/<p>/<ul>/<li>, что и в исходнике.\n\n"
                f"Найденные фактчеком проблемы:\n{problems}\n\n"
                f"Полный текст статьи для исправления:\n{html}"
            ),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
        fixed = strip_foreign_scripts((response.text or "").strip())
        original_words = len(re.sub(r'<[^>]+>', '', html).split())
        fixed_words = len(re.sub(r'<[^>]+>', '', fixed).split())
        # защита от пустого/усечённого ответа — не даём испорченному варианту
        # заменить рабочую статью
        if fixed_words < original_words * 0.7:
            print(f"    ⚠️ Исправленный вариант заметно короче оригинала "
                  f"({fixed_words} слов против {original_words}) — не применяю, "
                  f"ухожу в черновик с исходным текстом.")
            return html
        print("    ✅ Применена попытка исправления, перепроверяю фактчеком...")
        return fixed
    except Exception as e:
        print(f"    ⚠️ Исправление не сработало: {str(e)[:120]}")
        return html


MIN_WORDS = 1200  # ниже — жёсткий отказ от публикации (черновик), даже если фактчек OK


def check_structure(html):
    """Жёсткая проверка объёма и схемы заголовков перед публикацией.
    Появилась после инцидента с постом 2505 (26.07.2026): статья в 450 слов
    и 3×<h2> без единого <h3> ушла в publish, потому что self_check_facts
    проверяет только цифры/факты, а не объём и структуру."""
    reasons = []
    text_only = re.sub(r'<[^>]+>', '', html)
    word_count = len(text_only.split())
    h2_count = len(re.findall(r'<h2\b', html, re.IGNORECASE))
    h3_count = len(re.findall(r'<h3\b', html, re.IGNORECASE))

    if word_count < MIN_WORDS:
        reasons.append(f"объём {word_count} слов < {MIN_WORDS}")
    if h2_count != 1:
        reasons.append(f"должен быть ровно 1×<h2>, найдено {h2_count}")
    if h3_count < 3:
        reasons.append(f"должно быть минимум 3×<h3> (обычно 5), найдено {h3_count}")
    if re.search(r'<h[456]\b', html, re.IGNORECASE):
        reasons.append("найден запрещённый <h4>/<h5>/<h6>")

    return (len(reasons) == 0), reasons


SUSPICIOUS_ARTIFACT_PATTERNS = [
    r"\bWait,? I need to\b",
    r"\bLet me (fix|reconsider|re-?check|rewrite)\b",
    r"shouldn'?t be\b.{0,40}\bagain\b",
    r"\bper instructions\b",
    r"\berroneous\b",
    r"\bI already made\b",
    r"<h[1-6][^>]*style=[\"']display:\s*none[\"'][^>]*>\s*</h[1-6]>",
    r"\bas an AI\b",
    r"\bas a language model\b",
    r"\(this conclusion section\)",
]


def check_for_leaked_artifacts(html):
    """Проверка на утёкшие в текст статьи артефакты генерации — модель иногда
    вслух проговаривает самокоррекцию прямо внутри <html>...</html> (например,
    "Wait, I need to check structure... Let me fix...") вместо того, чтобы
    молча переписать текст, до того как отдать финальный ответ. self_check_facts
    проверяет только цифры, а check_structure — только объём/заголовки: оба
    пропускают такие вставки, т.к. они не портят ни счётчик слов, ни разметку.

    Найдено вручную 05.08.2026 (пользователь дал доступ к репозиторию и
    попросил разобраться, откуда в постах 2538 и 2546 взялись a) целый абзац
    рассуждений модели между <h2> и <p> и b) пустой скрытый <h2 style=
    "display:none"> — оба поста ушли в publish, т.к. проходили и фактчек,
    и check_structure. Оба вручную исправлены на сайте; это — защита от
    повтора на будущих прогонах."""
    hits = [p for p in SUSPICIOUS_ARTIFACT_PATTERNS if re.search(p, html, re.IGNORECASE)]
    return (len(hits) == 0), hits


MIN_PUBLISH_GAP_HOURS = 4  # 15.08.2026: не публиковать чаще, чем раз в 4 часа
                            # (даже при ручных/тестовых запусках --article 999),
                            # чтобы тестовые прогоны не заваливали ленту подряд.


def get_last_publish_time():
    """Возвращает datetime (UTC, aware) последней ОПУБЛИКОВАННОЙ статьи через
    WordPress REST API, или None если постов нет / запрос не удался (в этом
    случае публикация не блокируется — лучше пропустить проверку, чем
    случайно никогда не публиковать из-за временной ошибки API)."""
    try:
        response = requests.get(
            f"{WP_URL}/wp-json/wp/v2/posts",
            params={"status": "publish", "per_page": 1, "orderby": "date", "order": "desc"},
            auth=wp_auth,
            timeout=15,
        )
        response.raise_for_status()
        posts = response.json()
        if not posts:
            return None
        from datetime import timezone
        return datetime.fromisoformat(posts[0]["date_gmt"]).replace(tzinfo=timezone.utc)
    except Exception as e:
        print(f"    ⚠️ Не удалось проверить время последней публикации: {str(e)[:120]}")
        return None


def get_trending_topic(existing_topics):
    """15.08.2026: сканирует тренды в нише "нейросети/ИИ" за последние 24-48ч
    через Vertex AI grounding (google_search) и предлагает ОДНУ конкретную,
    ещё не освещённую тему для статьи. Идея - не писать по заранее заготовленной
    очереди тем в отрыве от того, что реально обсуждают сегодня, а ловить
    актуальную волну (по аналогии с trend-watching в чужих контент-пайплайнах).
    Возвращает строку темы или None (если ничего подходящего не нашлось / что-то
    пошло не так - в этом случае пайплайн просто продолжает работать по старой
    очереди из Google Sheets, ничего не ломается)."""
    try:
        avoid_list = "\n".join(f"- {t}" for t in existing_topics[-60:])
        response = vertex_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                "Ты - редактор Дзен-канала про нейросети и ИИ для широкой "
                "русскоязычной аудитории (не разработчики, обычные люди, которые "
                "пользуются ИИ в быту и работе). Через поиск найди, что реально "
                "обсуждают/что произошло в теме нейросетей и ИИ за последние "
                "24-48 часов: новые релизы моделей, громкие новости, вирусные "
                "кейсы использования ИИ, тренды. Выбери ОДНУ конкретную, узкую "
                "тему для статьи - такую, чтобы заголовок цеплял обычного "
                "человека, а не разработчика (пример хорошей темы: 'Новая "
                "функция Gemini бесплатно делает то, за что раньше платили "
                "дизайнерам' - НЕ 'Обзор архитектуры новой модели').\n\n"
                "Эти темы уже освещены на канале, НЕ повторяй их и не "
                "предлагай близкие по сути:\n"
                f"{avoid_list}\n\n"
                "Ответь СТРОГО одной строкой - только сама тема (5-12 слов), "
                "без кавычек, без номеров, без пояснений. Если за последние "
                "24-48ч не нашлось ничего конкретного и вирусного в этой нише "
                "- ответь ровно: НЕТ ТРЕНДА"
            ),
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            ),
        )
        result = (response.text or "").strip()
        if not result or result.upper().startswith("НЕТ ТРЕНДА"):
            return None
        result_lower = result.lower()
        for t in existing_topics:
            if t and (t.lower() in result_lower or result_lower in t.lower()):
                print(f"    ⚠️ Тренд-тема похожа на уже освещённую ({t[:50]}) - пропускаю.")
                return None
        return result
    except Exception as e:
        print(f"    ⚠️ Не удалось получить тренд-тему: {str(e)[:120]} - работаю по обычной очереди.")
        return None


def publish_next():
    print("[0/5] Проверяю тренд дня в нише...")
    try:
        gc = get_sheets_client()
        ws = gc.open_by_key(SHEET_ID).sheet1
        all_rows = ws.get_all_values()
        existing_topics = [row[0].strip() for row in all_rows[1:] if len(row) > 0 and row[0].strip()]
        trend_topic = get_trending_topic(existing_topics)
        if trend_topic:
            print(f"    🔥 Тренд дня: «{trend_topic}» - добавляю в начало очереди")
            ws.insert_row([trend_topic, ""], index=2)
        else:
            print("    Актуального тренда не найдено - работаю по обычной очереди тем.")
    except Exception as e:
        print(f"    ⚠️ Проверка тренда не удалась: {str(e)[:120]} - работаю по обычной очереди тем.")

    topic, row_index = get_next_topic()
    if not topic:
        print("Нет новых тем в таблице — все опубликованы.")
        return

    mark_published(row_index, "В работе...")  # бронируем строку СРАЗУ, до генерации

    try:
        article = generate_article(topic)

        # 15.08.2026: фактчек восстановлен через Vertex AI (Gemini + google_search) —
        # Anthropic/Groq/Tavily заблокированы на сетевом уровне с этого сервера,
        # обычный Gemini grounding (generativelanguage.googleapis.com) упирается в
        # баг 429 на стороне Google даже на оплаченном тарифе, а Vertex AI
        # (aiplatform.googleapis.com) работает без ограничений (см. test_vertex.py).
        needs_review, fact_reasons = self_check_facts(article["html"])
        if needs_review and fact_reasons:
            article["html"] = fix_flagged_issues(article["html"], fact_reasons)
            needs_review, fact_reasons = self_check_facts(article["html"])

        structure_ok, extra_structure_reasons = check_structure(article["html"])
        structure_reasons = extra_structure_reasons
        if not structure_ok:
            print("    ⚠️ Жёсткая проверка объёма/структуры не пройдена (пост уйдёт в черновики):")
            for reason in structure_reasons:
                print(f"       - {reason}")
            needs_review = True

        artifacts_ok, artifact_hits = check_for_leaked_artifacts(article["html"])
        if not artifacts_ok:
            print("    ⚠️ Найдены признаки утёкших артефактов генерации — рассуждения модели "
                  "или мусорные теги в тексте (пост уйдёт в черновики):")
            for hit in artifact_hits:
                print(f"       - паттерн сработал: {hit}")
            structure_reasons = structure_reasons + [f"подозрительный артефакт в тексте ({h})" for h in artifact_hits]
            needs_review = True

        if not needs_review:
            last_pub = get_last_publish_time()
            if last_pub is not None:
                from datetime import timezone
                elapsed_seconds = (datetime.now(timezone.utc) - last_pub).total_seconds()
                if elapsed_seconds < MIN_PUBLISH_GAP_HOURS * 3600:
                    elapsed_min = int(elapsed_seconds // 60)
                    print(f"    ⏳ С последней публикации прошло {elapsed_min} мин "
                          f"(< {MIN_PUBLISH_GAP_HOURS}ч) — ухожу в черновик, не публикую сразу.")
                    structure_reasons = structure_reasons + [
                        f"интервал публикации < {MIN_PUBLISH_GAP_HOURS}ч (прошло {elapsed_min} мин)"
                    ]
                    needs_review = True

        cover_tag = generate_cover_image_tag(article["image_prompt"], article["title"])
        html_with_images = cover_tag + "\n" + article["html"]
        html_with_images = insert_illustrations(html_with_images, article["image_prompt"])

        print("[4/5] Ищу подтверждённый мультик в очереди...")
        final_content, multik_row = assemble_final_content(html_with_images)

        status = "draft" if needs_review else "publish"
        post = publish_post(article["title"], final_content, status=status)

        # 17.08.2026: убрано условие status == "publish". Раньше мультик
        # помечался использованным только при немедленной публикации — если
        # статья уходила в черновик (needs_review, в т.ч. из-за интервала
        # публикации < MIN_PUBLISH_GAP_HOURS), мультик оставался в таблице как
        # свободный, хотя уже был вставлен в HTML черновика. Если такой черновик
        # потом публиковался/планировался вручную или через guard-задачу (которая
        # не трогает Google Таблицу), следующий обычный прогон мог выбрать тот же
        # мультик повторно (подтверждённый случай: мультик "Гомель" оказался
        # одновременно в постах 2742 и 2747, таблица отражала только 2747).
        # Теперь помечаем сразу при вставке в контент, вне зависимости от
        # финального статуса поста — мультик считается потраченным с момента
        # попадания в HTML, а не с момента публикации.
        if multik_row:
            mark_multik_used(multik_row, post["link"])

        if needs_review:
            reasons_all = list(structure_reasons)
            if fact_reasons:
                reasons_all.append(f"фактчек: {fact_reasons}")
            reason_note = "; ".join(reasons_all) if reasons_all else "проверка фактов"
            mark_published(row_index, f"ЧЕРНОВИК (нужна проверка: {reason_note}): {post['link']}")
        else:
            mark_published(row_index, post["link"])
    except Exception:
        mark_published(row_index, "ОШИБКА — требует ручной проверки")
        raise


# ─── Точка входа ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    slot_label = f" (слот --article {ARGS.article})" if ARGS.article else ""
    print(f"=== app.py запуск{slot_label}, {datetime.now().isoformat(timespec='seconds')} ===")
    try:
        publish_next()
    except Exception as e:
        msg = str(e)
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
            print("  Похоже, дневная квота Gemini исчерпана. Следующий запуск по расписанию.")
            sys.exit(0)
        raise
