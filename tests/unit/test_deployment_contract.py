"""Static deployment safety contracts; these do not pretend to build Docker."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DeploymentContractTests(unittest.TestCase):
    def test_container_has_numeric_non_root_default_and_host_uid_override(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        users = re.findall(r"(?m)^USER (\d+):(\d+)\s*$", dockerfile)
        self.assertTrue(users, "container must default to a numeric unprivileged user")
        self.assertNotEqual(users[-1][0], "0")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('user: "${HOTNEWS_UID:-1000}:${HOTNEWS_GID:-1000}"', compose)
        self.assertEqual(re.findall(r"(?m)^EXPOSE (.+)$", dockerfile), ["8080"])
        ports = compose.split("    ports:\n", 1)[1].split("    volumes:", 1)[0]
        self.assertEqual(re.findall(r'"([^\"]+)"', ports), ["127.0.0.1:8080:8080"])

    def test_readme_documents_host_ownership_and_direct_docker_user(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for requirement in ("mkdir -p data", "export HOTNEWS_UID", "export HOTNEWS_GID",
                            "docker run", "--user", "所有者", "root"):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, readme)


if __name__ == "__main__":
    unittest.main()
