"""
publish_new_articles.py — одноразовый скрипт публикации ДВУХ готовых статей
(article14 "Голос вместо текста", article15 "Голосовой помощник дома"),
написанных вручную и уже прошедших ту же проверку объёма/структуры
(check_structure из dzen_publisher.py: 1200+ слов, 1×h2 + 5×h3), что теперь
применяется к автогенерации после инцидента с постом 2505 (26.07.2026).

В отличие от backfill_covers.py (правит существующие посты), этот скрипт
СОЗДАЁТ два новых поста с нуля: генерирует обложку через Ideogram, грузит
её в WP, создаёт пост с готовым содержимым и сразу ставит featured_media,
статус — publish.

Использует те же секреты, что и dzen_publisher.py / backfill_covers.py.
"""

import os
import requests

WP_URL = os.environ["WP_URL"].rstrip("/")
WP_USER = os.environ["WP_USER"]
WP_APP_PASS = os.environ["WP_APP_PASS"]
IDEOGRAM_API_KEY = os.environ["IDEOGRAM_API_KEY"]

wp_auth = (WP_USER, WP_APP_PASS)

JOBS = [
    {
        "title": "Голос вместо текста: диктую статьи через ИИ за 10 минут вместо часа",
        "slug": "golos-vmesto-teksta-dictuyu-stati",
        "content_file": "post_article14_content.html",
        "ideogram_prompt": "A person speaking into a smartphone voice recorder at a home desk, "
                            "sound waves visually flowing from the phone into text appearing on a "
                            "laptop screen nearby, warm home office lighting, photorealistic editorial style",
    },
    {
        "title": "Голосовой помощник дома: настроил дешевле чем Яндекс Алиса",
        "slug": "golosovoy-pomoshnik-deshevle-alisy",
        "content_file": "post_article15_content.html",
        "ideogram_prompt": "A sleek gaming desktop PC tower with glowing lights on a desk at home "
                            "next to a small smart speaker for comparison, cozy living room lighting, "
                            "photorealistic editorial style",
    },
]

def run_diag():
    try:
        response = requests.get(f"{WP_URL}/wp-json/diag/v1/auth", auth=wp_auth, timeout=30)
        print(f"DIAG status: {response.status_code}, body: {response.text[:300]}")
    except Exception as e:
        print(f"DIAG failed: {e}")

def generate_cover_image(prompt):
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
    image_obj = response.json()["data"][0]
    img_response = requests.get(image_obj["url"], timeout=60)
    img_response.raise_for_status()
    content_type = img_response.headers.get("content-type", "").split(";")[0].strip()
    print(f"  Разрешение: {image_obj.get('resolution')}, размер: {len(img_response.content)} байт, формат: {content_type}")
    return img_response.content, content_type

def upload_image_to_wp(image_bytes, filename, content_type="image/jpeg"):
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
            image_bytes, content_type = generate_cover_image(job["ideogram_prompt"])
            ext = "png" if "png" in content_type else "jpg"
            media_id = upload_image_to_wp(image_bytes, f"cover_{job['slug']}.{ext}", content_type=content_type)
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
