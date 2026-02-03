# Epstein Data Set 11 Scraper

Disclaimer: This script is just an example of how to assist when manually pursuing the site. For others, you shouldn't use this unless it complies with the current policy of the source site. Use at your own discretion. Always respect terms of service, robots.txt, and legal guidelines when scraping any website. This is for educational purposes only and may not be suitable for production use without proper authorization.This script scrapes DOJ Epstein Data Set 11 pages, extracts PDF links, and downloads them. It can use Playwright for age‑gate/verification handling, then switch to fast requests for bulk crawling.


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
- `--dry-run`: List files without downloading.
- `--use-playwright`: Use Playwright for every page fetch (slower).
- `--hybrid`: Use Playwright once for cookies, then `requests`.
- `--headed`: Show browser window.
- `--pause`: Pause to allow manual interaction.
- `--debug-dir DIR`: Save first page HTML/screenshot.

## Notes

- The site uses an age/consent gate. Use `--headed --pause` the first time to clear it.
- Page numbering on the site may be zero-based. Use `--start-page 0` if needed.
