import tempfile
import unittest
from pathlib import Path

import worker as worker_module
from ambulance_bot.selenium_local import DutyCaseLookupResult


class WorkerTests(unittest.TestCase):
    def test_hash_cases_is_stable_for_same_content(self):
        left = [{"case_id": "1", "address": "A"}, {"case_id": "2", "address": "B"}]
        right = [{"address": "A", "case_id": "1"}, {"address": "B", "case_id": "2"}]

        self.assertEqual(worker_module.hash_cases(left), worker_module.hash_cases(right))

    def test_scheduled_lookup_skips_unchanged_cases(self):
        calls = {"posts": 0}
        original_fetch = worker_module.fetch_case_lookup_request
        original_query = worker_module.query_duty_emergency_cases
        original_post = worker_module.post_cases
        try:
            cases = [{"case_id": "1"}]
            case_hash = worker_module.hash_cases(cases)
            worker_module.fetch_case_lookup_request = lambda server_url: None
            worker_module.query_duty_emergency_cases = lambda artifacts_dir, lookup_range="today": DutyCaseLookupResult(
                True,
                "cases_loaded",
                "loaded",
                cases,
                artifacts_dir / "cases" / "latest.json",
            )
            worker_module.post_cases = lambda *args, **kwargs: calls.__setitem__("posts", calls["posts"] + 1)

            with tempfile.TemporaryDirectory() as tmp:
                last_lookup_at, last_case_hash = worker_module.maybe_run_case_lookup(
                    "http://nas",
                    Path(tmp),
                    0,
                    case_hash,
                    300,
                )
        finally:
            worker_module.fetch_case_lookup_request = original_fetch
            worker_module.query_duty_emergency_cases = original_query
            worker_module.post_cases = original_post

        self.assertGreater(last_lookup_at, 0)
        self.assertEqual(last_case_hash, case_hash)
        self.assertEqual(calls["posts"], 0)

    def test_manual_lookup_posts_even_when_cases_unchanged(self):
        calls = {"posts": 0}
        original_fetch = worker_module.fetch_case_lookup_request
        original_query = worker_module.query_duty_emergency_cases
        original_post = worker_module.post_cases
        try:
            cases = [{"case_id": "1"}]
            case_hash = worker_module.hash_cases(cases)
            worker_module.fetch_case_lookup_request = lambda server_url: {"lookup_range": "today"}
            worker_module.query_duty_emergency_cases = lambda artifacts_dir, lookup_range="today": DutyCaseLookupResult(
                True,
                "cases_loaded",
                "loaded",
                cases,
                artifacts_dir / "cases" / "latest.json",
            )
            worker_module.post_cases = lambda *args, **kwargs: calls.__setitem__("posts", calls["posts"] + 1)

            with tempfile.TemporaryDirectory() as tmp:
                _, last_case_hash = worker_module.maybe_run_case_lookup(
                    "http://nas",
                    Path(tmp),
                    0,
                    case_hash,
                    300,
                )
        finally:
            worker_module.fetch_case_lookup_request = original_fetch
            worker_module.query_duty_emergency_cases = original_query
            worker_module.post_cases = original_post

        self.assertEqual(last_case_hash, case_hash)
        self.assertEqual(calls["posts"], 1)


if __name__ == "__main__":
    unittest.main()
