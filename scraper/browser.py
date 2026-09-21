"""Playwright browser helpers for the GitHub Actions scraper."""

from __future__ import annotations

import time
from contextlib import contextmanager

from playwright.sync_api import sync_playwright

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT_MS = 30_000
GOTO_ATTEMPTS = 2

# Some hosts (e.g. dana.id) abort HTTP/2 connections from datacenter IPs
# with ERR_HTTP2_PROTOCOL_ERROR. Forcing HTTP/1.1 avoids that class of
# failure entirely.
LAUNCH_ARGS = ["--disable-http2"]


@contextmanager
def open_page(url: str, wait_seconds: float = 3.0, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    """Yield page HTML for a fully-rendered page, then clean up.

    Navigation retries once; wait strategy falls back from networkidle
    to load for pages with long-polling connections.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale="id-ID")
            page = context.new_page()
            last_error: Exception | None = None
            for attempt in range(1, GOTO_ATTEMPTS + 1):
                try:
                    try:
                        page.goto(url, timeout=timeout_ms, wait_until="networkidle")
                    except Exception:
                        page.goto(url, timeout=timeout_ms, wait_until="load")
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < GOTO_ATTEMPTS:
                        time.sleep(2.0 * attempt)
            if last_error is not None:
                raise last_error
            page.wait_for_timeout(int(wait_seconds * 1000))
            yield page.content()
        finally:
            browser.close()
