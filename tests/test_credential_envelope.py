from __future__ import annotations

import json
import unittest
from unittest import mock

from ambulance_bot.credential_envelope import (
    CredentialEnvelopeError,
    MAX_CREDENTIAL_PAYLOAD_BYTES,
    open_credential_payload,
    seal_credential_payload,
)


SECRET = "0123456789abcdef0123456789abcdef"


class CredentialEnvelopeTests(unittest.TestCase):
    def test_round_trip_preserves_unicode_credential_payload(self):
        payload = {
            "user_id": "新坡8號",
            "accounts": [
                {"actor_no": "8", "user_id": "user8", "password": "密碼-測試-🔐"},
                {"actor_no": "9", "user_id": "user9", "password": "second-secret"},
            ],
        }

        envelope = seal_credential_payload(payload, SECRET)

        self.assertEqual(open_credential_payload(envelope, SECRET), payload)
        encoded = json.dumps(envelope, ensure_ascii=False)
        self.assertNotIn("密碼-測試", encoded)
        self.assertNotIn("second-secret", encoded)
        self.assertEqual(envelope["version"], 1)

    def test_tampered_ciphertext_is_rejected_before_decryption(self):
        envelope = seal_credential_payload({"password": "secret"}, SECRET)
        ciphertext = str(envelope["ciphertext"])
        replacement = "A" if ciphertext[-1] != "A" else "B"
        envelope["ciphertext"] = ciphertext[:-1] + replacement

        with self.assertRaises(CredentialEnvelopeError):
            open_credential_payload(envelope, SECRET)

    def test_wrong_secret_is_rejected(self):
        envelope = seal_credential_payload({"password": "secret"}, SECRET)

        with self.assertRaises(CredentialEnvelopeError):
            open_credential_payload(envelope, "fedcba9876543210fedcba9876543210")

    def test_oversized_fixed_length_field_is_rejected_before_base64_decode(self):
        envelope = seal_credential_payload({"password": "secret"}, SECRET)
        envelope["nonce"] = "A" * 100_000

        with mock.patch(
            "ambulance_bot.credential_envelope.base64.b64decode",
            side_effect=AssertionError("oversized nonce must not be decoded"),
        ):
            with self.assertRaises(CredentialEnvelopeError):
                open_credential_payload(envelope, SECRET)

    def test_short_secret_is_rejected(self):
        with self.assertRaises(CredentialEnvelopeError):
            seal_credential_payload({"password": "secret"}, "too-short")

    def test_oversized_payload_is_rejected(self):
        payload = {"password": "x" * (MAX_CREDENTIAL_PAYLOAD_BYTES + 1)}

        with self.assertRaises(CredentialEnvelopeError):
            seal_credential_payload(payload, SECRET)


if __name__ == "__main__":
    unittest.main()
