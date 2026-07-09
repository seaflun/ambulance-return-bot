import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import consumables_login as consumables_login_module
from ambulance_bot.duty_credentials import save_duty_automation_credentials
from ambulance_bot.duty_credentials import update_saved_credential_id_number
from ambulance_bot.models import AmbulanceReturnRequest
from consumables_login import (
    _assert_consumable_rows_match,
    _case_id_sid_fragments,
    _consumable_sid_score,
    _emm_temsis_id_from_href,
    _find_consumable_detail_href,
    _load_acs_credentials,
    _wait_for_consumable_detail_page,
    save_consumables_record_enabled,
)


class ConsumablesLoginTests(unittest.TestCase):
    def test_assert_consumable_rows_match_allows_expected_rows(self):
        class FakeDriver:
            def execute_script(self, script):
                return [
                    {"itemId": "821", "quantity": "2"},
                    {"itemId": "816", "quantity": "02"},
                ]

        _assert_consumable_rows_match(
            FakeDriver(),
            [{"itemId": "816", "quantity": "2"}, {"itemId": "821", "quantity": "2"}],
            "耗材儲存前",
        )

    def test_assert_consumable_rows_match_rejects_extra_duplicate_row(self):
        class FakeDriver:
            def execute_script(self, script):
                return [
                    {"itemId": "816", "quantity": "2"},
                    {"itemId": "816", "quantity": "2"},
                    {"itemId": "821", "quantity": "2"},
                ]

        with self.assertRaisesRegex(RuntimeError, "停止儲存"):
            _assert_consumable_rows_match(
                FakeDriver(),
                [{"itemId": "816", "quantity": "2"}, {"itemId": "821", "quantity": "2"}],
                "耗材儲存前",
            )

    def test_save_consumables_record_flag_defaults_on(self):
        previous = os.environ.get("SAVE_CONSUMABLES_RECORD")
        try:
            os.environ.pop("SAVE_CONSUMABLES_RECORD", None)
            self.assertTrue(save_consumables_record_enabled())
            os.environ["SAVE_CONSUMABLES_RECORD"] = "0"
            self.assertFalse(save_consumables_record_enabled())
            os.environ["SAVE_CONSUMABLES_RECORD"] = "yes"
            self.assertTrue(save_consumables_record_enabled())
        finally:
            if previous is None:
                os.environ.pop("SAVE_CONSUMABLES_RECORD", None)
            else:
                os.environ["SAVE_CONSUMABLES_RECORD"] = previous

    def test_case_id_fragments_match_consumable_sid(self):
        self.assertEqual(
            _case_id_sid_fragments("20260602011652012"),
            ["20260602011652012", "20260602011652", "011652012", "011652"],
        )
        self.assertEqual(_consumable_sid_score("20260602011652012", "20260602101003011652"), 10)
        self.assertEqual(_consumable_sid_score("20260606163336003", "2026060610100316333603"), 10)
        self.assertEqual(_consumable_sid_score("20260606163336003", "20260606163336003"), 20)

    def test_extracts_emm_temsis_id_from_href(self):
        href = "/ACS/ACS15002?emmTemsisid=2026060210100301165202"
        self.assertEqual(_emm_temsis_id_from_href(href), "2026060210100301165202")

    def test_consumable_detail_prefers_matching_vehicle_text_for_two_vehicle_cases(self):
        class FakeWait:
            def __init__(self, driver, timeout):
                pass

            def until(self, predicate):
                return True

        class FakeDriver:
            def find_elements(self, by, value):
                return [object()]

            def execute_script(self, script):
                return [
                    {
                        "href": "/ACS/ACS15002?emmTemsisid=2026060201165201",
                        "sid": "2026060201165201",
                        "text": "01:16 \u65b0\u576191 \u6025\u75c5",
                    },
                    {
                        "href": "/ACS/ACS15002?emmTemsisid=2026060210100301165202",
                        "sid": "2026060210100301165202",
                        "text": "01:16 \u65b0\u576192 \u6025\u75c5",
                    },
                ]

        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            case_id="2026060201165201",
            case_time="0116",
            vehicle="\u65b0\u576192",
            case_reason="\u6025\u75c5",
        )
        with patch("consumables_login.WebDriverWait", FakeWait):
            href = _find_consumable_detail_href(FakeDriver(), request)

        self.assertEqual(href, "/ACS/ACS15002?emmTemsisid=2026060210100301165202")

    def test_consumable_detail_matches_vehicle_by_ppe_plate_text(self):
        class FakeWait:
            def __init__(self, driver, timeout):
                pass

            def until(self, predicate):
                return True

        class FakeElement:
            text = ""

        class FakeDriver:
            def find_elements(self, by, value):
                return [object()]

            def execute_script(self, script):
                return [
                    {
                        "href": "/ACS/ACS15002?emmTemsisid=2026060210100301165201",
                        "sid": "2026060210100301165201",
                        "text": "01:16 BGV-2310 \u6025\u75c5",
                    },
                    {
                        "href": "/ACS/ACS15002?emmTemsisid=2026060210100301165202",
                        "sid": "2026060210100301165202",
                        "text": "01:16 BXB-7593 \u6025\u75c5",
                    },
                ]

            def get(self, url):
                pass

            def find_element(self, by, value):
                return FakeElement()

        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            case_id="",
            case_time="0116",
            vehicle="\u65b0\u576192",
            case_reason="\u6025\u75c5",
        )
        with patch("consumables_login.WebDriverWait", FakeWait), patch("consumables_login.time.sleep"):
            href = _find_consumable_detail_href(FakeDriver(), request)

        self.assertEqual(href, "/ACS/ACS15002?emmTemsisid=2026060210100301165202")

    def test_consumable_detail_rejects_vehicle_only_old_case_match(self):
        class FakeWait:
            def __init__(self, driver, timeout):
                pass

            def until(self, predicate):
                return True

        class FakeDriver:
            def find_elements(self, by, value):
                return [object()]

            def execute_script(self, script):
                return [
                    {
                        "href": "/ACS/ACS15002?emmTemsisid=2026070610100316091001",
                        "sid": "2026070610100316091001",
                        "text": "2026/07/06 10:10:03 新坡93 車禍",
                    }
                ]

        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            case_id="20260708204710016",
            case_time="2047",
            vehicle="新坡93",
            case_address="桃園市中壢區月桃路一段270巷52號",
            case_reason="車禍",
        )
        with patch("consumables_login.WebDriverWait", FakeWait), self.assertRaisesRegex(RuntimeError, "耗材列表找不到符合案件"):
            _find_consumable_detail_href(FakeDriver(), request)

    def test_consumable_detail_rejects_old_case_even_when_time_and_vehicle_match(self):
        class FakeWait:
            def __init__(self, driver, timeout):
                pass

            def until(self, predicate):
                return True

        class FakeDriver:
            def find_elements(self, by, value):
                return [object()]

            def execute_script(self, script):
                return [
                    {
                        "href": "/ACS/ACS15002?emmTemsisid=2026070610100320471001",
                        "sid": "2026070610100320471001",
                        "text": "2026/07/06 20:47:03 \u65b0\u576193 \u8eca\u798d",
                    }
                ]

        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            case_id="20260708204710016",
            case_time="2047",
            vehicle="\u65b0\u576193",
            case_address="\u6843\u5712\u5e02\u4e2d\u58e2\u5340\u6708\u6843\u8def\u4e00\u6bb5270\u5df752\u865f",
            case_reason="\u8eca\u798d",
        )
        with patch("consumables_login.WebDriverWait", FakeWait), self.assertRaisesRegex(RuntimeError, "耗材列表找不到符合案件"):
            _find_consumable_detail_href(FakeDriver(), request)

    def test_consumable_detail_rejects_single_candidate_without_case_evidence(self):
        class FakeWait:
            def __init__(self, driver, timeout):
                pass

            def until(self, predicate):
                return True

        class FakeDriver:
            def find_elements(self, by, value):
                return [object()]

            def execute_script(self, script):
                return [
                    {
                        "href": "/ACS/ACS15002?emmTemsisid=2026070610100316091001",
                        "sid": "2026070610100316091001",
                        "text": "2026/07/06 10:10:03 新坡91 車禍",
                    }
                ]

        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            case_id="20260708204710016",
            case_time="2047",
            vehicle="新坡93",
            case_address="桃園市中壢區月桃路一段270巷52號",
            case_reason="車禍",
        )
        with patch("consumables_login.WebDriverWait", FakeWait), self.assertRaisesRegex(RuntimeError, "耗材列表找不到符合案件"):
            _find_consumable_detail_href(FakeDriver(), request)

    def test_consumable_detail_can_use_colon_time_as_case_evidence(self):
        class FakeWait:
            def __init__(self, driver, timeout):
                pass

            def until(self, predicate):
                return True

        class FakeDriver:
            def find_elements(self, by, value):
                return [object()]

            def execute_script(self, script):
                return [
                    {
                        "href": "/ACS/ACS15002?emmTemsisid=2026060210100301165202",
                        "sid": "2026060210100301165202",
                        "text": "01:16 新坡92",
                    },
                ]

        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            case_time="0116",
            vehicle="新坡92",
        )
        with patch("consumables_login.WebDriverWait", FakeWait):
            href = _find_consumable_detail_href(FakeDriver(), request)

        self.assertEqual(href, "/ACS/ACS15002?emmTemsisid=2026060210100301165202")

    def test_consumable_detail_checks_detail_page_vehicle_when_list_rows_are_ambiguous(self):
        class FakeWait:
            def __init__(self, driver, timeout):
                pass

            def until(self, predicate):
                return True

        class FakeElement:
            def __init__(self, text=""):
                self.text = text

        class FakeDriver:
            def __init__(self):
                self.current_url = ""

            def find_elements(self, by, value):
                return [object()]

            def execute_script(self, script):
                return [
                    {
                        "href": "/ACS/ACS15002?emmTemsisid=2026062510100312223801",
                        "sid": "2026062510100312223801",
                        "text": "2026/06/25 12:25:24 \u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e0a\u798f\u8def116\u5df746\u865f",
                    },
                    {
                        "href": "/ACS/ACS15002?emmTemsisid=2026062510100312223802",
                        "sid": "2026062510100312223802",
                        "text": "2026/06/25 12:24:31 \u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e0a\u798f\u8def116\u5df746\u865f",
                    },
                ]

            def get(self, url):
                self.current_url = url

            def find_element(self, by, value):
                if self.current_url.endswith("12223802"):
                    return FakeElement("\u65b0\u576192 BXB-7593")
                return FakeElement("\u65b0\u576191 BGV-2310")

        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            case_id="2026062512223801",
            case_time="1225",
            vehicle="\u65b0\u576192",
            case_address="\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4e0a\u798f\u8def116\u5df746\u865f",
        )
        with patch("consumables_login.WebDriverWait", FakeWait), patch("consumables_login.time.sleep"):
            href = _find_consumable_detail_href(FakeDriver(), request)

        self.assertEqual(href, "/ACS/ACS15002?emmTemsisid=2026062510100312223802")

    def test_consumable_detail_checks_detail_vehicle_before_unique_sid_fallback(self):
        class FakeWait:
            def __init__(self, driver, timeout):
                pass

            def until(self, predicate):
                return True

        class FakeElement:
            def __init__(self, text=""):
                self.text = text

        class FakeDriver:
            def __init__(self):
                self.current_url = ""
                self.visited = []

            def find_elements(self, by, value):
                return [object()]

            def execute_script(self, script):
                return [
                    {
                        "href": "/ACS/ACS15002?emmTemsisid=2026070910100321364403",
                        "sid": "2026070910100321364403",
                        "text": "2026/07/09 21:40:25 桃園市觀音區廣大路542巷3弄7號 OHCA",
                    },
                    {
                        "href": "/ACS/ACS15002?emmTemsisid=2026070910100399999901",
                        "sid": "2026070910100399999901",
                        "text": "2026/07/09 21:40:17 桃園市觀音區廣大路542巷3弄7號 OHCA",
                    },
                ]

            def get(self, url):
                self.current_url = url
                self.visited.append(url)

            def find_element(self, by, value):
                if self.current_url.endswith("99999901"):
                    return FakeElement("出勤單位 新坡92 BXB-7593 救護人員 張家和")
                return FakeElement("出勤單位 新坡93 BSL-9230 救護人員 曾彥綸")

        driver = FakeDriver()
        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            case_id="20260709213644003",
            case_time="2140",
            vehicle="新坡92",
            case_address="桃園市觀音區廣大路542巷3弄7號",
        )
        with patch("consumables_login.WebDriverWait", FakeWait), patch("consumables_login.time.sleep"):
            href = _find_consumable_detail_href(driver, request)

        self.assertEqual(href, "/ACS/ACS15002?emmTemsisid=2026070910100399999901")
        self.assertEqual(
            driver.visited,
            [
                "https://nfaemsap3.nfa.gov.tw/ACS/ACS15002?emmTemsisid=2026070910100321364403",
                "https://nfaemsap3.nfa.gov.tw/ACS/ACS15002?emmTemsisid=2026070910100399999901",
            ],
        )

    def test_consumable_detail_wait_fails_when_session_returns_to_sso(self):
        class FakeElement:
            text = ""

        class FakeDriver:
            current_url = "https://nfaemsap3.nfa.gov.tw/SSO/login"

            def find_elements(self, by, value):
                if value == "verificationCode":
                    return [object()]
                return []

            def find_element(self, by, value):
                return FakeElement()

        class FakeWait:
            def until(self, predicate):
                return predicate(FakeDriver())

        self.assertFalse(_wait_for_consumable_detail_page(FakeDriver(), FakeWait()))

    def test_load_acs_credentials_uses_selected_synced_id_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            previous_account = os.environ.get("ACS_ACCOUNT")
            previous_password = os.environ.get("ACS_PASSWORD")
            os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
            os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
            os.environ["ACS_ACCOUNT"] = "A123456789"
            os.environ["ACS_PASSWORD"] = "env-secret"
            try:
                save_duty_automation_credentials(
                    [
                        {
                            "actor_no": "8",
                            "user_id": "tyfd01510",
                            "password": "secret",
                            "display_name": "8番 曾彥綸",
                            "name": "曾彥綸",
                            "id_number": "B123017532",
                        },
                        {
                            "actor_no": "9",
                            "user_id": "tyfd00009",
                            "password": "selected-secret",
                            "display_name": "9番 測試員",
                            "name": "測試員",
                            "id_number": "C123456789",
                        }
                    ],
                    last_selected="9",
                )
                request = AmbulanceReturnRequest(
                    task_id="task-1",
                    created_at=__import__("datetime").datetime.now(),
                    raw_text="",
                    personnel_accounts=["tyfd01510"],
                )

                self.assertEqual(_load_acs_credentials(request), ("C123456789", "selected-secret"))
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override
                if previous_account is None:
                    os.environ.pop("ACS_ACCOUNT", None)
                else:
                    os.environ["ACS_ACCOUNT"] = previous_account
                if previous_password is None:
                    os.environ.pop("ACS_PASSWORD", None)
                else:
                    os.environ["ACS_PASSWORD"] = previous_password

    def test_load_acs_credentials_auto_looks_up_and_remembers_synced_id_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
            previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
            os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
            os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
            try:
                save_duty_automation_credentials(
                    [
                        {
                            "actor_no": "8",
                            "user_id": "tyfd01510",
                            "password": "secret",
                            "name": "曾彥綸",
                        }
                    ],
                    last_selected="8",
                )
                calls = []

                def fake_lookup():
                    calls.append(True)
                    update_saved_credential_id_number("tyfd01510", "B123017532")

                with patch.object(consumables_login_module, "_lookup_synced_credential_id_number_for_acs", side_effect=fake_lookup):
                    self.assertEqual(_load_acs_credentials(), ("B123017532", "secret"))
                    self.assertEqual(_load_acs_credentials(), ("B123017532", "secret"))

                self.assertEqual(len(calls), 1)
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override

    def test_load_acs_credentials_ignores_legacy_env_override(self):
        previous_account = os.environ.get("ACS_ACCOUNT")
        previous_password = os.environ.get("ACS_PASSWORD")
        previous_path = os.environ.get("DUTY_SAVED_LOGIN_PATH")
        previous_override = os.environ.get("DUTY_SAVED_LOGIN_PATH_OVERRIDE")
        os.environ["ACS_ACCOUNT"] = "A123456789"
        os.environ["ACS_PASSWORD"] = "env-secret"
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["DUTY_SAVED_LOGIN_PATH"] = str(Path(tmp) / "saved_login.json")
            os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = "1"
            try:
                with self.assertRaisesRegex(RuntimeError, "同步含身分證字號"):
                    _load_acs_credentials()
            finally:
                if previous_path is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH"] = previous_path
                if previous_override is None:
                    os.environ.pop("DUTY_SAVED_LOGIN_PATH_OVERRIDE", None)
                else:
                    os.environ["DUTY_SAVED_LOGIN_PATH_OVERRIDE"] = previous_override
                if previous_account is None:
                    os.environ.pop("ACS_ACCOUNT", None)
                else:
                    os.environ["ACS_ACCOUNT"] = previous_account
                if previous_password is None:
                    os.environ.pop("ACS_PASSWORD", None)
                else:
                    os.environ["ACS_PASSWORD"] = previous_password


if __name__ == "__main__":
    unittest.main()
