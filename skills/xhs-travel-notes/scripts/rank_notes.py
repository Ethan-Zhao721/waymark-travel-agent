"""Render the Xiaohongshu notes a researcher actually read. No network calls."""
import argparse
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

CATEGORIES = ("攻略", "景点", "青旅", "美食")
DETAILS = ("地点", "交通", "价格", "时间", "体验")
METHODS = {"original": "已读原帖", "provided": "用户提供正文"}


def identify(url):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password:
        return None, False
    if parts.hostname in {"xiaohongshu.com", "www.xiaohongshu.com", "www.rednote.com"}:
        match = re.fullmatch(r"/(?:zh/|en/)?(?:explore|discovery/item|search_result)/([0-9a-fA-F]{24})/?", parts.path)
        if match:
            return match[1].lower(), True
    if parts.hostname == "xhslink.com" and re.fullmatch(r"/[a-zA-Z0-9/_-]+", parts.path):
        return url.split("?")[0], False
    return None, False


def scoring(note):
    when = note.get("published")
    try:
        day = date.fromisoformat(when).toordinal()
    except (ValueError, TypeError):
        day = 0
    likes = note.get("likes")
    return (len(note["evidence"]), day, likes if type(likes) is int and likes >= 0 else -1)


PRICE_UNITS = {
    "美食": ("人均", "单品", "整单"),
    "青旅": ("床位/晚",),
    "景点": ("门票",),
    "攻略": ("行程/人",),
}


def render_places(data, limit):
    groups = {category: defaultdict(dict) for category in CATEGORIES}
    for note in data["notes"]:
        key, resolved = identify(note["url"])
        if not resolved or note.get("access") not in METHODS:
            continue
        for place in note.get("places", []):
            category = place["category"]
            name = place["name"].strip()
            if not name or not place.get("detail", "").strip():
                continue
            price = place.get("price")
            if price and (price["unit"] not in PRICE_UNITS[category] or not price["amount"].strip()):
                raise ValueError(f"请核对{name}的价格和单位")
            groups[category][name].setdefault(key, (place, note))

    lines = [f'# {data["city"]} · 小红书旅行清单', "", f'{data["checked"]} 整理。价格以作者当次记录为准。', ""]
    for category, places in groups.items():
        rows = []
        for name, sources in places.items():
            entries = list(sources.values())
            units = PRICE_UNITS[category]
            def price_order(entry):
                price = entry[0].get("price")
                return units.index(price["unit"]) if price else len(units)
            place, primary = min(entries, key=price_order)
            authors = {n["author"] for _, n in entries if n.get("author")}
            links = [f'[原帖]({primary["url"]})']
            secondary = next((n for _, n in entries if n.get("author") and n["author"] != primary.get("author")), None)
            if secondary:
                links.append(f'[对照]({secondary["url"]})')
            price = place.get("price")
            label = ""
            if price:
                label = f' · {price["unit"]} {price["amount"]}'
                if price.get("condition"):
                    label += f'（{price["condition"]}）'
            row = f'**{name}** · {place["detail"]}{label}。' + " ".join(links)
            rows.append((len(authors), price_order((place, primary)), row))
        rows.sort(key=lambda r: (-r[0], r[1]))
        lines += [f"## {category}", ""]
        lines.extend(f"{i}. {row}" for i, (_, _, row) in enumerate(rows[:limit], 1))
        if not rows:
            lines.append("本轮暂无已核实的选项。")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render(data, limit=None):
    if data.get("format") == "places" or any("places" in n for n in data["notes"]):
        return render_places(data, 4 if limit is None else limit)
    if limit is None:
        limit = 10
    ranked, pending = {}, {}
    for item in data["notes"]:
        note = dict(item)
        key, resolved = identify(note["url"])
        if not key or note["category"] not in CATEGORIES:
            raise ValueError("需要小红书原帖或分享链接，以及有效分类")
        note["evidence"] = {k: v.strip() for k, v in (note.get("evidence") or {}).items()
                            if k in DETAILS and isinstance(v, str) and v.strip()}
        if (resolved and note.get("access") in METHODS and note["evidence"]
                and note.get("title") and note.get("summary")):
            if key not in ranked or scoring(note) > scoring(ranked[key]):
                ranked[key] = note
        else:
            pending.setdefault(key, note)
    groups = defaultdict(list)
    for note in ranked.values():
        groups[note["category"]].append(note)
    lines = [f'# {data["city"]} · 小红书旅行笔记', '',
             f'{data["checked"]} 整理。本次可排名 {len(ranked)} 篇。', '']
    for category in CATEGORIES:
        lines += [f'## {category}', '', data.get("queries", {}).get(category) or "本轮未检索此分类。", '']
        notes = sorted(groups[category], key=scoring, reverse=True)
        for i, note in enumerate(notes[:limit], 1):
            who = ' · '.join(filter(None, [note.get("author"), METHODS[note["access"]]]))
            lines += [f'{i}. [{note["title"]}]({note["url"]}) · {who}。{note["summary"]}',
                      '   ' + ' / '.join(note["evidence"].values()), '']
        if not notes:
            lines += ["暂无已读且可排名的笔记。", ""]
    remaining = [n for k, n in pending.items() if k not in ranked]
    if remaining:
        lines += ['## 待核对的原帖链接', '']
        for n in remaining:
            lines.append(f'- {n["category"]} · [{n.get("title") or "原帖链接"}]({n["url"]})')
    return '\n'.join(lines).rstrip() + '\n'


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--limit", type=int, choices=range(1, 11), default=None)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    print(render(data, args.limit), end="")
