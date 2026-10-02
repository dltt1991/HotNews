"""The preview CLI renders actual cards with no DB or Feishu side effects."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DryRunTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.database_path = self.path / "must-not-create.db"
        self.config = self.path / "config.json"
        self.config.write_text(json.dumps({"database_path": str(self.database_path)}), encoding="utf-8")

    def input(self):
        return {"subscription": {"display_number": 2, "topic": "AI 新闻", "keywords": ["AI", "大模型"]},
                "search_window_days": 7,
                "results": [{"title": "模型发布", "url": "https://official.example/item?utm_source=search#top",
                             "source": "官方", "published_at": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
                             "summary": "发布了新模型。可执行复杂任务。", "event_key": "new model"}]}

    def invoke(self, value, command="dry-run"):
        return subprocess.run([sys.executable, "-m", "hotnews.cli", "--config", str(self.config), command],
                              input=json.dumps(value), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, env=dict(os.environ, PYTHONPATH="src", FEISHU_APP_SECRET="secret-preview"), timeout=10)

    def test_preview_renders_history_dates_original_keywords_and_canonical_url_without_database(self):
        process = self.invoke(self.input())
        self.assertEqual(process.returncode, 0, process.stderr)
        value = json.loads(process.stdout)
        self.assertEqual((value["result_count"], value["search_window_days"]), (1, 7))
        self.assertIn("订阅 #2", value["card"]["header"]["title"]["content"])
        self.assertIn("AI、大模型", value["card"]["header"]["title"]["content"])
        self.assertIn("历史补充", value["card"]["elements"][0]["text"]["content"])
        self.assertIn("发布日期：", value["card"]["elements"][0]["text"]["content"])
        self.assertEqual(value["card"]["elements"][1]["actions"][0]["url"], "https://official.example/item")
        self.assertFalse(self.database_path.exists())
        self.assertNotIn("secret-preview", process.stdout + process.stderr)

    def test_preview_empty_results_is_silent_no_empty_card(self):
        value = self.input()
        value["results"] = []
        process = self.invoke(value)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), {"card": None, "result_count": 0, "search_window_days": 7})
        self.assertFalse(self.database_path.exists())

    def test_preview_schema_rejects_unknown_fields_bad_dates_windows_and_eleven_items_before_any_writes(self):
        cases = []
        cases.append(dict(self.input(), owner="secret-preview"))
        cases.append(dict(self.input(), search_window_days=31))
        eleven = self.input()
        eleven["results"] *= 11
        cases.append(eleven)
        for field, wrong in (("published_at", "unknown"), ("published_at", (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()),
                             ("published_at", (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()), ("url", "javascript:alert(1)"),
                             ("references", ["https://u:p@example.com/"]), ("summary", "")):
            value = self.input()
            value["results"][0][field] = wrong
            cases.append(value)
        bad_context = self.input()
        bad_context["subscription"]["keywords"] = []
        cases.append(bad_context)
        bad_context = self.input()
        bad_context["subscription"]["display_number"] = True
        cases.append(bad_context)
        for value in cases:
            with self.subTest(value=value):
                process = self.invoke(value)
                self.assertEqual(process.returncode, 2, process.stderr)
                self.assertEqual(json.loads(process.stdout), {"error": "validation_error"})
                self.assertNotIn("secret-preview", process.stdout + process.stderr)
                self.assertFalse(self.database_path.exists())

    def test_preview_deduplicates_url_and_event_identity(self):
        value = self.input()
        original = value["results"][0]
        value["results"].extend([dict(original, event_key="duplicate url"),
                                  dict(original, url="https://official.example/other", event_key="NEW  MODEL")])
        process = self.invoke(value)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["result_count"], 1)

    def test_cli_has_expected_commands_and_sanitizes_serve_failure(self):
        process = subprocess.run([sys.executable, "-m", "hotnews.cli", "--help"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=dict(os.environ, PYTHONPATH="src"), timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertIn("{serve,check-feishu,agent,dry-run}", process.stdout)
        self.assertNotIn("scheduler", process.stdout)
        for command in ("once", "webhook"):
            self.assertEqual(self.invoke({}, command).returncode, 2)
        env = {key: value for key, value in os.environ.items() if not key.startswith("FEISHU_")}
        env["PYTHONPATH"] = "src"
        process = subprocess.run([sys.executable, "-m", "hotnews.cli", "--config", str(self.config), "serve"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, timeout=10)
        self.assertEqual(process.returncode, 1)
        self.assertNotIn("Traceback", process.stderr)
        self.assertIn("Runtime startup or service failed", process.stderr)
