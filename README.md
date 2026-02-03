# Epstein Data Set 11 Scraper

This script scrapes DOJ Epstein Data Set 11 pages, extracts PDF links, and downloads them. It can use Playwright for age‑gate/verification handling, then switch to fast requests for bulk crawling.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Basic Usage

```bash
python scrape_dataset11.py --use-playwright --headed --pause --pages 3745 --out downloads/dataset-11
```

## Recommended (Hybrid Mode)

Uses Playwright once to clear the gate and capture cookies, then uses `requests` for all pages.

```bash
python scrape_dataset11.py --hybrid --headed --pause --pages 3745 --out downloads/dataset-11
```

## Options

- `--pages N`: Force total number of pages to process.
- `--start-page N`: Set the starting page param (0 or 1 depending on site behavior).
- `--max-pages N`: Limit pages processed (relative to start).
- `--delay S`: Sleep between page fetches (seconds).
- `--cooldown S`: Sleep after each page’s downloads (seconds).
- `--threads N`: Download threads per page (default 1).
- `--zero-retries N`: Retry pages that returned zero PDFs.
- `--zero-cooldown S`: Sleep before retrying zero-result pages (seconds).
- `--dry-run`: List files without downloading.
- `--use-playwright`: Use Playwright for every page fetch (slower).
- `--hybrid`: Use Playwright once for cookies, then `requests`.
- `--headed`: Show browser window.
- `--pause`: Pause to allow manual interaction.
- `--debug-dir DIR`: Save first page HTML/screenshot.

## Notes

- The site uses an age/consent gate. Use `--headed --pause` the first time to clear it.
- Page numbering on the site may be zero-based. Use `--start-page 0` if needed.
- After downloads, an `index.html` is generated in the output folder for browsing and searching PDFs. For best results, serve the folder with a local web server (e.g. `python -m http.server`).
