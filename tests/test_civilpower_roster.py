import hashlib
import unittest

from ambulance_bot.civilpower_roster import (
    merge_roster_report,
    normalize_roster_members,
    roster_member_by_id,
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
            "可選人員",
            members[0]["member_id"],
        )

    def test_member_id_is_the_normalized_name_and_deduplicates_title_variants(self):
        members = normalize_roster_members(
            [
                {"unit": "大園救護分隊", "title": "隊員", "name": " 江尚諭 "},
                {"unit": "大園救護分隊", "title": "班長", "name": "江尚諭"},
            ]
        )

        self.assertEqual("江尚諭", roster_member_id("其他單位", "其他職稱", " 江尚諭 "))
        self.assertEqual(1, len(members))
        self.assertEqual("江尚諭", members[0]["member_id"])
        self.assertEqual("隊員", members[0]["title"])

    def test_roster_member_by_id_accepts_current_name_and_previous_hashed_id(self):
        unit = "大園救護分隊"
        title = "隊員"
        name = "江尚諭"
        legacy_identity = "\x1f".join((unit, title, name))
        legacy_member_id = f"civilpower-{hashlib.sha256(legacy_identity.encode('utf-8')).hexdigest()[:20]}"
        snapshot = {
            "members": [
                {
                    "member_id": legacy_member_id,
                    "unit": unit,
                    "title": title,
                    "name": name,
                }
            ]
        }

        current_member = roster_member_by_id(snapshot, name)
        legacy_member = roster_member_by_id(snapshot, legacy_member_id)

        self.assertIsNotNone(current_member)
        self.assertIsNotNone(legacy_member)
        self.assertEqual(name, current_member["member_id"])
        self.assertEqual(current_member, legacy_member)

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
