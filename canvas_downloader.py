#!/usr/bin/env python3
"""
Canvas LMS Course Material Downloader
Downloads all available course materials from every course you're enrolled in.

Usage:
    1. Set your Canvas URL and API token below (or via environment variables)
    2. pip install requests
    3. python canvas_downloader.py

Materials downloaded:
    - All course files (PDFs, docs, slides, etc.)
    - Assignment descriptions & rubrics (as HTML)
    - Module/page content (as HTML)
"""

import os
import re
import sys
import json
import time
import requests
from pathlib import Path
from urllib.parse import urlparse, unquote

# ──────────────────────────────────────────────
# CONFIGURATION — Edit these or set env vars
# ──────────────────────────────────────────────
CANVAS_BASE_URL = os.environ.get("CANVAS_URL", "https://canvas.cmu.edu")
CANVAS_API_TOKEN = os.environ.get("CANVAS_TOKEN")
DOWNLOAD_DIR = os.environ.get("CANVAS_DOWNLOAD_DIR", os.path.expanduser("~/CMU"))

# What to download (set False to skip)
DOWNLOAD_FILES = True
DOWNLOAD_ASSIGNMENTS = True
DOWNLOAD_MODULES_AND_PAGES = True

# Rate limiting (Canvas API typically allows 10 req/sec)
REQUEST_DELAY = 0.15  # seconds between requests
# ──────────────────────────────────────────────


session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {CANVAS_API_TOKEN}",
    "Accept": "application/json",
})


def sanitize_filename(name: str) -> str:
    """Remove or replace characters that are problematic in file/folder names."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip('. ')
    return name[:200]  # cap length


def api_get(endpoint: str, params: dict = None) -> list | dict:
    """GET from Canvas API with automatic pagination."""
    url = f"{CANVAS_BASE_URL}/api/v1/{endpoint.lstrip('/')}"
    all_results = []

    while url:
        time.sleep(REQUEST_DELAY)
        try:
            resp = session.get(url, params=params)
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"  ⚠ API error {resp.status_code} for {url}: {e}")
            return all_results if all_results else {}
        except requests.exceptions.RequestException as e:
            print(f"  ⚠ Request failed for {url}: {e}")
            return all_results if all_results else {}

        data = resp.json()
        params = None  # only use params on the first request

        if isinstance(data, list):
            all_results.extend(data)
        else:
            return data  # single object response

        # Handle pagination via Link header
        links = resp.headers.get("Link", "")
        url = None
        for part in links.split(","):
            if 'rel="next"' in part:
                url = part.split("<")[1].split(">")[0]
                break

    return all_results


def download_file(url: str, dest_path: Path) -> bool:
    """Download a file from a URL to a local path."""
    if dest_path.exists():
        print(f"    ✓ Already exists: {dest_path.name}")
        return True

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    time.sleep(REQUEST_DELAY)

    try:
        resp = session.get(url, stream=True, allow_redirects=True)
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        print(f"    ↓ Downloaded: {dest_path.name}")
        return True
    except Exception as e:
        print(f"    ✗ Failed to download {dest_path.name}: {e}")
        return False


def save_html(content: str, dest_path: Path, title: str = ""):
    """Save HTML content wrapped in a basic page structure."""
    if not content or not content.strip():
        return

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 900px;
            margin: 2rem auto;
            padding: 0 1rem;
            line-height: 1.6;
            color: #333;
        }}
        h1 {{ color: #1a1a1a; border-bottom: 2px solid #eee; padding-bottom: 0.5rem; }}
        img {{ max-width: 100%; height: auto; }}
        table {{ border-collapse: collapse; width: 100%; }}
        td, th {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        pre {{ background: #f5f5f5; padding: 1rem; overflow-x: auto; border-radius: 4px; }}
        code {{ background: #f5f5f5; padding: 2px 6px; border-radius: 3px; }}
        a {{ color: #0066cc; }}
    </style>
</head>
<body>
<h1>{title}</h1>
{content}
</body>
</html>"""

    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"    📄 Saved: {dest_path.name}")


def get_all_courses() -> list:
    """Fetch all courses the user is enrolled in (active + completed)."""
    print("📚 Fetching course list...")
    courses = []

    for state in ["available", "completed"]:
        result = api_get("courses", params={
            "enrollment_state": state,
            "per_page": 100,
            "include[]": ["term", "total_scores"],
        })
        if isinstance(result, list):
            courses.extend(result)

    # Deduplicate by course ID
    seen = set()
    unique = []
    for c in courses:
        if c["id"] not in seen:
            seen.add(c["id"])
            unique.append(c)

    # Sort by name
    unique.sort(key=lambda c: c.get("name", "Unknown"))
    return unique


def download_course_files(course_id: int, course_dir: Path):
    """Download all files from a course."""
    print("  📁 Fetching files...")
    files = api_get(f"courses/{course_id}/files", params={"per_page": 100})

    if not files:
        print("    (no files found)")
        return

    files_dir = course_dir / "files"

    for f in files:
        if not isinstance(f, dict):
            continue

        filename = sanitize_filename(f.get("display_name", f.get("filename", "unknown")))
        folder_name = f.get("folder_id", "")

        # Try to get the folder path for organization
        sub_dir = files_dir
        if folder_name:
            try:
                folder = api_get(f"courses/{course_id}/folders/{folder_name}")
                if isinstance(folder, dict) and "full_name" in folder:
                    # full_name looks like "course files/Week 1/Readings"
                    rel_path = folder["full_name"].replace("course files", "").strip("/")
                    if rel_path:
                        sub_dir = files_dir / sanitize_filename(rel_path)
            except Exception:
                pass

        download_url = f.get("url")
        if download_url:
            download_file(download_url, sub_dir / filename)


def download_assignments(course_id: int, course_dir: Path):
    """Download assignment descriptions and rubrics."""
    print("  📝 Fetching assignments...")
    assignments = api_get(f"courses/{course_id}/assignments", params={
        "per_page": 100,
        "include[]": ["rubric"],
    })

    if not assignments:
        print("    (no assignments found)")
        return

    assignments_dir = course_dir / "assignments"

    for a in assignments:
        if not isinstance(a, dict):
            continue

        name = sanitize_filename(a.get("name", "Untitled Assignment"))
        description = a.get("description", "") or ""
        points = a.get("points_possible", "N/A")
        due = a.get("due_at", "No due date")

        # Build content with metadata
        meta = f"""
<div style="background: #f0f4f8; padding: 1rem; border-radius: 8px; margin-bottom: 1.5rem;">
    <strong>Points:</strong> {points} &nbsp;|&nbsp;
    <strong>Due:</strong> {due}
</div>
"""
        content = meta + description

        # Add rubric if present
        rubric = a.get("rubric")
        if rubric:
            content += "\n<h2>Rubric</h2>\n<table><tr><th>Criteria</th><th>Points</th><th>Description</th></tr>"
            for criterion in rubric:
                content += f"""<tr>
                    <td>{criterion.get('description', '')}</td>
                    <td>{criterion.get('points', '')}</td>
                    <td>{criterion.get('long_description', '')}</td>
                </tr>"""
            content += "</table>"

        save_html(content, assignments_dir / f"{name}.html", title=a.get("name", "Assignment"))


def download_modules_and_pages(course_id: int, course_dir: Path):
    """Download module structure and all page content."""
    print("  📦 Fetching modules...")
    modules = api_get(f"courses/{course_id}/modules", params={
        "per_page": 100,
        "include[]": ["items"],
    })

    modules_dir = course_dir / "modules"

    if modules:
        for mod in modules:
            if not isinstance(mod, dict):
                continue

            mod_name = sanitize_filename(mod.get("name", "Untitled Module"))
            mod_dir = modules_dir / mod_name
            print(f"    📦 Module: {mod.get('name', 'Untitled')}")

            # Get module items
            items = mod.get("items", [])
            if not items:
                items = api_get(f"courses/{course_id}/modules/{mod['id']}/items", params={"per_page": 100})

            for item in items:
                if not isinstance(item, dict):
                    continue

                item_type = item.get("type", "")
                item_title = sanitize_filename(item.get("title", "Untitled"))

                if item_type == "Page":
                    # Fetch the page content
                    page_url = item.get("page_url")
                    if page_url:
                        page = api_get(f"courses/{course_id}/pages/{page_url}")
                        if isinstance(page, dict):
                            body = page.get("body", "") or ""
                            save_html(body, mod_dir / f"{item_title}.html", title=item.get("title", "Page"))

                elif item_type == "File":
                    # Download the file
                    content_id = item.get("content_id")
                    if content_id:
                        file_info = api_get(f"courses/{course_id}/files/{content_id}")
                        if isinstance(file_info, dict) and file_info.get("url"):
                            fname = sanitize_filename(file_info.get("display_name", f"{item_title}"))
                            download_file(file_info["url"], mod_dir / fname)

                elif item_type == "ExternalUrl":
                    # Save external links as a simple HTML bookmark
                    ext_url = item.get("external_url", "")
                    if ext_url:
                        link_html = f'<p><a href="{ext_url}" target="_blank">{ext_url}</a></p>'
                        save_html(link_html, mod_dir / f"{item_title}_link.html", title=item.get("title", "Link"))

                elif item_type == "Assignment":
                    # Already captured in assignments download, but note it in module
                    pass

    # Also grab any standalone pages not in modules
    print("  📄 Fetching standalone pages...")
    pages = api_get(f"courses/{course_id}/pages", params={"per_page": 100})
    if pages:
        pages_dir = course_dir / "pages"
        for p in pages:
            if not isinstance(p, dict):
                continue
            page_url = p.get("url")
            if page_url:
                page = api_get(f"courses/{course_id}/pages/{page_url}")
                if isinstance(page, dict):
                    title = page.get("title", "Untitled Page")
                    body = page.get("body", "") or ""
                    if body.strip():
                        save_html(body, pages_dir / f"{sanitize_filename(title)}.html", title=title)


def generate_course_index(course_dir: Path, course_name: str):
    """Generate an index.html listing all downloaded content for a course."""
    items = []
    for root, dirs, files in os.walk(course_dir):
        for fname in sorted(files):
            if fname == "index.html":
                continue
            rel = os.path.relpath(os.path.join(root, fname), course_dir)
            items.append(f'<li><a href="{rel}">{rel}</a></li>')

    if not items:
        return

    content = f"<ul>{''.join(items)}</ul>"
    save_html(content, course_dir / "index.html", title=f"📚 {course_name} — Downloaded Materials")


def main():
    # Validate config
    if "YOUR_" in CANVAS_BASE_URL or "YOUR_" in CANVAS_API_TOKEN:
        print("=" * 60)
        print("⚠  SETUP REQUIRED")
        print("=" * 60)
        print()
        print("Edit this script or set environment variables:")
        print()
        print("  Option A — Edit the script:")
        print("    CANVAS_BASE_URL = 'https://yourschool.instructure.com'")
        print("    CANVAS_API_TOKEN = 'your_token_here'")
        print()
        print("  Option B — Environment variables:")
        print("    export CANVAS_URL='https://yourschool.instructure.com'")
        print("    export CANVAS_TOKEN='your_token_here'")
        print()
        print("  To generate a token:")
        print("    Canvas → Account → Settings → + New Access Token")
        print()
        sys.exit(1)

    # Test connection
    print(f"🔗 Connecting to {CANVAS_BASE_URL}...")
    user = api_get("users/self")
    if not user or not isinstance(user, dict) or "name" not in user:
        print("✗ Could not connect. Check your URL and token.")
        sys.exit(1)

    print(f"✓ Authenticated as: {user['name']}")
    print(f"📂 Download directory: {DOWNLOAD_DIR}")
    print()

    # Get all courses
    courses = get_all_courses()
    print(f"\n✓ Found {len(courses)} courses:\n")
    for i, c in enumerate(courses, 1):
        term = c.get("term", {}).get("name", "Unknown Term") if isinstance(c.get("term"), dict) else "Unknown Term"
        print(f"  {i:3}. {c.get('name', 'Unknown')} ({term})")

    print(f"\n{'='*60}")
    print("Starting download...\n")

    # Process each course
    success_count = 0
    error_count = 0

    for c in courses:
        course_name = c.get("name", f"Course_{c['id']}")
        term = c.get("term", {}).get("name", "") if isinstance(c.get("term"), dict) else ""
        folder_name = sanitize_filename(f"{course_name}" + (f" ({term})" if term else ""))
        course_dir = Path(DOWNLOAD_DIR) / folder_name

        print(f"\n{'─'*60}")
        print(f"📚 {course_name}")
        print(f"{'─'*60}")

        try:
            if DOWNLOAD_FILES:
                download_course_files(c["id"], course_dir)

            if DOWNLOAD_ASSIGNMENTS:
                download_assignments(c["id"], course_dir)

            if DOWNLOAD_MODULES_AND_PAGES:
                download_modules_and_pages(c["id"], course_dir)

            generate_course_index(course_dir, course_name)
            success_count += 1

        except Exception as e:
            print(f"  ✗ Error processing course: {e}")
            error_count += 1

    # Summary
    print(f"\n{'='*60}")
    print("✅ DOWNLOAD COMPLETE")
    print(f"{'='*60}")
    print(f"  Courses processed: {success_count}")
    if error_count:
        print(f"  Courses with errors: {error_count}")
    print(f"  Files saved to: {DOWNLOAD_DIR}")
    print()

    # Calculate total size
    total_size = sum(
        f.stat().st_size
        for f in Path(DOWNLOAD_DIR).rglob("*")
        if f.is_file()
    )
    if total_size > 0:
        if total_size > 1_000_000_000:
            print(f"  Total size: {total_size / 1_000_000_000:.1f} GB")
        elif total_size > 1_000_000:
            print(f"  Total size: {total_size / 1_000_000:.1f} MB")
        else:
            print(f"  Total size: {total_size / 1_000:.1f} KB")


if __name__ == "__main__":
    main()