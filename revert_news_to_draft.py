import os, requests
WP_URL = os.environ["WP_URL"].rstrip("/")
WP_USER = os.environ["WP_USER"]
WP_APP_PASS = os.environ["WP_APP_PASS"]
wp_auth = (WP_USER, WP_APP_PASS)
for post_id in [2532, 2534]:
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/posts/{post_id}", json={"status": "draft"}, auth=wp_auth, timeout=30)
    print(post_id, r.status_code, r.json().get("status") if r.status_code < 400 else r.text[:200])
