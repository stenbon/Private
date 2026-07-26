"""
backfill_covers.py — одноразовый бэкфилл обложек (Ideogram) для уже
опубликованных постов, у которых нет featured image.
Использует те же секреты, что и dzen_publisher.py.
"""

import os
import requests

WP_URL = os.environ["WP_URL"].rstrip("/")
WP_USER = os.environ["WP_USER"]
WP_APP_PASS = os.environ["WP_APP_PASS"]
IDEOGRAM_API_KEY = os.environ["IDEOGRAM_API_KEY"]

wp_auth = (WP_USER, WP_APP_PASS)

# 2496/2501/2503 уже обработаны раньше (см. историю коммитов) — оставлены
# закомментированными для справки, НЕ перезапускать, чтобы не перегенерировать
# им обложки заново и не жечь квоту Ideogram зря.
JOBS = [
    # (2496, "..."),  # уже сделано
    # (2501, "..."),  # уже сделано
    # (2503, "..."),  # уже сделано
    (2505, "A laptop screen during a video call showing a live meeting transcript scrolling with speaker labels, blurred colleagues in the background on a video grid, warm modern office lighting, photorealistic editorial style"),
]

# Посты, для которых помимо обложки нужно ещё заменить содержимое (одноразовый
# фикс после инцидента 26.07.2026 — короткая статья без структуры).
CONTENT_REPLACEMENTS = {
    2505: "post_2505_content.html",
}

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

def set_featured_media(post_id, media_id):
    payload = {"featured_media": media_id}
    if post_id in CONTENT_REPLACEMENTS:
        with open(CONTENT_REPLACEMENTS[post_id], encoding="utf-8") as f:
            payload["content"] = f.read()
        print(f"  Post {post_id}: содержимое будет заменено из {CONTENT_REPLACEMENTS[post_id]}")
    response = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts/{post_id}",
        json=payload,
        auth=wp_auth,
        timeout=30,
    )
    if response.status_code >= 400:
        print(f"  UPDATE FAILED: HTTP {response.status_code} — {response.text[:500]}")
    response.raise_for_status()
    print(f"  Post {post_id}: featured_media установлен на {media_id}")

def main():
    run_diag()
    if os.environ.get("DIAG_ONLY") == "1":
        return
    failures = []
    for post_id, prompt in JOBS:
        print(f"Обрабатываю пост {post_id}...")
        try:
            image_bytes, content_type = generate_cover_image(prompt)
            ext = "png" if "png" in content_type else "jpg"
            media_id = upload_image_to_wp(image_bytes, f"cover_backfill_{post_id}.{ext}", content_type=content_type)
            set_featured_media(post_id, media_id)
        except Exception as e:
            print(f"  ОШИБКА на посте {post_id}: {e}")
            failures.append(post_id)
    if failures:
        print(f"Не удалось обработать: {failures}")
    else:
        print("Все посты успешно обработаны.")

if __name__ == "__main__":
    main()
