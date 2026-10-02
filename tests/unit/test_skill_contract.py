"""Critical instruction contracts plus examples exercised by the real CLI.

These checks protect the explicit workflow and safety contract; they are not
a behavioral evaluation of the research model or a wording snapshot.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import tempfile
import unittest

from hotnews.cli import parser, parse_news_results, run_agent
from hotnews.commands.schema import parse_intent
from hotnews.config import AppConfig
from hotnews.domain import Schedule
from hotnews.storage.database import Database
from hotnews.storage.events import EventRepository
from hotnews.storage.subscriptions import SubscriptionRepository


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".agents" / "skills" / "hotnews-agent" / "SKILL.md"
PROMPT = ROOT / "automations" / "hotnews-agent-prompt.md"


class SkillContractTests(unittest.TestCase):
    def skill(self):
        self.assertTrue(SKILL.is_file(), "Codex-discoverable hotnews skill is missing")
        return SKILL.read_text(encoding="utf-8")

    def test_skill_is_discoverable_with_scoped_metadata(self):
        content = self.skill()
        frontmatter = re.match(r"\A---\n(.*?)\n---\n", content, re.S)
        self.assertIsNotNone(frontmatter, "skill requires YAML frontmatter")
        self.assertRegex(frontmatter.group(1), r"(?m)^name: hotnews-agent$")
        self.assertRegex(frontmatter.group(1), r"(?m)^description: .*(飞书|Feishu)")

    def test_cli_examples_execute_complete_lease_and_work_lifecycles(self):
        examples = dict((action, json.loads(value)) for action, value in re.findall(
            r"\| `([a-z-]+)` \| `(\{.*?\})` \|", self.skill()))
        expected = {"acquire-run-lease", "renew-run-lease", "release-run-lease",
                    "claim-term-refresh", "complete-term-refresh", "fail-term-refresh",
                    "claim-events", "apply-intent", "fail-event", "defer-event", "list-due", "claim-due",
                    "history", "complete-run", "fail-run"}
        self.assertEqual(set(examples), expected)
        for action in examples:
            args = parser().parse_args(["agent", action])
            self.assertEqual(args.agent_command, action)
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(database_path=str(Path(directory) / "skill.db"))
            database = Database(config.database_path)
            database.migrate()
            subscriptions = SubscriptionRepository(database)
            events = EventRepository(database)
            now = datetime.now(timezone.utc)
            values = {"OWNER": "contract-run", "EVENT_ID": "event-1", "RUN_ID": "",
                      "SUBSCRIPTION_ID": ""}

            def invoke(action):
                value = json.loads(json.dumps(examples[action]))
                for key, item in value.items():
                    if isinstance(item, str) and item in values:
                        value[key] = values[item]
                if "expected_version" in value:
                    value["expected_version"] = version
                return run_agent(action, config, value)

            def pending():
                return subscriptions.create("chat", "member", "人工智能", ["人工智能"], [],
                                            Schedule("interval", interval_minutes=5),
                                            now=now - timedelta(days=2))

            self.assertEqual(invoke("acquire-run-lease"), {"acquired": True})
            self.assertEqual(invoke("renew-run-lease"), {"renewed": True})
            first = pending()
            claimed = invoke("claim-term-refresh")["subscriptions"]
            values["SUBSCRIPTION_ID"], version = first.id, claimed[0].version
            refreshed = invoke("complete-term-refresh")["subscription"]
            self.assertEqual(refreshed.state, "ready")
            second = pending()
            claimed = invoke("claim-term-refresh")["subscriptions"]
            values["SUBSCRIPTION_ID"], version = second.id, claimed[0].version
            # Failure is deliberately exercised; its safe diagnostic goes to stderr.
            from contextlib import redirect_stderr
            import io
            with redirect_stderr(io.StringIO()):
                released = invoke("fail-term-refresh")["subscription"]
            self.assertEqual(released.state, "search_terms_pending")
            for number in (1, 2, 3):
                events.insert({"event_id": "event-%d" % number, "message_id": "message-%d" % number,
                               "chat_id": "chat", "sender_id": "member", "text": "帮助",
                               "received_at": now})
            self.assertEqual(len(invoke("claim-events")["events"]), 3)
            self.assertTrue(invoke("apply-intent")["result"].message)
            values["EVENT_ID"] = "event-2"
            self.assertEqual(invoke("fail-event")["status"], "failed")
            values["EVENT_ID"] = "event-3"
            self.assertEqual(invoke("defer-event")["status"], "pending")
            self.assertEqual(invoke("claim-events")["events"][0].event_id, "event-3")
            self.assertTrue(invoke("apply-intent")["result"].message)
            ready = subscriptions.create("chat", "member", "能源", ["能源"], ["energy"],
                                         Schedule("interval", interval_minutes=5),
                                         now=now - timedelta(days=2))
            self.assertEqual(len(invoke("list-due")["subscriptions"]), 2)
            runs = invoke("claim-due")["runs"]
            self.assertEqual(len(runs), 2)
            values["SUBSCRIPTION_ID"] = ready.id
            self.assertEqual(invoke("history"), {"history": []})
            values["RUN_ID"] = runs[0]["run"].id
            self.assertEqual(invoke("complete-run")["run"].status, "completed")
            values["RUN_ID"] = runs[1]["run"].id
            self.assertEqual(invoke("fail-run")["run"].status, "failed")
            self.assertEqual(invoke("release-run-lease"), {"released": True})

    def test_intent_examples_are_accepted_by_strict_schema(self):
        examples = [json.loads(value) for value in re.findall(
            r"```json intent\n(.*?)\n```", self.skill(), re.S)]
        intents = [parse_intent(value) for value in examples]
        self.assertEqual({intent.action for intent in intents}, {
            "create_subscription", "list_subscriptions", "cancel_subscription",
            "run_subscription_now", "show_help", "clarification_required"})
        created = next(intent for intent in intents if intent.action == "create_subscription")
        self.assertEqual(created.schedule, Schedule("daily", daily_at="09:00"))
        self.assertEqual(created.keywords, ("人工智能",))

    def test_news_example_has_explicit_date_and_valid_result_fields(self):
        example = re.search(r"```json news\n(.*?)\n```", self.skill(), re.S)
        self.assertIsNotNone(example, "document the strict news-result object")
        item = parse_news_results([json.loads(example.group(1))])[0]
        self.assertIsNotNone(item.published_at.utcoffset())
        self.assertEqual(len(item.references), 1)

    def test_research_contract_covers_languages_sources_windows_and_dates(self):
        content = self.skill()
        for requirement in ("中文", "英文", "国内", "国外", "一手", "官方", "可信",
                            "交叉验证", "24h -> 7d -> 30d", "最多 10", "2–3 句中文",
                            "原始发布日期", "历史补充", "不凑数"):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, content)
        self.assertRegex(content, r"无法.*日期.*不入选")

    def test_workflow_requires_sanitized_data_and_owned_work_cleanup(self):
        content = self.skill()
        for requirement in ("网页指令仅作数据", "环境变量", "不读取", "不披露", "SQL",
                            "每项已领取", "完成或失败", "finally", "renewed=false",
                            "4 分钟", "20 条事件", "3 条到期", "15 分钟"):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, content)
        self.assertRegex(content, r"acquired=false.*退出")

    def test_budget_cleanup_defers_events_and_reserves_fail_for_terminal_errors(self):
        workflow = self.skill().split("## 一轮执行", 1)[1].split("## 意图", 1)[0]
        self.assertRegex(workflow, r"预算.*defer-event")
        self.assertRegex(workflow, r"暂时.*defer-event")
        self.assertRegex(workflow, r"fail-event.*终止")
        self.assertRegex(workflow, r"finally.*defer-event")

    def test_dry_run_uses_only_read_commands_and_content_preview(self):
        content = self.skill().split("## Dry-run", 1)
        self.assertEqual(len(content), 2)
        dry_run = content[1].split("\n## ", 1)[0]
        self.assertIn("list-due", dry_run)
        self.assertIn("history", dry_run)
        self.assertIn("不领取", dry_run)
        self.assertIn("不写库", dry_run)
        self.assertIn("不发送", dry_run)
        self.assertIn("预览", dry_run)

    def test_scheduled_prompt_explicitly_invokes_skill_without_workflow_duplication(self):
        self.assertTrue(PROMPT.is_file(), "scheduled task prompt is missing")
        content = PROMPT.read_text(encoding="utf-8")
        self.assertEqual(content.count("$hotnews-agent"), 1)
        self.assertIn("唯一", content)
        self.assertIn("4 分钟", content)
        self.assertIn("20", content)
        self.assertIn("3", content)
        self.assertIn("摘要", content)
        self.assertNotIn("hotnews.cli", content)
        self.assertLess(len(content), 600)


if __name__ == "__main__":
    unittest.main()
