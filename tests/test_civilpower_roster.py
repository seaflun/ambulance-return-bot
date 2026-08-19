import unittest

from ambulance_bot.civilpower_roster import (
    merge_roster_report,
    normalize_roster_members,
    roster_member_id,
)


class CivilPowerRosterTests(unittest.TestCase):
    def test_normalize_roster_members_keeps_daguan_rescue_members_and_excludes_consultants(self):
        members = normalize_roster_members(
            [
                {"unit": "大園救護分隊", "title": "隊員", "name": "可選人員"},
                {"unit": "大園救護分隊", "title": "顧問", "name": "顧問人員"},
                {"unit": "大園救護分隊", "title": "技術顧問", "name": "技術顧問人員"},
                {"unit": "其他救護分隊", "title": "隊員", "name": "其他人員"},
            ]
        )

        self.assertEqual(1, len(members))
        self.assertEqual("可選人員", members[0]["name"])
        self.assertEqual("大園救護分隊", members[0]["unit"])
        self.assertEqual(
            roster_member_id("大園救護分隊", "隊員", "可選人員"),
            members[0]["member_id"],
        )

    def test_failed_refresh_retains_last_known_good_members(self):
        existing = {
            "status": "civilpower_roster_loaded",
            "last_success_at": "2026-08-10T09:00:00",
            "members": [
                {
                    "member_id": "civilpower-abc",
                    "unit": "大園救護分隊",
                    "title": "隊員",
                    "name": "可選人員",
                }
            ],
        }

        merged = merge_roster_report(
            existing,
            {
                "status": "civilpower_roster_failed",
                "detail": "登入失敗",
                "attempted_at": "2026-08-17T09:00:00",
                "members": [],
            },
        )

        self.assertEqual("civilpower_roster_failed", merged["status"])
        self.assertEqual("2026-08-10T09:00:00", merged["last_success_at"])
        self.assertEqual(["可選人員"], [member["name"] for member in merged["members"]])
        self.assertEqual("登入失敗", merged["detail"])
