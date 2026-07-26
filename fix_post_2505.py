"""
Разовый фикс поста 2505: генерирует обложку (Ideogram), загружает её в WP,
ставит featured_media и заменяет содержимое поста на расширенную версию
(post_2505_content.html) — 1225 слов, структура 1×h2+5×h3.
Инцидент и обоснование: см. ДОСТУПЫ_НЕ_СПРАШИВАТЬ_ПОВТОРНО.txt в Dzen writer Pro.
"""
import os
import requests

WP_URL = os.environ["WP_URL"].rstrip("/")
WP_USER = os.environ["WP_USER"]
WP_APP_PASS = os.environ["WP_APP_PASS"]
IDEOGRAM_API_KEY = os.environ["IDEOGRAM_API_KEY"]

wp_auth = (WP_USER, WP_APP_PASS)
POST_ID = 2505
IMAGE_PROMPT = (
    "A laptop screen during a video call showing a live meeting transcript "
    "scrolling with speaker labels, blurred colleagues in the background on "
    "a video grid, warm modern office lighting, photorealistic editorial "
    "style, no text, no watermarks"
)


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
    print(f"  Ideogram OK: {image_obj.get('resolution')}, {len(img_response.content)} байт, {content_type}")
    return img_response.content, content_type


def upload_image_to_wp(image_bytes, filename, content_type):
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


def update_post(post_id, media_id, html_content):
    response = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts/{post_id}",
        json={"featured_media": media_id, "content": html_content},
        auth=wp_auth,
        timeout=30,
    )
    if response.status_code >= 400:
        print(f"  UPDATE FAILED: HTTP {response.status_code} — {response.text[:500]}")
    response.raise_for_status()
    print(f"  Пост {post_id} обновлён: featured_media={media_id}, содержимое заменено")


def main():
    with open("post_2505_content.html", encoding="utf-8") as f:
        html_content = f.read()
    print(f"Загружен контент: {len(html_content)} символов")

    image_bytes, content_type = generate_cover_image(IMAGE_PROMPT)
    ext = "png" if "png" in content_type else "jpg"
    media_id = upload_image_to_wp(image_bytes, f"cover_backfill_{POST_ID}.{ext}", content_type)
    update_post(POST_ID, media_id, html_content)
    print("Готово.")


if __name__ == "__main__":
    main()
