"""Playwright browser helpers for the GitHub Actions scraper."""

from contextlib import contextmanager

from playwright.sync_api import sync_playwright

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT_MS = 30_000


@contextmanager
def open_page(url: str, wait_seconds: float = 3.0, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    """Yield page HTML for a fully-rendered page, then clean up.

    Networkidle is attempted first (SPA pages), falling back to load.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale="id-ID")
            page = context.new_page()
            try:
                page.goto(url, timeout=timeout_ms, wait_until="networkidle")
            except Exception:
                page.goto(url, timeout=timeout_ms, wait_until="load")
            page.wait_for_timeout(int(wait_seconds * 1000))
            yield page.content()
        finally:
            browser.close()
