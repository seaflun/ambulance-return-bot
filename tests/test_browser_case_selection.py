import json
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ambulance_bot.browser_case_selection import BrowserCaseSelectionStore


class BrowserCaseSelectionStoreTests(unittest.TestCase):
    def test_interleaved_browsers_keep_their_own_case_and_consume_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selections.json"
            browser_a = BrowserCaseSelectionStore(path)
            browser_b = BrowserCaseSelectionStore(path)
            case_a = {"case_id": "A", "address": "桃園市", "personnel": ["甲", "乙"]}
            case_b = {"case_id": "B", "category": "災害搶救"}

            token_a = browser_a.put(case_a)
            token_b = browser_b.put(case_b)

            self.assertRegex(token_a, re.compile(r"^[A-Za-z0-9_-]{43}$"))
            self.assertNotEqual(token_a, token_b)
            self.assertEqual(browser_b.pop(token_b), case_b)
            self.assertEqual(browser_a.pop(token_a), case_a)
            self.assertEqual(browser_a.pop(token_a), {})
            self.assertEqual(browser_b.pop(token_b), {})
            self.assertTrue(path.is_file())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {})
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_selection_survives_store_recreation_and_caller_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selections.json"
            selected = {"case_id": "A", "personnel": ["甲"]}
            token = BrowserCaseSelectionStore(path).put(selected)
            selected["personnel"].append("乙")

            self.assertEqual(BrowserCaseSelectionStore(path).pop(token), {"case_id": "A", "personnel": ["甲"]})

    def test_expiry_boundary_and_pruning_do_not_affect_a_newer_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selections.json"
            store = BrowserCaseSelectionStore(path, ttl_seconds=300)
            with patch("ambulance_bot.browser_case_selection.time.time", return_value=1000):
                expired = store.put({"case_id": "old"})
            with patch("ambulance_bot.browser_case_selection.time.time", return_value=1100):
                fresh = store.put({"case_id": "fresh"})
            with patch("ambulance_bot.browser_case_selection.time.time", return_value=1300):
                self.assertEqual(store.pop(expired), {})
                self.assertEqual(store.pop(fresh), {"case_id": "fresh"})
            self.assertTrue(path.is_file())
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {})

    def test_invalid_and_unknown_tokens_cannot_consume_another_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BrowserCaseSelectionStore(Path(tmp) / "selections.json")
            token = store.put({"case_id": "retained"})

            for invalid in (None, 1, True, {}, "", "1", "../selections.json", "A" * 42, "!" * 43, "B" * 43):
                with self.subTest(token=invalid):
                    self.assertEqual(store.pop(invalid), {})
            self.assertEqual(store.pop(token), {"case_id": "retained"})

    def test_corrupt_json_and_invalid_mapping_fail_closed(self):
        token = "A" * 43
        bad_payloads = (
            "{truncated",
            "[]",
            "null",
            json.dumps({"selected_case": {"case_id": "other"}}),
            json.dumps({token: {"expires_at": "forever", "payload": {"case_id": "other"}}}),
            json.dumps({token: {"expires_at": float("inf"), "payload": {"case_id": "other"}}}),
            json.dumps({token: {"expires_at": 9999999999, "payload": {"case_id": float("nan")}}}),
            json.dumps({token: {"expires_at": 9999999999, "payload": []}}),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selections.json"
            store = BrowserCaseSelectionStore(path)
            for malformed in bad_payloads:
                with self.subTest(payload=malformed):
                    path.write_text(malformed, encoding="utf-8")
                    self.assertEqual(store.pop(token), {})
            fresh_token = store.put({"case_id": "fresh"})
            self.assertEqual(store.pop(fresh_token), {"case_id": "fresh"})

    def test_concurrent_stores_do_not_lose_selections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selections.json"
            equivalent = path.parent / "unused" / ".." / path.name
            stores = [BrowserCaseSelectionStore(path), BrowserCaseSelectionStore(equivalent)]

            def put_case(index):
                return stores[index % 2].put({"case_id": str(index)})

            with ThreadPoolExecutor(max_workers=12) as executor:
                tokens = list(executor.map(put_case, range(80)))
                results = list(executor.map(lambda token: BrowserCaseSelectionStore(path).pop(token), tokens))

            self.assertEqual(len(set(tokens)), 80)
            self.assertEqual(results, [{"case_id": str(index)} for index in range(80)])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {})

    def test_concurrent_replay_returns_payload_to_exactly_one_caller(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selections.json"
            token = BrowserCaseSelectionStore(path).put({"case_id": "once"})
            with ThreadPoolExecutor(max_workers=12) as executor:
                results = list(executor.map(lambda _: BrowserCaseSelectionStore(path).pop(token), range(40)))

            self.assertEqual(results.count({"case_id": "once"}), 1)
            self.assertEqual(results.count({}), 39)

    def test_failed_atomic_replace_preserves_the_previous_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "selections.json"
            store = BrowserCaseSelectionStore(path)
            token = store.put({"case_id": "retained"})
            previous = path.read_bytes()

            with patch("ambulance_bot.browser_case_selection.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    store.put({"case_id": "not-published"})
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(store.pop(token), {"case_id": "retained"})


if __name__ == "__main__":
    unittest.main()
