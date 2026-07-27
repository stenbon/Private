"""
publish_new_articles.py — одноразовый скрипт публикации ДВУХ готовых статей
(article14 "Голос вместо текста", article15 "Голосовой помощник дома"),
написанных вручную и уже прошедших ту же проверку объёма/структуры
(check_structure из dzen_publisher.py: 1200+ слов, 1×h2 + 5×h3).

27.07.2026: IDEOGRAM_API_KEY стал возвращать 401 (см. заметку в
ДОСТУПЫ_НЕ_СПРАШИВАТЬ_ПОВТОРНО.txt на стороне пользователя) — весь пайплайн
Ideogram недоступен. Чтобы не публиковать статьи БЕЗ обложки (это и был
исходный баг, который чинили 26.07 — пост 2505), обложка временно генерируется
локально через Pillow: простая графическая карточка с градиентным фоном и
заголовком статьи, без обращения к каким-либо AI-сервисам. Как только
IDEOGRAM_API_KEY почему будет восстановлен, для этих двух постов можно будет
отдельно прогнать backfill с настоящей фотореалистичной обложкой (по образцу
backfill_covers.py), а этот plaсeholder — временное решение, чтобы не блокировать
срочную публикацию.
"""

import os
import textwrap
import requests
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

WP_URL = os.environ["WP_URL"].rstrip("/")
WP_USER = os.environ["WP_USER"]
WP_APP_PASS = os.environ["WP_APP_PASS"]

wp_auth = (WP_USER, WP_APP_PASS)

JOBS = [
    {
        "title": "Голос вместо текста: диктую статьи через ИИ за 10 минут вместо часа",
        "slug": "golos-vmesto-teksta-dictuyu-stati",
        "content_file": "post_article14_content.html",
        "cover_bg": ((30, 40, 80), (70, 100, 180)),  # тёмно-синий градиент
    },
    {
        "title": "Голосовой помощник дома: настроил дешевле чем Яндекс Алиса",
        "slug": "golosovoy-pomoshnik-deshevle-alisy",
        "content_file": "post_article15_content.html",
        "cover_bg": ((40, 30, 60), (140, 70, 160)),  # тёмно-фиолетовый градиент
    },
]

FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

def run_diag():
    try:
        response = requests.get(f"{WP_URL}/wp-json/diag/v1/auth", auth=wp_auth, timeout=30)
        print(f"DIAG status: {response.status_code}, body: {response.text[:300]}")
    except Exception as e:
        print(f"DIAG failed: {e}")

def load_font(size):
    for path in FONT_PATHS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()

def generate_placeholder_cover(title, bg_colors):
    width, height = 1600, 900
    top_color, bottom_color = bg_colors
    img = Image.new("RGB", (width, height), top_color)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / height
        r = int(top_color[0] * (1 - t) + bottom_color[0] * t)
        g = int(top_color[1] * (1 - t) + bottom_color[1] * t)
        b = int(top_color[2] * (1 - t) + bottom_color[2] * t)
        draw.line([(0, y), (width, y)], fill=(r, g, b))

    font = load_font(72)
    wrapped = textwrap.wrap(title, width=22)
    line_height = 90
    total_height = line_height * len(wrapped)
    y = (height - total_height) / 2
    for line in wrapped:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_width = bbox[2] - bbox[0]
        x = (width - line_width) / 2
        draw.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += line_height

    buf = BytesIO()
    img.save(buf, format="PNG")
    print(f"  Обложка сгенерирована локально (Pillow), {width}x{height}, {buf.tell()} байт")
    return buf.getvalue(), "image/png"

def upload_image_to_wp(image_bytes, filename, content_type="image/png"):
    response = requests.post(
        f"{WP_URL}/wp-json/wp/v2/media",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": content_type,
        },
        data=image_bytes,
        auth=wp_auth,
        timeout=60,
    )
    response.raise_for_status()
    media_id = response.json()["id"]
    print(f"  Media ID: {media_id}")
    return media_id

def create_post(title, slug, content, media_id):
    payload = {
        "title": title,
        "slug": slug,
        "content": content,
        "status": "publish",
        "featured_media": media_id,
    }
    response = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        json=payload,
        auth=wp_auth,
        timeout=60,
    )
    if response.status_code >= 400:
        print(f"  CREATE FAILED: HTTP {response.status_code} — {response.text[:500]}")
    response.raise_for_status()
    data = response.json()
    print(f"  Пост создан: ID {data['id']}, ссылка: {data.get('link')}")
    return data

def main():
    run_diag()
    if os.environ.get("DIAG_ONLY") == "1":
        return
    failures = []
    for job in JOBS:
        print(f"Публикую: {job['title']}...")
        try:
            with open(job["content_file"], encoding="utf-8") as f:
                content = f.read()
            image_bytes, content_type = generate_placeholder_cover(job["title"], job["cover_bg"])
            media_id = upload_image_to_wp(image_bytes, f"cover_{job['slug']}.png", content_type=content_type)
            create_post(job["title"], job["slug"], content, media_id)
        except Exception as e:
            print(f"  ОШИБКА на статье '{job['title']}': {e}")
            failures.append(job["title"])
    if failures:
        print(f"Не удалось опубликовать: {failures}")
    else:
        print("Обе статьи успешно опубликованы.")

if __name__ == "__main__":
    main()
