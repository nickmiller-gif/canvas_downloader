#!/usr/bin/env python3
"""
Canvas Link Harvester
Scans downloaded HTML files and attempts to retrieve linked content.

Handles:
  - Canvas-hosted files (via API with auth token)
  - Canvas media objects (direct download with auth)
  - Google Slides  → PDF + PPTX export
  - Google Docs    → PDF export
  - Google Sheets  → XLSX export
  - General webpages and direct file links → saved locally

Skips: YouTube, Vimeo, social media, and other streaming platforms.

Re-run safe: tracks processed links in _link_manifest.json and skips
already-downloaded content. Delete the manifest to force a full re-scan.

Usage:
    python canvas_link_harvester.py
"""

import os
import re
import json
import time
import requests
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlparse

# ──────────────────────────────────────────────
# CONFIGURATION — match your canvas_downloader.py settings
# ──────────────────────────────────────────────
CANVAS_BASE_URL = os.environ.get("CANVAS_URL", "https://canvas.cmu.edu")
CANVAS_API_TOKEN = os.environ.get("CANVAS_TOKEN")
DOWNLOAD_DIR = Path(os.environ.get("CANVAS_DOWNLOAD_DIR", os.path.expanduser("~/CMU")))

REQUEST_DELAY = 0.2        # seconds between requests
WEBPAGE_TIMEOUT = 20       # seconds before giving up on a webpage
LINKED_DIR_NAME = "_linked"
MANIFEST_FILE = DOWNLOAD_DIR / "_link_manifest.json"

# Domains to silently skip (streaming, social, etc.)
SKIP_DOMAINS = {
    "youtube.com", "youtu.be", "vimeo.com",
    "twitter.com", "x.com", "linkedin.com",
    "facebook.com", "instagram.com", "tiktok.com",
    "twitch.tv", "spotify.com", "soundcloud.com",
    "reddit.com", "wikipedia.org",
}
# ──────────────────────────────────────────────


canvas_session = requests.Session()
canvas_session.headers.update({"Authorization": f"Bearer {CANVAS_API_TOKEN}"})

plain_session = requests.Session()
plain_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
})


# ──────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", name)
    name = name.strip(". ")
    return name[:200] or "unnamed"


def filename_from_url(url: str) -> str:
    """Derive a base filename from a URL (uses final redirected URL)."""
    parsed = urlparse(url)
    domain = parsed.netloc.lstrip("www.")
    path_part = parsed.path.rstrip("/").split("/")[-1] or "index"
    path_part = path_part.split("?")[0] or "index"
    return sanitize_filename(f"{domain}_{path_part}")


def title_from_html(html: str) -> str | None:
    """Extract the <title> tag value from HTML."""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        title = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        return title or None
    return None


# ──────────────────────────────────────────────
# LINK EXTRACTION
# ──────────────────────────────────────────────

class _LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: set[str] = set()

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        for attr in ("href", "src", "data-api-endpoint"):
            val = attrs_dict.get(attr, "")
            if val and not val.startswith(("#", "mailto:", "javascript:", "data:")):
                self.links.add(val)


def extract_links(html_content: str) -> set[str]:
    """Extract and normalize all absolute HTTP(S) links from HTML."""
    parser = _LinkExtractor()
    try:
        parser.feed(html_content)
    except Exception:
        pass

    normalized = set()
    canvas_origin = CANVAS_BASE_URL.rstrip("/")
    for link in parser.links:
        if link.startswith("//"):
            link = "https:" + link
        elif link.startswith("/"):
            link = canvas_origin + link
        if link.startswith(("http://", "https://")):
            normalized.add(link)
    return normalized


# ──────────────────────────────────────────────
# LINK CLASSIFICATION
# ──────────────────────────────────────────────

def classify_link(url: str) -> str:
    """
    Returns one of:
      canvas_file | canvas_media | google_slides | google_doc |
      google_sheet | webpage | skip
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "skip"

    domain = parsed.netloc.lower().lstrip("www.")
    path = parsed.path
    canvas_domain = urlparse(CANVAS_BASE_URL).netloc.lower().lstrip("www.")

    if domain == canvas_domain:
        if re.search(r"/files/\d+", path):
            return "canvas_file"
        if "/media_objects/" in path or "/media_attachments/" in path:
            return "canvas_media"
        return "skip"

    if "docs.google.com" in domain:
        if "/presentation/" in path:
            return "google_slides"
        if "/document/" in path:
            return "google_doc"
        if "/spreadsheets/" in path:
            return "google_sheet"
        return "skip"

    if any(d in domain for d in SKIP_DOMAINS):
        return "skip"

    if parsed.scheme in ("http", "https"):
        return "webpage"

    return "skip"


# ──────────────────────────────────────────────
# CANVAS FILE / MEDIA DOWNLOAD
# ──────────────────────────────────────────────

MAX_RETRIES = 3


def _request_with_retry(sess, url, max_retries=MAX_RETRIES, **kwargs):
    """Make a request with retry on transient errors. Returns response or raises."""
    for attempt in range(1, max_retries + 1):
        time.sleep(REQUEST_DELAY)
        try:
            resp = sess.get(url, **kwargs)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                time.sleep(REQUEST_DELAY * (2 ** attempt))
                continue
            return resp
        except requests.exceptions.ConnectionError:
            if attempt < max_retries:
                time.sleep(REQUEST_DELAY * (2 ** attempt))
                continue
            raise
    return resp  # return last response even if bad status


def download_canvas_file(url: str, dest_dir: Path) -> tuple[bool, str]:
    """Resolve a Canvas file URL via the API and download it."""
    match = re.search(r"/files/(\d+)", url)
    if not match:
        return False, "Could not extract file ID from URL"
    file_id = match.group(1)

    try:
        resp = _request_with_retry(canvas_session, f"{CANVAS_BASE_URL}/api/v1/files/{file_id}")
        if resp.status_code == 404:
            return False, "404 — file no longer exists on Canvas"
        resp.raise_for_status()
        file_info = resp.json()
    except Exception as e:
        return False, f"API error: {e}"

    if not isinstance(file_info, dict) or not file_info.get("url"):
        return False, "No download URL in API response"

    filename = sanitize_filename(file_info.get("display_name", f"canvas_file_{file_id}"))
    dest_path = dest_dir / filename

    if dest_path.exists():
        return True, f"Already exists: {filename}"

    try:
        dl = _request_with_retry(canvas_session, file_info["url"], stream=True, allow_redirects=True)
        dl.raise_for_status()
        dest_dir.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as f:
            for chunk in dl.iter_content(8192):
                f.write(chunk)
        return True, f"Downloaded: {filename}"
    except Exception as e:
        return False, f"Download failed: {e}"


def download_canvas_media(url: str, dest_dir: Path) -> tuple[bool, str]:
    """Attempt direct download of a Canvas media object using auth."""
    if url.startswith("/"):
        url = CANVAS_BASE_URL.rstrip("/") + url

    try:
        resp = _request_with_retry(canvas_session, url, stream=True, allow_redirects=True, timeout=WEBPAGE_TIMEOUT)
        if resp.status_code == 404:
            return False, "404 — media no longer exists on Canvas"
        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "").split(";")[0].strip()
        if "text/html" in content_type:
            return False, "Got HTML page instead of media (likely auth-gated or deleted)"

        ext_map = {
            "video/mp4": ".mp4", "video/webm": ".webm", "video/ogg": ".ogv",
            "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "audio/wav": ".wav",
            "application/pdf": ".pdf",
        }
        ext = ext_map.get(content_type, "")
        media_id = url.rstrip("/").split("/")[-1]
        dest_path = dest_dir / sanitize_filename(f"media_{media_id}{ext}")

        if dest_path.exists():
            return True, f"Already exists: {dest_path.name}"

        dest_dir.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        return True, f"Downloaded: {dest_path.name}"
    except Exception as e:
        return False, f"Download failed: {e}"


# ──────────────────────────────────────────────
# GOOGLE EXPORT
# ──────────────────────────────────────────────

def download_google_file(url: str, link_type: str, dest_dir: Path) -> tuple[bool, str]:
    """Export a publicly shared Google Slides/Doc/Sheet."""
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if not match:
        return False, "Could not extract Google document ID"
    doc_id = match.group(1)

    exports: list[tuple[str, str]] = []
    if link_type == "google_slides":
        exports = [
            (f"https://docs.google.com/presentation/d/{doc_id}/export/pdf",  ".pdf"),
            (f"https://docs.google.com/presentation/d/{doc_id}/export/pptx", ".pptx"),
        ]
    elif link_type == "google_doc":
        exports = [
            (f"https://docs.google.com/document/d/{doc_id}/export?format=pdf", ".pdf"),
        ]
    elif link_type == "google_sheet":
        exports = [
            (f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=xlsx", ".xlsx"),
        ]

    results = []
    any_ok = False
    for export_url, ext in exports:
        label = link_type.split("_")[1]
        dest_path = dest_dir / f"google_{label}_{doc_id}{ext}"

        if dest_path.exists():
            results.append(f"Already exists: {dest_path.name}")
            any_ok = True
            continue

        try:
            resp = _request_with_retry(plain_session, export_url, timeout=WEBPAGE_TIMEOUT, allow_redirects=True)

            if "accounts.google.com" in resp.url:
                results.append(f"Private — requires Google login ({ext})")
                continue
            if resp.status_code in (401, 403):
                results.append(f"Access denied ({ext})")
                continue
            resp.raise_for_status()

            ct = resp.headers.get("content-type", "").lower()
            if "text/html" in ct and ext != ".html":
                results.append(f"Got HTML instead of {ext} (likely private or deleted)")
                continue

            dest_dir.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(resp.content)
            results.append(f"Downloaded: {dest_path.name}")
            any_ok = True
        except Exception as e:
            results.append(f"Export failed ({ext}): {e}")

    return any_ok, " | ".join(results) if results else "No exports attempted"


# ──────────────────────────────────────────────
# GENERAL WEBPAGE / FILE DOWNLOAD
# ──────────────────────────────────────────────

CONTENT_TYPE_EXT = {
    "application/pdf":                                                           ".pdf",
    "application/msword":                                                        ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":  ".docx",
    "application/vnd.ms-powerpoint":                                             ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation":".pptx",
    "application/vnd.ms-excel":                                                  ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":         ".xlsx",
    "application/zip":                                                           ".zip",
    "text/plain":                                                                ".txt",
    "text/csv":                                                                  ".csv",
    "image/jpeg":                                                                ".jpg",
    "image/png":                                                                 ".png",
    "image/gif":                                                                 ".gif",
    "image/svg+xml":                                                             ".svg",
}


def download_webpage(url: str, dest_dir: Path) -> tuple[bool, str]:
    """Download a webpage or direct file link to dest_dir."""
    try:
        resp = _request_with_retry(
            plain_session, url, timeout=WEBPAGE_TIMEOUT, allow_redirects=True, stream=True
        )
        resp.raise_for_status()
    except requests.exceptions.HTTPError:
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, f"Request failed: {e}"

    content_type = resp.headers.get("content-type", "").lower().split(";")[0].strip()
    base_name = filename_from_url(resp.url)  # use post-redirect URL for cleaner names

    if content_type in CONTENT_TYPE_EXT:
        ext = CONTENT_TYPE_EXT[content_type]
        dest_path = dest_dir / f"{base_name}{ext}"
        if dest_path.exists():
            return True, f"Already exists: {dest_path.name}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        return True, f"Downloaded file: {dest_path.name}"

    if content_type == "text/html":
        html = resp.text
        title = title_from_html(html)
        fname = sanitize_filename(title) if title else base_name
        dest_path = dest_dir / f"{fname}.html"
        if dest_path.exists():
            return True, f"Already exists: {dest_path.name}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        with open(dest_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(html)
        return True, f"Saved webpage: {dest_path.name}"

    return False, f"Unhandled content-type: {content_type}"


# ──────────────────────────────────────────────
# MANIFEST (tracks what has been processed)
# ──────────────────────────────────────────────

def load_manifest() -> dict:
    if MANIFEST_FILE.exists():
        try:
            with open(MANIFEST_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_manifest(manifest: dict):
    with open(MANIFEST_FILE, "w") as f:
        json.dump(manifest, f, indent=2)


# ──────────────────────────────────────────────
# PER-FILE PROCESSING
# ──────────────────────────────────────────────

def process_html_file(html_path: Path, file_manifest: dict, run_stats: dict) -> dict:
    """Scan one HTML file and download any new linked content."""
    try:
        content = html_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        print(f"  ✗ Could not read {html_path}: {e}")
        return file_manifest

    links = extract_links(content)
    new_links = sorted(url for url in links if url not in file_manifest)
    if not new_links:
        return file_manifest

    dest_dir = html_path.parent / LINKED_DIR_NAME / html_path.stem

    print(f"\n  📄 {html_path.relative_to(DOWNLOAD_DIR)}  ({len(new_links)} new links)")

    for url in new_links:
        link_type = classify_link(url)

        if link_type == "skip":
            file_manifest[url] = {"status": "skipped"}
            run_stats["skipped"] += 1
            continue

        short_url = url if len(url) <= 90 else url[:87] + "..."
        print(f"    [{link_type:<14}] {short_url}")

        if link_type == "canvas_file":
            ok, msg = download_canvas_file(url, dest_dir)
        elif link_type == "canvas_media":
            ok, msg = download_canvas_media(url, dest_dir)
        elif link_type in ("google_slides", "google_doc", "google_sheet"):
            ok, msg = download_google_file(url, link_type, dest_dir)
        else:
            ok, msg = download_webpage(url, dest_dir)

        print(f"      {'✓' if ok else '✗'} {msg}")
        file_manifest[url] = {
            "status": "downloaded" if ok else "failed",
            "message": msg,
            "type": link_type,
        }
        run_stats["downloaded" if ok else "failed"] += 1

    return file_manifest


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    if not CANVAS_API_TOKEN:
        print("⚠  CANVAS_TOKEN not set — Canvas file/media downloads will fail.")
        print("   Set it with: export CANVAS_TOKEN='your_token_here'\n")

    if not DOWNLOAD_DIR.exists():
        print(f"✗ Download directory not found: {DOWNLOAD_DIR}")
        print("  Run canvas_downloader.py first, then re-run this script.")
        return

    print(f"🔍 Scanning HTML files in: {DOWNLOAD_DIR}")

    html_files = sorted(
        f for f in DOWNLOAD_DIR.rglob("*.html")
        if LINKED_DIR_NAME not in f.parts and f.name != "index.html"
    )
    print(f"   Found {len(html_files)} HTML files to scan\n")

    manifest = load_manifest()
    run_stats = {"downloaded": 0, "failed": 0, "skipped": 0}

    for html_path in html_files:
        rel_path = str(html_path.relative_to(DOWNLOAD_DIR))
        file_manifest = manifest.get(rel_path, {})
        file_manifest = process_html_file(html_path, file_manifest, run_stats)
        manifest[rel_path] = file_manifest
        save_manifest(manifest)  # save after every file so Ctrl-C doesn't lose progress

    print(f"\n{'='*60}")
    print("✅ LINK HARVEST COMPLETE")
    print(f"{'='*60}")
    print(f"  Downloaded this run : {run_stats['downloaded']}")
    print(f"  Failed this run     : {run_stats['failed']}")
    print(f"  Skipped (no-op)     : {run_stats['skipped']}")
    print(f"  Manifest saved to   : {MANIFEST_FILE}")
    if run_stats["failed"]:
        print()
        print("  Tip: failures on old courses are expected (content deleted).")
        print(f"       Inspect {MANIFEST_FILE} for per-link failure details.")


if __name__ == "__main__":
    main()
