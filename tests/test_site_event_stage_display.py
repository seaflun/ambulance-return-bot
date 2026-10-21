import copy
import unittest

import app as web
from ambulance_bot.site_diagnostics import civilpower_stage_from_detail, site_stage_from_detail


class SiteEventStageDisplayTests(unittest.TestCase):
    def row(self, site, detail, *, status=None, action=None, **diagnostics):
        status = status or f"{site}_running"
        event = dict(event_id="history", time="2026-10-21T09:00:00", status=status,
                     action=action or web.status_label(status), detail=detail, **diagnostics)
        report = {"events": [event], "site_statuses": {site: {"status": f"{site}_saved"}}}
        before = copy.deepcopy(report)
        row = web.public_pc_event_rows(report)[0]
        self.assertEqual(before, report)
        self.assertEqual(status, row["status"])
        return row

    def test_worker_startups_cover_other_sites_and_nas_wrappers(self):
        for site, execution in (("consumables", "耗材"), ("disinfection", "消毒紀錄")):
            name = web.site_display_name(site)
            for wrapper in ("", name + ": "):
                for audit in ("", "登入帳號：測試帳號。"):
                    for action in ("執行中", f"公務電腦回報{name}"):
                        with self.subTest(site=site, wrapper=wrapper, audit=audit, action=action):
                            row = self.row(site, wrapper + audit + f"公務電腦 worker 執行{execution}：pc-1",
                                           action=action)
                            self.assertEqual("準備登打", row["stage_group"])
                            self.assertIn("｜準備登打｜執行中", row["action"])

    def test_exact_requeues_cover_all_sites_and_legacy_statuses(self):
        for site in web.SITE_DEFINITIONS:
            for status in ("manual_confirmation_required", "manual_captcha_required", "queued_for_worker"):
                with self.subTest(site=site.key, status=status):
                    row = self.row(site.key, f"後台重新送出{site.name}，已排入公務電腦處理。",
                                   status=status, action=f"後台重新送出{site.name}")
                    self.assertEqual("排隊等候", row["stage_group"])
                    self.assertTrue(row["action"].endswith("｜排隊等候｜等候執行"))

    def test_known_browser_recovery_is_retrying_instead_of_unknown(self):
        for site in web.SITE_DEFINITIONS:
            messages = (
                (f"{site.name} Chrome 工作階段失效，正在自動重啟瀏覽器並重試。", f"公務電腦回報{site.name}"),
                (f"{site.name} 網頁工作階段失效，自動重啟瀏覽器後重試", None),
            )
            for detail, action in messages:
                with self.subTest(site=site.key, detail=detail):
                    row = self.row(site.key, detail, action=action or detail)
                    self.assertEqual("重啟瀏覽器", row["stage_group"])
                    self.assertTrue(row["action"].endswith("｜重啟瀏覽器｜重試中"))

    def test_edited_site_requires_another_run_without_claiming_it_was_queued(self):
        row = self.row("fuel_record", "任務內容已修改，請更新此站。",
                       status="fuel_record_needs_update", action="按下單站登打：登打加油紀錄")
        self.assertEqual("加油｜內容已修改｜待重新登打", row["action"])
        self.assertNotIn("排隊", row["action"])

    def test_known_messages_inside_errors_or_unrelated_actions_stay_unclassified(self):
        startup = "公務電腦 worker 執行耗材：pc-1"
        for changes in (
            {"detail": "錯誤附註：" + startup},
            {"detail": startup + "，登入失敗"},
            {"detail": startup, "action": "其他耗材事件"},
            {"detail": startup, "failure_stage": "未知步驟", "failure_reason": "明確錯誤"},
            {"detail": "公務電腦 worker 執行消毒紀錄：pc-1"},
            {"detail": "公務電腦 worker 執行車輛里程：pc-1"},
        ):
            with self.subTest(changes=changes):
                row = self.row("consumables", **changes)
                self.assertNotEqual("準備登打", row["stage_group"])
                self.assertNotIn("已完成", row["action"])
        queued = "後台重新送出消防勤務工作紀錄，已排入公務電腦處理。"
        for detail, action in (("錯誤附註：" + queued, "後台重新送出消防勤務工作紀錄"),
                               (queued, "其他工作事件")):
            with self.subTest(detail=detail, action=action):
                row = self.row("duty_work_log", detail, status="manual_confirmation_required", action=action)
                self.assertNotEqual("排隊等候", row.get("stage_group"))

    def test_failure_statuses_and_diagnostics_are_never_relabelled_as_lifecycle(self):
        recovery = "消防勤務工作紀錄 Chrome 工作階段失效，正在自動重啟瀏覽器並重試。"
        for status in ("duty_work_log_failed", "duty_work_log_waiting_confirmation"):
            with self.subTest(status=status):
                row = self.row("duty_work_log", recovery, status=status, action="公務電腦回報消防勤務工作紀錄")
                self.assertNotEqual("重啟瀏覽器", row.get("stage_group"))
        row = self.row("duty_work_log", recovery, action="公務電腦回報消防勤務工作紀錄",
                       failure_stage="儲存", failure_reason="明確儲存錯誤")
        self.assertEqual("儲存確認", row["stage_group"])

    def test_unknown_history_is_not_hidden_by_the_latest_saved_state(self):
        for site in web.SITE_DEFINITIONS:
            with self.subTest(site=site.key):
                row = self.row(site.key, "未提供可辨識的進度內容")
                self.assertEqual("階段未判定", row["stage_group"])
                self.assertNotIn("已完成", row["action"])

    def test_actual_query_retry_and_civilpower_startup_have_stage_groups(self):
        for site, progress, group in (
            ("duty_work_log", "既有工作紀錄查詢逾時，重新查詢一次（尚未新增）", "登入與案件"),
            ("volunteer_assist", "準備登入民力運用管理系統", "登入與出／入登記"),
        ):
            name = web.site_display_name(site)
            for prefix in (f"公務電腦 worker {name}：", f"本機快速 {name}："):
                for wrapper in ("", name + ": 登入帳號：測試帳號。"):
                    with self.subTest(site=site, prefix=prefix, wrapper=wrapper):
                        row = self.row(site, wrapper + prefix + progress)
                        self.assertEqual(group, row["stage_group"])
                        self.assertTrue(row["action"].endswith(f"｜{group}｜執行中"))

    def test_aliases_keep_the_last_reported_stage(self):
        retry = "公務電腦 worker 消防勤務工作紀錄：既有工作紀錄查詢逾時，重新查詢一次（尚未新增）"
        saved = "公務電腦 worker 消防勤務工作紀錄：確認勤務紀錄"
        self.assertEqual("查詢既有工作紀錄", site_stage_from_detail("duty_work_log", saved + " | " + retry))
        self.assertEqual("確認勤務紀錄", site_stage_from_detail("duty_work_log", retry + " | " + saved))
        login = "公務電腦 worker 民力系統：準備登入民力運用管理系統"
        work = "公務電腦 worker 民力系統：選取義消人員"
        self.assertEqual("登入內部入口網", civilpower_stage_from_detail(work + " | " + login))
        self.assertEqual("選取義消人員", civilpower_stage_from_detail(login + " | " + work))

    def test_new_aliases_require_an_actual_progress_prefix(self):
        for site, progress in (
            ("duty_work_log", "既有工作紀錄查詢逾時，重新查詢一次（尚未新增）"),
            ("volunteer_assist", "準備登入民力運用管理系統"),
        ):
            parser = civilpower_stage_from_detail if site == "volunteer_assist" else lambda text: site_stage_from_detail(site, text)
            for detail in (progress, "worker.py 原始錯誤：" + progress,
                           f"錯誤附註：公務電腦 worker {web.site_display_name(site)}：" + progress):
                with self.subTest(site=site, detail=detail):
                    self.assertEqual("", parser(detail))
