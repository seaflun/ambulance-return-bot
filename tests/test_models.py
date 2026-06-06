import unittest
from datetime import datetime

from ambulance_bot.models import clean_case_address, parse_consumables, parse_request
from ambulance_bot.models import AmbulanceReturnRequest


class ModelParsingTests(unittest.TestCase):
    def test_default_consumables(self):
        request = parse_request("\u6551\u8b77\u56de\u7a0b\n\u8eca\u8f1b:91A1")

        self.assertEqual(request.vehicle, "91A1")
        self.assertEqual(request.consumables, {"\u53e3\u7f69": 2, "\u624b\u5957": 2})
        self.assertEqual(request.patient_summary, "\u7537\u4e00\u540d")

    def test_parse_full_request(self):
        request = parse_request(
            "\u6551\u8b77\u56de\u7a0b\n"
            "\u8eca\u8f1b:91A1\n"
            "\u53f8\u6a5f:\u66fe\u5f65\u7db8\n"
            "\u91cc\u7a0b:12345\n"
            "\u6848\u4ef6\u6642\u9593:1420\n"
            "\u56de\u7a0b\u6642\u9593:1505\n"
            "\u4e8b\u7531:\u6025\u75c5\n"
            "\u50b7\u75c5\u60a3:\u7537\u4e00\u540d\n"
            "\u8017\u6750:\u53e3\u7f69=2,\u624b\u5957=2,\u6c27\u6c23\u9762\u7f69=1\n"
            "\u6d88\u6bd2:\u5df2\u6d88\u6bd2\n"
            "\u5de5\u4f5c\u7d00\u9304:\u6551\u8b77\u8fd4\u968a"
        )

        self.assertEqual(request.vehicle, "91A1")
        self.assertEqual(request.driver, "\u66fe\u5f65\u7db8")
        self.assertEqual(request.mileage, "12345")
        self.assertEqual(request.case_time, "1420")
        self.assertEqual(request.return_time, "1505")
        self.assertEqual(request.case_reason, "\u6025\u75c5")
        self.assertEqual(request.consumables["\u6c27\u6c23\u9762\u7f69"], 1)
        self.assertEqual(request.disinfection, "\u5df2\u6d88\u6bd2")
        self.assertEqual(request.work_note, "\u6551\u8b77\u8fd4\u968a")
        self.assertEqual(request.duty_status_text, "1.91A1:\u66fe\u5f65\u7db8\n2.\u7537\u4e00\u540d")

    def test_return_time_description_uses_mobile_hhmm_with_zero_seconds(self):
        request = AmbulanceReturnRequest(
            task_id="task-1",
            created_at=datetime(2026, 6, 6, 18, 7, 0),
            raw_text="",
            return_time="1806",
        )

        self.assertEqual(request.return_time_hhmm, "1806")
        self.assertEqual(request.return_time_description_line, "\u8fd4\u968a\u6642\u9593:2026/06/06 18:06:00")

    def test_parse_consumables_accepts_multiple_separators(self):
        self.assertEqual(
            parse_consumables("\u53e3\u7f69*2\u3001\u624b\u5957x2,\u6c27\u6c23\u9762\u7f69=1"),
            {"\u53e3\u7f69": 2, "\u624b\u5957": 2, "\u6c27\u6c23\u9762\u7f69": 1},
        )

    def test_clean_case_address_removes_cancel_noise(self):
        self.assertEqual(
            clean_case_address("\u6843\u5712\u5e02\u4e2d\u58e2\u5340\u5c71\u6771\u8def673\u865f-\u4f86\u96fb\u53d6\u6d88"),
            "\u6843\u5712\u5e02\u4e2d\u58e2\u5340\u5c71\u6771\u8def673\u865f",
        )
        self.assertEqual(
            clean_case_address("\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4fdd\u969c\u4e8c\u8def-\u6848\u4ef6\u91cd\u8907"),
            "\u6843\u5712\u5e02\u89c0\u97f3\u5340\u4fdd\u969c\u4e8c\u8def",
        )


if __name__ == "__main__":
    unittest.main()
