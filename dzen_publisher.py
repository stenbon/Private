"""
Dzen Publisher — автоматическая публикация статей в WordPress для Яндекс Дзен
Пайплайн: тема → Groq (текст) → Pollinations.ai (обложка) → WordPress REST API → RSS → Дзен

Требования:
    pip install groq requests python-dotenv

Настройка:
    GitHub Secrets: WP_URL, WP_USER, WP_APP_PASS, WP_CATEGORY, GROQ_API_KEY
"""

import os
import re
import sys
import time
import urllib.parse
import requests
from datetime import datetime
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

# ─── Конфигурация ────────────────────────────────────────────────────────────

WP_URL       = os.getenv("WP_URL", "").rstrip("/")
WP_USER      = os.getenv("WP_USER")
WP_APP_PASS  = os.getenv("WP_APP_PASS")
WP_CATEGORY  = int(os.getenv("WP_CATEGORY", "1"))
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL   = "llama-3.3-70b-versatile"

wp_auth     = (WP_USER, WP_APP_PASS)
groq_client = Groq(api_key=GROQ_API_KEY)


# ─── 1. Генерация текста статьи ──────────────────────────────────────────────

ARTICLE_SYSTEM = """Ты — опытный автор для Яндекс Дзен.
Пиши живым разговорным языком, от первого лица или нейтрально.
Структура: цепляющий вступ (2–3 предложения) → 3–5 смысловых блоков → вывод.
Форматирование: только HTML-теги <p>, <h2>, <h3>, <ul>, <li>, <b>, <i>.
Без markdown. Без заголовка в начале текста — он будет добавлен отдельно.
Объём: 800–1200 слов."""


def generate_article(topic):
    """Возвращает {'title': str, 'html': str, 'image_prompt': str}"""
    print(f"[1/4] Генерирую текст: «{topic}»...")

    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        max_tokens=4096,
        messages=[
            {"role": "system", "content": ARTICLE_SYSTEM},
            {"role": "user", "content": f"""Напиши статью для Яндекс Дзен на тему: {topic}

Верни ответ строго в таком формате (без лишних слов до и после):
<title>цепляющий заголовок до 60 символов</title>
<html>полный HTML текст статьи</html>
<image_prompt>описание обложки на английском, фотореализм, без текста, 16:9</image_prompt>"""},
        ],
    )

    raw = response.choices[0].message.content

    title        = re.search(r"<title>(.*?)</title>",               raw, re.DOTALL).group(1).strip()
    html         = re.search(r"<html>(.*?)</html>",                 raw, re.DOTALL).group(1).strip()
    image_prompt = re.search(r"<image_prompt>(.*?)</image_prompt>", raw, re.DOTALL).group(1).strip()

    print(f"    Заголовок: {title}")
    return {"title": title, "html": html, "image_prompt": image_prompt}


# ─── 2. Генерация обложки через Pollinations.ai ───────────────────────────────

def generate_cover_image(prompt):
    """Возвращает bytes изображения от Pollinations.ai (бесплатно, без API ключа)"""
    print("[2/4] Генерирую обложку (Pollinations.ai)...")

    full_prompt = f"{prompt}. Photorealistic, editorial style, no text, no watermarks."
    encoded     = urllib.parse.quote(full_prompt)
    url         = f"https://image.pollinations.ai/prompt/{encoded}?width=1792&height=1024&nologo=true&model=flux"

    response = requests.get(url, timeout=120)
    print(f"    Статус: {response.status_code}, размер: {len(response.content)} байт")
    response.raise_for_status()
    return response.content


# ─── 3. Загрузка обложки в WordPress ─────────────────────────────────────────

def upload_image_to_wp(image_bytes, filename):
    """Загружает изображение в медиатеку WP, возвращает media_id"""
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
    return media_id


# ─── 4. Публикация поста в WordPress ─────────────────────────────────────────

def publish_post(title, html, media_id):
    """Публикует пост, возвращает данные поста (id, link)"""
    print("[4/4] Публикую пост в WordPress...")

    response = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        json={
            "title":          title,
            "content":        html,
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

def publish_to_dzen(topic):
    print(f"\n{'='*55}")
    print(f"  Тема: {topic}")
    print(f"  Старт: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*55}\n")

    article     = generate_article(topic)
    image_bytes = generate_cover_image(article["image_prompt"])
    filename    = f"cover_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    media_id    = upload_image_to_wp(image_bytes, filename)
    post        = publish_post(article["title"], article["html"], media_id)

    print(f"\n✓ Готово! URL поста: {post['link']}\n")


# ─── Пакетная публикация ──────────────────────────────────────────────────────

def publish_batch(topics, delay_seconds=60):
    print(f"Пакетная публикация: {len(topics)} статей")
    for i, topic in enumerate(topics, 1):
        print(f"\n[{i}/{len(topics)}]")
        try:
            publish_to_dzen(topic)
        except Exception as e:
            print(f"  ОШИБКА: {e}")
        if i < len(topics):
            print(f"  Пауза {delay_seconds} сек...")
            time.sleep(delay_seconds)


# ─── Точка входа ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование:")
        print("  Одна статья:  python dzen_publisher.py \"Тема статьи\"")
        print("  Из файла:     python dzen_publisher.py topics.txt")
        sys.exit(1)

    arg = sys.argv[1]

    if arg.endswith(".txt") and os.path.isfile(arg):
        with open(arg, encoding="utf-8") as f:
            topics = [line.strip() for line in f if line.strip()]
        publish_batch(topics)
    else:
        publish_to_dzen(arg)
