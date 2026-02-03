#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover - optional dependency
    sync_playwright = None


BASE_URL = "https://www.justice.gov/epstein/doj-disclosures/data-set-11-files"

# Chrome-like headers (lightweight emulation)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
    "Referer": "https://www.justice.gov/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="121", "Not A(Brand";v="24", "Google Chrome";v="121"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}


def warm_up_session(session: requests.Session) -> None:
    # Some sites set cookies or require an initial visit to the root domain
    session.get("https://www.justice.gov/", headers=HEADERS, timeout=30)


def get_soup_requests(url: str, session: requests.Session) -> BeautifulSoup:
    resp = session.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def maybe_handle_age_gate(page) -> None:
    # Heuristic: click common consent/age gate buttons if present
    patterns = re.compile(r"(agree|accept|enter|yes|continue|i am|i'm)\b", re.I)
    try:
        buttons = page.get_by_role("button")
        for i in range(0, min(5, buttons.count())):
            btn = buttons.nth(i)
            text = btn.inner_text(timeout=500)
            if patterns.search(text):
                btn.click(timeout=2000)
                page.wait_for_timeout(1000)
                break
    except Exception:
        pass
    try:
        links = page.get_by_role("link")
        for i in range(0, min(5, links.count())):
            link = links.nth(i)
            text = link.inner_text(timeout=500)
            if patterns.search(text):
                link.click(timeout=2000)
                page.wait_for_timeout(1000)
                break
    except Exception:
        pass


class PlaywrightFetcher:
    def __init__(self, headed: bool, pause: bool, debug_dir: str | None) -> None:
        if sync_playwright is None:
            raise RuntimeError("Playwright is not installed.")
        self.headed = headed
        self.pause = pause
        self.debug_dir = debug_dir
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=not headed)
        self._context = self._browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
        )
        self._gate_handled = False

    def close(self) -> None:
        self._context.close()
        self._browser.close()
        self._playwright.stop()

    def get_soup(self, url: str, is_first: bool = False) -> BeautifulSoup:
        page = self._context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if not self._gate_handled:
            maybe_handle_age_gate(page)
            self._gate_handled = True
            if self.pause:
                print("Paused for manual inspection. Close the browser or press Enter here to continue.")
                try:
                    input()
                except EOFError:
                    pass
        page.wait_for_load_state("networkidle", timeout=60000)
        html = page.content()
        if self.debug_dir and is_first:
            os.makedirs(self.debug_dir, exist_ok=True)
            with open(os.path.join(self.debug_dir, "page.html"), "w", encoding="utf-8") as f:
                f.write(html)
            page.screenshot(path=os.path.join(self.debug_dir, "page.png"), full_page=True)
        page.close()
        return BeautifulSoup(html, "html.parser")

    def export_cookies(self) -> list[dict]:
        return self._context.cookies()


def get_soup(
    url: str,
    session: requests.Session,
    force_playwright: bool,
    headed: bool,
    pause: bool,
    debug_dir: str | None,
    pw: "PlaywrightFetcher | None" = None,
    is_first: bool = False,
) -> BeautifulSoup:
    try:
        if force_playwright:
            if pw is None:
                raise RuntimeError("Playwright fetcher is not initialized.")
            return pw.get_soup(url, is_first=is_first)
        return get_soup_requests(url, session)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            if pw is None:
                raise RuntimeError("Playwright fetcher is not initialized.")
            return pw.get_soup(url, is_first=is_first)
        raise


def find_pagination_bounds(soup: BeautifulSoup) -> tuple[int, int]:
    # Look for pagination links containing ?page=
    pages = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "page=" in href:
            m = re.search(r"[?&]page=(\d+)", href)
            if m:
                pages.append(int(m.group(1)))
    if not pages:
        return 1, 1
    return min(pages), max(pages)


def find_last_page_link(soup: BeautifulSoup) -> int | None:
    last = soup.select_one('a[aria-label="Last page"][href*="page="]')
    if not last:
        return None
    m = re.search(r"[?&]page=(\d+)", last.get("href", ""))
    if not m:
        return None
    return int(m.group(1))


def extract_pdf_links(soup: BeautifulSoup) -> list[str]:
    links = []
    for a in soup.select("a[href$='.pdf'], a[href*='.pdf?']"):
        href = a.get("href")
        if href:
            links.append(href)
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for href in links:
        if href not in seen:
            seen.add(href)
            unique.append(href)
    return unique


def safe_filename(url: str) -> str:
    path = urlparse(url).path
    name = os.path.basename(path) or "download.pdf"
    return name


def download_file(
    url: str,
    dest_dir: str,
    session: requests.Session,
    dry_run: bool,
    cookies: dict[str, str] | None = None,
) -> tuple[str, int, bool]:
    filename = safe_filename(url)
    dest_path = os.path.join(dest_dir, filename)
    if os.path.exists(dest_path):
        return dest_path, 0, True
    if dry_run:
        return dest_path, 0, True

    with requests.get(url, headers=HEADERS, cookies=cookies, stream=True, timeout=60) as r:
        r.raise_for_status()
        bytes_written = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    bytes_written += len(chunk)
    return dest_path, bytes_written, False


def write_index_html(output_dir: str) -> None:
    files = [
        f for f in os.listdir(output_dir) if f.lower().endswith(".pdf") and os.path.isfile(os.path.join(output_dir, f))
    ]
    files.sort()
    index_path = os.path.join(output_dir, "index.html")
    files_json = json.dumps(files)
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Dataset 11 PDFs</title>
  <style>
    :root { --bg: #0f172a; --panel: #111827; --text: #e5e7eb; --muted: #9ca3af; --accent: #38bdf8; }
    body { margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background: var(--bg); color: var(--text); }
    header { padding: 12px 16px; background: #0b1220; border-bottom: 1px solid #1f2937; }
    .wrap { display: grid; grid-template-columns: 320px 1fr; height: calc(100vh - 52px); }
    .list { overflow: auto; border-right: 1px solid #1f2937; background: var(--panel); }
    .viewer { overflow: auto; padding: 12px; }
    input { width: 100%; padding: 8px; box-sizing: border-box; border: 1px solid #374151; background: #0b1220; color: var(--text); }
    .item { padding: 8px 12px; border-bottom: 1px solid #1f2937; cursor: pointer; }
    .item:hover, .item.active { background: #111a2a; color: var(--accent); }
    .controls { display: flex; gap: 8px; align-items: center; padding: 8px 0; }
    button { background: #0b1220; color: var(--text); border: 1px solid #374151; padding: 6px 10px; cursor: pointer; }
    button:hover { border-color: var(--accent); color: var(--accent); }
    #canvas { width: 100%; max-width: 100%; }
    #match { color: var(--muted); font-size: 12px; }
    #pageInfo { color: var(--muted); font-size: 12px; }
    #notice { color: var(--muted); font-size: 12px; }
  </style>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.10.38/pdf.min.js"></script>
</head>
<body>
  <header>
    <div><strong>Dataset 11 PDFs</strong></div>
    <div id="notice">If PDFs fail to load, serve this folder with a local web server (e.g. <code>python -m http.server</code>).</div>
  </header>
  <div class="wrap">
    <div class="list">
      <input id="filter" placeholder="Filter filenames…" />
      <div id="items"></div>
    </div>
    <div class="viewer">
      <div class="controls">
        <button id="prev">Prev</button>
        <button id="next">Next</button>
        <span id="pageInfo"></span>
        <input id="search" placeholder="Search text…" />
        <button id="find">Find</button>
        <span id="match"></span>
      </div>
      <canvas id="canvas"></canvas>
    </div>
  </div>
<script>
const files = __FILES_JSON__;
const itemsEl = document.getElementById('items');
const filterEl = document.getElementById('filter');
const canvas = document.getElementById('canvas');
const ctx = canvas.getContext('2d');
const pageInfo = document.getElementById('pageInfo');
const matchEl = document.getElementById('match');
let pdf = null;
let pageNum = 1;
let currentFile = null;

pdfjsLib.GlobalWorkerOptions.workerSrc = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.10.38/pdf.worker.min.js";

function renderPage(num) {
  if (!pdf) return;
  pdf.getPage(num).then(page => {
    const viewport = page.getViewport({ scale: 1.25 });
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    page.render({ canvasContext: ctx, viewport });
    pageInfo.textContent = `Page ${num} / ${pdf.numPages}`;
  });
}

function loadPdf(file) {
  currentFile = file;
  pageNum = 1;
  const url = encodeURI(file);
  pdfjsLib.getDocument(url).promise.then(doc => {
    pdf = doc;
    renderPage(pageNum);
  });
}

function renderList(list) {
  itemsEl.innerHTML = "";
  list.forEach(name => {
    const div = document.createElement('div');
    div.className = 'item' + (name === currentFile ? ' active' : '');
    div.textContent = name;
    div.onclick = () => {
      document.querySelectorAll('.item').forEach(i => i.classList.remove('active'));
      div.classList.add('active');
      loadPdf(name);
    };
    itemsEl.appendChild(div);
  });
}

filterEl.addEventListener('input', () => {
  const q = filterEl.value.toLowerCase();
  renderList(files.filter(f => f.toLowerCase().includes(q)));
});

document.getElementById('prev').onclick = () => {
  if (pdf && pageNum > 1) { pageNum--; renderPage(pageNum); }
};
document.getElementById('next').onclick = () => {
  if (pdf && pageNum < pdf.numPages) { pageNum++; renderPage(pageNum); }
};

document.getElementById('find').onclick = async () => {
  if (!pdf) return;
  const term = document.getElementById('search').value.trim().toLowerCase();
  if (!term) return;
  for (let i = 1; i <= pdf.numPages; i++) {
    const page = await pdf.getPage(i);
    const text = await page.getTextContent();
    const full = text.items.map(t => t.str).join(' ').toLowerCase();
    if (full.includes(term)) {
      pageNum = i;
      renderPage(pageNum);
      matchEl.textContent = `Match on page ${i}`;
      return;
    }
  }
  matchEl.textContent = "No matches";
};

renderList(files);
if (files.length) loadPdf(files[0]);
</script>
</body>
</html>
"""
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(template.replace("__FILES_JSON__", files_json))


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape DOJ Epstein data set 11 PDFs.")
    parser.add_argument("--out", default="downloads/dataset-11", help="Output directory")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between page fetches (seconds)")
    parser.add_argument("--max-pages", type=int, default=0, help="Override max pages (0=auto)")
    parser.add_argument("--pages", type=int, default=0, help="Force total number of pages to process")
    parser.add_argument("--dry-run", action="store_true", help="List files without downloading")
    parser.add_argument("--use-playwright", action="store_true", help="Use Playwright for HTML fetches")
    parser.add_argument("--headed", action="store_true", help="Show browser window for Playwright")
    parser.add_argument("--pause", action="store_true", help="Pause for manual interaction in browser")
    parser.add_argument("--debug-dir", default="", help="Write debug HTML/screenshot to this dir")
    parser.add_argument("--hybrid", action="store_true", help="Use Playwright once for cookies, then Requests")
    parser.add_argument("--start-page", type=int, default=None, help="Start page param (default auto)")
    parser.add_argument("--cooldown", type=float, default=0.0, help="Cooldown after each page downloads (seconds)")
    parser.add_argument("--threads", type=int, default=1, help="Download threads per page (default 1)")
    parser.add_argument("--zero-retries", type=int, default=1, help="Retry pages with zero results")
    parser.add_argument("--zero-cooldown", type=float, default=30.0, help="Cooldown before retrying zero-result pages")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    session = requests.Session()
    session.headers.update(HEADERS)
    warm_up_session(session)

    pw = None
    if args.use_playwright or args.hybrid:
        pw = PlaywrightFetcher(
            headed=args.headed,
            pause=args.pause,
            debug_dir=args.debug_dir or None,
        )

    first_url = f"{BASE_URL}?page=1"
    debug_dir = args.debug_dir or None
    soup = get_soup(
        first_url,
        session,
        force_playwright=args.use_playwright,
        headed=args.headed,
        pause=args.pause,
        debug_dir=debug_dir,
        pw=pw,
        is_first=True,
    )
    min_page, max_page = find_pagination_bounds(soup)
    last_link = find_last_page_link(soup)
    if last_link is not None:
        max_page = max(max_page, last_link)
    if args.start_page is not None:
        min_page = args.start_page
    if args.pages > 0:
        max_page = min_page + args.pages - 1
    elif args.max_pages > 0:
        max_page = min_page + args.max_pages - 1

    total_pages = max_page - min_page + 1
    print(f"Detected pages: {total_pages} (page param {min_page}..{max_page})")

    seen_urls: set[str] = set()
    total_downloaded_bytes = 0
    total_files_downloaded = 0
    total_files_skipped = 0
    start_time = time.time()
    zero_pages: list[int] = []

    if args.hybrid and pw is not None:
        # Transfer cookies to requests session after clearing any gate once.
        for c in pw.export_cookies():
            session.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))

    for page in range(min_page, max_page + 1):
        page_start = time.time()
        url = f"{BASE_URL}?page={page}"
        soup = get_soup(
            url,
            session,
            force_playwright=args.use_playwright and not args.hybrid,
            headed=args.headed,
            pause=args.pause,
            debug_dir=debug_dir,
            pw=pw,
        )
        page_links = extract_pdf_links(soup)
        if not page_links:
            zero_pages.append(page)
        abs_page_links = [urljoin(BASE_URL, href) for href in page_links]
        human_page = (page - min_page) + 1
        print(f"Page {human_page}/{total_pages} (param {page}): {len(page_links)} pdf links")

        page_downloaded_bytes = 0
        page_downloaded_files = 0
        page_skipped_files = 0

        cookies = session.cookies.get_dict()

        if args.threads <= 1:
            for link in abs_page_links:
                if link in seen_urls:
                    page_skipped_files += 1
                    continue
                seen_urls.add(link)
                path, bytes_written, skipped = download_file(link, args.out, session, args.dry_run, cookies)
                if skipped:
                    page_skipped_files += 1
                else:
                    page_downloaded_files += 1
                    page_downloaded_bytes += bytes_written
                status = "SKIP" if skipped else "DOWN"
                print(f"{status} {link} -> {path}")
        else:
            to_fetch = []
            for link in abs_page_links:
                if link in seen_urls:
                    page_skipped_files += 1
                else:
                    seen_urls.add(link)
                    to_fetch.append(link)

            with ThreadPoolExecutor(max_workers=args.threads) as pool:
                future_map = {
                    pool.submit(download_file, link, args.out, session, args.dry_run, cookies): link
                    for link in to_fetch
                }
                for fut in as_completed(future_map):
                    link = future_map[fut]
                    try:
                        path, bytes_written, skipped = fut.result()
                    except Exception as e:
                        page_skipped_files += 1
                        print(f"ERR  {link} -> {e}")
                        continue
                    if skipped:
                        page_skipped_files += 1
                    else:
                        page_downloaded_files += 1
                        page_downloaded_bytes += bytes_written
                    status = "SKIP" if skipped else "DOWN"
                    print(f"{status} {link} -> {path}")

        total_downloaded_bytes += page_downloaded_bytes
        total_files_downloaded += page_downloaded_files
        total_files_skipped += page_skipped_files

        page_elapsed = time.time() - page_start
        elapsed = time.time() - start_time
        pages_done = human_page
        pages_left = total_pages - pages_done
        avg_per_page = elapsed / pages_done if pages_done > 0 else 0
        eta = avg_per_page * pages_left
        mb = page_downloaded_bytes / (1024 * 1024)

        print(
            f"Page {human_page}/{total_pages} done: "
            f"{page_downloaded_files} downloaded, {page_skipped_files} skipped, "
            f"{mb:.2f} MB in {page_elapsed:.1f}s. "
            f"ETA {eta/60:.1f} min."
        )

        if args.delay > 0:
            time.sleep(args.delay)
        if args.cooldown > 0:
            time.sleep(args.cooldown)

    if zero_pages and args.zero_retries > 0:
        print(f"Zero-result pages: {len(zero_pages)}. Retrying after {args.zero_cooldown}s...")
        time.sleep(args.zero_cooldown)
        retry_pages = list(zero_pages)
        zero_pages = []
        for attempt in range(1, args.zero_retries + 1):
            print(f"Retry attempt {attempt}/{args.zero_retries}...")
            cookies = session.cookies.get_dict()
            for page in retry_pages:
                url = f"{BASE_URL}?page={page}"
                soup = get_soup(
                    url,
                    session,
                    force_playwright=args.use_playwright and not args.hybrid,
                    headed=args.headed,
                    pause=args.pause,
                    debug_dir=debug_dir,
                    pw=pw,
                )
                page_links = extract_pdf_links(soup)
                if not page_links:
                    zero_pages.append(page)
                    continue
                abs_page_links = [urljoin(BASE_URL, href) for href in page_links]
                for link in abs_page_links:
                    if link in seen_urls:
                        continue
                    seen_urls.add(link)
                    _, bytes_written, skipped = download_file(link, args.out, session, args.dry_run, cookies)
                    if skipped:
                        total_files_skipped += 1
                    else:
                        total_files_downloaded += 1
                        total_downloaded_bytes += bytes_written
                print(f"Retry page {page}: {len(page_links)} pdf links")

            if not zero_pages:
                break
            retry_pages = list(zero_pages)
            zero_pages = []
            time.sleep(args.zero_cooldown)

    if zero_pages:
        zero_path = os.path.join(args.out, "zero_pages.txt")
        with open(zero_path, "w", encoding="utf-8") as f:
            for p in sorted(set(zero_pages)):
                f.write(f"{p}\n")
        print(f"Remaining zero-result pages written to {zero_path}")

    total_mb = total_downloaded_bytes / (1024 * 1024)
    print(
        f"All pages done: {total_files_downloaded} downloaded, "
        f"{total_files_skipped} skipped, {total_mb:.2f} MB total."
    )

    if not args.dry_run:
        write_index_html(args.out)
        print(f"Index written to {os.path.join(args.out, 'index.html')}")

    if pw is not None:
        pw.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
