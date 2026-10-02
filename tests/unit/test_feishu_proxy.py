import unittest

from hotnews.domain import ValidationError
from hotnews.feishu.proxy import redact_sensitive, resolve_ws_proxy


class FeishuProxyTests(unittest.TestCase):
    def test_dedicated_proxy_wins_and_whitespace_falls_through(self):
        environ = {
            "FEISHU_WS_PROXY": " http://127.0.0.1:7890 ",
            "HTTPS_PROXY": "http://127.0.0.1:7891",
            "https_proxy": "http://127.0.0.1:7892",
            "ALL_PROXY": "socks5://127.0.0.1:7893",
            "all_proxy": "socks5h://127.0.0.1:7894",
        }
        self.assertEqual(resolve_ws_proxy(environ), "http://127.0.0.1:7890")
        environ["FEISHU_WS_PROXY"] = "  "
        self.assertEqual(resolve_ws_proxy(environ), "http://127.0.0.1:7891")
        environ["HTTPS_PROXY"] = ""
        self.assertEqual(resolve_ws_proxy(environ), "http://127.0.0.1:7892")

    def test_supported_proxy_schemes_validate(self):
        for scheme in ("http", "https", "socks5", "socks5h"):
            value = "%s://127.0.0.1:7890" % scheme
            with self.subTest(scheme=scheme):
                self.assertEqual(resolve_ws_proxy({"FEISHU_WS_PROXY": value}), value)

    def test_invalid_proxy_urls_fail_before_network_access(self):
        invalid = (
            "ftp://127.0.0.1:7890",
            "http:///missing-host",
            "http://127.0.0.1:99999",
            "http://127.0.0.1:7890/path",
            "http://127.0.0.1:7890?secret=yes",
            "http://127.0.0.1:7890#fragment",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                resolve_ws_proxy({"FEISHU_WS_PROXY": value})

    def test_redaction_removes_proxy_credentials_and_temporary_query(self):
        unsafe = ("proxy failed for http://us%65r:p%40ss@proxy.example:7890; "
                  "wss://open.feishu.cn/ws?ticket=temporary-secret&service_id=7")
        safe = redact_sensitive(RuntimeError(unsafe))
        for secret in ("us%65r", "p%40ss", "user", "p@ss", "temporary-secret", "ticket="):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, safe)
        self.assertIn("http://***:***@proxy.example:7890", safe)
        self.assertIn("wss://open.feishu.cn/ws?<redacted>", safe)

    def test_no_proxy_returns_none(self):
        self.assertIsNone(resolve_ws_proxy({}))


if __name__ == "__main__":
    unittest.main()
