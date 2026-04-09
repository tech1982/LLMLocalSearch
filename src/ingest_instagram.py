"""
Instagram post ingestion via Instaloader.
Pulls captions from posts of followed accounts or specified accounts.
"""
import os
import sys
import time
from datetime import datetime
from dotenv import load_dotenv
import instaloader

load_dotenv()

INSTA_USER = os.environ.get("INSTA_USERNAME", "")
INSTA_PASS = os.environ.get("INSTA_PASSWORD", "")
INSTA_ACCOUNTS = [a.strip() for a in os.environ.get("INSTA_ACCOUNTS", "").split(",") if a.strip()]
MAX_POSTS = int(os.environ.get("MAX_POSTS_PER_ACCOUNT", 200))
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SESSION_DIR = os.environ.get("SESSION_DIR", os.path.join(_PROJECT_ROOT, "sessions"))


def login() -> instaloader.Instaloader:
    """Login to Instagram."""
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )

    session_file = os.path.join(SESSION_DIR, f"insta_session_{INSTA_USER}")

    try:
        L.load_session_from_file(INSTA_USER, session_file)
        print(f"✅ Loaded Instagram session for {INSTA_USER}")
    except FileNotFoundError:
        if not INSTA_USER or not INSTA_PASS:
            print("❌ INSTA_USERNAME and INSTA_PASSWORD must be set in .env")
            sys.exit(1)
        print(f"🔐 Logging in as {INSTA_USER}...")
        L.login(INSTA_USER, INSTA_PASS)
        L.save_session_to_file(session_file)
        print(f"✅ Logged in and session saved")

    return L


def get_followed_accounts(L: instaloader.Instaloader) -> list[str]:
    """Get list of accounts the user follows."""
    profile = instaloader.Profile.from_username(L.context, INSTA_USER)
    followees = []
    print(f"📋 Fetching followees for {INSTA_USER}...")
    for followee in profile.get_followees():
        followees.append(followee.username)
        if len(followees) % 50 == 0:
            print(f"  ... {len(followees)} accounts")
        time.sleep(0.5)  # Rate limit
    print(f"  Total followed: {len(followees)}")
    return followees


def ingest_account(L: instaloader.Instaloader, username: str) -> tuple[list, list, list]:
    """Pull posts from an Instagram account."""
    texts, metadatas, ids = [], [], []

    try:
        profile = instaloader.Profile.from_username(L.context, username)
    except Exception as e:
        print(f"  ❌ Cannot access {username}: {e}")
        return texts, metadatas, ids

    print(f"  📸 Account: {username} ({profile.mediacount} posts)")

    count = 0
    for post in profile.get_posts():
        if count >= MAX_POSTS:
            break

        caption = post.caption or ""
        if len(caption.strip()) < 10:
            continue

        text = caption.strip()
        post_date = post.date_utc.strftime("%Y-%m-%d %H:%M") if post.date_utc else ""
        url = f"https://www.instagram.com/p/{post.shortcode}/"
        doc_id = f"insta_{username}_{post.shortcode}"

        texts.append(text)
        metadatas.append({
            "source": "instagram",
            "channel": username,
            "author": profile.full_name or username,
            "date": post_date,
            "url": url,
            "topic": "",
            "message_id": post.shortcode,
        })
        ids.append(doc_id)

        count += 1
        if count % 50 == 0:
            print(f"    Collected {count} posts...")
            time.sleep(2)  # Rate limit

    print(f"  ✅ Collected {count} posts from {username}")
    return texts, metadatas, ids


def main():
    os.makedirs(SESSION_DIR, exist_ok=True)
    L = login()

    # Determine which accounts to index
    accounts = INSTA_ACCOUNTS
    if not accounts:
        print("\n📋 No specific accounts set, fetching all followed accounts...")
        accounts = get_followed_accounts(L)
        if not accounts:
            print("❌ No followed accounts found")
            sys.exit(1)
        print(f"Will index {len(accounts)} accounts")

    sys.path.insert(0, os.path.join(_PROJECT_ROOT, "src"))
    from search_engine import add_documents

    for i, account in enumerate(accounts, 1):
        print(f"\n🔄 [{i}/{len(accounts)}] Processing: {account}")
        try:
            texts, metadatas, ids = ingest_account(L, account)
            if texts:
                added = add_documents(texts, metadatas, ids)
                print(f"  📊 Indexed {added} new posts")
            time.sleep(3)  # Rate limit between accounts
        except Exception as e:
            print(f"  ⚠️ Error with {account}: {e}")
            time.sleep(10)

    print("\n🎉 Instagram ingestion complete!")


if __name__ == "__main__":
    main()
