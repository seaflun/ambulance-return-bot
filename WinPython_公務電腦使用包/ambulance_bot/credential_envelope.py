from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from typing import Any


MAX_CREDENTIAL_PAYLOAD_BYTES = 1024 * 1024
MIN_SECRET_BYTES = 32
_VERSION = 1
_ALGORITHM = "HMAC-SHA256-STREAM+HMAC-SHA256"
_DOMAIN = b"ambulance-return-credential-envelope-v1"
_NONCE_BYTES = 32
_TAG_BYTES = hashlib.sha256().digest_size


class CredentialEnvelopeError(ValueError):
    pass


def seal_credential_payload(payload: dict[str, Any], secret: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise CredentialEnvelopeError("帳密同步資料必須是 JSON 物件。")
    secret_bytes = _validated_secret(secret)
    try:
        plaintext = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CredentialEnvelopeError("帳密同步資料無法序列化。") from exc
    if len(plaintext) > MAX_CREDENTIAL_PAYLOAD_BYTES:
        raise CredentialEnvelopeError("帳密同步資料超過允許大小。")

    nonce = secrets.token_bytes(_NONCE_BYTES)
    encryption_key, mac_key = _derive_keys(secret_bytes)
    ciphertext = _xor_with_keystream(plaintext, encryption_key, nonce)
    tag = hmac.digest(mac_key, _authenticated_bytes(nonce, ciphertext), "sha256")
    return {
        "version": _VERSION,
        "algorithm": _ALGORITHM,
        "nonce": _encode(nonce),
        "ciphertext": _encode(ciphertext),
        "tag": _encode(tag),
    }


def open_credential_payload(envelope: dict[str, object], secret: str) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise CredentialEnvelopeError("封存資料格式錯誤。")
    if envelope.get("version") != _VERSION or envelope.get("algorithm") != _ALGORITHM:
        raise CredentialEnvelopeError("不支援的帳密封存版本。")
    secret_bytes = _validated_secret(secret)
    nonce = _decode_field(envelope, "nonce", _NONCE_BYTES)
    tag = _decode_field(envelope, "tag", _TAG_BYTES)
    ciphertext_text = envelope.get("ciphertext")
    if not isinstance(ciphertext_text, str):
        raise CredentialEnvelopeError("封存資料缺少密文。")
    max_encoded_length = ((MAX_CREDENTIAL_PAYLOAD_BYTES + 2) // 3) * 4
    if len(ciphertext_text) > max_encoded_length:
        raise CredentialEnvelopeError("封存密文超過允許大小。")
    ciphertext = _decode(ciphertext_text)
    if len(ciphertext) > MAX_CREDENTIAL_PAYLOAD_BYTES:
        raise CredentialEnvelopeError("封存密文超過允許大小。")

    encryption_key, mac_key = _derive_keys(secret_bytes)
    expected_tag = hmac.digest(mac_key, _authenticated_bytes(nonce, ciphertext), "sha256")
    if not hmac.compare_digest(tag, expected_tag):
        raise CredentialEnvelopeError("帳密封存驗證失敗。")
    plaintext = _xor_with_keystream(ciphertext, encryption_key, nonce)
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CredentialEnvelopeError("帳密封存內容無法解析。") from exc
    if not isinstance(payload, dict):
        raise CredentialEnvelopeError("帳密封存內容必須是 JSON 物件。")
    return payload


def _validated_secret(secret: str) -> bytes:
    if not isinstance(secret, str):
        raise CredentialEnvelopeError("帳密封存密鑰格式錯誤。")
    encoded = secret.encode("utf-8")
    if len(encoded) < MIN_SECRET_BYTES:
        raise CredentialEnvelopeError("帳密封存密鑰至少需要 32 bytes。")
    return encoded


def _derive_keys(secret: bytes) -> tuple[bytes, bytes]:
    encryption_key = hmac.digest(secret, _DOMAIN + b"\x00encryption", "sha256")
    mac_key = hmac.digest(secret, _DOMAIN + b"\x00authentication", "sha256")
    return encryption_key, mac_key


def _xor_with_keystream(data: bytes, key: bytes, nonce: bytes) -> bytes:
    output = bytearray(len(data))
    offset = 0
    counter = 0
    while offset < len(data):
        block = hmac.digest(
            key,
            _DOMAIN + b"\x00stream" + nonce + counter.to_bytes(8, "big"),
            "sha256",
        )
        chunk = data[offset : offset + len(block)]
        output[offset : offset + len(chunk)] = bytes(
            left ^ right for left, right in zip(chunk, block)
        )
        offset += len(chunk)
        counter += 1
    return bytes(output)


def _authenticated_bytes(nonce: bytes, ciphertext: bytes) -> bytes:
    algorithm = _ALGORITHM.encode("ascii")
    return b"".join(
        (
            _DOMAIN,
            _VERSION.to_bytes(1, "big"),
            len(algorithm).to_bytes(2, "big"),
            algorithm,
            nonce,
            len(ciphertext).to_bytes(8, "big"),
            ciphertext,
        )
    )


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise CredentialEnvelopeError("封存資料不是有效的 Base64。") from exc


def _decode_field(envelope: dict[str, object], name: str, expected_length: int) -> bytes:
    value = envelope.get(name)
    if not isinstance(value, str):
        raise CredentialEnvelopeError(f"封存資料缺少 {name}。")
    expected_encoded_length = ((expected_length + 2) // 3) * 4
    if len(value) != expected_encoded_length:
        raise CredentialEnvelopeError(f"封存資料的 {name} 長度錯誤。")
    decoded = _decode(value)
    if len(decoded) != expected_length:
        raise CredentialEnvelopeError(f"封存資料的 {name} 長度錯誤。")
    return decoded
