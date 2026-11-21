"""Search planning and report regression tests with independent, fictional destinations."""
from datetime import date
import json
import sqlite3
import unittest

from collect_city import plan_activities
from city_report import report


def source(key, author, text):
    return dict(id=key, author=author, text=text)


class DiscoveryTests(unittest.TestCase):
    def test_author_defined_routes_and_local_activity(self):
        notes = [source("a", "one", "Day 1 · 旧仓库漫步\n蓝镜湖：坐船。傍晚可以看日落。")]
        result = plan_activities(notes, [])
        queries = {q["kind"]: q["query"] for q in result["next"]}
        self.assertIn("旧仓库漫步", queries["routes"])
        self.assertIn("蓝镜湖", queries["experiences"])
        self.assertIn("日落", queries["experiences"])
        for group in result["leads"].values():
            for lead in group:
                for evidence in lead["evidence"]:
                    self.assertIn(evidence["quote"], notes[0]["text"])
        self.assertEqual(plan_activities(notes, result["next"])["next"], [])

    def test_unknown_local_experience_is_discovered_from_prose(self):
        result = plan_activities([source("a", "one", "特色体验：玻璃吹制作画、河畔听评弹。")], [])
        self.assertEqual({lead["term"] for lead in result["leads"]["experiences"]},
                         {"玻璃吹制作画", "河畔听评弹"})

    def test_separate_locations_and_negation_stay_unconfirmed(self):
        notes = [source("a", "one", "青镜湖。\n西湾看不到日落。")]
        result = plan_activities(notes, [])
        follow = next(q for q in result["next"] if q["kind"] == "experiences")
        self.assertNotIn("青镜湖", follow["query"])
        evidence = result["leads"]["experiences"][0]["evidence"][0]
        self.assertIn("看不到日落", evidence["quote"])

    def test_independent_authors_and_city_isolation(self):
        notes = [source("a", "one", "小圈"), source("b", "one", "小圈")]
        result = plan_activities(notes, [])
        self.assertEqual(result["leads"]["routes"][0]["authors"], 1)
        notes.append(source("c", "two", "小圈"))
        self.assertNotIn("routes", [q["kind"] for q in plan_activities(notes, [])["next"]])
        other = plan_activities([source("x", "two", "这次只写住宿")], [])
        self.assertFalse(any(other["leads"].values()))
        self.assertNotIn("小圈", str(other))

    def test_report_keeps_distinct_items_and_flags_only_evidenced_omissions(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("create table notes(id text, city text, source text, result text)")
        names = ["步行线甲", "步行线乙", "步行线丙", "步行线丁", "步行线戊"]
        src = dict(city="甲城", title="玩法", body="、".join(names + ["青镜湖"]),
                   author_id="a", date="2026-11-01", url="https://www.xiaohongshu.com/explore/"+"a"*24,
                   comments=[], comments_mode="unverified", activity_leads=["青镜湖", "不存在的湖"])
        items = [dict(dimension="游览线路", name=name, area=None, source="body", reason="行走记录",
                      stance="信息", price=None, update=None, evidence=name) for name in names]
        db.execute("insert into notes values (?,?,?,?)",
                   ("a", "甲城", json.dumps(src), json.dumps(dict(ad="organic", items=items, note_id="a"*24))))
        text = report(db, "甲城", date(2026,11,21), {})
        self.assertNotIn("未覆盖", text)
        self.assertNotIn("## 交通", text)
        self.assertNotIn("待核实", text)
        text = report(db, "甲城", date(2026,11,21), {}, evidence=True)
        verified, pending = text.split("## 原帖已提到、仍待核实的玩法")
        for name in names:
            self.assertIn(name, verified)
        self.assertNotIn("青镜湖", verified)
        self.assertIn("青镜湖", pending)
        self.assertNotIn("不存在的湖", text)
