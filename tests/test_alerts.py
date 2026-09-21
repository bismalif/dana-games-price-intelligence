"""Unit tests for scraper.alerts - signature building, offline, no network."""

import unittest
from unittest.mock import patch

from scraper.alerts import build_signed_url, send_markdown, send_ops_alert, send_undercut_alert


class TestBuildSignedUrl(unittest.TestCase):
    def test_contains_timestamp_and_encoded_sign(self):
        url = build_signed_url(
            "https://oapi.dingtalk.com/robot/send?access_token=abc",
            "SECsecret",
            timestamp_ms=1700000000000,
        )
        self.assertIn("timestamp=1700000000000", url)
        self.assertIn("sign=", url)
        # base64 '+' must be percent-encoded
        self.assertNotIn("sign=+ ", url)
        self.assertTrue(url.startswith("https://oapi.dingtalk.com/robot/send?access_token=abc&"))

    def test_webhook_without_existing_query_uses_question_mark(self):
        url = build_signed_url(
            "https://oapi.dingtalk.com/robot/send",
            "SECsecret",
            timestamp_ms=1700000000000,
        )
        self.assertIn("robot/send?timestamp=", url)

    def test_sign_is_deterministic(self):
        url_a = build_signed_url("https://x", "SECk", timestamp_ms=123)
        url_b = build_signed_url("https://x", "SECk", timestamp_ms=123)
        self.assertEqual(url_a, url_b)

    def test_different_secret_different_sign(self):
        url_a = build_signed_url("https://x", "SECk1", timestamp_ms=123)
        url_b = build_signed_url("https://x", "SECk2", timestamp_ms=123)
        self.assertNotEqual(url_a, url_b)


class TestSendMarkdown(unittest.TestCase):
    def test_posts_markdown_payload(self):
        captured = {}

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"errcode": 0, "errmsg": "ok"}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

        with patch("scraper.alerts.requests.post", side_effect=fake_post):
            result = send_markdown(
                "https://oapi.dingtalk.com/robot/send?access_token=t",
                "SECs",
                "Title",
                "Body text",
            )

        self.assertEqual(result, {"errcode": 0, "errmsg": "ok"})
        self.assertEqual(captured["json"]["msgtype"], "markdown")
        self.assertEqual(captured["json"]["markdown"]["title"], "Title")
        self.assertEqual(captured["json"]["markdown"]["text"], "Body text")
        self.assertIn("timestamp=", captured["url"])


class TestAlertContent(unittest.TestCase):
    def test_undercut_alert_contains_key_facts(self):
        sent = {}

        def fake_send(url, secret, title, text, at_all=False):
            sent["title"] = title
            sent["text"] = text
            return {"errcode": 0}

        env = {"DINGTALK_WEBHOOK_URL": "https://oapi.dingtalk.com/robot/send?access_token=t", "DINGTALK_SECRET": "SECs"}
        with patch.dict("os.environ", env), patch("scraper.alerts.send_markdown", side_effect=fake_send):
            send_undercut_alert(
                game_name="Mobile Legends: Bang Bang",
                sku_name="86 Diamonds + 8 Bonus",
                source_name="Codashop",
                dana_effective=265.96,
                competitor_effective=230.85,
                pct_cheaper=13.2,
                product_url="https://example.com/86",
            )

        self.assertIn("Codashop", sent["text"])
        self.assertIn("86 Diamonds + 8 Bonus", sent["text"])
        self.assertIn("13.2%", sent["text"])
        self.assertIn("https://example.com/86", sent["text"])

    def test_undercut_alert_is_noop_without_credentials(self):
        with patch.dict("os.environ", {}, clear=True), patch("scraper.alerts.send_markdown") as mock_send:
            result = send_undercut_alert(
                game_name="G", sku_name="S", source_name="C",
                dana_effective=1, competitor_effective=0.5, pct_cheaper=50,
            )
        self.assertIsNone(result)
        mock_send.assert_not_called()

    def test_ops_alert_uses_title(self):
        captured = {}

        def fake_send(url, secret, title, text, at_all=False):
            captured["title"] = title
            return {"errcode": 0}

        env = {"DINGTALK_WEBHOOK_URL": "https://oapi.dingtalk.com/robot/send?access_token=t", "DINGTALK_SECRET": "SECs"}
        with patch.dict("os.environ", env), patch("scraper.alerts.send_markdown", side_effect=fake_send):
            send_ops_alert("Scraper Ops Alert", "something broke")

        self.assertEqual(captured["title"], "Scraper Ops Alert")


if __name__ == "__main__":
    unittest.main()
