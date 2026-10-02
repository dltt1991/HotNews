"""Feishu protocol fixtures follow the official v2 event and HTTP shapes."""

import base64
import hashlib
import io
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.message import Message
from http.client import IncompleteRead
from unittest.mock import patch
from urllib.response import addinfourl

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from hotnews.config import FeishuConfig
from hotnews.domain import HttpResponse, NewsResult, Schedule, Subscription, ValidationError
from hotnews.feishu.cards import render_command_result, render_digest
from hotnews.feishu.client import FeishuAPIError, FeishuClient
from hotnews.feishu.crypto import decrypt_payload, verify_signature
from hotnews.feishu.events import decode_callback, decode_request, normalize_event, url_verification_response
from hotnews.http import UrllibTransport


NOW = datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc)


def config(encrypted=False, bot_open_id="ou_bot"):
    return FeishuConfig("cli_test", "test-app-secret", "test-verification-token",
                        "test-encrypt-key" if encrypted else None, bot_open_id)


def message_event():
    return {
        "schema": "2.0",
        "header": {
            "event_id": "evt_1", "event_type": "im.message.receive_v1",
            "create_time": "1790906400000", "token": "test-verification-token",
            "app_id": "cli_test", "tenant_key": "tenant_test",
        },
        "event": {
            "sender": {"sender_id": {"open_id": "ou_member", "user_id": "member",
                                      "union_id": "on_member"},
                       "sender_type": "user", "tenant_key": "tenant_test"},
            "message": {
                "message_id": "om_1", "root_id": "", "parent_id": "",
                "create_time": "1790906400000", "chat_id": "oc_group",
                "chat_type": "group", "message_type": "text",
                "content": '{"text":"@_user_1 订阅 AI，每天 9 点"}',
                "mentions": [{"key": "@_user_1", "id": {"open_id": "ou_bot",
                              "union_id": "", "user_id": ""},
                              "name": "热点机器人", "tenant_key": "tenant_test"}],
            },
        },
    }


def encrypt_bytes(plaintext):
    """Independent encoder: Feishu IV prefix, SHA256 key, 16-byte PKCS#7."""
    iv = bytes(range(16))
    pad_size = 16 - len(plaintext) % 16
    padded = plaintext + bytes([pad_size]) * pad_size
    key = hashlib.sha256(b"test-encrypt-key").digest()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend()).encryptor()
    return base64.b64encode(iv + cipher.update(padded) + cipher.finalize()).decode("ascii")


def encrypted_payload(payload):
    return {"encrypt": encrypt_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))}


def signed_headers(body):
    return {"X-Lark-Request-Timestamp": "1790906400", "X-Lark-Request-Nonce": "nonce-test",
            "X-Lark-Signature": hashlib.sha256(
                b"1790906400nonce-testtest-encrypt-key" + body).hexdigest()}


def response(payload, status=200, headers=None):
    return HttpResponse(status, headers or {}, json.dumps(payload).encode("utf-8"))


def token(value="tenant-token-1", expire=7200):
    return response({"code": 0, "msg": "ok", "tenant_access_token": value, "expire": expire})


def sent(message_id="om_sent"):
    return response({"code": 0, "msg": "success", "data": {
        "message_id": message_id, "root_id": "", "parent_id": "",
        "msg_type": "text", "create_time": "1790906400000", "update_time": "1790906400000",
        "deleted": False, "updated": False, "chat_id": "oc_group",
        "sender": {"id": "cli_test", "id_type": "app_id", "sender_type": "app", "tenant_key": "tenant_test"},
        "body": {"content": '{"text":"hello"}'},
    }})


class ScriptedTransport:
    """Only replaces the external network; records the real client's wire requests."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, headers=None, body=None, timeout=15):
        self.requests.append((method, url, dict(headers or {}), body, timeout))
        if not self.responses:
            raise AssertionError("unexpected external request")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FeishuEventTests(unittest.TestCase):
    def test_group_text_mention_becomes_normalized_event(self):
        before = datetime.now(timezone.utc)
        event = normalize_event(message_event(), config())
        self.assertEqual((event.event_id, event.message_id, event.chat_id, event.sender_id, event.text),
                         ("evt_1", "om_1", "oc_group", "ou_member", "订阅 AI，每天 9 点"))
        self.assertLessEqual(before, event.received_at)
        self.assertLessEqual(event.received_at, datetime.now(timezone.utc))

    def test_private_nontext_and_bot_authored_messages_are_ignored(self):
        for field, value in (("chat_type", "p2p"), ("message_type", "image")):
            payload = message_event()
            payload["event"]["message"][field] = value
            with self.subTest(field=field):
                self.assertIsNone(normalize_event(payload, config()))
        for sender_type in ("app", "bot", ""):
            payload = message_event()
            payload["event"]["sender"]["sender_type"] = sender_type
            with self.subTest(sender_type=sender_type):
                self.assertIsNone(normalize_event(payload, config()))

    def test_self_open_id_is_ignored_even_with_user_sender_type(self):
        payload = message_event()
        payload["event"]["sender"]["sender_id"]["open_id"] = "ou_bot"
        self.assertIsNone(normalize_event(payload, config()))

    def test_literal_bot_name_does_not_replace_structural_mention(self):
        payload = message_event()
        payload["event"]["message"].update(content='{"text":"@热点机器人 帮助"}', mentions=[])
        self.assertIsNone(normalize_event(payload, config()))

    def test_missing_or_wrong_bot_identity_fails_closed(self):
        for bot_open_id in (None, "", "ou_other"):
            with self.subTest(bot_open_id=bot_open_id):
                self.assertIsNone(normalize_event(message_event(), config(bot_open_id=bot_open_id)))
        payload = message_event()
        payload["event"]["message"]["mentions"][0]["id"]["open_id"] = "ou_other"
        self.assertIsNone(normalize_event(payload, config()))

    def test_removes_only_this_bots_mentions_and_keeps_other_words(self):
        payload = message_event()
        message = payload["event"]["message"]
        message["content"] = json.dumps({"text": "@_user_1 订阅  AI\n问 @_user_10，@热点机器人 @_user_1"})
        message["mentions"].append({"key": "@_user_10", "id": {"open_id": "ou_other"},
                                    "name": "Other", "tenant_key": "tenant_test"})
        self.assertEqual(normalize_event(payload, config()).text,
                         "订阅  AI\n问 @_user_10，@热点机器人")

    def test_empty_body_after_removing_mentions_is_ignored(self):
        payload = message_event()
        payload["event"]["message"]["content"] = '{"text":" @_user_1 "}'
        self.assertIsNone(normalize_event(payload, config()))

    def test_invalid_message_json_and_nonstring_text_are_rejected(self):
        for content in ('{"text":', "[]", '{"text":null}', {"text": "help"}):
            payload = message_event()
            payload["event"]["message"]["content"] = content
            with self.subTest(content=content), self.assertRaises(ValidationError):
                normalize_event(payload, config())

    def test_bad_token_is_rejected_even_for_ignored_events(self):
        for supplied in (None, "", "wrong", "非ASCII", "\ud800"):
            payload = message_event()
            payload["header"].update(token=supplied, event_type="unsupported")
            with self.subTest(token=supplied), self.assertRaises(PermissionError):
                normalize_event(payload, config())

    def test_wrong_app_id_is_rejected(self):
        payload = message_event()
        payload["header"]["app_id"] = "cli_someone_else"
        with self.assertRaises(PermissionError):
            normalize_event(payload, config())

    def test_missing_required_event_identifiers_are_rejected(self):
        for location, key in (("header", "event_id"), ("message", "message_id"), ("message", "chat_id")):
            payload = message_event()
            part = payload["header"] if location == "header" else payload["event"]["message"]
            part.pop(key)
            with self.subTest(key=key), self.assertRaises(ValidationError):
                normalize_event(payload, config())

    def test_unrelated_event_is_ignored(self):
        payload = message_event()
        payload["header"]["event_type"] = "im.chat.updated_v1"
        self.assertIsNone(normalize_event(payload, config()))

    def test_plain_url_verification_checks_token_and_returns_challenge(self):
        payload = {"type": "url_verification", "token": "test-verification-token", "challenge": 'challenge"\n值'}
        self.assertEqual(url_verification_response(payload, config()), {"challenge": 'challenge"\n值'})
        self.assertIsNone(normalize_event(payload, config()))
        payload["token"] = "wrong"
        with self.assertRaises(PermissionError):
            url_verification_response(payload, config())

    def test_encrypted_url_verification_and_message_decode(self):
        challenge = {"type": "url_verification", "token": "test-verification-token", "challenge": "challenge-test"}
        self.assertEqual(url_verification_response(encrypted_payload(challenge), config(True)),
                         {"challenge": "challenge-test"})
        self.assertEqual(normalize_event(encrypted_payload(message_event()), config(True)).text,
                         "订阅 AI，每天 9 点")

    def test_encrypted_body_requires_encrypt_key_and_inner_token(self):
        with self.assertRaises(PermissionError):
            decode_callback(encrypted_payload(message_event()), config())
        payload = message_event()
        payload["header"]["token"] = "wrong"
        with self.assertRaises(PermissionError):
            decode_callback(encrypted_payload(payload), config(True))

    def test_callback_rejects_nonobject_and_malformed_structures(self):
        for payload in (None, [], {"header": []}, {"encrypt": 1}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                decode_callback(payload, config(True))


class FeishuCryptoTests(unittest.TestCase):
    def test_decrypt_uses_ciphertext_iv_and_sha256_key(self):
        self.assertEqual(decrypt_payload(encrypt_bytes(b'{"text":"hello"}'), "test-encrypt-key"),
                         {"text": "hello"})

    def test_decrypt_rejects_bad_base64_length_padding_and_nonobject_json(self):
        bad_values = ("not base64!", base64.b64encode(b"short").decode(),
                      base64.b64encode(bytes(32)).decode(), encrypt_bytes(b"[]"), encrypt_bytes(b"{bad"))
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                decrypt_payload(value, "test-encrypt-key")

    def test_signature_covers_exact_raw_bytes_and_is_case_insensitive_for_header_names(self):
        body = b'{ "encrypt": "test" }'
        headers = signed_headers(body)
        verify_signature(body, {key.lower(): value for key, value in headers.items()}, "test-encrypt-key")
        with self.assertRaises(PermissionError):
            verify_signature(b'{"encrypt":"test"}', headers, "test-encrypt-key")

    def test_missing_wrong_or_unicode_signature_is_rejected(self):
        body = b"{}"
        for signature in (None, "wrong", "非ASCII", "\ud800"):
            headers = signed_headers(body)
            headers["X-Lark-Signature"] = signature
            with self.subTest(signature=signature), self.assertRaises(PermissionError):
                verify_signature(body, headers, "test-encrypt-key")

    def test_decode_request_enforces_signature_for_encrypted_events(self):
        body = json.dumps(encrypted_payload(message_event())).encode()
        decoded = decode_request(body, signed_headers(body), config(True))
        self.assertEqual(decoded["header"]["event_id"], "evt_1")
        with self.assertRaises(PermissionError):
            decode_request(body, {}, config(True))

    def test_url_verification_does_not_need_signature_but_still_checks_token(self):
        payload = {"type": "url_verification", "token": "test-verification-token", "challenge": "test"}
        body = json.dumps(encrypted_payload(payload)).encode()
        self.assertEqual(decode_request(body, {}, config(True))["challenge"], "test")
        payload["token"] = "wrong"
        with self.assertRaises(PermissionError):
            decode_request(json.dumps(encrypted_payload(payload)).encode(), {}, config(True))

    def test_raw_request_rejects_malformed_json_or_utf8(self):
        for body in (b"{", b"[]", b"\xff"):
            with self.subTest(body=body), self.assertRaises(ValidationError):
                decode_request(body, {}, config())


class FeishuClientTests(unittest.TestCase):
    def test_token_cache_and_text_wire_payload(self):
        transport = ScriptedTransport([token(), sent(), sent("om_sent2")])
        client = FeishuClient(config(), transport)
        text = '中文 "quote"\\path\n<at id=all>'
        self.assertEqual(client.send_text("oc_group", text, "outbox-text")["data"]["message_id"], "om_sent")
        client.send_text("oc_group", "second", "outbox-second")
        self.assertEqual(len(transport.requests), 3)
        auth = transport.requests[0]
        self.assertEqual(json.loads(auth[3]), {"app_id": "cli_test", "app_secret": "test-app-secret"})
        self.assertTrue(auth[1].endswith("/auth/v3/tenant_access_token/internal"))
        send = transport.requests[1]
        self.assertEqual(send[0], "POST")
        self.assertTrue(send[1].endswith("/im/v1/messages?receive_id_type=chat_id"))
        self.assertEqual(send[2]["Authorization"], "Bearer tenant-token-1")
        payload = json.loads(send[3])
        self.assertEqual((payload["receive_id"], payload["msg_type"], payload["uuid"]),
                         ("oc_group", "text", "outbox-text"))
        self.assertEqual(json.loads(payload["content"]), {"text": text})

    def test_expired_token_is_renewed(self):
        clock = [0.0]
        transport = ScriptedTransport([token(expire=120), sent(), sent(), token("tenant-token-2"), sent()])
        client = FeishuClient(config(), transport, clock=lambda: clock[0])
        client.send_text("oc_group", "first", "one")
        clock[0] = 59
        client.send_text("oc_group", "cached", "two")
        clock[0] = 120
        client.send_text("oc_group", "renewed", "three")
        self.assertEqual(transport.requests[-1][2]["Authorization"], "Bearer tenant-token-2")

    def test_authorization_failure_refreshes_once_and_reuses_identical_uuid(self):
        transport = ScriptedTransport([token(), response({"code": 99991663, "msg": "Invalid token"}, 400),
                                       token("tenant-token-2"), sent()])
        client = FeishuClient(config(), transport)
        client.send_text("oc_group", "hello", "stable-outbox-key")
        self.assertEqual(transport.requests[1][3], transport.requests[3][3])
        self.assertEqual(json.loads(transport.requests[3][3])["uuid"], "stable-outbox-key")
        self.assertEqual(transport.requests[3][2]["Authorization"], "Bearer tenant-token-2")

    def test_repeated_authorization_failure_stops_after_one_refresh(self):
        bad = response({"code": 99991665, "msg": "tenant token invalid"}, 401)
        transport = ScriptedTransport([token(), bad, token("tenant-token-2"), bad])
        with self.assertRaises(FeishuAPIError) as caught:
            FeishuClient(config(), transport).send_text("oc_group", "hello", "stable-key")
        self.assertEqual(len(transport.requests), 4)
        self.assertEqual(caught.exception.code, 99991665)
        self.assertTrue(caught.exception.retryable)

    def test_card_wire_content_and_uuid_are_stable_across_external_retry(self):
        transport = ScriptedTransport([token(), OSError("network unavailable"), sent()])
        client = FeishuClient(config(), transport)
        card = render_command_result('已创建 "AI"\n第二行')
        with self.assertRaises(FeishuAPIError) as caught:
            client.send_card("oc_group", card, "outbox-card")
        self.assertTrue(caught.exception.retryable)
        client.send_card("oc_group", card, "outbox-card")
        self.assertEqual(transport.requests[1][3], transport.requests[2][3])
        payload = json.loads(transport.requests[2][3])
        self.assertEqual(payload["msg_type"], "interactive")
        self.assertEqual(json.loads(payload["content"]), card)

    def test_rate_limit_exposes_retry_after_without_retrying_or_refreshing(self):
        transport = ScriptedTransport([token(), response({"code": 99991400, "msg": "rate limited"},
                                                       429, {"Retry-After": "7"})])
        with self.assertRaises(FeishuAPIError) as caught:
            FeishuClient(config(), transport).send_text("oc_group", "hello", "key")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual((caught.exception.status, caught.exception.retry_after), (429, 7.0))
        self.assertEqual(len(transport.requests), 2)

    def test_feishu_rate_limit_reset_seconds_take_precedence_for_http_and_api_limits(self):
        cases = (
            (429, {"X-Ogw-RateLimit-Reset": "9", "Retry-After": "7"}, 9.0),
            (400, {"retry-after": "7", "x-ogw-ratelimit-reset": "2.5"}, 2.5),
            (400, {"x-ogw-ratelimit-reset": "12"}, 12.0),
            (200, {"RETRY-AFTER": "7", "X-OGW-RATELIMIT-RESET": "0"}, 0.0),
        )
        for status, headers, expected in cases:
            transport = ScriptedTransport([token(), response(
                {"code": 99991400, "msg": "request trigger frequency limit"}, status, headers)])
            with self.subTest(status=status):
                with self.assertRaises(FeishuAPIError) as caught:
                    FeishuClient(config(), transport).send_text("oc_group", "hello", "key")
                self.assertEqual((caught.exception.status, caught.exception.code), (status, 99991400))
                self.assertEqual(caught.exception.retry_after, expected)
                self.assertTrue(caught.exception.retryable)
                self.assertEqual(len(transport.requests), 2)

    def test_invalid_feishu_reset_falls_back_to_valid_retry_after(self):
        for reset in (None, "", "invalid", "-1", "nan", "inf", "-inf"):
            transport = ScriptedTransport([token(), response(
                {"code": 99991400, "msg": "request trigger frequency limit"}, 400,
                {"X-Ogw-RateLimit-Reset": reset, "rEtRy-AfTeR": "7.5"})])
            with self.subTest(reset=reset), self.assertRaises(FeishuAPIError) as caught:
                FeishuClient(config(), transport).send_text("oc_group", "hello", "key")
            self.assertEqual(caught.exception.retry_after, 7.5)

    def test_invalid_or_absent_rate_limit_delays_leave_retry_after_unset(self):
        cases = ({}, {"x-ogw-ratelimit-reset": "nan"},
                 {"x-ogw-ratelimit-reset": "-1", "Retry-After": "-2"},
                 {"x-ogw-ratelimit-reset": "inf", "Retry-After": "nan"},
                 {"x-ogw-ratelimit-reset": "invalid", "Retry-After": "inf"})
        for headers in cases:
            transport = ScriptedTransport([token(), response(
                {"code": 99991400, "msg": "request trigger frequency limit"}, 429, headers)])
            with self.subTest(headers=headers), self.assertRaises(FeishuAPIError) as caught:
                FeishuClient(config(), transport).send_text("oc_group", "hello", "key")
            self.assertIsNone(caught.exception.retry_after)

    def test_nonfinite_retry_after_is_ignored(self):
        transport = ScriptedTransport([token(), response({"code": 99991400}, 429, {"Retry-After": "inf"})])
        with self.assertRaises(FeishuAPIError) as caught:
            FeishuClient(config(), transport).send_text("oc_group", "hello", "key")
        self.assertIsNone(caught.exception.retry_after)

    def test_permission_and_payload_failures_do_not_refresh(self):
        for code in (99991672, 230001):
            transport = ScriptedTransport([token(), response({"code": code, "msg": "denied"}, 403)])
            with self.subTest(code=code), self.assertRaises(FeishuAPIError) as caught:
                FeishuClient(config(), transport).send_text("oc_group", "hello", "key")
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(len(transport.requests), 2)

    def test_auth_response_requires_success_token_and_positive_expiry(self):
        bad_payloads = ({"code": 1, "msg": "bad"}, {"code": 0, "expire": 7200},
                        {"code": 0, "tenant_access_token": "value", "expire": 0},
                        {"code": 0, "tenant_access_token": "value", "expire": True})
        for payload in bad_payloads:
            with self.subTest(payload=payload), self.assertRaises(FeishuAPIError):
                FeishuClient(config(), ScriptedTransport([response(payload)])).send_text("oc", "text", "key")

    def test_http_success_with_missing_or_nonzero_code_is_not_delivery_success(self):
        for payload in ({}, {"code": 230001, "msg": "bad payload"}, {"code": False}, [],
                        {"code": []}, {"code": {}}):
            with self.subTest(payload=payload), self.assertRaises(FeishuAPIError):
                FeishuClient(config(), ScriptedTransport([token(), response(payload)])).send_text("oc", "text", "key")

    def test_non_json_errors_keep_http_retry_metadata_but_malformed_success_is_permanent(self):
        cases = ((429, {"Retry-After": "600"}, True, 600),
                 (503, {"x-ogw-ratelimit-reset": "900", "Retry-After": "600"}, True, 900),
                 (500, {"x-ogw-ratelimit-reset": "invalid", "Retry-After": "600"}, True, 600),
                 (200, {"Retry-After": "600"}, False, None),
                 (403, {}, False, None))
        for status, headers, retryable, delay in cases:
            for body in (b"<html>test-app-secret</html>", b"", b"{\"truncated\":", b"\xff"):
                with self.subTest(status=status, body=body), self.assertRaises(FeishuAPIError) as caught:
                    FeishuClient(config(), ScriptedTransport([token(), HttpResponse(status, headers, body)])).send_text("oc", "text", "key")
                self.assertEqual((caught.exception.status, caught.exception.retryable, caught.exception.retry_after),
                                 (status, retryable, delay))
                self.assertNotIn("test-app-secret", str(caught.exception))

    def test_errors_do_not_expose_remote_body_headers_or_transport_secrets(self):
        secret = "test-app-secret tenant-token-1 test-verification-token"
        cases = (response({"code": 230001, "msg": secret}), OSError(secret))
        for failure in cases:
            with self.subTest(failure=type(failure)), self.assertRaises(FeishuAPIError) as caught:
                FeishuClient(config(), ScriptedTransport([token(), failure])).send_text("oc", "text", "key")
            for value in ("test-app-secret", "tenant-token-1", "test-verification-token"):
                self.assertNotIn(value, str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_lookup_bot_open_id_uses_official_top_level_bot_and_caches(self):
        bot_info = {"code": 0, "msg": "ok", "bot": {"activate_status": 2, "app_name": "Hotnews",
                    "avatar_url": "https://example.com/avatar.png", "ip_white_list": [], "open_id": "ou_bot"}}
        transport = ScriptedTransport([token(), response(bot_info)])
        client = FeishuClient(config(bot_open_id=None), transport)
        self.assertEqual(client.get_bot_open_id(), "ou_bot")
        self.assertEqual(client.get_bot_open_id(), "ou_bot")
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(transport.requests[1][0], "GET")
        self.assertTrue(transport.requests[1][1].endswith("/bot/v3/info"))

    def test_supplied_bot_identity_needs_no_network_lookup(self):
        self.assertEqual(FeishuClient(config(), ScriptedTransport([])).get_bot_open_id(), "ou_bot")

    def test_missing_bot_identity_in_response_is_an_error(self):
        with self.assertRaises(FeishuAPIError):
            FeishuClient(config(bot_open_id=None), ScriptedTransport([token(), response({"code": 0, "bot": {}})])).get_bot_open_id()

    def test_empty_idempotency_key_is_rejected_before_network_io(self):
        with self.assertRaises(ValidationError):
            FeishuClient(config(), ScriptedTransport([])).send_text("oc", "text", "")


class FeishuCardTests(unittest.TestCase):
    def subscription(self):
        return Subscription("sub", "oc", 3, "member", "AI 热点", ("AI", "大模型"), ("AI news",),
                            Schedule("daily", daily_at="09:00"), "ready", NOW, 1, NOW, NOW)

    def news(self, age=timedelta(hours=1), number=1):
        return NewsResult('标题 "引号" <at id=all>', "https://example.com/news/%d" % number,
                          "来源", NOW - age, "摘要第一句。\n摘要第二句。", "event-%d" % number,
                          ("https://example.com/reference",))

    def test_header_contains_number_keywords_and_search_window(self):
        card = render_digest(self.subscription(), [self.news()], 7, now=NOW)
        title = card["header"]["title"]["content"]
        for expected in ("3", "AI", "大模型", "7"):
            self.assertIn(expected, title)

    def test_digest_contains_explicit_shanghai_publication_date_source_summary_and_links(self):
        card = render_digest(self.subscription(), [self.news()], 1, now=NOW)
        text = json.dumps(card, ensure_ascii=False)
        for expected in ("2026-10-02", "来源", "摘要第一句", "https://example.com/news/1", "https://example.com/reference"):
            self.assertIn(expected, text)
        self.assertNotIn("历史补充", text)

    def test_older_than_24_hours_has_historical_marker_and_boundary_does_not(self):
        old = render_digest(self.subscription(), [self.news(timedelta(days=2))], 7, now=NOW)
        self.assertIn("历史补充", json.dumps(old, ensure_ascii=False))
        self.assertIn("2026-09-30", json.dumps(old, ensure_ascii=False))
        boundary = render_digest(self.subscription(), [self.news(timedelta(hours=24))], 7, now=NOW)
        self.assertNotIn("历史补充", json.dumps(boundary, ensure_ascii=False))

    def test_digest_caps_items_at_ten(self):
        card = render_digest(self.subscription(), [self.news(number=n) for n in range(1, 13)], 30, now=NOW)
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("https://example.com/news/10\"", text)
        self.assertNotIn("https://example.com/news/11\"", text)
        self.assertNotIn("https://example.com/news/12\"", text)

    def test_untrusted_card_text_is_plain_text_and_json_roundtrips(self):
        card = render_digest(self.subscription(), [self.news()], 1, now=NOW)
        self.assertEqual(json.loads(json.dumps(card, ensure_ascii=False)), card)
        for element in card["elements"]:
            if element["tag"] == "div":
                self.assertEqual(element["text"]["tag"], "plain_text")
        command = render_command_result('已创建 "AI"\\path\n<at id=all>')
        self.assertEqual(command["elements"][0]["text"],
                         {"tag": "plain_text", "content": '已创建 "AI"\\path\n<at id=all>'})

    def test_empty_digest_and_unsafe_reference_links_are_rejected(self):
        with self.assertRaises(ValidationError):
            render_digest(self.subscription(), [], 1, now=NOW)
        item = replace(self.news(), references=("javascript:alert(1)",))
        with self.assertRaises(ValidationError):
            render_digest(self.subscription(), [item], 1, now=NOW)


class HttpTransportTests(unittest.TestCase):
    def network_response(self, url, status, body, headers):
        message = Message()
        for key, value in headers.items():
            message[key] = value
        result = addinfourl(io.BytesIO(body), message, url, status)
        if not hasattr(result, "status"):
            result.status = status  # Python 3.8 fixture also mirrors HTTPS.status.
        result.msg = "HTTP response"
        return result

    def test_urllib_transport_returns_error_status_headers_and_body(self):
        def fetch(request):
            return self.network_response(request.full_url, 429, request.data, {"Retry-After": "7"})

        with patch("urllib.request.HTTPHandler.http_open", side_effect=fetch):
            result = UrllibTransport().request("POST", "http://example.test/",
                                              headers={"Content-Type": "application/json"},
                                              body=b'{"hello":1}')
            self.assertEqual((result.status, result.headers["Retry-After"], result.body),
                             (429, "7", b'{"hello":1}'))

    def test_real_urllib_response_read_failure_becomes_sanitized_retryable_client_error(self):
        def fetch(request):
            if request.full_url.endswith("tenant_access_token/internal"):
                return self.network_response(request.full_url, 200, token().body, {})
            reply = self.network_response(request.full_url, 200, b"", {})
            def incomplete_body():
                raise IncompleteRead(b"test-app-secret sensitive body", 99)
            reply.read = incomplete_body
            return reply

        with patch("urllib.request.HTTPSHandler.https_open", side_effect=fetch):
            with self.assertRaises(FeishuAPIError) as caught:
                FeishuClient(config(), UrllibTransport()).send_text("oc", "hello", "key")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(str(caught.exception), "Feishu network request failed")
        self.assertTrue(caught.exception.__suppress_context__)

    def test_urllib_transport_does_not_forward_authorization_on_redirect(self):
        forwarded = []

        def fetch(request):
            if request.full_url == "http://example.test/":
                return self.network_response(request.full_url, 302, b"redirect", {"Location": "http://other.test/"})
            forwarded.append(request.get_header("Authorization"))
            return self.network_response(request.full_url, 200, b"done", {})

        with patch("urllib.request.HTTPHandler.http_open", side_effect=fetch):
            result = UrllibTransport().request("GET", "http://example.test/",
                                              headers={"Authorization": "Bearer private-token"})
        self.assertEqual(result.status, 302)
        self.assertEqual(forwarded, [])


if __name__ == "__main__":
    unittest.main()
