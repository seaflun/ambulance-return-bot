import unittest

from ambulance_bot.consumables import consumable_inventory_options


class ConsumableInventoryTests(unittest.TestCase):
    def test_common_consumables_are_listed_first_in_requested_order(self):
        names = [item["name"] for item in consumable_inventory_options()]

        self.assertEqual(
            names[:25],
            [
                "桃-血糖試紙(片)",
                "桃-安全型採血針(支)",
                "桃-可拋棄式耳溫槍耳套-福爾TD-1118(個)",
                "桃-心電圖電極貼片(片)",
                "桃-拋棄式CPR回饋貼片(組)",
                "桃-鼻管(條)",
                "桃-成人氧氣面罩(個)",
                "桃-成人非再呼吸型面罩(組)",
                "桃-成人甦醒球(組)",
                "桃-連接管-長管(條)",
                "桃-非充氣聲門上呼吸道-3號(組)",
                "桃-非充氣聲門上呼吸道-4號(組)",
                "桃-非充氣聲門上呼吸道-5號(組)",
                "桃-細菌過濾器(組)",
                "桃-酒精棉片(片)",
                "桃-18號防回血IC針(支)",
                "桃-20號防回血IC針(支)",
                "桃-22號防回血IC針(支)",
                "桃-24號防回血IC針(支)",
                "桃-免針型輸液套(組)",
                "桃-透明敷料op site(片)",
                "桃-15mm拋棄式骨內血管穿刺針具(組)",
                "桃-25mm拋棄式骨內血管穿刺針具(組)",
                "桃-45mm拋棄式骨內血管穿刺針具(組)",
                "桃-10ml預充式導管沖洗器(支)",
            ],
        )

    def test_gauze_package_item_uses_trauma_category(self):
        options = consumable_inventory_options()
        gauze = next(item for item in options if item["name"] == "桃-4吋紗布塊(包)")

        self.assertEqual(gauze["category"], "創傷類")


if __name__ == "__main__":
    unittest.main()
