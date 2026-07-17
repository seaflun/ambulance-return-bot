import unittest

from ambulance_bot.task_edit_impact import analyze_task_edit, changed_site_keys


class TaskEditImpactTests(unittest.TestCase):
    def test_consumables_only_affects_consumables(self):
        previous = {
            "vehicle": "新坡92",
            "consumables": {"紗布": 2},
        }
        current = {
            "vehicle": "新坡92",
            "consumables": {"口罩": 1},
        }

        impact = analyze_task_edit(previous, current)

        self.assertEqual(impact["changed_labels"], ["耗材"])
        self.assertEqual(changed_site_keys(impact), {"consumables"})
        self.assertEqual(impact["site_summaries"], ["耗材"])

    def test_second_vehicle_mileage_targets_only_second_vehicle_mileage(self):
        previous = {
            "two_vehicle": True,
            "vehicle_entries": [
                {"vehicle": "新坡92", "mileage": "100"},
                {"vehicle": "新坡93", "mileage": "200"},
            ],
        }
        current = {
            "two_vehicle": True,
            "vehicle_entries": [
                {"vehicle": "新坡92", "mileage": "100"},
                {"vehicle": "新坡93", "mileage": "220"},
            ],
        }

        impact = analyze_task_edit(previous, current)

        self.assertEqual(impact["changed_labels"], ["第 2 車里程"])
        self.assertEqual(changed_site_keys(impact), {"vehicle_mileage"})
        self.assertEqual(
            impact["affected_sites"]["vehicle_mileage"]["vehicle_keys"],
            ["新坡93"],
        )
        self.assertEqual(
            impact["site_summaries"],
            ["里程（只重登第 2 車）"],
        )

    def test_case_address_affects_work_and_mileage(self):
        impact = analyze_task_edit(
            {"case_address": "桃園市龍潭區 A 路"},
            {"case_address": "桃園市龍潭區 B 路"},
        )

        self.assertEqual(
            changed_site_keys(impact),
            {"duty_work_log", "vehicle_mileage"},
        )

    def test_driver_affects_work_mileage_and_enabled_fuel(self):
        previous = {
            "vehicle": "新坡92",
            "driver": "王小明",
            "fuel_record": {"enabled": True},
        }
        current = {
            "vehicle": "新坡92",
            "driver": "陳小華",
            "fuel_record": {"enabled": True},
        }

        impact = analyze_task_edit(previous, current)

        self.assertEqual(
            changed_site_keys(impact),
            {"duty_work_log", "vehicle_mileage", "fuel_record"},
        )

    def test_normalized_equivalent_values_create_no_impact(self):
        previous = {
            "vehicle": "新坡92",
            "consumables": {"紗布": 2, "口罩": 1},
            "disinfection_items": ["車內", "器材"],
        }
        current = {
            "vehicle": " 新坡92 ",
            "consumables": {"口罩": "1", "紗布": "2"},
            "disinfection_items": ["器材", "車內"],
        }

        impact = analyze_task_edit(previous, current)

        self.assertEqual(impact["changed_labels"], [])
        self.assertEqual(impact["affected_sites"], {})
        self.assertEqual(impact["site_summaries"], [])

    def test_adding_second_vehicle_affects_only_the_added_vehicle_sites(self):
        previous = {
            "vehicle_entries": [
                {"vehicle": "新坡92", "driver": "王小明", "mileage": "100"},
            ],
        }
        current = {
            "two_vehicle": True,
            "vehicle_entries": [
                {"vehicle": "新坡92", "driver": "王小明", "mileage": "100"},
                {"vehicle": "新坡93", "driver": "陳小華", "mileage": "200"},
            ],
        }

        impact = analyze_task_edit(previous, current)

        self.assertEqual(
            changed_site_keys(impact),
            {"duty_work_log", "vehicle_mileage", "consumables", "disinfection"},
        )
        for site_key in changed_site_keys(impact):
            self.assertEqual(
                impact["affected_sites"][site_key]["vehicle_keys"],
                ["新坡93"],
            )


if __name__ == "__main__":
    unittest.main()
