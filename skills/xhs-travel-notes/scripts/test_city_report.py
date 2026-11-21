"""Regression checks for city isolation, source attribution and author voting."""
import copy
from datetime import date
import json
import sqlite3
import unittest

from city_report import aggregate, prepare, summarize, validate

DAY = date(2026, 11, 21)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.source = dict(city="甲城", url="https://www.rednote.com/discovery/item/" + "a"*24,
                           date="2026-08-01", author_id="author", title="实住", body="房间安静，订价200元",
                           comments_mode="all", comment_total=1, comments=[
                               dict(id="c1", author_id="author", basis="author", date="2026-08-02",
                                    text="更正，当次是240元", likes=2)])
        self.item = dict(dimension="酒店", name="东店", area=None, stance="推荐", reason="安静",
                         source="body", update=None, evidence="房间安静",
                         price=dict(label="订价 ¥200", quote="订价200元"))
        self.correction = dict(self.item, source="c1", stance="信息", reason="作者更正价格",
                               evidence="更正，当次是240元", update="price",
                               price=dict(label="订价 ¥240", quote="240元"))
        self.result = dict(note_id="a"*24, ad="organic", ad_reason="人工样本",
                           items=[self.item, self.correction])

    def test_attribution(self):
        key, src = prepare(self.source)
        validate(key, src, self.result)
        self.correction["price"]["quote"] = "180元"
        with self.assertRaises(ValueError):
            validate(key, src, self.result)
        self.source["comments_mode"] = "unverified"
        _, src = prepare(self.source)
        self.assertEqual(src["comments"], [])
        with self.assertRaises(ValueError):
            validate(key, src, self.result)

    def test_top_twenty(self):
        source = dict(self.source, comment_total=25, comments=[
            dict(self.source["comments"][0], id=str(i), likes=i) for i in range(25)])
        _, src = prepare(source)
        self.assertEqual([c["likes"] for c in src["comments"]], list(range(24,4,-1)))

    def test_aggregate(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("create table notes(id text, city text, source text, result text)")
        def add(char, city="甲城", author="author", name="东店", when="2026-08-01", ad="organic", vote="推荐"):
            source = dict(self.source, date=when, city=city, author_id=author)
            result = copy.deepcopy(self.result)
            result.update(note_id=char*24, ad=ad)
            result["items"][0].update(stance=vote, name=name)
            if char != "a":
                result["items"].pop()
            db.execute("insert into notes values(?,?,?,?)", (char, city, json.dumps(source), json.dumps(result)))
        add("a")
        add("b", name="东店别名")
        add("c", author="b", vote="避雷")
        add("d", author="c", ad="ad")
        add("e", author="d", when="2025-11-20")
        add("f", city="乙城")
        add("g", name="西店")
        grouped, stats = aggregate(db, "甲城", DAY, {"甲城|酒店|东店别名": "东店"})
        self.assertEqual(len(grouped), 2)
        _, line = summarize(grouped["酒店", "东店"])
        for token in ("提及 2 人", "推荐 1 / 避雷 1", "有争议", "¥240", "2026-08-02"):
            self.assertIn(token, line)
        self.assertNotIn("¥200", line)
        self.assertEqual((stats["可能过期"], stats["广告或商业待核"]), (1,1))


if __name__ == "__main__":
    unittest.main()
