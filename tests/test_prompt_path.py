# ABOUTME: Tests the reusable prompt-building path and UTC date filtering.

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from edookit import AuthError, build_translation_prompt
from gather_updates import (
    PRAGUE_TZ,
    _item_timestamp_in_utc,
    _normalize_edookit_url,
    filter_items_for_utc_date,
    filter_new_items,
    parse_inbox_timestamp,
)


class FixedPragueNow(datetime):
    @classmethod
    def now(cls, tz=None):
        base = cls(2026, 6, 5, 0, 30, tzinfo=ZoneInfo("Europe/Prague"))
        return base if tz is None else base.astimezone(tz)


class PromptPathTests(unittest.TestCase):
    def test_normalize_edookit_url_handles_relative_absolute_and_encoded_urls(self):
        self.assertEqual(
            _normalize_edookit_url("assignments/detail?id=7"),
            "/assignments/detail?id=7",
        )
        self.assertEqual(
            _normalize_edookit_url("/assignments/detail?id=7"),
            "/assignments/detail?id=7",
        )
        absolute_url = "https://example.test/assignments/detail?id=7"
        self.assertEqual(_normalize_edookit_url(absolute_url), absolute_url)
        self.assertEqual(
            _normalize_edookit_url("messages/detail?id=7&amp;view=full"),
            "/messages/detail?id=7&view=full",
        )

    def test_build_translation_prompt_returns_exact_prompt_parts(self):
        summary = "- **Test**\n  Details"
        prompts = build_translation_prompt(summary, {"target_language": "English"})

        self.assertEqual(prompts["user_prompt"], summary)
        self.assertIn("You translate Czech school notifications to English.", prompts["system_prompt"])
        self.assertIn("Output only the translated text, no commentary", prompts["system_prompt"])

    def test_filter_items_for_utc_date_uses_utc_day_boundaries(self):
        items = [
            {"title": "inside", "timestamp": datetime(2026, 6, 4, 23, 30)},
            {"title": "next-day-utc", "timestamp": datetime(2026, 6, 5, 2, 30)},
        ]

        result = filter_items_for_utc_date(items, date(2026, 6, 4))

        self.assertEqual([item["title"] for item in result], ["inside"])

    def test_parse_inbox_timestamp_uses_prague_for_relative_labels(self):
        with patch("gather_updates.datetime", FixedPragueNow):
            ts = parse_inbox_timestamp("Yesterday, 23:30")

        self.assertEqual(ts.tzinfo, ZoneInfo("Europe/Prague"))
        self.assertEqual(_item_timestamp_in_utc({"timestamp": ts}), datetime(2026, 6, 4, 21, 30, tzinfo=timezone.utc))

    def test_parse_inbox_timestamp_returns_prague_aware_absolute_dates(self):
        ts = parse_inbox_timestamp("4. 6. 2026, 23:30")

        self.assertEqual(ts.tzinfo, PRAGUE_TZ)
        self.assertEqual(ts, datetime(2026, 6, 4, 23, 30, tzinfo=PRAGUE_TZ))

    def test_filter_new_items_handles_relative_and_absolute_timestamps(self):
        with patch("gather_updates.datetime", FixedPragueNow):
            items = [
                {"title": "relative", "timestamp": parse_inbox_timestamp("Today, 23:30")},
                {"title": "absolute", "timestamp": parse_inbox_timestamp("4. 6. 2026, 23:30")},
            ]

        last_run = datetime(2026, 6, 4, 22, 0, tzinfo=PRAGUE_TZ)

        filtered = filter_new_items(items, last_run)

        self.assertEqual([item["title"] for item in filtered], ["relative", "absolute"])

    def test_filter_new_items_accepts_persisted_naive_last_run(self):
        items = [
            {"title": "new", "timestamp": datetime(2026, 6, 5, 12, 30, tzinfo=PRAGUE_TZ)},
        ]

        filtered = filter_new_items(items, datetime(2026, 6, 5, 11, 0))

        self.assertEqual([item["title"] for item in filtered], ["new"])

    def test_prompt_for_date_skips_downloads_and_email_side_effects(self):
        item = {
            "type": "inboxMessage",
            "title": "Test",
            "description": "Body",
            "timestamp_raw": "4. 6. 2026, 23:30",
            "creator": "Teacher",
            "url": "/messages/1",
            "timestamp": datetime(2026, 6, 4, 23, 30, tzinfo=timezone.utc),
        }

        with patch("gather_updates.load_cookies", return_value={}), \
                patch("gather_updates.keepalive"), \
                patch("gather_updates.fetch_page", side_effect=["inbox", "dashboard"]), \
                patch("gather_updates.parse_inbox", return_value=[item]), \
                patch("gather_updates.parse_action_items", return_value=[]), \
                patch("gather_updates.fetch_upcoming_events", return_value=[]), \
                patch("gather_updates.fetch_item_detail", return_value={"name": "Test", "attachments": [{"name": "doc.pdf", "download_url": "/downloads/1"}]}), \
                patch("gather_updates.download_attachment") as download_attachment, \
                patch("gather_updates.send_email") as send_email, \
                patch("gather_updates.load_config", return_value={"target_language": "English"}), \
                patch("gather_updates.save_cookies") as save_cookies:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                with self.assertRaises(SystemExit) as exc:
                    import gather_updates
                    gather_updates.main(["cookies.json", "--prompt-for-date", "2026-06-04"])

        self.assertEqual(exc.exception.code, 0)
        download_attachment.assert_not_called()
        send_email.assert_not_called()
        save_cookies.assert_not_called()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["utc_date"], "2026-06-04")

    def test_prompt_for_date_does_not_send_auth_alert_email(self):
        with patch("gather_updates.load_cookies", return_value={}), \
                patch("gather_updates.load_config", return_value={"smtp_host": "smtp.example"}), \
                patch("gather_updates.keepalive", side_effect=AuthError("expired")), \
                patch("gather_updates._send_alert_email") as send_alert_email:
            with self.assertRaises(SystemExit) as exc:
                import gather_updates
                gather_updates.main(["cookies.json", "--prompt-for-date", "2026-06-04"])

        self.assertEqual(exc.exception.code, 1)
        send_alert_email.assert_not_called()


if __name__ == "__main__":
    unittest.main()
