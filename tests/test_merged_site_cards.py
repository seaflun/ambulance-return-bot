import copy
from datetime import datetime, timedelta
from html.parser import HTMLParser
import json
import unittest
from unittest.mock import patch

import app as web
from ambulance_bot.models import FuelRecord
from tests import test_lan_login_visibility as fixture


class Element:
    def __init__(self, tag="", attributes=()):
        self.tag, self.attrs, self.children = tag, dict(attributes), []

    @property
    def text(self):
        return " ".join(" ".join(child.text if isinstance(child, Element) else child
                                  for child in self.children).split())

    def find(self, class_name=None, tag=None):
        matches = []
        for child in self.children:
            if not isinstance(child, Element):
                continue
            if ((class_name is None or class_name in child.attrs.get("class", "").split())
                    and (tag is None or tag == child.tag)):
                matches.append(child)
            matches.extend(child.find(class_name, tag))
        return matches


class SiteCards(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.root = Element()
        self.stack = [self.root]
        self.feed(body)

    def handle_starttag(self, tag, attributes):
        node = Element(tag, attributes)
        self.stack[-1].children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                return

    def handle_data(self, text):
        self.stack[-1].children.append(text)

    @property
    def cards(self):
        return {card.attrs.get("data-site-key"): card for card in self.root.find("site", "div")}


class MergedSiteCardsTests(unittest.TestCase):
    setUp = fixture.LanLoginVisibilityTests.setUp
    task = fixture.LanLoginVisibilityTests.task

    def render(self, service="ems", *, optional=False, accounts=None, site_updates=None):
        self.renders = getattr(self, "renders", 0) + 1
        task = self.task(
            task_id=f"merged-cards-{self.renders}", service_type=service,
            fuel_record=FuelRecord(enabled=optional), volunteer_assist=optional,
            **({"vehicle": "新坡11", "vehicle_entries": [
                {"vehicle": "新坡11", "driver": "甲", "mileage": "35444", "fuel_record": {"enabled": optional}},
                {"vehicle": "新坡15", "driver": "乙", "mileage": "25400"},
            ]} if service == "disaster" else {}),
        )
        payload = self.store.get(task.task_id)
        started = datetime.now().replace(microsecond=0) - timedelta(minutes=10)
        payload["events"] = []
        for index, (key, title) in enumerate(web.task_site_display_pairs(task.to_dict())):
            site = payload["site_statuses"][key]
            site.update(status=f"{key}_saved", detail="登打已完成。")
            site.update((site_updates or {}).get(key, {}))
            payload["events"].extend([
                {"time": started.isoformat(), "status": f"{key}_running", "action": f"{title} 開始"},
                {"time": (started + timedelta(seconds=20 + index)).isoformat(),
                 "status": site["status"], "action": f"{title} 結果", "detail": site["detail"]},
            ])
        payload["site_login_accounts"] = accounts or {}
        self.store.save_payload(task.task_id, payload)
        report = {**copy.deepcopy(payload), "task_id": task.task_id, "title": "合併站別卡片測試",
                  "operator": "OPERATOR_META_SHOULD_NOT_RENDER", "synced_account": "SYNC_META_SHOULD_NOT_RENDER"}
        path = web.public_pc_report_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tasks": [report]}, ensure_ascii=False), encoding="utf-8")
        before = {source: source.read_bytes() for source in (path, self.store.path_for(task.task_id))}
        response = self.client.get(f"/admin/{service}")
        self.assertEqual(200, response.status_code)
        for source, content in before.items():
            self.assertEqual(content, source.read_bytes(), str(source))
        body = response.get_data(as_text=True)
        return SiteCards(body), body, task

    def account_box(self, card, title):
        boxes = card.find("site-login-account", "div")
        self.assertEqual(1, len(boxes))
        self.assertEqual(f"{title}登入帳號", boxes[0].attrs.get("aria-label"))
        return boxes[0]

    def test_both_services_keep_each_stations_account_status_and_duration_in_one_card(self):
        accounts = {key: f"{index + 1}番 站別{index} - tyfd{10001 + index}（任務司機）"
                    for index, key in enumerate(web.SITE_RUN_ORDER)}
        for service, keys in (("ems", ("duty_work_log", "vehicle_mileage", "consumables", "disinfection")),
                              ("disaster", ("duty_work_log", "vehicle_mileage"))):
            with self.subTest(service=service):
                parsed, _, _ = self.render(service, accounts=accounts)
                groups = parsed.root.find("site-grid", "div")
                self.assertEqual(1, len(groups))
                self.assertEqual("group", groups[0].attrs.get("role"))
                self.assertEqual("各站登打狀態", groups[0].attrs.get("aria-label"))
                self.assertEqual(set(keys), set(parsed.cards))
                self.assertFalse(parsed.root.find("login-summary"))
                meta = " ".join(node.text for node in parsed.root.find("meta"))
                for removed in ("同步帳號", "OPERATOR_META_SHOULD_NOT_RENDER", "SYNC_META_SHOULD_NOT_RENDER"):
                    self.assertNotIn(removed, meta)
                for index, key in enumerate(keys):
                    card = parsed.cards[key]
                    title = web.SITE_DISPLAY_NAMES[key]
                    self.assertEqual(title, card.find("site-name")[0].text)
                    self.assertEqual(web.status_label(f"{key}_saved"), card.find("status")[0].text)
                    self.assertEqual(f"完成耗時 約 {20 + index} 秒", card.find("site-duration")[0].text)
                    box = self.account_box(card, title)
                    self.assertEqual(accounts[key].split("（")[0], box.find("login-account-value")[0].text)
                    self.assertEqual("任務司機", box.find("site-account-source")[0].text)

    def test_optional_fuel_and_volunteer_cards_follow_the_same_contract(self):
        for service, keys in (("ems", set(web.SITE_RUN_ORDER)),
                              ("disaster", {"duty_work_log", "vehicle_mileage", "fuel_record"})):
            with self.subTest(service=service):
                parsed, _, _ = self.render(service, optional=True, accounts={
                    "fuel_record": "7番 加油帳號 - tyfd10007（同步帳號）",
                    "volunteer_assist": "8番 協勤帳號 - tyfd10008（出勤人員）",
                })
                self.assertEqual(keys, set(parsed.cards))
                for key, source in (("fuel_record", "同步帳號"), ("volunteer_assist", "出勤人員")):
                    if key in keys:
                        box = self.account_box(parsed.cards[key], web.SITE_DISPLAY_NAMES[key])
                        self.assertEqual(source, box.find("site-account-source")[0].text)

    def test_unknown_and_masked_accounts_remain_inside_their_own_cards(self):
        parsed, body, _ = self.render(accounts={"consumables": "8番 測試丙 - A123456789（同步帳號）"})
        box = self.account_box(parsed.cards["consumables"], "耗材")
        self.assertEqual("8番 測試丙 - A123***789", box.find("login-account-value")[0].text)
        self.assertEqual("同步帳號", box.find("site-account-source")[0].text)
        self.assertNotIn("A123456789", body)
        for key in ("duty_work_log", "vehicle_mileage", "disinfection"):
            box = self.account_box(parsed.cards[key], web.SITE_DISPLAY_NAMES[key])
            self.assertIn("登入帳號尚未回報", box.text.replace(" ", ""))
            self.assertFalse(box.find("site-account-source"))

    def test_multiple_vehicle_accounts_keep_vehicle_current_and_unreported_context(self):
        parsed, _, _ = self.render("disaster", accounts={"vehicle_mileage":
            "新坡11：12番 測試甲 - tyfd10001（任務司機）；新坡15：尚未回報；目前：7番 測試乙 - tyfd10002（出勤人員）"})
        box = self.account_box(parsed.cards["vehicle_mileage"], "里程")
        rows = box.find("site-account-line")
        self.assertEqual(3, len(rows))
        values = [row.find("login-account-value")[0].text for row in rows]
        self.assertEqual(["新坡11：12番 測試甲 - tyfd10001", "新坡15：尚未回報",
                          "目前：7番 測試乙 - tyfd10002"], values)
        self.assertEqual(["任務司機", "出勤人員"], [node.text for node in box.find("site-account-source")])

    def test_vehicle_details_retry_manual_confirmation_and_history_are_preserved(self):
        conflict = "案件與既有用車時間重疊，停止補登。"
        with patch.dict("os.environ", {"WORKER_TOKEN": "0123456789abcdef0123456789abcdef"}):
            parsed, _, task = self.render("disaster", site_updates={"vehicle_mileage": {
                "status": "vehicle_mileage_failed", "detail": conflict,
                "vehicle_results": {"新坡15": {"status": "vehicle_mileage_failed", "detail": conflict}},
            }})
        self.assertEqual(1, len(parsed.root.find("vehicle-summary", "section")))
        self.assertIn("新坡11 / 甲", parsed.root.find("vehicle-summary")[0].text)
        self.assertIn("新坡15 / 乙", parsed.root.find("vehicle-summary")[0].text)
        card = parsed.cards["vehicle_mileage"]
        actions = {form.attrs.get("action") for form in card.find(tag="form")}
        self.assertIn(f"/tasks/{task.task_id}/sites/vehicle_mileage/run", actions)
        self.assertIn(f"/tasks/{task.task_id}/sites/vehicle_mileage/complete", actions)
        acknowledgements = [node for node in card.find(tag="input")
                            if node.attrs.get("name") == "confirmed_existing_mileage"]
        self.assertEqual(1, len(acknowledgements))
        self.assertIn("required", acknowledgements[0].attrs)
        self.assertTrue(card.find("site-error-history", "details"))
        self.assertTrue(parsed.root.find("task-details", "details"))
        self.assertIn("完整事件", parsed.root.find("task-details")[0].text)


if __name__ == "__main__":
    unittest.main()
