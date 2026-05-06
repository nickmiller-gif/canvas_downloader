# Canvas Downloader

A pair of Python scripts for archiving your Canvas LMS course materials to local disk.

---

## Scripts

| Script | Purpose |
|---|---|
| `canvas_downloader.py` | Downloads all course files, assignments, modules, and pages from every Canvas course you're enrolled in |
| `canvas_link_harvester.py` | Post-processor that scans the downloaded HTML files and attempts to retrieve any content they link to — Canvas files, Google Docs/Slides/Sheets, and general webpages |

Run them in order: downloader first, harvester second.

> **PDFs embedded as links inside pages, assignments, or discussions are only
> downloaded in Step 2.** Step 1 saves those pages as HTML files; Step 2 scans
> those HTML files and pulls down every PDF (and other linked file) it finds.
> Always run both scripts for complete coverage.

---

## Requirements

```bash
pip install requests
```

Python 3.10+ is required (uses `str | None` union type syntax).

---

## Setup

### 1. Get a Canvas API token

1. Log in to Canvas
2. Go to **Account → Settings → + New Access Token**
3. Give it a name, leave expiry blank (or set one), and copy the token

### 2. Set environment variables

```bash
export CANVAS_URL="https://canvas.cmu.edu"       # your institution's Canvas URL
export CANVAS_TOKEN="your_token_here"
export CANVAS_DOWNLOAD_DIR="$HOME/CMU"           # optional, defaults to ~/CMU
```

Add these to your `~/.zshrc` or `~/.bashrc` to persist them across sessions.

---

## Usage

### Step 1 — Download course materials

```bash
python canvas_downloader.py
```

This will:
- Authenticate and list every course you're enrolled in (active and completed)
- Prompt you to select specific courses (or press Enter to download all)
- For each course, download:
  - **Files** — everything in the course Files section, organized by folder (downloaded in parallel)
  - **Assignments** — descriptions and rubrics saved as HTML
  - **Modules & Pages** — module structure and all page content as HTML; files linked inside modules are downloaded directly
  - **Standalone pages** — any pages not inside a module
  - **Submissions** — your own submitted work (file uploads, text entries, URLs) and grades
  - **Discussions & Announcements** — topic content and threaded replies
- Generate an `index.html` per course linking to all downloaded content

Re-running is safe — files already on disk are skipped automatically. Transient errors (429, 5xx) are retried with exponential backoff.

### Step 2 — Harvest linked content from HTML files

```bash
python canvas_link_harvester.py
```

This scans every HTML file produced by Step 1 and attempts to download anything linked inside:

| Link type | What happens |
|---|---|
| Canvas file (`/files/{id}`) | Resolved via the API and downloaded |
| Canvas media object | Direct download attempted with your auth token |
| Google Slides | Exported as both PDF and PPTX |
| Google Doc | Exported as PDF |
| Google Sheet | Exported as XLSX |
| **Direct PDF link** (URL ends in `.pdf`) | **Downloaded directly as a PDF file**, regardless of `Content-Type` header |
| Any other webpage or direct file URL | Downloaded as HTML or the raw file (PDF, DOCX, ZIP, etc.) based on `Content-Type`; `application/octet-stream` responses fall back to the URL extension |
| YouTube, Vimeo, social media | Skipped silently |
| Canvas UI pages (assignment pages, etc.) | Skipped silently |

Downloaded content lands in a `_linked/{page_name}/` subfolder next to the source HTML file.

The harvester is also re-run safe — every processed link is recorded in `~/CMU/_link_manifest.json`. Subsequent runs only process links that haven't been seen before. To force a full re-scan, delete the manifest file.

---

## Output structure

```
~/CMU/
├── _link_manifest.json               # harvester progress tracker
│
├── Course Name (Term)/
│   ├── index.html                    # browsable index of all downloaded content
│   │
│   ├── files/                        # raw course file downloads
│   │   ├── Subfolder Name/
│   │   │   └── lecture_01.pdf
│   │   └── syllabus.pdf
│   │
│   ├── assignments/
│   │   ├── Homework 1.html
│   │   └── Final Project.html
│   │
│   ├── submissions/
│   │   ├── Homework 1/
│   │   │   ├── my_solution.pdf
│   │   │   └── grade.html
│   │   └── Final Project/
│   │       └── submission_text.html
│   │
│   ├── discussions/
│   │   ├── announcements/
│   │   │   └── Welcome to the Course.html
│   │   └── discussion_topics/
│   │       └── Week 1 Discussion.html
│   │
│   ├── modules/
│   │   ├── Week 1 - Introduction/
│   │   │   ├── Overview.html
│   │   │   ├── slides.pdf
│   │   │   └── _linked/
│   │   │       └── Overview/        # content harvested from Overview.html
│   │   │           ├── google_slides_DOCID.pdf
│   │   │           └── some_article.html
│   │   └── Week 2 - Topic/
│   │
│   └── pages/                        # standalone pages not inside modules
│       └── Course Info.html
│
└── Another Course (Term)/
    └── ...
```

---

## What it can and can't retrieve

### What works well

- Course files (PDFs, DOCX, PPTX, ZIP, images) uploaded directly to Canvas
- Assignment and rubric HTML
- Canvas page content
- **PDFs linked inside pages, assignments, and discussions** — the harvester catches direct `.pdf` links, Canvas file embeds, `<object>`/`<embed>` tags, and Canvas RCE `data-url`/`data-download-url` attributes
- Publicly shared Google Slides, Docs, and Sheets
- Webpages and direct file downloads linked from pages (including `application/octet-stream` responses identified by URL extension)
- Module-linked files

### What will fail or produce limited results

- **Content from concluded/archived courses** — institutions commonly delete media and files when courses end. Expect 404s on old course content. This is not a limitation of the scripts; the content no longer exists on the server.
- **Canvas Studio / Kaltura videos** — lecture recordings are auth-gated and served through separate platforms (Kaltura, Panopto, Echo360). The harvester will attempt a direct download but these almost always require a separate login.
- **Private Google files** — share links that require a Google account will be reported as "Private — requires Google login."
- **JavaScript-heavy pages** — the harvester uses plain HTTP requests with no browser engine. Pages that require JavaScript to render their content will be saved as blank or near-blank HTML.
- **LTI-embedded content** — tools embedded via LTI (Panopto, VoiceThread, etc.) appear as iframes and cannot be downloaded automatically.
- **Videos on external platforms** — YouTube, Vimeo, and similar are intentionally skipped. Use a dedicated tool like `yt-dlp` if you need those.

---

## Configuration options

Both scripts share the same three environment variables:

| Variable | Default | Description |
|---|---|---|
| `CANVAS_URL` | `https://canvas.cmu.edu` | Your institution's Canvas base URL |
| `CANVAS_TOKEN` | _(required)_ | Your Canvas API access token |
| `CANVAS_DOWNLOAD_DIR` | `~/CMU` | Where to save everything |

Additional tunables at the top of each script:

**`canvas_downloader.py`**

| Variable | Default | Description |
|---|---|---|
| `DOWNLOAD_FILES` | `True` | Toggle file section downloads |
| `DOWNLOAD_ASSIGNMENTS` | `True` | Toggle assignment downloads |
| `DOWNLOAD_MODULES_AND_PAGES` | `True` | Toggle module/page downloads |
| `DOWNLOAD_SUBMISSIONS` | `True` | Toggle submission/grade downloads |
| `DOWNLOAD_DISCUSSIONS` | `True` | Toggle discussion/announcement downloads |
| `REQUEST_DELAY` | `0.15` | Seconds between API requests |
| `MAX_DOWNLOAD_WORKERS` | `4` | Concurrent file download threads |

**`canvas_link_harvester.py`**

| Variable | Default | Description |
|---|---|---|
| `REQUEST_DELAY` | `0.2` | Seconds between requests |
| `WEBPAGE_TIMEOUT` | `20` | Seconds before a webpage request times out |
| `LINKED_DIR_NAME` | `_linked` | Subfolder name for harvested content |
| `SKIP_DOMAINS` | _(set)_ | Domains to silently ignore |

---

## Tips

- **Interrupted runs** — both scripts are safe to interrupt and re-run. The downloader skips existing files; the harvester uses the manifest to skip processed links.
- **Refreshing content** — to re-download a specific assignment or page, delete its file and re-run the downloader. To re-process a page's links, remove that file's entry from `_link_manifest.json`.
- **Browsing offline** — open any course's `index.html` in a browser for a clickable table of contents. The HTML pages render fine offline; linked content in `_linked/` subfolders opens locally too.
- **Rate limiting** — Canvas allows roughly 10 requests/second. The default delays are conservative. If you see 403 or 429 errors, increase `REQUEST_DELAY`.
