"""Static deployment safety contracts; these do not pretend to build Docker."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DeploymentContractTests(unittest.TestCase):
    def test_container_has_no_callback_port_and_passes_proxy_configuration(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        users = re.findall(r"(?m)^USER (\d+):(\d+)\s*$", dockerfile)
        self.assertTrue(users, "container must default to a numeric unprivileged user")
        self.assertNotEqual(users[-1][0], "0")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('user: "${HOTNEWS_UID:-1000}:${HOTNEWS_GID:-1000}"', compose)
        self.assertEqual(re.findall(r"(?m)^EXPOSE (.+)$", dockerfile), [])
        self.assertNotIn("ports:", compose)
        self.assertIn("FEISHU_WS_PROXY", compose)
        self.assertIn("HTTPS_PROXY", compose)
        self.assertNotIn("FEISHU_VERIFICATION_TOKEN", compose)
        self.assertNotIn("FEISHU_ENCRYPT_KEY", compose)

    def test_readme_documents_host_ownership_and_direct_docker_user(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for requirement in ("mkdir -p data", "export HOTNEWS_UID", "export HOTNEWS_GID",
                            "docker run", "--user", "所有者", "root"):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, readme)

    def test_operator_readme_uses_long_connection_without_public_callback(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for requirement in ("check-feishu", "使用长连接接收事件", "im.message.receive_v1",
                            "FEISHU_WS_PROXY", "127.0.0.1:8081"):
            self.assertIn(requirement, readme)
        for obsolete in ("/callbacks/feishu", "Verification Token", "Encrypt Key",
                         "Cloudflare Tunnel", "-p 127.0.0.1:8080:8080"):
            self.assertNotIn(obsolete, readme)


if __name__ == "__main__":
    unittest.main()
