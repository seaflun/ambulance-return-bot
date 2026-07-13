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
    _consumable_detail_vehicle_label,
    _consumable_sid_score,
    _distribute_consumables,
    _emm_temsis_id_from_href,
    _find_consumable_detail_href,
    _find_consumable_detail_hrefs,
    _load_acs_credentials,
    _patient_sid_parts,
    _wait_for_consumable_detail_page,
    open_consumable_record_for_task,
    save_consumables_record_enabled,
)


class ConsumablesLoginTests(unittest.TestCase):
    def test_distribute_consumables_splits_remainder_to_lower_suffix(self):
        self.assertEqual(
            _distribute_consumables({"桃-9吋手套-L(雙)": 3, "桃-口罩(片)": 3}, 2),
            [
                {"桃-9吋手套-L(雙)": 2, "桃-口罩(片)": 2},
                {"桃-9吋手套-L(雙)": 1, "桃-口罩(片)": 1},
            ],
        )

    def test_distribute_consumables_adds_one_glove_only_to_empty_pages(self):
        self.assertEqual(
            _distribute_consumables({"桃-口罩(片)": 2}, 5),
            [
                {"桃-口罩(片)": 1},
                {"桃-口罩(片)": 1},
                {"桃-9吋手套-L(雙)": 1},
                {"桃-9吋手套-L(雙)": 1},
                {"桃-9吋手套-L(雙)": 1},
            ],
        )

    def test_detail_vehicle_prefers_selected_control_over_all_body_options(self):
        class FakeElement:
            text = "出勤單位 新坡91 BGV-2310 新坡92 BXB-7593 新坡93 BSL-9230"

        class FakeDriver:
            def find_element(self, by, value):
                return FakeElement()

            def execute_script(self, script):
                return "新坡93 BSL-9230"

        self.assertEqual(_consumable_detail_vehicle_label(FakeDriver()), "新坡93")

    def test_open_consumable_record_writes_every_patient_page_in_suffix_order(self):
        hrefs = [
            "/ACS/ACS15002?emmTemsisid=2026071310100308031901",
            "/ACS/ACS15002?emmTemsisid=2026071310100308031902",
        ]
        written = []

        class FakeDriver:
            current_url = ""

            def get(self, url):
                self.current_url = url

        request = AmbulanceReturnRequest(
            task_id="task-multi-write",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            vehicle="新坡93",
            consumables={"桃-9吋手套-L(雙)": 3, "桃-口罩(片)": 3},
        )

        def fake_write(driver, wait, page_request):
            written.append((_emm_temsis_id_from_href(driver.current_url)[-2:], dict(page_request.consumables)))
            return "saved"

        with patch.object(consumables_login_module, "_open_consumable_maintenance_page"), patch.object(
            consumables_login_module, "_find_consumable_detail_hrefs", return_value=hrefs
        ), patch.object(consumables_login_module, "_wait_for_consumable_detail_page", return_value=True), patch.object(
            consumables_login_module, "_consumable_detail_vehicle_label", return_value="新坡93"
        ), patch.object(
            consumables_login_module, "_write_current_consumable_page", side_effect=fake_write, create=True
        ), patch.object(consumables_login_module, "save_consumables_record_enabled", return_value=True):
            detail = open_consumable_record_for_task(FakeDriver(), request)

        self.assertEqual(
            written,
            [
                ("01", {"桃-9吋手套-L(雙)": 2, "桃-口罩(片)": 2}),
                ("02", {"桃-9吋手套-L(雙)": 1, "桃-口罩(片)": 1}),
            ],
        )
        self.assertIn("辨識新坡93同案2位患者", detail)
        self.assertIn("01填入4件", detail)
        self.assertIn("02填入2件", detail)
        self.assertIn("兩頁均已儲存確認", detail)

    def test_open_consumable_record_reports_completed_suffix_when_later_page_fails(self):
        hrefs = [
            "/ACS/ACS15002?emmTemsisid=2026071310100308031901",
            "/ACS/ACS15002?emmTemsisid=2026071310100308031902",
        ]

        class FakeDriver:
            current_url = ""

            def get(self, url):
                self.current_url = url

        request = AmbulanceReturnRequest(
            task_id="task-multi-failure",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            vehicle="新坡93",
            consumables={"桃-9吋手套-L(雙)": 2, "桃-口罩(片)": 2},
        )
        writes = []

        def fake_write(driver, wait, page_request):
            suffix = _emm_temsis_id_from_href(driver.current_url)[-2:]
            writes.append(suffix)
            if suffix == "02":
                raise RuntimeError("耗材儲存後讀回不一致")
            return "saved"

        with patch.object(consumables_login_module, "_open_consumable_maintenance_page"), patch.object(
            consumables_login_module, "_find_consumable_detail_hrefs", return_value=hrefs
        ), patch.object(consumables_login_module, "_wait_for_consumable_detail_page", return_value=True), patch.object(
            consumables_login_module, "_consumable_detail_vehicle_label", return_value="新坡93"
        ), patch.object(
            consumables_login_module, "_write_current_consumable_page", side_effect=fake_write
        ), patch.object(consumables_login_module, "save_consumables_record_enabled", return_value=True):
            with self.assertRaisesRegex(
                RuntimeError,
                "同案多患者耗材分配／確認失敗：成功=01；失敗=02；原因=耗材儲存後讀回不一致",
            ):
                open_consumable_record_for_task(FakeDriver(), request)

        self.assertEqual(writes, ["01", "02"])

    def test_patient_sid_parts_uses_last_two_digits(self):
        self.assertEqual(
            _patient_sid_parts("2026071310100308031901"),
            ("20260713101003080319", "01"),
        )
        with self.assertRaisesRegex(RuntimeError, "TEMSISID.*患者序號"):
            _patient_sid_parts("20260713101003080319AA")

    def test_consumable_detail_returns_all_same_vehicle_patient_pages(self):
        candidates = [
            {
                "href": "/ACS/ACS15002?emmTemsisid=2026071310100308031901",
                "sid": "2026071310100308031901",
                "text": "2026/07/13 08:05:05 桃園市中壢區月桃路一段和月山路的交叉路口 交通事故",
            },
            {
                "href": "/ACS/ACS15002?emmTemsisid=2026071310100308031902",
                "sid": "2026071310100308031902",
                "text": "2026/07/13 08:05:05 桃園市中壢區月桃路一段和月山路的交叉路口 交通事故",
            },
        ]

        class FakeWait:
            def __init__(self, driver, timeout):
                pass

            def until(self, predicate):
                return True

        class FakeElement:
            text = "出勤單位 新坡93 BSL-9230"

        class FakeDriver:
            current_url = ""

            def find_elements(self, by, value):
                return [object()]

            def execute_script(self, script):
                if "a.btn_t02" in script:
                    return candidates
                if "document.readyState" in script:
                    return "complete"
                return ""

            def get(self, url):
                self.current_url = url

            def find_element(self, by, value):
                return FakeElement()

        request = AmbulanceReturnRequest(
            task_id="task-multi-patient",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            case_id="20260713080319001",
            case_time="0805",
            vehicle="新坡93",
            case_address="桃園市中壢區月桃路一段和月山路的交叉路口",
            case_reason="交通事故",
        )
        with patch("consumables_login.WebDriverWait", FakeWait), patch("consumables_login.time.sleep"):
            hrefs = _find_consumable_detail_hrefs(FakeDriver(), request)

        self.assertEqual([_emm_temsis_id_from_href(href)[-2:] for href in hrefs], ["01", "02"])

    def test_consumable_detail_partitions_two_vehicles_before_patients(self):
        base = "20260713101003080319"
        candidates = [
            {
                "href": f"/ACS/ACS15002?emmTemsisid={base}{suffix}",
                "sid": f"{base}{suffix}",
                "text": "2026/07/13 08:05:05 桃園市中壢區月桃路一段和月山路的交叉路口 交通事故",
            }
            for suffix in ("01", "02", "03", "04", "05")
        ]

        class FakeWait:
            def __init__(self, driver, timeout):
                pass

            def until(self, predicate):
                return True

        class FakeElement:
            def __init__(self, text):
                self.text = text

        class FakeDriver:
            current_url = ""

            def find_elements(self, by, value):
                return [object()]

            def execute_script(self, script):
                if "a.btn_t02" in script:
                    return candidates
                if "document.readyState" in script:
                    return "complete"
                return ""

            def get(self, url):
                self.current_url = url

            def find_element(self, by, value):
                suffix = _emm_temsis_id_from_href(self.current_url)[-2:]
                vehicle_text = "新坡92 BXB-7593" if suffix in {"01", "02"} else "新坡93 BSL-9230"
                return FakeElement(f"出勤單位 {vehicle_text}")

        def request_for(vehicle):
            return AmbulanceReturnRequest(
                task_id=f"task-{vehicle}",
                created_at=__import__("datetime").datetime.now(),
                raw_text="",
                case_id="20260713080319001",
                case_time="0805",
                vehicle=vehicle,
                case_address="桃園市中壢區月桃路一段和月山路的交叉路口",
                case_reason="交通事故",
            )

        driver = FakeDriver()
        with patch("consumables_login.WebDriverWait", FakeWait), patch("consumables_login.time.sleep"):
            hrefs_92 = _find_consumable_detail_hrefs(driver, request_for("新坡92"))
            hrefs_93 = _find_consumable_detail_hrefs(driver, request_for("新坡93"))

        self.assertEqual([_emm_temsis_id_from_href(href)[-2:] for href in hrefs_92], ["01", "02"])
        self.assertEqual([_emm_temsis_id_from_href(href)[-2:] for href in hrefs_93], ["03", "04", "05"])

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

    def test_consumable_detail_allows_single_candidate_with_different_detail_vehicle(self):
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
                    }
                ]

            def get(self, url):
                self.current_url = url
                self.visited.append(url)

            def find_element(self, by, value):
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

        self.assertEqual(href, "/ACS/ACS15002?emmTemsisid=2026070910100321364403")
        self.assertEqual(driver.visited, ["https://nfaemsap3.nfa.gov.tw/ACS/ACS15002?emmTemsisid=2026070910100321364403"])

    def test_open_consumable_record_notes_single_vehicle_detail_mismatch(self):
        class FakeElement:
            def __init__(self, text=""):
                self.text = text

        class FakeDriver:
            def __init__(self):
                self.current_url = ""

            def get(self, url):
                self.current_url = url

            def find_element(self, by, value):
                return FakeElement("出勤單位 新坡93 BSL-9230 救護人員 曾彥綸")

        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=__import__("datetime").datetime.now(),
            raw_text="",
            vehicle="新坡92",
        )
        with patch("consumables_login._open_consumable_maintenance_page"), patch(
            "consumables_login._find_consumable_detail_hrefs",
            return_value=["/ACS/ACS15002?emmTemsisid=2026070910100321364403"],
        ), patch("consumables_login._wait_for_consumable_detail_page", return_value=True), patch(
            "consumables_login._needs_extra_consumable_row",
            return_value=False,
        ):
            detail = open_consumable_record_for_task(FakeDriver(), request)

        self.assertIn("APP車輛=新坡92", detail)
        self.assertIn("出勤單位=新坡93", detail)
        self.assertIn("已依內容頁車輛登打", detail)

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
