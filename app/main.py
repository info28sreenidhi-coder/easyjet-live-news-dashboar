import hashlib
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from flask import Flask, jsonify, send_from_directory

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "news.db")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "900"))  # source check every 15 min
REQUEST_TIMEOUT = 25
MAX_ARTICLES = 100
HEALTH_SOURCE_URL = "https://timesofindia.indiatimes.com/health"
NEWS_WINDOW_DAYS = int(os.getenv("NEWS_WINDOW_DAYS", "30"))
SLIDE_SECONDS = int(os.getenv("SLIDE_SECONDS", "30"))

# Official easyJet Media Centre pages only.
LISTING_URLS = [
    "https://www.easyjet.com/en/news/stories",
    "https://www.easyjet.com/en/news/stories/2",
    "https://www.easyjet.com/en/news/stories/3",
    "https://www.easyjet.com/en/news/stories/4",
    "https://www.easyjet.com/en/news/airline/stories",
    "https://www.easyjet.com/en/news/airline/stories/2",
    "https://www.easyjet.com/en/news/holidays/stories",
    "https://www.easyjet.com/en/news/holidays/stories/2",
    "https://www.easyjet.com/en/news/articles",
    "https://www.easyjet.com/en/news/articles/1",
    "https://www.easyjet.com/en/news/articles/2",
    "https://www.easyjet.com/en/news/airline/articles",
    "https://www.easyjet.com/en/news/airline/articles/2",
    "https://www.easyjet.com/en/news/holidays/articles/1",
    "https://www.easyjet.com/en/news/holidays/articles/2",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

SOURCE_URL = "https://www.easyjet.com/en/news/stories"

app = Flask(__name__, static_folder=os.path.join(BASE, "static"))
lock = threading.Lock()

state = {
    "last_run": None,
    "last_ok": None,
    "last_error": None,
    "counts": {},
}


def db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            published TEXT,
            category TEXT,
            image TEXT,
            summary TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )
        """
    )
    # Safe migration for an older database created by the first version.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    if "summary" not in columns:
        conn.execute("ALTER TABLE articles ADD COLUMN summary TEXT")
    conn.commit()
    return conn


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def parse_date(value):
    value = clean(value)
    if not value:
        return None
    try:
        dt = dateparser.parse(value, dayfirst=False, fuzzy=True)
        if dt:
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
    except Exception:
        pass
    return None


def fingerprint(source, url, title):
    return hashlib.sha256(
        f"{source}|{url}|{title}".encode("utf-8")
    ).hexdigest()


def is_listing_url(url):
    path = urlparse(url).path.rstrip("/")
    listing_paths = {
        "/en/news/stories",
        "/en/news/articles",
        "/en/news/airline",
        "/en/news/airline/stories",
        "/en/news/airline/articles",
        "/en/news/holidays",
        "/en/news/holidays/stories",
        "/en/news/holidays/articles",
    }
    if path in listing_paths:
        return True
    if re.fullmatch(r"/en/news/(stories|articles)/\d+", path):
        return True
    if re.fullmatch(r"/en/news/(airline|holidays)/(stories|articles)/\d+", path):
        return True
    return False


def is_article_url(url):
    if not url.startswith("https://www.easyjet.com/en/news/"):
        return False
    if is_listing_url(url):
        return False

    path = urlparse(url).path
    return "/story/" in path or "/articles/" in path


def category_for(url, text=""):
    path = urlparse(url).path.lower()
    if "/holidays/" in path or "holiday" in text.lower():
        return "Holidays"
    return "News"


def extract_date_from_text(text):
    patterns = [
        r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})\b",
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\s+\d{4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return parse_date(match.group(1))
    return None


def parse_relative_time(value):
    value = clean(value)
    match = re.search(r"\b(\d+)\s+(minute|minutes|hour|hours|day|days)\s+ago\b", value, re.I)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("minute"):
        delta = timedelta(minutes=amount)
    elif unit.startswith("hour"):
        delta = timedelta(hours=amount)
    else:
        delta = timedelta(days=amount)
    return (datetime.now(timezone.utc) - delta).isoformat()


def title_and_excerpt(anchor):
    raw = clean(anchor.get_text(" ", strip=True))
    if not raw:
        return None, None

    # easyJet listing cards contain: headline + publication date + short excerpt.
    date_match = re.search(
        r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{4}\b",
        raw,
        re.I,
    )
    if date_match:
        title = clean(raw[:date_match.start()])
        excerpt = clean(raw[date_match.end():])
    else:
        title = raw
        excerpt = ""

    excerpt = re.sub(r"\bread\s+more\b.*$", "", excerpt, flags=re.I).strip()
    title = re.sub(r"\bread\s+more\b.*$", "", title, flags=re.I).strip()
    title = re.sub(r"\b(?:Article|Holidays\s*[-–]\s*(?:Consumer|trade))\b", "", title, flags=re.I)
    title = clean(title)
    excerpt = clean(excerpt)

    if len(title) < 12 or "@easyjet.com" in title.lower():
        return None, None
    return title, excerpt or None


def image_from_container(container, base_url):
    if not container:
        return None
    img = container.find("img")
    if not img:
        return None
    for attr in ("src", "data-src", "data-lazy-src", "data-original"):
        value = img.get(attr)
        if value:
            return urljoin(base_url, value)
    srcset = img.get("srcset")
    if srcset:
        value = srcset.split(",")[0].strip().split(" ")[0]
        if value:
            return urljoin(base_url, value)
    return None


def parse_listing(html, listing_url):
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for a in soup.select("a[href]"):
        href = urljoin(listing_url, a.get("href", "")).split("#")[0]
        if not is_article_url(href):
            continue

        title, excerpt = title_and_excerpt(a)
        if not title:
            continue

        parent = a
        blob = clean(a.get_text(" ", strip=True))
        for _ in range(6):
            parent = parent.parent if parent else None
            if not parent:
                break
            blob = clean(parent.get_text(" ", strip=True))
            if extract_date_from_text(blob):
                break

        published = extract_date_from_text(blob)
        if not published:
            published = extract_date_from_text(a.get_text(" ", strip=True))

        image = image_from_container(parent, href)

        results.append(
            {
                "source": "easyjet",
                "title": title,
                "url": href,
                "published": published,
                "category": category_for(href, a.get_text(" ", strip=True)),
                "image": image,
                "summary": excerpt,
            }
        )

    return results


def parse_health_page(html, listing_url):
    """Return the single newest health story visible on the official TOI Health page."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    seen = set()

    for a in soup.select("a[href]"):
        href = urljoin(listing_url, a.get("href", "")).split("#")[0]
        if "timesofindia.indiatimes.com/health/" not in href:
            continue
        if "articleshow" not in href:
            continue
        title = clean(a.get_text(" ", strip=True))
        if len(title) < 18 or title.lower() in seen:
            continue
        # Ignore navigation/promotional labels.
        if title.lower() in {"load more stories", "see all", "read more"}:
            continue
        seen.add(title.lower())

        parent = a
        blob = clean(a.get_text(" ", strip=True))
        for _ in range(5):
            parent = parent.parent if parent else None
            if not parent:
                break
            blob = clean(parent.get_text(" ", strip=True))
            if extract_date_from_text(blob) or parse_relative_time(blob):
                break

        published = extract_date_from_text(blob) or parse_relative_time(blob)
        image = image_from_container(parent, href)
        summary = ""
        if parent:
            # Prefer a nearby non-navigation sentence.
            for node in parent.find_all(["p", "div", "span"]):
                text = clean(node.get_text(" ", strip=True))
                if 45 <= len(text) <= 700 and text.lower() != title.lower():
                    low = text.lower()
                    if not any(x in low for x in ["subscribe", "advertisement", "read more"]):
                        summary = text
                        break

        candidates.append({
            "source": "timesofindia_health",
            "title": title,
            "url": href,
            "published": published,
            "category": "Health News",
            "image": image,
            "summary": summary,
        })

    # The page is ordered newest-first; keep the first usable article.
    return candidates[0] if candidates else None


def fetch_health_story():
    try:
        html = fetch_page(HEALTH_SOURCE_URL)
        item = parse_health_page(html, HEALTH_SOURCE_URL)
        if not item:
            raise RuntimeError("No current health article found on Times of India Health")
        summary, image = extract_article_details_generic(item["url"], item.get("summary"), item.get("image"))
        item["summary"], item["image"] = summary, image
        return item, None
    except Exception as exc:
        return None, str(exc)


def fetch_page(url):
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def extract_article_details_generic(url, fallback_summary, fallback_image=None):
    """Read an article page for description and lead image, regardless of source."""
    try:
        html = fetch_page(url)
        soup = BeautifulSoup(html, "html.parser")
        candidates = []
        image = None
        for selector in [
            'meta[property="og:description"]',
            'meta[name="description"]',
        ]:
            tag = soup.select_one(selector)
            if tag and tag.get("content"):
                candidates.append(clean(tag.get("content")))

        for selector in [
            'meta[property="og:image"]',
            'meta[name="twitter:image"]',
        ]:
            tag = soup.select_one(selector)
            if tag and tag.get("content"):
                image = urljoin(url, tag.get("content"))
                break

        roots = soup.select("main article, article, main")
        for root in roots[:4]:
            for p in root.find_all("p"):
                text = clean(p.get_text(" ", strip=True))
                if 45 <= len(text) <= 700:
                    lowered = text.lower()
                    if not any(x in lowered for x in [
                        "cookie", "privacy", "terms and conditions", "sign in",
                        "manage bookings", "check in", "find cheap flights",
                    ]):
                        candidates.append(text)

        unique = []
        seen = set()
        for text in candidates:
            key = re.sub(r"[^a-z0-9]+", "", text.lower())
            if key and key not in seen:
                seen.add(key)
                unique.append(text)

        summary = clean(" ".join(unique[:3]))[:1100] if unique else fallback_summary
        return summary or fallback_summary, image or fallback_image
    except Exception:
        return fallback_summary, fallback_image


def extract_article_details(url, fallback_summary, fallback_image=None):
    return extract_article_details_generic(url, fallback_summary, fallback_image)

def dedupe(items):
    by_url = {}
    for item in items:
        url = item["url"].split("#")[0]
        if url not in by_url:
            by_url[url] = item
        else:
            old = by_url[url]
            if not old.get("published") and item.get("published"):
                old["published"] = item["published"]
            if not old.get("image") and item.get("image"):
                old["image"] = item["image"]
            if not old.get("summary") and item.get("summary"):
                old["summary"] = item["summary"]
            if len(item["title"]) > len(old["title"]) and len(old["title"]) < 40:
                old["title"] = item["title"]
    return list(by_url.values())


def within_news_window(item):
    if not item.get("published"):
        return False
    try:
        published = dateparser.parse(item["published"])
        cutoff = datetime.now(timezone.utc) - timedelta(days=NEWS_WINDOW_DAYS)
        return published >= cutoff
    except Exception:
        return False


def fetch_source():
    all_items = []
    errors = []

    for listing_url in LISTING_URLS:
        try:
            html = fetch_page(listing_url)
            all_items.extend(parse_listing(html, listing_url))
        except Exception as exc:
            errors.append(f"{listing_url}: {exc}")

    items = dedupe(all_items)
    items = [item for item in items if within_news_window(item)]
    items.sort(key=lambda x: x.get("published") or "", reverse=True)

    # Build explanations only for the current 30-day window.
    for item in items[:MAX_ARTICLES]:
        item["summary"], item["image"] = extract_article_details(
            item["url"], item.get("summary"), item.get("image")
        )

    if not items and errors:
        raise RuntimeError("; ".join(errors))

    return items[:MAX_ARTICLES], errors


def sync():
    if not lock.acquire(blocking=False):
        return

    state["last_run"] = datetime.now(timezone.utc).isoformat()
    conn = None

    try:
        conn = db()
        now = datetime.now(timezone.utc).isoformat()

        try:
            items, fetch_errors = fetch_source()
            state["counts"] = {"easyjet": len(items)}

            for item in items:
                article_id = fingerprint(item["source"], item["url"], item["title"])
                conn.execute(
                    """
                    INSERT INTO articles
                    (id, source, title, url, published, category, image, summary, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title=excluded.title,
                        url=excluded.url,
                        published=COALESCE(excluded.published, articles.published),
                        category=excluded.category,
                        image=COALESCE(excluded.image, articles.image),
                        summary=COALESCE(excluded.summary, articles.summary),
                        last_seen=excluded.last_seen
                    """,
                    (
                        article_id,
                        item["source"],
                        item["title"],
                        item["url"],
                        item["published"],
                        item["category"],
                        item["image"],
                        item.get("summary"),
                        now,
                        now,
                    ),
                )

            # Keep the database aligned with the dashboard's 30-day window.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=NEWS_WINDOW_DAYS)).isoformat()
            conn.execute(
                "DELETE FROM articles WHERE source='easyjet' AND (published IS NULL OR published < ?)",
                (cutoff,),
            )
            conn.commit()

            state["last_ok"] = True
            state["last_error"] = " | ".join(fetch_errors) if fetch_errors else None
        except Exception as exc:
            state["last_ok"] = False
            state["last_error"] = f"easyjet: {exc}"
    finally:
        if conn:
            conn.close()
        lock.release()


def background():
    sync()
    while True:
        time.sleep(POLL_SECONDS)
        sync()


def row_to_dict(row):
    return dict(row)


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/news")
def news():
    conn = db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=NEWS_WINDOW_DAYS)).isoformat()
    rows = conn.execute(
        """
        SELECT *
        FROM articles
        WHERE source = 'easyjet' AND published IS NOT NULL AND published >= ?
        ORDER BY published DESC, first_seen DESC
        LIMIT 80
        """,
        (cutoff,),
    ).fetchall()
    conn.close()

    now = datetime.now(timezone.utc)
    articles = []
    for row in rows:
        item = row_to_dict(row)
        item["is_new"] = False
        item["slide_type"] = "easyjet"
        try:
            first_seen = dateparser.parse(item["first_seen"])
            item["is_new"] = (now - first_seen).total_seconds() < 86400
        except Exception:
            pass
        articles.append(item)

    # Exactly one current health story is appended after all easyJet stories.
    health, health_error = fetch_health_story()
    if health:
        health["id"] = "health-current"
        health["first_seen"] = now.isoformat()
        health["last_seen"] = now.isoformat()
        health["is_new"] = False
        health["slide_type"] = "health"
        articles.append(health)

    # The final slide is a fixed cyber-security awareness card.
    articles.append({
        "id": "security-awareness",
        "source": "internal_awareness",
        "title": "Cybersecurity Awareness",
        "url": "#security-awareness",
        "published": None,
        "category": "Security Awareness",
        "image": None,
        "summary": "",
        "first_seen": now.isoformat(),
        "last_seen": now.isoformat(),
        "is_new": False,
        "slide_type": "security",
    })

    combined_state = dict(state)
    combined_state["health_ok"] = bool(health)
    combined_state["health_error"] = health_error

    return jsonify(
        {
            "articles": articles,
            "updated_at": state["last_run"],
            "next_check_seconds": POLL_SECONDS,
            "slide_seconds": SLIDE_SECONDS,
            "news_window_days": NEWS_WINDOW_DAYS,
            "source_url": SOURCE_URL,
            "health_source_url": HEALTH_SOURCE_URL,
            "state": combined_state,
        }
    )


@app.get("/api/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "poll_seconds": POLL_SECONDS,
            "news_window_days": NEWS_WINDOW_DAYS,
            "slide_seconds": SLIDE_SECONDS,
            "last_run": state["last_run"],
            "last_ok": state["last_ok"],
            "last_error": state["last_error"],
        }
    )


if __name__ == "__main__":
    threading.Thread(target=background, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
else:
    threading.Thread(target=background, daemon=True).start()
