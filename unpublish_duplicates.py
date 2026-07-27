"""
unpublish_duplicates.py — экстренный откат: перевести в draft два поста
(2526, 2528), опубликованные 27.07.2026 (article14/article15), т.к. по словам
Владимира эти темы уже были опубликованы им в самом Дзен вчера — новые посты
на 000l.ru дублируют контент, что через RSS уйдёт в канал Дзен как дубль.
"""
import os
import requests

WP_URL = os.environ["WP_URL"].rstrip("/")
WP_USER = os.environ["WP_USER"]
WP_APP_PASS = os.environ["WP_APP_PASS"]
wp_auth = (WP_USER, WP_APP_PASS)

POST_IDS = [2526, 2528]

def main():
    for post_id in POST_IDS:
        r = requests.post(
            f"{WP_URL}/wp-json/wp/v2/posts/{post_id}",
            json={"status": "draft"},
            auth=wp_auth,
            timeout=30,
        )
        if r.status_code >= 400:
            print(f"Пост {post_id}: FAILED HTTP {r.status_code} — {r.text[:300]}")
        else:
            data = r.json()
            print(f"Пост {post_id}: статус теперь '{data.get('status')}'")

if __name__ == "__main__":
    main()
