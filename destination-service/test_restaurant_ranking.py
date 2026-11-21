import copy
import json
import unittest
from pathlib import Path
from guide_validation import validate
from restaurant_ranking import paired_shortlist, shortlist
from unittest.mock import patch
import server


class RestaurantRankTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.otaru = json.loads((Path(__file__).parent / "guides/2128574.json").read_text())
        cls.city = {"id": "2128574", "latitude": 43.18944, "longitude": 141.00222}

    def google(self, key, score, count):
        row = copy.deepcopy(self.otaru["places"][0])
        row.update(id=str(key), name=str(key), englishName=str(key), address=str(key))
        url = "https://www.google.com/maps?cid=" + str(key)
        row["sources"] = [{"name": "Google Maps", "url": url}]
        row["signals"] = [{"platform": "google", "rating": score, "reviews": count, "url": url}]
        return row

    def test_threshold_order_and_limit(self):
        inputs = [self.google(1, 4.5, 90000), self.google(2, 4.9, 299), self.google(3, 4.6, 300)]
        self.assertEqual([p["id"] for p in shortlist(inputs)], ["3"])
        inputs += [self.google(n, 4.6, 500+n) for n in range(4, 12)]
        result = sorted(shortlist(inputs), key=lambda p: p["signals"][0]["rank"])
        self.assertEqual([p["id"] for p in result], ["11","10","9","8","7","6"])

    def test_merge_branch_not_brand_or_shared_note(self):
        row = copy.deepcopy(self.otaru["places"][0])
        left, right = copy.deepcopy(row), copy.deepcopy(row)
        left["signals"] = [s for s in row["signals"] if s["platform"] == "google"]
        right.update(id="translated", signals=[s for s in row["signals"] if s["platform"] == "xhs"])
        other = dict(right, id="other-branch", address="different street number")
        result = shortlist([left, right, other])
        self.assertEqual([len(p["signals"]) for p in result], [2,1])

    def test_merged_price_uses_one_source(self):
        row = copy.deepcopy(self.otaru["places"][0])
        row["usdPerPerson"] = 45
        other = dict(row, usdPerPerson=None, priceTier=None, priceNote="套餐价格", priceURL=row["signals"][0]["url"])
        restaurant, = shortlist([row, other])
        self.assertEqual(restaurant["priceURL"], other["priceURL"])
        self.assertIsNone(restaurant["usdPerPerson"])

    def test_new_city_price_range(self):
        city = dict(id="new-city", latitude=13.36, longitude=103.86)
        row = self.google(99, 4.8, 500)
        source = row["sources"][0]["url"]
        span = dict(low=6, high=12, meal="lunch")
        row.update(latitude=city["latitude"], longitude=city["longitude"], priceURL=source,
                   priceNote=None, perPersonUSDRange=span)
        def price(**changes):
            guide = dict(self.otaru, cityID=city["id"], places=[dict(row, **changes)])
            return validate(guide, city)["places"][0]["perPersonUSDRange"]
        self.assertEqual(price(), span)
        self.assertIsNone(price(perPersonUSDRange=dict(span, high=2)))
        self.assertIsNone(price(priceURL=None))

    def test_sources_prices_and_legacy(self):
        row = self.google(11, 5, 999)
        row["signals"][0]["url"] = "https://example.com/maps?cid=11"
        self.assertEqual(shortlist([row]), [])
        row = self.google(10, 4.6, 400)
        guide = validate(dict(self.otaru, places=[row]), self.city)
        self.assertIsNone(guide["places"][0]["priceNote"])
        self.assertEqual(shortlist([dict(row, signals=None)]), [])

    def paired(self, index, category="饭店", score=4.7, reviews=600):
        row = self.google(index, score, reviews)
        note = "https://www.xiaohongshu.com/explore/" + format(index, "024x")
        row["category"] = category
        row["sources"].append(dict(name="小红书", url=note))
        row["signals"].append(dict(platform="xhs", url=note))
        return row

    def test_paired_categories_and_cutoffs(self):
        rows = [self.paired(i, reviews=500+i) for i in range(1, 13)]
        rows += [self.paired(30+i, "景点", 4.3, 10+i) for i in range(12)]
        rows += [self.google(100, 4.9, 99999), self.paired(101, score=4.5),
                 self.paired(102, reviews=299), dict(rows[11], id="duplicate")]
        kept = paired_shortlist(rows)
        for category, first in (("饭店","12"), ("景点","41")):
            places = [p for p in kept if p["category"] == category]
            self.assertEqual(len(places), 10)
            self.assertEqual(places[0]["id"], first)
            self.assertEqual([p["signals"][1]["rank"] for p in places], list(range(1, 11)))

    def test_two_stage_evidence_and_branch_matching(self):
        rows = [self.paired(i) for i in range(1, 5)]
        seeds = [{k: r[k] for k in ("category","name","englishName","address","id")}
                 | {"url":r["sources"][1]["url"]} for r in rows]
        confirmed_notes = {server.public_url(s["url"]) for s in seeds[:3]}
        google_urls = {server.public_url(r["sources"][0]["url"]) for r in rows[:2]}
        rows[1]["address"] = "other branch"
        answers = [({"places":seeds}, confirmed_notes), ({"places":rows}, google_urls)]
        with patch.dict(server.os.environ, GOOGLE_PLACES_API_KEY=""), patch.object(server, "search_web", side_effect=answers):
            found = server.discover_candidates(self.city)
            result = server.verify_candidates(self.city, found)
        self.assertEqual(set(found), {"1", "2", "3"})
        places = validate(result, self.city)["places"]
        self.assertEqual([p["id"] for p in places], ["1"])
        self.assertEqual(len(places[0]["signals"]), 2)

    def test_google_outage_does_not_replace_last_guide(self):
        seed = dict(id="park", category="景点", name="Park", englishName="Park",
                    address="21 First Street", url="https://www.xiaohongshu.com/explore/" + "a"*24)
        with patch.dict(server.os.environ, GOOGLE_PLACES_API_KEY=""), patch.object(server, "search_web", side_effect=TimeoutError):
            with self.assertRaises(TimeoutError):
                server.verify_candidates(self.city, {"park": seed})

    def test_rating_floor_for_both_categories(self):
        rows = [self.paired(1, score=4.6, reviews=300), self.paired(2, score=4.5),
                self.paired(3, "景点", 4.5, 10000), self.paired(4, "景点", 4.6, 15),
                self.paired(5, score=4.9, reviews=299), self.paired(6)]
        rows[-1]["signals"] = rows[-1]["signals"][1:]
        for row in rows:
            row.update(priceTier=None, usdPerPerson=None)
        accepted = validate(dict(self.otaru, rankingVersion=4, places=rows), self.city)["places"]
        self.assertEqual({r["id"] for r in accepted}, {"1", "4"})

    def test_direct_google_lookup_checks_branch_before_rating(self):
        from io import BytesIO
        city = dict(self.city, countryCode="JP", name="Otaru")
        seed = self.paired(11)
        seed.update(name="Shop", address="小樽市堺町2-18")
        row = dict(displayName={"text": "Shop"}, formattedAddress="北海道小樽市堺町2丁目18",
                   addressComponents=[dict(types=["country"], shortText="JP")],
                   googleMapsUri="https://maps.google.com/?cid=123", rating=4.7, userRatingCount=327,
                   location={"latitude":city["latitude"], "longitude":city["longitude"]})
        wrong = dict(row, formattedAddress="北海道小樽市堺町21-8")
        def lookup(rows):
            response = BytesIO(json.dumps({"places": rows}).encode())
            with patch.object(server, "urlopen", return_value=response):
                return server.lookup_google_place(city, seed, "test")
        self.assertEqual(lookup([wrong, row]), row)
        self.assertIsNone(lookup([wrong]))
        self.assertIsNone(lookup([row, dict(row, googleMapsUri="https://maps.google.com/?cid=456")]))

    def test_resource_round_trip_and_independent_platforms(self):
        guide = validate(self.otaru, self.city)
        self.assertEqual(len(guide["places"]), 11)
        self.assertTrue(all(p["priceNote"] and p["priceURL"] for p in guide["places"]))
        for provider, count in [("xhs",6),("google",6),("dianping",0)]:
            self.assertEqual(sorted(s["rank"] for p in guide["places"] for s in p["signals"] if s["platform"] == provider), list(range(1,count+1)))
        dp = self.google(99,4.6,300)
        url = "https://www.dianping.com/shop/12345"
        dp["sources"] = [dict(name="大众点评",url=url)]
        dp["signals"] = [dict(platform="dianping", url=url, reviews=238,rating=4.4)]
        self.assertEqual(shortlist([dp])[0]["signals"][0]["rank"],1)


if __name__ == "__main__":
    unittest.main()
