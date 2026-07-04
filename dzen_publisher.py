"""
Dzen Publisher — автоматическая публикация статей в WordPress для Яндекс Дзен
Пайплайн: Google Sheets (тема) → Groq (текст) → Ideogram AI (обложка) → WordPress → RSS → Дзен

Требования:
    pip install groq requests python-dotenv gspread google-auth

Настройка GitHub Secrets:
    WP_URL, WP_USER, WP_APP_PASS, WP_CATEGORY, GROQ_API_KEY, GOOGLE_CREDENTIALS, IDEOGRAM_API_KEY
"""

import os
import re
import sys
import json
import time
import urllib.parse
import requests
from datetime import datetime
from dotenv import load_dotenv
from groq import Groq
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

# ─── Конфигурация ────────────────────────────────────────────────────────────

WP_URL          = os.getenv("WP_URL", "").rstrip("/")
WP_USER         = os.getenv("WP_USER")
WP_APP_PASS     = os.getenv("WP_APP_PASS")
WP_CATEGORY     = int(os.getenv("WP_CATEGORY", "1"))
GROQ_API_KEY    = os.getenv("GROQ_API_KEY")
GROQ_MODEL      = "llama-3.3-70b-versatile"
IDEOGRAM_API_KEY = os.getenv("IDEOGRAM_API_KEY")

SHEET_ID = "1d8VS3BmMAZUWCXG0Ha2I-R1b7gdXiVEO_p8RssyaXME"

AUTHOR_BANNER = (
    '<img src="https://000l.ru/wp-content/uploads/2026/06/'
    'ChatGPT-Image-5-июн.-2026-г.-19_59_37.png" '
    'style="width:100%;display:block;margin:20px 0;" />'
)

wp_auth     = (WP_USER, WP_APP_PASS)
groq_client = Groq(api_key=GROQ_API_KEY)


# ─── Google Sheets ────────────────────────────────────────────────────────────

def get_sheets_client():
    creds_json = os.getenv("GOOGLE_CREDENTIALS")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ],
    )
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


def insert_banner(html):
    """Вставляет баннер автора после 3-го тега <h5>"""
    parts = re.split(r'(<h5\b[^>]*>.*?</h5>)', html, flags=re.DOTALL)
    h5_count = 0
    result = []
    inserted = False
    for part in parts:
        result.append(part)
        if re.match(r'<h5\b', part) and not inserted:
            h5_count += 1
            if h5_count == 3:
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
- 6 смысловых разделов, каждый содержит:
  * заголовок <h3>
  * 2–3 подраздела с заголовком <h5> и 2–3 абзацами <p> каждый
  * при необходимости <ul><li>
- Заключение: 2 абзаца <p>

ОБЪЁМ: не менее 1500 слов — это критически важно. Пиши развёрнуто, с примерами и деталями.
ЗАПРЕЩЕНО: <h1>, <h2>, <h4>, markdown, заголовок статьи в начале текста."""


def generate_article(topic):
    print(f"[1/4] Генерирую текст: «{topic}»...")

    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        max_tokens=8000,
        messages=[
            {"role": "system", "content": ARTICLE_SYSTEM},
            {"role": "user", "content": f"""Напиши статью для Яндекс Дзен на тему: {topic}

Верни ответ строго в таком формате (без лишних слов до и после):
<title>цепляющий заголовок 40–60 символов, конкретный, отражает суть темы</title>
<html>полный HTML текст статьи — строго не менее 1500 слов, развёрнуто с примерами</html>
<image_prompt>описание обложки на английском, фотореализм, без текста, 16:9</image_prompt>"""},
        ],
    )

    raw = response.choices[0].message.content

    title        = re.search(r"<title>(.*?)</title>",               raw, re.DOTALL).group(1).strip()
    html         = re.search(r"<html>(.*?)</html>",                 raw, re.DOTALL).group(1).strip()
    image_prompt = re.search(r"<image_prompt>(.*?)</image_prompt>", raw, re.DOTALL).group(1).strip()

    # Расширяем статью если меньше 1400 слов
    text_only  = re.sub(r'<[^>]+>', '', html)
    word_count = len(text_only.split())
    if word_count < 1400:
        print(f"    Объём {word_count} слов — дописываю...")
        expand = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            max_tokens=4000,
            messages=[
                {"role": "system", "content": ARTICLE_SYSTEM},
                {"role": "user", "content": f"Продолжи и расширь следующую статью. Добавь 3–4 новых раздела <h3> с подразделами <h5> и абзацами <p>. Верни только новые HTML разделы без вступления и заключения:\n\n{html}"},
            ],
        )
        extra = expand.choices[0].message.content.strip()
        # Вставляем перед последним </p> заключения
        html = html + "\n" + extra
        text_only  = re.sub(r'<[^>]+>', '', html)
        word_count = len(text_only.split())

    html = insert_banner(html)

    print(f"    Заголовок: {title}")
    print(f"    Объём: {word_count} слов")

    return {"title": title, "html": html, "image_prompt": image_prompt}


# ─── 2. Генерация обложки ─────────────────────────────────────────────────────

def generate_cover_image(prompt):
    print("[2/4] Генерирую обложку (Ideogram AI)...")
    full_prompt = f"{prompt}. Photorealistic, editorial style, no text, no watermarks."
    response = requests.post(
        "https://api.ideogram.ai/v1/ideogram-v3/generate",
        headers={"Api-Key": IDEOGRAM_API_KEY},
        json={
            "prompt": full_prompt,
            "aspect_ratio": "16x9",
            "style_type": "REALISTIC",
            "rendering_speed": "DEFAULT",
            "magic_prompt": "OFF",
        },
        timeout=120,
    )
    response.raise_for_status()
    result = response.json()

    image_obj = result["data"][0]
    if not image_obj.get("is_image_safe", True) or not image_obj.get("url"):
        raise RuntimeError(f"Ideogram отклонил генерацию (safety-check): {result}")

    img_response = requests.get(image_obj["url"], timeout=60)
    img_response.raise_for_status()

    content_type = img_response.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        raise RuntimeError(f"Ideogram вернул не изображение (content-type: {content_type})")

    print(f"    Разрешение: {image_obj.get('resolution')}, размер: {len(img_response.content)} байт")
    return img_response.content


# ─── 3. Загрузка обложки в WordPress ─────────────────────────────────────────

def upload_image_to_wp(image_bytes, filename):
    print("[3/4] Загружаю обложку в WordPress...")
    response = requests.post(
        f"{WP_URL}/wp-json/wp/v2/media",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "image/jpeg",
        },
        data=image_bytes,
        auth=wp_auth,
        timeout=60,
    )
    response.raise_for_status()
    media_id = response.json()["id"]
    print(f"    Media ID: {media_id}")

    try:
        requests.post(
            f"{WP_URL}/wp-json/wp/v2/media/{media_id}",
            json={"caption": "", "description": ""},
            auth=wp_auth,
            timeout=30,
        )
    except requests.RequestException:
        pass

    return media_id


# ─── 4. Публикация поста в WordPress ─────────────────────────────────────────

def publish_post(title, html, media_id):
    print("[4/4] Публикую пост в WordPress...")
    slug = make_slug(title)
    print(f"    Slug: {slug}")
    response = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        json={
            "title":          title,
            "content":        html,
            "slug":           slug,
            "status":         "publish",
            "featured_media": media_id,
            "categories":     [WP_CATEGORY],
            "comment_status": "closed",
        },
        auth=wp_auth,
        timeout=30,
    )
    response.raise_for_status()
    post = response.json()
    print(f"    Пост опубликован: {post['link']}")
    return post


# ─── Главная функция ──────────────────────────────────────────────────────────

def publish_next():
    topic, row_index = get_next_topic()
    if not topic:
        print("Нет новых тем в таблице — все опубликованы.")
        return

    print(f"\n{'='*55}")
    print(f"  Тема: {topic}  (строка {row_index})")
    print(f"  Старт: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*55}\n")

    article     = generate_article(topic)
    image_bytes = generate_cover_image(article["image_prompt"])
    filename    = f"cover_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    media_id    = upload_image_to_wp(image_bytes, filename)
    post        = publish_post(article["title"], article["html"], media_id)

    mark_published(row_index, post["link"])
    print(f"\n✓ Готово! Статус в таблице обновлён.\n")


# ─── Точка входа ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        publish_next()
    except Exception as e:
        msg = str(e)
        if "rate_limit_exceeded" in msg or "429" in msg:
            print(f"  Лимит токенов Groq исчерпан на сегодня. Следующий запуск по расписанию.")
            sys.exit(0)
        raise
