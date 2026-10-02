"""Feishu callback SHA256 signatures and IV-prefixed AES-256-CBC payloads."""

import base64
import binascii
import hashlib
import hmac
import json
from typing import Mapping

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..domain import ValidationError


def decrypt_payload(encrypted: str, encrypt_key: str) -> dict:
    """Decode Feishu's base64(IV + ciphertext), with strict 16-byte PKCS#7."""
    if not isinstance(encrypted, str) or not isinstance(encrypt_key, str) or not encrypt_key:
        raise ValidationError("invalid encrypted Feishu callback")
    try:
        ciphertext = base64.b64decode(encrypted, validate=True)
        if len(ciphertext) < 32 or len(ciphertext) % 16:
            raise ValueError("invalid ciphertext length")
        key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
        decryptor = Cipher(algorithms.AES(key), modes.CBC(ciphertext[:16]),
                           backend=default_backend()).decryptor()
        padded = decryptor.update(ciphertext[16:]) + decryptor.finalize()
        size = padded[-1]
        if not 1 <= size <= 16 or padded[-size:] != bytes([size]) * size:
            raise ValueError("invalid padding")
        payload = json.loads(padded[:-size].decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("callback must be an object")
        return payload
    except (ValueError, UnicodeError, binascii.Error, RecursionError):
        raise ValidationError("invalid encrypted Feishu callback") from None


def verify_signature(raw_body: bytes, headers: Mapping[str, str], encrypt_key: str) -> None:
    """Authenticate the exact request bytes; HTTP header names are case insensitive."""
    lowered = {key.lower(): value for key, value in headers.items()}
    timestamp = lowered.get("x-lark-request-timestamp")
    nonce = lowered.get("x-lark-request-nonce")
    supplied = lowered.get("x-lark-signature")
    if not all(isinstance(value, str) and value for value in (timestamp, nonce, supplied, encrypt_key)):
        raise PermissionError("invalid Feishu callback signature")
    try:
        digest = hashlib.sha256((timestamp + nonce + encrypt_key).encode("utf-8") + raw_body).hexdigest()
        matches = hmac.compare_digest(digest.encode("ascii"), supplied.encode("utf-8"))
    except UnicodeError:
        raise PermissionError("invalid Feishu callback signature") from None
    if not matches:
        raise PermissionError("invalid Feishu callback signature")
