import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import app as app_module
from ambulance_bot import sinposmart_backend as backend


class DisasterRosterFireDayTests(unittest.TestCase):
    @staticmethod
    def roster(day="2026-10-19"):
        return {"schema_version": 1, "source": "official_duty_staff", "target_date": day,
                "complete": True, "members": [
                    {"actor_no": "10", "name": "員十", "title": "隊員"},
                    {"actor_no": "2", "name": "員二", "title": "隊員"},
                    {"actor_no": "1", "name": "分長", "title": "分隊長"},
                    {"actor_no": "4", "name": "小四", "title": "小隊長"}]}

    def write_roster(self, root, roster, at):
        folder = root / "sinposmart"
        folder.mkdir(exist_ok=True)
        path = folder / "2026-10-19.json"
        events = json.loads(path.read_text(encoding="utf-8"))["events"] if path.exists() else []
        events.append({"record_type": "schedule_snapshot", "occurred_at": at,
                       "snapshot": {"personnel_titles": roster}})
        path.write_text(json.dumps({"events": events}), encoding="utf-8")
        return path

    def test_explicit_previous_fire_day_roster_survives_midnight_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.write_roster(root, self.roster(), "2026-10-19T22:56:00")
            before = path.read_bytes()
            actual = backend.read_personnel_title_snapshot(root, now=datetime(2026, 10, 20, 1, 40),
                                                          target_date="2026-10-19")
            self.assertEqual(self.roster(), actual)
            self.assertEqual(before, path.read_bytes())

    def test_requested_day_does_not_mix_new_calendar_day_or_revoked_rosters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_roster(root, self.roster(), "2026-10-19T22:56:00")
            self.write_roster(root, self.roster("2026-10-20"), "2026-10-20T01:00:00")
            self.assertEqual(self.roster(), backend.read_personnel_title_snapshot(
                root, now=datetime(2026, 10, 20, 1, 40), target_date="2026-10-19"))
            self.write_roster(root, {**self.roster(), "complete": False, "members": []}, "2026-10-20T01:30:00")
            self.assertEqual({}, backend.read_personnel_title_snapshot(
                root, now=datetime(2026, 10, 20, 1, 40), target_date="2026-10-19"))

    def test_explicit_day_preserves_expiration_and_future_date_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_roster(root, self.roster(), "2026-10-19T01:00:00")
            self.assertEqual({}, backend.read_personnel_title_snapshot(
                root, now=datetime(2026, 10, 20, 1, 40), target_date="2026-10-19"))
            for day in ("2026-10-21", "bad", "2026-02-30"):
                with self.subTest(day=day):
                    self.assertEqual({}, backend.read_personnel_title_snapshot(
                        root, now=datetime(2026, 10, 20, 1, 40), target_date=day))

    def test_nas_and_local_keep_three_orders_on_both_sides_of_midnight(self):
        for case_day, time in (("2026/10/19", "2310"), ("2026/10/20", "0130"), ("2026/10/20", "0759")):
            for local in (False, True):
                with self.subTest(case_day=case_day, time=time, local=local):
                    case = {"case_date": case_day, "case_time": time, "personnel": ["員十", "小四", "分長", "員二"],
                            "firecam_people": ["員十"], "vehicle_entries": [{"driver": "員二"}]}
                    original = copy.deepcopy(case)
                    settings = {"disaster_personnel_titles": self.roster()} if local else None
                    with app_module.app.test_request_context(base_url="http://localhost:8090" if local else "http://nas.test:8091"), \
                            mock.patch.object(app_module, "read_personnel_title_snapshot", return_value=self.roster()):
                        actual = app_module.disaster_personnel_display_options(case, settings)
                    self.assertEqual(["分長", "小四", "員二", "員十"], [p[0] for p in actual["current_personnel_options"]])
                    self.assertEqual(actual["current_personnel_options"], actual["firecam_person_options"])
                    self.assertEqual(["員二", "員十", "小四", "分長"], [p[0] for p in actual["driver_person_options"]])
                    self.assertEqual({"員十": 10, "員二": 2, "分長": 1, "小四": 4}, actual["personnel_actor_numbers"])
                    self.assertEqual(original, case)

    def test_eight_am_and_unknown_time_do_not_borrow_previous_day(self):
        for time in ("0800", "", "bad", "2500"):
            with self.subTest(time=time), app_module.app.test_request_context(base_url="http://nas.test:8091"), \
                    mock.patch.object(app_module, "read_personnel_title_snapshot", return_value=self.roster()):
                actual = app_module.disaster_personnel_display_options(
                    {"case_date": "2026/10/20", "case_time": time, "personnel": ["員十", "員二"]})
                self.assertEqual({}, actual["personnel_titles"])
                self.assertEqual({}, actual["personnel_actor_numbers"])

    def test_worker_settings_read_current_fire_day_after_midnight(self):
        clock = mock.Mock(wraps=datetime)
        clock.now.return_value = datetime(2026, 10, 20, 1, 40)
        with mock.patch.object(backend, "datetime", clock), mock.patch.object(app_module, "datetime", clock), \
                mock.patch.object(app_module, "read_personnel_title_snapshot", return_value=self.roster()) as reader:
            actual = app_module.vehicle_settings_api_payload()
        self.assertEqual(self.roster(), actual["disaster_personnel_titles"])
        self.assertEqual("2026-10-19", reader.call_args.kwargs.get("target_date"))


if __name__ == "__main__":
    unittest.main()
