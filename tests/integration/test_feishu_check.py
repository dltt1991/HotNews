import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hotnews.cli import main
from hotnews.domain import ValidationError
from hotnews.feishu.connection import ConnectionSnapshot


class FeishuCheckTests(unittest.TestCase):
    def invoke(self, result=None, error=None, environ=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        arguments = ["hotnews", "--config", "/path/that/must/not/be/opened.json", "check-feishu"]
        def check(_environ):
            if error is not None:
                raise error
            return result
        with patch("sys.argv", arguments), patch("sys.stdout", stdout), patch("sys.stderr", stderr), \
             patch.dict(os.environ, environ or {}, clear=True), patch("hotnews.cli.check_feishu", check):
            code = main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_success_is_safe_json_and_does_not_open_config_or_database(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-exist.db"
            result = {"state": "connected", "bot_identity": "resolved",
                      "proxy": "http://127.0.0.1:7890"}
            code, stdout, stderr = self.invoke(result=result)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout), result)
            self.assertEqual(stderr, "")
            self.assertFalse(marker.exists())

    def test_validation_and_connection_failures_use_distinct_codes_without_secrets(self):
        cases = ((ValidationError("secret-config"), 2, "Invalid Feishu configuration"),
                 (OSError("wss://host/ws?ticket=secret-network"), 1,
                  "Feishu connection check failed"))
        for error, expected_code, message in cases:
            with self.subTest(error=type(error).__name__):
                code, stdout, stderr = self.invoke(error=error)
                self.assertEqual(code, expected_code)
                self.assertEqual(json.loads(stdout), {"error": "validation_error" if expected_code == 2
                                                       else "connection_error"})
                self.assertIn(message, stderr)
                self.assertNotIn("secret", stdout + stderr)


if __name__ == "__main__":
    unittest.main()
