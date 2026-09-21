"""DingTalk signed-webhook notifications (Markdown messages).

Uses the custom group robot "signature" security mode:
  sign = urlsafe_base64(HmacSHA256(f"{timestamp}\n{secret}", key=secret))
  POST {webhook}&timestamp=...&sign=...
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from urllib.parse import quote_plus

import requests

TIMEOUT_SECONDS = 15


def build_signed_url(webhook_url: str, secret: str, timestamp_ms: int | None = None) -> str:
    timestamp = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = base64.b64encode(digest).decode("utf-8")
    separator = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{separator}timestamp={timestamp}&sign={quote_plus(sign)}"


def send_markdown(webhook_url: str, secret: str, title: str, text: str, at_all: bool = False) -> dict:
    url = build_signed_url(webhook_url, secret)
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": text},
        "at": {"isAtAll": at_all},
    }
    response = requests.post(url, json=payload, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def send_undercut_alert(
    game_name: str,
    sku_name: str,
    source_name: str,
    dana_effective,
    competitor_effective,
    pct_cheaper,
    product_url: str | None = None,
) -> dict | None:
    """Send one undercut alert. Reads credentials from the environment.

    Returns the DingTalk response dict, or None when not configured.
    """
    webhook_url = os.environ.get("DINGTALK_WEBHOOK_URL", "").strip()
    secret = os.environ.get("DINGTALK_SECRET", "").strip()
    if not webhook_url or not secret:
        return None

    def fmt(value) -> str:
        return f"IDR {value:,.2f}" if value is not None else "n/a"

    lines = [
        "### Competitor Undercut Alert",
        "",
        f"**{game_name}** - {sku_name}",
        "",
        f"- DANA effective price: **{fmt(dana_effective)}**",
        f"- {source_name} effective price: **{fmt(competitor_effective)}**",
        f"- Difference: **{pct_cheaper}% cheaper**",
    ]
    if product_url:
        lines.append(f"- Product: {product_url}")
    lines.append("")
    lines.append("The competitor is at least 10% cheaper than DANA.")

    return send_markdown(
        webhook_url,
        secret,
        "Competitor Undercut Alert",
        "\n".join(lines),
    )


def send_ops_alert(title: str, text: str) -> dict | None:
    """Operational alert (workflow failure, repeated source failures)."""
    webhook_url = os.environ.get("DINGTALK_WEBHOOK_URL", "").strip()
    secret = os.environ.get("DINGTALK_SECRET", "").strip()
    if not webhook_url or not secret:
        return None
    return send_markdown(webhook_url, secret, title, text)
