import unittest

from ambulance_bot.mileage_address import disaster_mileage_addresses_match


class DisasterMileageAddressTests(unittest.TestCase):
    def assert_addresses_match(self, left, right, expected):
        for first, second in ((left, right), (right, left)):
            with self.subTest(left=first, right=second):
                self.assertIs(expected, disaster_mileage_addresses_match(first, second))

    def test_known_disaster_address_allows_missing_administration_or_landmark(self):
        variants = ["桃園市觀音區新生路552巷內土地公廟對面",
                    "觀音區新生路552巷", "新生路552巷內土地公廟對面"]
        for index, left in enumerate(variants):
            for right in variants[index:]:
                self.assert_addresses_match(left, right, True)

    def test_equivalent_address_formatting_and_numbers(self):
        pairs = [
            (" 桃園市，觀音區 新生路５５２巷（土地公廟對面） ", "桃園市觀音區新生路五百五十二巷"),
            ("臺中市西屯區臺灣大道二段十二巷", "台中市西屯區台灣大道2段12巷內"),
            ("觀音區中山路一段１０之２號", "中山路1段十之二號"),
            ("新生路10號之2", "新生路１０－２號"),
            ("新生路552巷2弄十三號前", "新生路552巷2弄13號"),
            ("中山一路30號", "中山1路三十號"),
            ("新竹縣竹北市中正路123巷", "竹北市中正路123巷"),
            ("桃園市新生路552巷", "桃園市觀音區新生路552巷"),
            ("新生路552巷內土地公廟對面", "新生路552巷土地公廟旁"),
            ("新生路552巷", "新生路552巷內約200公尺"),
            ("新生路552巷", "新生路552巷2弄13號"),
            ("光明六路東一段123巷5號", "光明6路東1段123巷5號"),
        ]
        for left, right in pairs:
            self.assert_addresses_match(left, right, True)

    def test_explicit_address_components_must_not_conflict(self):
        pairs = [
            ("桃園市觀音區新生路552巷", "桃園市中壢區新生路552巷"),
            ("桃園市觀音區新生路552巷", "台中市觀音區新生路552巷"),
            ("新生路552巷", "新生街552巷"),
            ("新生路552巷", "新興路552巷"),
            ("中山東路12號", "中山西路12號"),
            ("中山一路12號", "中山二路12號"),
            ("新生路1段552巷", "新生路2段552巷"),
            ("新生路1段552巷", "新生路552巷"),
            ("新生路552巷", "新生路5521巷"),
            ("新生路552巷", "新生路552號"),
            ("新生路552巷8弄", "新生路552巷9弄"),
            ("新生路552巷12號", "新生路552巷120號"),
            ("新生路552巷12號", "新生路552巷12之1號"),
            ("新生路552巷12之1號", "新生路552巷12之2號"),
            ("新生路552巷3弄12號", "新生路552巷12號"),
            ("新生路552巷3弄", "新生路552巷12號"),
            ("新生路552巷3弄", "新生路3弄"),
            ("新生路552巷內福德祠旁", "新生路552巷內媽祖廟旁"),
            ("新生路552巷1、3號", "新生路552巷13號"),
            ("新生路五、五二號", "新生路552號"),
        ]
        for left, right in pairs:
            self.assert_addresses_match(left, right, False)

    def test_insufficient_or_ambiguous_addresses_are_not_matches(self):
        for address in ("", " ", "轄內", "轄內A3", "觀音區", "桃園市觀音區",
                        "新生路", "桃園市觀音區新生路", "新生路1段", "552巷", "土地公廟對面",
                        "新生路十十號", "新生路552巷十十號"):
            self.assert_addresses_match(address, address, False)
            self.assert_addresses_match(address, "桃園市觀音區新生路552巷", False)
