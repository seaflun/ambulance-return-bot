import unittest

import worker_panel


class WorkerPanelTests(unittest.TestCase):
    def setUp(self):
        self.original_thread = worker_panel.worker_thread
        self.original_open = worker_panel.open_url_in_worker_chrome
        worker_panel.worker_thread = None
        worker_panel.last_opened.clear()
        worker_panel.app.config.update(TESTING=True)
        self.client = worker_panel.app.test_client()

    def tearDown(self):
        worker_panel.worker_thread = self.original_thread
        worker_panel.open_url_in_worker_chrome = self.original_open
        worker_panel.last_opened.clear()

    def test_panel_loads(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        body = response.data.decode("utf-8")
        self.assertIn("SinpoSmart - 救災救護Worker 面板", body)
        self.assertIn("車輛里程", body)
        self.assertIn("消防勤務工作紀錄", body)
        self.assertNotIn("開啟全部四站", body)

    def test_open_site_uses_worker_chrome_launcher(self):
        opened = []
        worker_panel.open_url_in_worker_chrome = lambda url: opened.append(url) or "opened_worker_chrome"

        response = self.client.post("/open/vehicle_mileage", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(opened), 1)
        self.assertIn("ppe.tyfd.gov.tw", opened[0])
        self.assertIn("vehicle_mileage", worker_panel.last_opened)


if __name__ == "__main__":
    unittest.main()
