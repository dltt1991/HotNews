import json
import unittest

from hotnews.feishu.connection import SDKCompatibilityError
from hotnews.feishu.ws_compat import (proxy_socket_settings, sdk_event_payload,
                                      verify_sdk_version)


class FeishuSDKCompatibilityTests(unittest.TestCase):
    def test_only_the_pinned_sdk_version_is_accepted(self):
        verify_sdk_version("1.7.3")
        for value in ("1.7.2", "1.7.4", "2.0.0"):
            with self.subTest(value=value), self.assertRaises(SDKCompatibilityError):
                verify_sdk_version(value)

    def test_proxy_socket_settings_preserve_remote_dns_and_decode_credentials(self):
        socks = proxy_socket_settings("socks5h://us%65r:p%40ss@proxy.example:1080")
        self.assertEqual(socks, {
            "scheme": "socks5", "host": "proxy.example", "port": 1080,
            "remote_dns": True, "username": "user", "password": "p@ss",
        })
        http = proxy_socket_settings("http://127.0.0.1:7890")
        self.assertEqual(http, {
            "scheme": "http", "host": "127.0.0.1", "port": 7890,
            "remote_dns": True, "username": None, "password": None,
        })

    def test_sdk_event_is_marshaled_to_an_independent_mapping(self):
        original = object()
        payload = sdk_event_payload(original, marshal=lambda value: json.dumps({
            "schema": "2.0", "header": {"event_id": "evt"}, "event": {"message": {}}
        }))
        self.assertEqual(payload["header"]["event_id"], "evt")
        self.assertIsNot(payload, original)

    def test_nonobject_sdk_payload_is_rejected(self):
        for value in ("[]", "null", '"text"'):
            with self.subTest(value=value), self.assertRaises(SDKCompatibilityError):
                sdk_event_payload(object(), marshal=lambda _: value)


if __name__ == "__main__":
    unittest.main()
