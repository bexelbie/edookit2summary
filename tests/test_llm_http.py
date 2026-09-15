# ABOUTME: Tests LLM API calls use Python HTTP instead of curl subprocesses.

import json
import unittest
from unittest.mock import patch

import edookit


class _Response:
    status = 200

    def __init__(self, body):
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body


class LlmHttpTests(unittest.TestCase):
    def test_azure_openai_uses_urllib(self):
        config = {
            "azure_openai_endpoint": "https://example.openai.azure.com/",
            "azure_openai_key": "secret",
            "azure_openai_api_version": "2024-05-01-preview",
        }

        with patch("edookit.subprocess.run", side_effect=AssertionError("curl used")), \
                patch("edookit.urllib_request.urlopen",
                      return_value=_Response('{"choices":[{"message":{"content":"ok"}}]}')) as urlopen:
            result = edookit._azure_openai_chat(config, [{"role": "user", "content": "hi"}], "deploy")

        self.assertEqual(result, "ok")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.full_url,
            "https://example.openai.azure.com/openai/deployments/deploy/chat/completions?api-version=2024-05-01-preview",
        )
        self.assertEqual(json.loads(request.data), {"messages": [{"role": "user", "content": "hi"}]})

    def test_gemini_uses_urllib(self):
        with patch("edookit.subprocess.run", side_effect=AssertionError("curl used")), \
                patch("edookit.urllib_request.urlopen",
                      return_value=_Response('{"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}')) as urlopen:
            result = edookit._gemini_chat({"gemini_api_key": "secret"}, "hi", "gemini-model")

        self.assertEqual(result, "ok")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.full_url,
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-model:generateContent",
        )
        self.assertEqual(json.loads(request.data), {"contents": [{"parts": [{"text": "hi"}]}]})


if __name__ == "__main__":
    unittest.main()
