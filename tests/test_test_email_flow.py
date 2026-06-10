# ABOUTME: Tests the optional Azure test-email lane and fallback config behavior.

import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import gather_updates


class TestEmailFlowTests(unittest.TestCase):
    def test_build_test_config_uses_main_azure_fallbacks_and_disables_gemini(self):
        config = {
            "azure_openai_endpoint": "https://main.openai.azure.com",
            "azure_openai_key": "main-key",
            "azure_openai_deployment": "main-deploy",
            "azure_openai_api_version": "2024-05-01-preview",
            "gemini_api_key": "gemini-key",
            "gemini_models": "gemini-model",
            "email_to": "main@example.com",
            "email_test": "test@example.com",
        }

        test_config = gather_updates.build_test_config(config)

        self.assertEqual(test_config["azure_openai_endpoint"], "https://main.openai.azure.com")
        self.assertEqual(test_config["azure_openai_key"], "main-key")
        self.assertEqual(test_config["azure_openai_deployment"], "main-deploy")
        self.assertEqual(test_config["azure_openai_api_version"], "2024-05-01-preview")
        self.assertEqual(test_config["email_to"], "test@example.com")
        self.assertEqual(test_config["gemini_api_key"], "")
        self.assertEqual(test_config["gemini_models"], "")

    def test_build_test_config_allows_per_field_test_overrides(self):
        config = {
            "azure_openai_endpoint": "https://main.openai.azure.com",
            "azure_openai_key": "main-key",
            "azure_openai_deployment": "main-deploy",
            "azure_openai_api_version": "2024-05-01-preview",
            "azure_test_deployment": "test-deploy",
            "azure_test_api_version": "2025-01-01-preview",
            "email_to": "main@example.com",
            "email_test": "test@example.com",
        }

        test_config = gather_updates.build_test_config(config)

        self.assertEqual(test_config["azure_openai_endpoint"], "https://main.openai.azure.com")
        self.assertEqual(test_config["azure_openai_key"], "main-key")
        self.assertEqual(test_config["azure_openai_deployment"], "test-deploy")
        self.assertEqual(test_config["azure_openai_api_version"], "2025-01-01-preview")

    def test_send_test_email_uses_test_recipient_and_azure_only_config(self):
        config = {
            "email_to": "main@example.com",
            "email_test": "test@example.com",
            "azure_openai_endpoint": "https://main.openai.azure.com",
            "azure_openai_key": "main-key",
            "azure_openai_deployment": "main-deploy",
            "azure_openai_api_version": "2024-05-01-preview",
            "gemini_api_key": "gemini-key",
            "gemini_models": "gemini-model",
        }

        with patch("gather_updates.translate_text", return_value="Test translated") as translate_text, \
                patch("gather_updates.send_email") as send_email:
            result = gather_updates.send_test_email(
                "Subject",
                "Summary",
                config,
                ["/tmp/attachment.pdf"],
            )

        self.assertTrue(result)
        translate_text.assert_called_once()
        send_email.assert_called_once()
        self.assertEqual(send_email.call_args.args[0], "Subject")
        self.assertEqual(send_email.call_args.args[1], "Test translated")
        self.assertEqual(send_email.call_args.kwargs["to_addr"], "test@example.com")

    def test_send_test_email_skips_incomplete_effective_azure_config(self):
        config = {
            "email_to": "main@example.com",
            "email_test": "test@example.com",
            "azure_openai_endpoint": "https://main.openai.azure.com",
            "azure_openai_key": "main-key",
            "azure_openai_deployment": "main-deploy",
            "azure_openai_api_version": "",
        }

        with patch("gather_updates.translate_text") as translate_text, \
                patch("gather_updates.send_email") as send_email:
            result = gather_updates.send_test_email(
                "Subject",
                "Summary",
                config,
                [],
            )

        self.assertFalse(result)
        translate_text.assert_not_called()
        send_email.assert_not_called()

    def test_send_test_email_uses_partial_test_azure_overrides_with_main_fallbacks(self):
        config = {
            "email_to": "main@example.com",
            "email_test": "test@example.com",
            "azure_openai_endpoint": "https://primary.openai.azure.com",
            "azure_openai_key": "primary-key",
            "azure_openai_deployment": "primary-deploy",
            "azure_openai_api_version": "2024-05-01-preview",
            "azure_test_deployment": "test-deploy",
            "azure_test_api_version": "2025-01-01-preview",
            "gemini_api_key": "gemini-key",
            "gemini_models": "gemini-model",
        }

        with patch("gather_updates.translate_text", return_value="Test translated") as translate_text, \
                patch("gather_updates.send_email") as send_email:
            result = gather_updates.send_test_email(
                "Subject",
                "Summary",
                config,
                [],
            )

        self.assertTrue(result)
        translate_text.assert_called_once()
        used_config = translate_text.call_args.args[1]
        self.assertEqual(used_config["azure_openai_endpoint"], "https://primary.openai.azure.com")
        self.assertEqual(used_config["azure_openai_key"], "primary-key")
        self.assertEqual(used_config["azure_openai_deployment"], "test-deploy")
        self.assertEqual(used_config["azure_openai_api_version"], "2025-01-01-preview")
        send_email.assert_called_once()

    def test_main_persists_last_run_before_test_lane(self):
        item = {
            "type": "inboxMessage",
            "title": "Test",
            "description": "Body",
            "timestamp_raw": "4. 6. 2026, 23:30",
            "creator": "Teacher",
            "url": "/messages/1",
            "timestamp": datetime(2026, 6, 4, 23, 30, tzinfo=timezone.utc),
        }
        order = []

        def fake_send_email(subject, body, config, downloaded_files=None, to_addr=None):
            order.append("send_email")

        def fake_send_test_email(subject, summary_markdown, config, downloaded_files):
            order.append(f"test_email:{len(order)}:{gather_updates.save_cookies.call_count}")
            return True

        with patch("gather_updates.load_cookies", return_value={"last_run": None}), \
                patch("gather_updates.keepalive"), \
                patch("gather_updates.fetch_page", side_effect=["inbox", "dashboard"]), \
                patch("gather_updates.parse_inbox", return_value=[item]), \
                patch("gather_updates.parse_action_items", return_value=[]), \
                patch("gather_updates.fetch_upcoming_events", return_value=[]), \
                patch("gather_updates.fetch_item_detail", return_value={"name": "Test", "attachments": []}), \
                patch("gather_updates.download_attachment"), \
                patch("gather_updates.translate_text", side_effect=["Primary translation", "Test translation"]), \
                patch("gather_updates.send_email", side_effect=fake_send_email) as send_email, \
                patch("gather_updates.send_test_email", side_effect=fake_send_test_email) as send_test_email, \
                patch("gather_updates.load_config", return_value={
                    "target_language": "English",
                    "smtp_host": "smtp.example",
                    "email_to": "main@example.com",
                    "email_test": "test@example.com",
                    "azure_openai_endpoint": "https://main.openai.azure.com",
                    "azure_openai_key": "main-key",
                    "azure_openai_deployment": "main-deploy",
                    "azure_openai_api_version": "2024-05-01-preview",
                    "azure_test_endpoint": "https://test.openai.azure.com",
                    "azure_test_key": "test-key",
                    "azure_test_deployment": "test-deploy",
                    "azure_test_api_version": "2024-05-01-preview",
                }), \
                patch("gather_updates.save_cookies", side_effect=lambda cookies, path: order.append("save_cookies")) as save_cookies:
            gather_updates.main(["cookies.json"])

        self.assertIn("send_email", order)
        self.assertIn("save_cookies", order)
        self.assertTrue(any(item.startswith("test_email:") for item in order))
        self.assertLess(order.index("save_cookies"), order.index(next(item for item in order if item.startswith("test_email:"))))

    def test_main_updates_last_run_without_aware_naive_mismatch(self):
        item = {
            "type": "inboxMessage",
            "title": "Test",
            "description": "Body",
            "timestamp_raw": "4. 6. 2026, 23:30",
            "creator": "Teacher",
            "url": "/messages/1",
            "timestamp": datetime(2026, 6, 4, 23, 30, tzinfo=ZoneInfo("Europe/Prague")),
        }

        with patch("gather_updates.load_cookies", return_value={"last_run": "2026-06-03T10:00:00"}), \
                patch("gather_updates.keepalive"), \
                patch("gather_updates.fetch_page", side_effect=["inbox", "dashboard"]), \
                patch("gather_updates.parse_inbox", return_value=[item]), \
                patch("gather_updates.parse_action_items", return_value=[]), \
                patch("gather_updates.fetch_upcoming_events", return_value=[]), \
                patch("gather_updates.fetch_item_detail", return_value={"name": "Test", "attachments": []}), \
                patch("gather_updates.download_attachment"), \
                patch("gather_updates.translate_text", return_value="Translated"), \
                patch("gather_updates.send_email") as send_email, \
                patch("gather_updates.load_config", return_value={"target_language": "English", "smtp_host": "smtp.example"}), \
                patch("gather_updates.save_cookies") as save_cookies:
            gather_updates.main(["cookies.json"])

        self.assertEqual(send_email.call_count, 1)
        save_cookies.assert_called_once()
        saved_cookies = save_cookies.call_args.args[0]
        self.assertTrue(saved_cookies["last_run"].endswith("+02:00") or "+00:00" in saved_cookies["last_run"])

    def test_main_keeps_primary_email_path_when_test_email_fails(self):
        item = {
            "type": "inboxMessage",
            "title": "Test",
            "description": "Body",
            "timestamp_raw": "4. 6. 2026, 23:30",
            "creator": "Teacher",
            "url": "/messages/1",
            "timestamp": datetime(2026, 6, 4, 23, 30, tzinfo=timezone.utc),
        }

        with patch("gather_updates.load_cookies", return_value={"last_run": None}), \
                patch("gather_updates.keepalive"), \
                patch("gather_updates.fetch_page", side_effect=["inbox", "dashboard"]), \
                patch("gather_updates.parse_inbox", return_value=[item]), \
                patch("gather_updates.parse_action_items", return_value=[]), \
                patch("gather_updates.fetch_upcoming_events", return_value=[]), \
                patch("gather_updates.fetch_item_detail", return_value={"name": "Test", "attachments": []}), \
                patch("gather_updates.download_attachment"), \
                patch("gather_updates.translate_text", side_effect=["Primary translation", RuntimeError("boom")]) as translate_text, \
                patch("gather_updates.send_email") as send_email, \
                patch("gather_updates.load_config", return_value={
                    "target_language": "English",
                    "smtp_host": "smtp.example",
                    "email_to": "main@example.com",
                    "email_test": "test@example.com",
                    "azure_openai_endpoint": "https://main.openai.azure.com",
                    "azure_openai_key": "main-key",
                    "azure_openai_deployment": "main-deploy",
                    "azure_openai_api_version": "2024-05-01-preview",
                }), \
                patch("gather_updates.save_cookies") as save_cookies:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                gather_updates.main(["cookies.json"])

        self.assertEqual(translate_text.call_count, 2)
        self.assertEqual(send_email.call_count, 1)
        self.assertEqual(send_email.call_args_list[0].kwargs.get("to_addr"), None)
        save_cookies.assert_called_once()


if __name__ == "__main__":
    unittest.main()
