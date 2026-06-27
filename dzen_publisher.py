import os, sys, json, re, requests
from datetime import datetime
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

WP_URL      = os.getenv("WP_URL")
WP_USER     = os.getenv("WP_USER")
WP_APP_PASS = os.getenv("WP_APP_PASS")
WP_CATEGORY = int(os.getenv("WP_CATEGORY", "1"))
GROQ_API_KEY     = os.getenv("GROQ_API_KEY")
IDEOGRAM_API_KEY = os.getenv("IDEOGRAM_API_KEY")

GROQ_MODEL = "llama-3.3-70b-versatile"
groq_client = Groq(api_key=GROQ_API_KEY)
wp_auth = (WP_USER, WP_APP_PASS)

ARTICLE_SYSTEM = """Ты — опытный автор для Яндекс Дзен.
Пиши живым разговорным языком. Структура: вступ -> 3-5 блоков -> вывод.
Форматирование: только теги <p><h2><h3><ul><li><b><i>. Без markdown.
Объём: 800-1200 слов.
ВАЖНО: ответ должен быть валидным JSON в одну строку без переносов строк внутри значений."""

def fix_json(raw):
    """Исправляет литеральные управляющие символы внутри JSON-строк."""
    result = []
    in_string = False
    escape_next = False
    for ch in raw:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == '\\':
            result.append(ch)
            escape_next = True
        elif ch == '"':
            in_string = not in_string
            result.append(ch)
        elif in_string and ord(ch) < 0x20:
            if ch == '\n': result.append('\\n')
            elif ch == '\r': result.append('\\r')
            elif ch == '\t': result.append('\\t')
            # остальные управляющие символы — пропускаем
        else:
            result.append(ch)
    return ''.join(result)

def generate_article(topic):
    print("[1/4] Генерирую текст...")
    response = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": ARTICLE_SYSTEM},
            {"role": "user", "content": f"""Напиши статью для Яндекс Дзен на тему: {topic}

Верни JSON:
{{"title":"заголовок до 60 символов","html":"HTML текст","image_prompt":"описание обложки на английском, фотореализм, без текста, 16:9"}}"""}
        ],
        max_tokens=4096,
    )
    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    raw = fix_json(raw.strip())
    data = json.loads(raw)
    print(f"    Заголовок: {data['title']}")
    return data

def generate_cover_image(prompt):
    print("[2/4] Генерирую обложку (Ideogram)...")
    payload = {
        "image_request": {
            "prompt": f"{prompt}. Photorealistic, editorial style, no text, no watermarks.",
            "model": "V_2",
            "aspect_ratio": "ASPECT_16_9",
        }
    }
    print(f"    Запрос: {payload}")
    response = requests.post(
        "https://api.ideogram.ai/generate",
        headers={"Api-Key": IDEOGRAM_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=60,
    )
    print(f"    Статус: {response.status_code}")
    print(f"    Ответ: {response.text[:500]}")
    response.raise_for_status()
    img_url = response.json()["data"][0]["url"]
    return requests.get(img_url, timeout=30).content

def upload_image_to_wp(image_bytes, filename):
    print("[3/4] Загружаю обложку...")
    response = requests.post(
        f"{WP_URL}/wp-json/wp/v2/media",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "Content-Type": "image/jpeg"},
        data=image_bytes, auth=wp_auth, timeout=60,
    )
    response.raise_for_status()
    media_id = response.json()["id"]
    print(f"    Media ID: {media_id}")
    return media_id

def publish_post(title, html, media_id):
    print("[4/4] Публикую пост...")
    response = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        json={"title": title, "content": html, "status": "publish",
              "featured_media": media_id, "categories": [WP_CATEGORY],
              "comment_status": "closed"},
        auth=wp_auth, timeout=30,
    )
    response.raise_for_status()
    post = response.json()
    print(f"    URL: {post['link']}")
    return post

def publish_to_dzen(topic):
    print(f"\n{'='*55}\n  {topic}\n{'='*55}\n")
    article = generate_article(topic)
    image_bytes = generate_cover_image(article["image_prompt"])
    filename = f"cover_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    media_id = upload_image_to_wp(image_bytes, filename)
    post = publish_post(article["title"], article["html"], media_id)
    print(f"\nГотово! {post['link']}\n")

def publish_batch(topics, delay_seconds=60):
    import time
    for i, topic in enumerate(topics, 1):
        print(f"\n[{i}/{len(topics)}]")
        try:
            publish_to_dzen(topic)
        except Exception as e:
            print(f"  ОШИБКА: {e}")
        if i < len(topics):
            time.sleep(delay_seconds)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("python dzen_publisher.py \"Тема\" или topics.txt")
        sys.exit(1)
    arg = sys.argv[1]
    if arg.endswith(".txt") and os.path.isfile(arg):
        with open(arg, encoding="utf-8") as f:
            topics = [l.strip() for l in f if l.strip()]
        publish_batch(topics)
    else:
        publish_to_dzen(arg)
