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

JOBS = [
    (2323, "A modern open-plan office with a large conference table, several empty chairs and only two people actually working attentively at laptops among a dozen unused seats, soft natural window light, muted corporate color palette, symbolizing that only a few companies get real results from technology adoption"),
    (2316, "A split composition: on one side an empty professional photo studio with unused camera and lighting equipment, on the other side a laptop screen glowing with a freshly generated product photo, cool modern office setting, symbolizing AI replacing expensive manual services"),
    (2313, "A minimalist office desk at dusk with a computer screen showing multiple browser windows and a workflow diagram actively updating on its own, no hands on the keyboard, soft blue screen glow lighting the room, symbolizing an autonomous AI agent completing a multi-step task by itself"),
    (2318, "A translator's desk with three separate monitors side by side, each showing a different translation app actively working on the same paragraph of text, clean modern workspace, soft daylight, muted color palette, symbolizing comparing several AI translation tools side by side"),
]


def run_diag():
    print("=== DIAG: /wp-json/diag/v1/auth ===")
    try:
        r = requests.get(f"{WP_URL}/wp-json/diag/v1/auth", auth=wp_auth, timeout=30)
        print(f"HTTP status: {r.status_code}")
        print(f"Body: {r.text[:1500]}")
    except Exception as e:
        print(f"DIAG request failed: {e}")
    print("=== END DIAG ===")


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
    print(f"  Ideogram status: {response.status_code}")
    if response.status_code >= 400:
        print(f"  Response body: {response.text[:1500]}")
    response.raise_for_status()
    result = response.json()

    image_obj = result["data"][0]
    if not image_obj.get("is_image_safe", True) or not image_obj.get("url"):
        raise RuntimeError(f"Ideogram отклонил генерацию (safety-check): {result}")

    img_response = requests.get(image_obj["url"], timeout=60)
    img_response.raise_for_status()
    print(f"  Разрешение: {image_obj.get('resolution')}, размер: {len(img_response.content)} байт")
    return img_response.content


def upload_image_to_wp(image_bytes, filename):
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
    if response.status_code >= 400:
        print(f"  WP media upload FAILED: HTTP {response.status_code}")
        print(f"  Response body: {response.text[:1500]}")
    response.raise_for_status()
    media_id = response.json()["id"]
    print(f"  Media ID: {media_id}")
    return media_id


def set_featured_media(post_id, media_id):
    response = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts/{post_id}",
        json={"featured_media": media_id},
        auth=wp_auth,
        timeout=30,
    )
    if response.status_code >= 400:
        print(f"  WP set featured_media FAILED: HTTP {response.status_code}")
        print(f"  Response body: {response.text[:1500]}")
    response.raise_for_status()
    print(f"  Пост {post_id}: featured_media установлен на {media_id}")


def main():
    run_diag()
    if os.environ.get("DIAG_ONLY") == "1":
        print("DIAG_ONLY=1 — завершаю без генерации обложек.")
        return

    failures = []
    for post_id, prompt in JOBS:
        print(f"\n=== Пост {post_id} ===")
        try:
            image_bytes = generate_cover_image(prompt)
            media_id = upload_image_to_wp(image_bytes, f"cover_backfill_{post_id}.jpg")
            set_featured_media(post_id, media_id)
        except Exception as e:
            print(f"  ОШИБКА на посте {post_id}: {e}")
            failures.append(post_id)

    print("\n" + "=" * 40)
    if failures:
        print(f"Готово с ошибками. Не удалось: {failures}")
        raise SystemExit(1)
    print("Готово! Все обложки установлены.")


if __name__ == "__main__":
    main()
