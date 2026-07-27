"""
publish_news_articles.py — публикация двух новостных статей (сингулярность
Альтмана + инцидент с побегом ИИ-агента; релиз открытых весов Kimi K3),
написанных на основе реальных новостей 26-27.07.2026, проверенных перекрёстно
по нескольким независимым источникам (Reuters, ВЗГЛЯД, RT, Vesti, Fontanka,
techtimes, qz.com, interconnects.ai и др.).

Это НОВЫЕ темы, не пересекающиеся с тем, что Владимир мог вручную опубликовать
в Дзен ранее (см. правило в ДОСТУПЫ_НЕ_СПРАШИВАТЬ_ПОВТОРНО.txt после инцидента
27.07.2026 с article14/article15).

Обложки — через Ideogram (ключ IDEOGRAM_API_KEY восстановлен 27.07.2026).
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
        "title": "Альтман заявил о «точке сингулярности»: что на самом деле произошло",
        "slug": "altman-tochka-singulyarnosti-chto-proizoshlo",
        "content_file": "post_singularity_content.html",
        "ideogram_prompt": "A dramatic photorealistic image of a glowing abstract neural network "
                            "breaking out of a glass containment box on a dark server room background, "
                            "editorial tech photography style, no text, no logos",
    },
    {
        "title": "Крупнейшая открытая ИИ-модель в истории вышла бесплатно: разбираюсь, кому она реально нужна",
        "slug": "kimi-k3-krupneyshaya-otkrytaya-model",
        "content_file": "post_kimi_k3_content.html",
        "ideogram_prompt": "A photorealistic image of a massive server room data center with rows of "
                            "glowing GPU racks, blue and white lighting, editorial tech photography style, "
                            "no text, no logos",
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
