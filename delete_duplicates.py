"""
delete_duplicates.py — экстренное ПОЛНОЕ удаление (не draft, а force delete)
постов 2526/2528 (article14/article15), которые дублировали темы, уже
опубликованные Владимиром вручную в самом Дзен 26-27.07.2026. Ранее переведены
в draft (см. unpublish_duplicates.py), сейчас по прямому указанию — удаляются
насовсем.

ВАЖНОЕ ПРАВИЛО (зафиксировано 27.07.2026 после этого инцидента): контент
сайта (000l.ru) и контент Дзен — РАЗНЫЕ пулы, пока Дзен не даст разрешение
на автоматический RSS-импорт с сайта. Не публиковать на сайте темы, которые
Владимир уже/собирается публиковать в Дзен вручную, без явного разрешения.
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
        r = requests.delete(
            f"{WP_URL}/wp-json/wp/v2/posts/{post_id}",
            params={"force": "true"},
            auth=wp_auth,
            timeout=30,
        )
        if r.status_code >= 400:
            print(f"Пост {post_id}: FAILED HTTP {r.status_code} — {r.text[:300]}")
        else:
            data = r.json()
            print(f"Пост {post_id}: удалён (deleted={data.get('deleted')})")

if __name__ == "__main__":
    main()
