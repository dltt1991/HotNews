import base64
import hashlib
import json
import os
import struct
from typing import Any, Dict

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _unpad(data: bytes) -> bytes:
    size = data[-1]
    if not 1 <= size <= 32 or data[-size:] != bytes([size]) * size:
        raise ValueError("invalid callback padding")
    return data[:-size]


def _pad(data: bytes) -> bytes:
    size = 32 - len(data) % 32
    return data + bytes([size]) * size


def aes_decrypt(ciphertext: str, key: bytes, with_envelope: bool = False) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16]), backend=default_backend()).decryptor()
    plain = _unpad(decryptor.update(base64.b64decode(ciphertext)) + decryptor.finalize())
    if not with_envelope:
        return plain
    length = struct.unpack("!I", plain[16:20])[0]
    return plain[20:20 + length]


def aes_encrypt(message: bytes, key: bytes, receive_id: str = "") -> str:
    plain = os.urandom(16) + struct.pack("!I", len(message)) + message + receive_id.encode("utf-8")
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16]), backend=default_backend()).encryptor()
    return base64.b64encode(encryptor.update(_pad(plain)) + encryptor.finalize()).decode("ascii")


def wecom_key(encoding_aes_key: str) -> bytes:
    return base64.b64decode(encoding_aes_key + "=")


def wecom_signature(token: str, timestamp: str, nonce: str, encrypted: str) -> str:
    return hashlib.sha1("".join(sorted([token, timestamp, nonce, encrypted])).encode("utf-8")).hexdigest()


def wecom_decrypt(encrypted: str, encoding_aes_key: str) -> Dict[str, Any]:
    return json.loads(aes_decrypt(encrypted, wecom_key(encoding_aes_key), True).decode("utf-8"))


def wecom_encrypt(payload: Dict[str, Any], token: str, encoding_aes_key: str,
                  receive_id: str = "") -> Dict[str, str]:
    import time
    timestamp, nonce = str(int(time.time())), base64.b16encode(os.urandom(8)).decode("ascii")
    encrypted = aes_encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                            wecom_key(encoding_aes_key), receive_id)
    return {"encrypt": encrypted, "msgsignature": wecom_signature(token, timestamp, nonce, encrypted),
            "timestamp": timestamp, "nonce": nonce}


def feishu_decrypt(encrypted: str, encrypt_key: str) -> Dict[str, Any]:
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    return json.loads(aes_decrypt(encrypted, key).decode("utf-8"))
