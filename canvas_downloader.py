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
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

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
DOWNLOAD_SUBMISSIONS = True
DOWNLOAD_DISCUSSIONS = True

# Rate limiting (Canvas API typically allows 10 req/sec)
REQUEST_DELAY = 0.15  # seconds between requests
MAX_DOWNLOAD_WORKERS = 4  # concurrent file download threads
# ──────────────────────────────────────────────

print_lock = Lock()


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


def download_file(url: str, dest_path: Path, prefix: str = "    ", max_retries: int = 3) -> bool:
    """Download a file from a URL to a local path, with retry on transient errors."""
    if dest_path.exists():
        with print_lock:
            print(f"{prefix}✓ Already exists: {dest_path.name}")
        return True

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, max_retries + 1):
        time.sleep(REQUEST_DELAY)
        try:
            resp = session.get(url, stream=True, allow_redirects=True)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                wait = REQUEST_DELAY * (2 ** attempt)
                with print_lock:
                    print(f"{prefix}⟳ {resp.status_code} — retrying in {wait:.1f}s ({attempt}/{max_retries})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            with print_lock:
                print(f"{prefix}↓ Downloaded: {dest_path.name}")
            return True
        except requests.exceptions.ConnectionError:
            if attempt < max_retries:
                wait = REQUEST_DELAY * (2 ** attempt)
                with print_lock:
                    print(f"{prefix}⟳ Connection error — retrying in {wait:.1f}s ({attempt}/{max_retries})")
                time.sleep(wait)
                continue
            with print_lock:
                print(f"{prefix}✗ Failed to download {dest_path.name}: connection error after {max_retries} attempts")
            return False
        except Exception as e:
            with print_lock:
                print(f"{prefix}✗ Failed to download {dest_path.name}: {e}")
            return False
    return False


def save_html(content: str, dest_path: Path, title: str = ""):
    """Save HTML content wrapped in a basic page structure."""
    if not content or not content.strip():
        return

    if dest_path.exists():
        print(f"    ✓ Already exists: {dest_path.name}")
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
    folder_cache = {}  # folder_id -> resolved sub_dir Path

    # Resolve folder paths first (requires API calls, done sequentially)
    download_tasks = []  # list of (url, dest_path)
    for f in files:
        if not isinstance(f, dict):
            continue

        filename = sanitize_filename(f.get("display_name", f.get("filename", "unknown")))
        folder_id = f.get("folder_id", "")

        sub_dir = files_dir
        if folder_id:
            if folder_id not in folder_cache:
                try:
                    folder = api_get(f"courses/{course_id}/folders/{folder_id}")
                    if isinstance(folder, dict) and "full_name" in folder:
                        rel_path = folder["full_name"].replace("course files", "").strip("/")
                        if rel_path:
                            parts = rel_path.split("/")
                            sanitized = Path(*[sanitize_filename(p) for p in parts])
                            folder_cache[folder_id] = files_dir / sanitized
                        else:
                            folder_cache[folder_id] = files_dir
                    else:
                        folder_cache[folder_id] = files_dir
                except Exception:
                    folder_cache[folder_id] = files_dir
            sub_dir = folder_cache[folder_id]

        download_url = f.get("url")
        if download_url:
            download_tasks.append((download_url, sub_dir / filename))

    # Download files in parallel
    total = len(download_tasks)
    with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as pool:
        futures = {
            pool.submit(download_file, url, dest, f"    [{i}/{total}] "): i
            for i, (url, dest) in enumerate(download_tasks, 1)
        }
        for future in as_completed(futures):
            future.result()  # propagate exceptions


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
        # Note: PDF/file links embedded in the assignment HTML are resolved
        # by canvas_link_harvester.py (run it after this script).


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
                            # Note: PDF/file links embedded in the page body are
                            # resolved by canvas_link_harvester.py.

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


def download_submissions(course_id: int, course_dir: Path):
    """Download the user's own assignment submissions."""
    print("  📤 Fetching submissions...")
    assignments = api_get(f"courses/{course_id}/assignments", params={"per_page": 100})
    if not assignments:
        print("    (no assignments found)")
        return

    submissions_dir = course_dir / "submissions"

    for a in assignments:
        if not isinstance(a, dict):
            continue

        assignment_id = a["id"]
        assignment_name = sanitize_filename(a.get("name", f"Assignment_{assignment_id}"))

        submission = api_get(f"courses/{course_id}/assignments/{assignment_id}/submissions/self")
        if not isinstance(submission, dict):
            continue

        # Skip if nothing was submitted
        workflow_state = submission.get("workflow_state", "")
        if workflow_state == "unsubmitted":
            continue

        sub_dir = submissions_dir / assignment_name
        saved_anything = False

        # Download attachments (file uploads)
        attachments = submission.get("attachments", [])
        for att in attachments:
            if not isinstance(att, dict):
                continue
            url = att.get("url")
            fname = sanitize_filename(att.get("display_name", att.get("filename", "submission")))
            if url:
                download_file(url, sub_dir / fname)
                saved_anything = True

        # Save submission body (online text entry)
        body = submission.get("body", "") or ""
        if body.strip():
            save_html(body, sub_dir / "submission_text.html", title=f"Submission — {a.get('name', '')}")
            saved_anything = True

        # Save submission URL (URL submissions)
        sub_url = submission.get("url", "") or ""
        if sub_url.strip():
            link_html = f'<p>Submitted URL: <a href="{sub_url}" target="_blank">{sub_url}</a></p>'
            save_html(link_html, sub_dir / "submission_url.html", title=f"Submission URL — {a.get('name', '')}")
            saved_anything = True

        # Save grade/score info if available
        grade = submission.get("grade")
        score = submission.get("score")
        if (grade or score) and saved_anything:
            meta = f"""
<div style="background: #f0f4f8; padding: 1rem; border-radius: 8px;">
    <strong>Score:</strong> {score if score is not None else 'N/A'} / {a.get('points_possible', 'N/A')}
    &nbsp;|&nbsp; <strong>Grade:</strong> {grade or 'N/A'}
</div>"""
            save_html(meta, sub_dir / "grade.html", title=f"Grade — {a.get('name', '')}")


def download_discussions(course_id: int, course_dir: Path):
    """Download announcements and discussion topics with their replies."""
    discussions_dir = course_dir / "discussions"

    for topic_type, label in [("announcements", "📢 Fetching announcements..."),
                               ("discussion_topics", "💬 Fetching discussions...")]:
        print(f"  {label}")

        # Announcements use a different endpoint
        if topic_type == "announcements":
            endpoint = f"courses/{course_id}/discussion_topics"
            params = {"per_page": 100, "only_announcements": "true"}
        else:
            endpoint = f"courses/{course_id}/discussion_topics"
            params = {"per_page": 100}

        topics = api_get(endpoint, params=params)
        if not topics:
            print(f"    (no {topic_type} found)")
            continue

        type_dir = discussions_dir / topic_type

        for topic in topics:
            if not isinstance(topic, dict):
                continue

            # Skip announcements when fetching regular discussions
            if topic_type == "discussion_topics" and topic.get("is_announcement"):
                continue

            title = topic.get("title", "Untitled")
            topic_id = topic["id"]
            safe_title = sanitize_filename(title)

            # Build the topic HTML
            message = topic.get("message", "") or ""
            posted_at = topic.get("posted_at", "Unknown date")
            author = topic.get("author", {}).get("display_name", "Unknown") if isinstance(topic.get("author"), dict) else "Unknown"

            content = f"""
<div style="background: #f0f4f8; padding: 1rem; border-radius: 8px; margin-bottom: 1.5rem;">
    <strong>Author:</strong> {author} &nbsp;|&nbsp;
    <strong>Posted:</strong> {posted_at}
</div>
{message}"""

            # Fetch replies
            full_topic = api_get(f"courses/{course_id}/discussion_topics/{topic_id}/view")
            if isinstance(full_topic, dict):
                participants = {}
                for p in full_topic.get("participants", []):
                    if isinstance(p, dict):
                        participants[p.get("id")] = p.get("display_name", "Unknown")

                replies = full_topic.get("view", [])
                if replies:
                    content += "\n<h2>Replies</h2>"
                    content += _render_replies(replies, participants)

            save_html(content, type_dir / f"{safe_title}.html", title=title)


def _render_replies(entries: list, participants: dict, depth: int = 0) -> str:
    """Recursively render discussion replies as nested HTML."""
    html = ""
    indent = depth * 20
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("deleted"):
            continue
        author_id = entry.get("user_id")
        author = participants.get(author_id, "Unknown")
        message = entry.get("message", "") or ""
        created = entry.get("created_at", "")

        html += f"""
<div style="margin-left: {indent}px; border-left: 3px solid #ddd; padding: 0.5rem 1rem; margin-bottom: 0.5rem;">
    <div style="color: #666; font-size: 0.9em;">
        <strong>{author}</strong> &mdash; {created}
    </div>
    {message}
</div>"""
        # Recurse into sub-replies
        sub_replies = entry.get("replies", [])
        if sub_replies:
            html += _render_replies(sub_replies, participants, depth + 1)
    return html


def generate_course_index(course_dir: Path, course_name: str):
    """Generate an index.html listing all downloaded content for a course.

    Always regenerated (not skipped if exists) since new content may have been
    added since the last run.
    """
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
    dest_path = course_dir / "index.html"
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    title = f"📚 {course_name} — Downloaded Materials"
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
    print(f"    📄 Generated: index.html")


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

    # Course selection
    print(f"\nEnter course numbers to download (comma-separated), a range (e.g. 3-7),")
    print(f"or press Enter to download all:")
    selection = input("> ").strip()

    if selection:
        selected_indices = set()
        for part in selection.split(","):
            part = part.strip()
            if "-" in part:
                try:
                    start, end = part.split("-", 1)
                    selected_indices.update(range(int(start), int(end) + 1))
                except ValueError:
                    print(f"  ⚠ Invalid range: {part}")
            else:
                try:
                    selected_indices.add(int(part))
                except ValueError:
                    print(f"  ⚠ Invalid number: {part}")
        courses = [c for i, c in enumerate(courses, 1) if i in selected_indices]
        if not courses:
            print("No valid courses selected.")
            sys.exit(1)
        print(f"\n→ Selected {len(courses)} course(s)")

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

            if DOWNLOAD_SUBMISSIONS:
                download_submissions(c["id"], course_dir)

            if DOWNLOAD_DISCUSSIONS:
                download_discussions(c["id"], course_dir)

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