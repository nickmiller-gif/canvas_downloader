# CLAUDE.md

## Project overview

Two-script Python project for archiving Canvas LMS course materials locally.

- `canvas_downloader.py` — primary downloader (Canvas API)
- `canvas_link_harvester.py` — post-processor that chases links in saved HTML files

## Architecture

Both scripts are intentionally self-contained single files with no internal imports between them. Keep them that way — the goal is zero-friction execution (`python canvas_downloader.py`) without package setup.

The only external dependency is `requests`. Do not introduce others unless there is a strong reason, and document it in the README.

## Shared conventions

- Environment variables for all credentials and paths: `CANVAS_URL`, `CANVAS_TOKEN`, `CANVAS_DOWNLOAD_DIR`
- Configuration constants live at the top of each file under a clearly marked `# CONFIGURATION` block
- `sanitize_filename()` must be used for all user-derived strings that become filenames or directory names
- `REQUEST_DELAY` sleeps are placed before every outbound request to respect Canvas rate limits
- All file writes are idempotent: check existence before writing, never overwrite silently

## Re-run safety

Both scripts must remain safe to re-run against an already-populated download directory.

- `canvas_downloader.py`: `download_file()` checks `dest_path.exists()` before writing; `save_html()` does the same; `generate_course_index()` always regenerates since content may change between runs
- `canvas_link_harvester.py`: `_link_manifest.json` at the root of `CANVAS_DOWNLOAD_DIR` tracks every processed URL; only unrecorded URLs are attempted each run

## Output layout

```
CANVAS_DOWNLOAD_DIR/
  _link_manifest.json
  {Course Name (Term)}/
    index.html
    files/
    assignments/
    submissions/
      {Assignment Name}/
    discussions/
      announcements/
      discussion_topics/
    modules/
      {Module Name}/
        {Page}.html
        _linked/
          {Page}/          # harvested content for that page
    pages/
```

The `_linked/` directory name is controlled by `LINKED_DIR_NAME` in the harvester and must be excluded from HTML file scans (already handled in `main()`).

## Known limitations (do not try to fix without user input)

- Concluded-course content (files, media) returns 404 — content is deleted server-side, not recoverable
- Canvas Studio / Kaltura / Panopto / Echo360 videos require separate platform auth — not automatable here
- LTI iframe embeds cannot be downloaded
- JavaScript-rendered pages save as bare HTML without dynamic content

## Testing

There is no test suite. Manual testing requires a valid `CANVAS_TOKEN` pointing at a real Canvas instance. When making changes, verify against at least one active course and one concluded course.

## Style

- Snake_case filenames (e.g. `canvas_downloader.py`, not `canvas-downloader.py`)
- No type annotations beyond what already exists; don't add them unless fixing a real bug
- Print-based progress output only — no logging framework
- Keep functions short and focused; avoid classes except where state genuinely requires it (e.g. `_LinkExtractor`)
