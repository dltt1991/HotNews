import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from hotnews.agent import HotNewsAgent, _matches
from hotnews.channels import render_markdown
from hotnews.models import NewsItem
from hotnews.scheduler import _due


class HotNewsTests(unittest.TestCase):
    def test_matches_keywords_and_excludes(self):
        item = NewsItem("test", "A new LLM agent", "https://example.com", score=12)
        self.assertTrue(_matches(item, {"keywords": ["llm"], "min_score": 10}))
        self.assertFalse(_matches(item, {"keywords": ["llm"], "exclude_keywords": ["agent"]}))

    def test_markdown(self):
        text = render_markdown("日报", [NewsItem("source", "title", "https://example.com")])
        self.assertIn("[title](https://example.com)", text)

    def test_daily_schedule_once_per_minute(self):
        now = datetime(2025, 1, 1, 9, 0)
        self.assertTrue(_due({"type": "daily", "at": "09:00"}, now, {}, "daily"))
        self.assertFalse(_due({"type": "daily", "at": "09:00"}, now,
                              {"daily": "2025-01-01 09:00"}, "daily"))

    @patch("hotnews.agent.send")
    @patch("hotnews.agent.HotNewsAgent.gather")
    def test_delivery_is_deduplicated(self, gather, sender):
        item = NewsItem("source", "AI news", "https://example.com/1")
        gather.return_value = {"source": [item]}
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "database": directory + "/db.sqlite",
                "sources": [{"name": "source", "type": "rss", "url": "unused"}],
                "subscriptions": [{"name": "sub", "keywords": ["AI"],
                                   "channels": [{"name": "group", "type": "wecom", "webhook": "x"}]}],
            }
            agent = HotNewsAgent(config)
            self.assertEqual(agent.run_once(), 1)
            self.assertEqual(agent.run_once(), 0)
            self.assertEqual(sender.call_count, 1)


if __name__ == "__main__":
    unittest.main()
