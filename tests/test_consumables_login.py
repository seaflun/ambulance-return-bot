import unittest

from consumables_login import _case_id_sid_fragments, _consumable_sid_score, _emm_temsis_id_from_href


class ConsumablesLoginTests(unittest.TestCase):
    def test_case_id_fragments_match_consumable_sid(self):
        self.assertEqual(_case_id_sid_fragments("20260602011652012"), ["011652"])
        self.assertEqual(_consumable_sid_score("20260602011652012", "20260602101003011652"), 10)

    def test_extracts_emm_temsis_id_from_href(self):
        href = "/ACS/ACS15002?emmTemsisid=2026060210100301165202"
        self.assertEqual(_emm_temsis_id_from_href(href), "2026060210100301165202")


if __name__ == "__main__":
    unittest.main()
