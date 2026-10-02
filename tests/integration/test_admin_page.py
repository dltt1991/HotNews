"""Packaged assets and observable browser behavior at the localhost boundary."""

from datetime import datetime, timezone
import ast
from fnmatch import fnmatchcase
from html.parser import HTMLParser
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import shutil
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile

from hotnews.admin.app import AdminApplication
from hotnews.config import AppConfig
from hotnews.storage.database import Database
from hotnews.storage.subscriptions import SubscriptionRepository


ROOT = Path(__file__).resolve().parents[2]
HOST = "127.0.0.1:8081"


class PageParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.nodes = []
        self.references = []
        self.feed(html)

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        self.nodes.append({"tag": tag, "attrs": attributes})
        for name in ("src", "href"):
            if name in attributes:
                self.references.append(attributes[name])


class AdminPageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = AppConfig(database_path=str(Path(directory.name) / "page.db"))
        database = Database(config.database_path)
        database.migrate()
        repository = SubscriptionRepository(database)
        self.app = AdminApplication(config, repository, csrf_token="page-token")
        sub = repository.create("chat-a", "member", "<img src=x onerror=alert(1)>",
                                ["AI", "<script>bad()</script>"], ["AI"],
                                now=datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.sub = json.loads(self.get("/api/subscriptions/" + sub.id).body)["subscription"]

    def get(self, path, method="GET", host=HOST):
        return self.app.handle(method, path, {"Host": host}, b"")

    def page(self):
        response = self.get("/")
        self.assertEqual(response.status, 200, "packaged admin page must be served")
        return PageParser(response.body.decode("utf-8"))

    def test_page_uses_local_assets_and_accessible_labeled_controls(self):
        page = self.page()
        self.assertEqual(self.get("/").headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(set(page.references), {"/static/app.js", "/static/styles.css"})
        tags = {node["tag"] for node in page.nodes}
        self.assertTrue({"main", "table", "caption", "thead", "tbody", "form"}.issubset(tags))
        labels = {node["attrs"].get("for") for node in page.nodes if node["tag"] == "label"}
        for node in page.nodes:
            if node["tag"] in ("input", "select", "textarea"):
                self.assertIn(node["attrs"]["id"], labels)
        self.assertTrue(any(node["attrs"].get("role") == "status" and
                            node["attrs"].get("aria-live") == "polite" for node in page.nodes))

    def test_static_mime_security_headers_and_exact_paths(self):
        for route, content_type in (("/", "text/html; charset=utf-8"),
                                    ("/static/app.js", "text/javascript; charset=utf-8"),
                                    ("/static/styles.css", "text/css; charset=utf-8")):
            with self.subTest(route=route):
                response = self.get(route)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"], content_type)
                self.assertEqual(int(response.headers["Content-Length"]), len(response.body))
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
                self.assertEqual(self.get(route, host="evil.example:8081").status, 403)
                self.assertEqual(self.get(route, method="POST").status, 405)
        for route in ("/static/../app.py", "/static/%2e%2e/app.py", "/static/missing.js", "/static/"):
            self.assertEqual(self.get(route).status, 404)

    def test_wheel_contains_assets_that_the_served_page_references(self):
        self.page()
        try:
            build_version = version("setuptools")
        except PackageNotFoundError:
            self.skipTest("setuptools build backend is not installed")
        if int(build_version.split(".")[0]) < 61:
            self.skipTest("installed setuptools is below the project's declared >=61 build requirement")
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            shutil.copy2(str(ROOT / "pyproject.toml"), str(build / "pyproject.toml"))
            shutil.copytree(str(ROOT / "src"), str(build / "src"))
            result = subprocess.run([sys.executable, "-c",
                "from setuptools.build_meta import build_wheel; build_wheel('dist')"],
                cwd=str(build), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with zipfile.ZipFile(str(next((build / "dist").glob("*.whl")))) as wheel:
                for name in ("index.html", "app.js", "styles.css"):
                    self.assertTrue(wheel.read("hotnews/admin/static/" + name))

    def test_package_data_patterns_include_all_page_assets(self):
        self.page()
        # This single TOML array has Python-compatible literal syntax; no TOML runtime dependency.
        config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        section = re.search(r"(?ms)^\[tool\.setuptools\.package-data\]\s*\n(.*?)(?=^\[|\Z)", config)
        self.assertIsNotNone(section, "package-data table required by the build backend")
        value = re.search(r'(?m)^"hotnews\.admin"\s*=\s*(\[[^\n]*\])', section.group(1))
        self.assertIsNotNone(value, "admin assets must be assigned to their owning package")
        patterns = ast.literal_eval(value.group(1))
        for name in ("static/index.html", "static/app.js", "static/styles.css"):
            self.assertTrue(any(fnmatchcase(name, pattern) for pattern in patterns), name)

    def browser(self, case):
        if shutil.which("node") is None:
            self.skipTest("Node is an optional development-only JavaScript test runner")
        page = self.page()
        response = self.get("/static/app.js")
        self.assertEqual(response.status, 200)
        scenario = {"case": case, "nodes": page.nodes,
                    "javascript": response.body.decode("utf-8"), "subscription": self.sub}
        result = subprocess.run(["node", str(ROOT / "tests/integration/admin_browser_harness.js")],
                                input=json.dumps(scenario), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_untrusted_fields_are_rendered_as_text_and_dates_use_shanghai(self):
        self.browser("render")

    def test_filters_and_history_toggle_request_the_matching_list(self):
        self.browser("filters")

    def test_keyword_matches_are_owned_by_the_server_unicode_filter(self):
        self.browser("casefold")

    def test_edit_validates_then_sends_version_csrf_and_new_schedule(self):
        self.browser("edit")

    def test_pause_resume_and_run_now_send_current_version(self):
        self.browser("actions")

    def test_cancellation_requires_confirmation_and_hides_cancelled_row(self):
        self.browser("cancel")

    def test_conflict_blocks_stale_edits_until_explicit_refresh(self):
        self.browser("conflict")

    def test_network_and_api_errors_are_visible_and_controls_recover(self):
        self.browser("errors")

    def test_pending_paused_and_cancelled_rows_offer_only_valid_actions(self):
        self.browser("states")


if __name__ == "__main__":
    unittest.main()
