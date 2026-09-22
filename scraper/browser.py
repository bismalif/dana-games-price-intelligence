"""Playwright browser helpers for the GitHub Actions scraper."""

from __future__ import annotations

import time
from contextlib import contextmanager

from playwright.sync_api import sync_playwright

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT_MS = 45_000
GOTO_ATTEMPTS = 2

# Some hosts (e.g. dana.id) abort HTTP/2 connections from datacenter IPs
# with ERR_HTTP2_PROTOCOL_ERROR. Forcing HTTP/1.1 avoids that class of
# failure entirely.
LAUNCH_ARGS = ["--disable-http2"]


def _settle(page, wait_seconds: float) -> None:
    """Wait for render, then scroll through the page to trigger any lazy
    loading (many catalogs only mount package cards when scrolled into
    view), then return to the top."""
    page.wait_for_timeout(int(wait_seconds * 1000))
    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(600)
    except Exception:
        pass


@contextmanager
def open_page(url: str, wait_seconds: float = 3.0, timeout_ms: int = DEFAULT_TIMEOUT_MS):
    """Yield page HTML for a fully-rendered page, then clean up.

    Navigation retries once; wait strategy falls back from networkidle
    through load to domcontentloaded so a single hanging subresource
    cannot poison the whole attempt.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale="id-ID")
            page = context.new_page()
            last_error: Exception | None = None
            for attempt in range(1, GOTO_ATTEMPTS + 1):
                try:
                    for wait_until in ("networkidle", "load", "domcontentloaded"):
                        try:
                            page.goto(url, timeout=timeout_ms, wait_until=wait_until)
                            last_error = None
                            break
                        except Exception as exc:
                            last_error = exc
                    if last_error is None:
                        break
                except Exception as exc:
                    last_error = exc
                if attempt < GOTO_ATTEMPTS:
                    time.sleep(2.0 * attempt)
            if last_error is not None:
                raise last_error
            _settle(page, wait_seconds)
            yield page.content()
        finally:
            browser.close()


def open_catalog_pages(
    url: str,
    wait_seconds: float = 3.0,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    max_tabs: int = 12,
) -> list[str]:
    """Load a catalog page, click through its category pills/tabs, and
    return one HTML snapshot per visible state (first = default view).

    Many top-up catalogs (DANA, Codashop, UniPin...) hide package groups
    like 'Weekly Diamond Pass' behind tabs. We click each pill once and
    capture the DOM after it settles, so extraction sees every group.
    """
    snapshots: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale="id-ID")
            page = context.new_page()
            last_error: Exception | None = None
            for attempt in range(1, GOTO_ATTEMPTS + 1):
                try:
                    for wait_until in ("networkidle", "load", "domcontentloaded"):
                        try:
                            page.goto(url, timeout=timeout_ms, wait_until=wait_until)
                            last_error = None
                            break
                        except Exception as exc:
                            last_error = exc
                    if last_error is None:
                        break
                except Exception as exc:
                    last_error = exc
                if attempt < GOTO_ATTEMPTS:
                    time.sleep(2.0 * attempt)
            if last_error is not None:
                raise last_error
            _settle(page, wait_seconds)
            snapshots.append(page.content())

            # Click candidate tab/pill elements; capture DOM after each.
            clicked: set[str] = set()
            handles = page.query_selector_all(
                "[role=tab], button, [class*=tab i], [class*=pill i], [class*=chip i], [class*=category i]"
            )
            for handle in handles[:200]:
                if len(snapshots) > max_tabs:
                    break
                try:
                    label = (handle.inner_text() or "").strip()
                    if not label or len(label) > 40 or label.lower() in clicked:
                        continue
                    # Only click things that look like category tabs, not
                    # 'buy' buttons or links that navigate away.
                    if not handle.is_visible():
                        continue
                    clicked.add(label.lower())
                    handle.click(timeout=2000)
                    page.wait_for_timeout(1200)
                    snapshots.append(page.content())
                except Exception:
                    continue  # unclickable element - skip
        finally:
            browser.close()
    return snapshots
