from unittest import TestCase
from unittest.mock import patch
from source_policy import sources, international_snapshot
import server


class SourcePolicyTests(TestCase):
    def test_regional_sources(self):
        self.assertIn("Tabelog 食べログ", sources("JP")["food"])
        self.assertNotIn("Tabelog 食べログ", sources("GB")["food"])
        self.assertTrue(any("大众点评" in s for s in sources("CN")["food"]))
        self.assertTrue(any("Tripadvisor" in s for s in sources("KH")["sights"]))

    def test_overseas_metrics_need_same_source_link_rating_and_count(self):
        url = "https://www.google.com/maps?cid=22"
        place = dict(name="A", category="饭店", sources=[dict(url=url)],
                     signals=[dict(platform="google", url=url, rating=4.7, reviews=301)])
        missing = dict(place, name="B", signals=[dict(platform="google", url=url, rating=4.8)])
        mismatched = dict(place, name="C", sources=[dict(url="https://www.example.com")])
        guide = dict(checked="2026-10-01", places=[place, missing, mismatched])
        with patch.object(server,"get_cached",return_value=guide):
            snap = international_snapshot(dict(id="sample",countryCode="JP"))
        self.assertEqual(snap["checked"], guide["checked"])
        self.assertEqual([(r["name"],r["google"]["reviews"]) for r in snap["places"]], [("A",301)])

    def test_mainland_guide_does_not_require_google(self):
        seed = dict(id="pond",category="景点",name="水塘公园",englishName="Park",address="中路1号",
                    url="https://www.xiaohongshu.com/explore/"+"a"*24,
                    boardSources=[dict(platform="高德",order="原榜名次",position=3,url="https://www.amap.com/place/B123")])
        city = dict(id="sample",countryCode="CN",latitude=25,longitude=107)
        guide = server.validate(server.verify_candidates(city,{"pond":seed}),city)
        self.assertEqual(guide["places"][0]["name"],seed["name"])
        with self.assertRaises(ValueError):
            server.validate(guide,dict(city,countryCode="JP"))
