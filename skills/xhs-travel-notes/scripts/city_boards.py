"""Read public citywide lists. A Dianping display position is not an editorial rank."""
import argparse
from datetime import date
from html import unescape
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
import subprocess

AMAP = "https://www.amap.com/ranking"
DIANPING = "https://plat.dianping.com/app/femember-musteat-web/musteat-rank"
CACHE = Path.home() / "Library/Caches/Waymark/city-boards"


def page(url):
    try:
        return subprocess.run(["curl", "-fsSL", "--max-time", "12", "--max-filesize", "3000000", url],
                              check=True, capture_output=True).stdout.decode("utf-8")
    except subprocess.CalledProcessError as exc:
        raise OSError(f"公开榜页读取失败 (curl {exc.returncode})") from exc


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(f.name, path)


def norm(s):
    return "".join(c for c in unicodedata.normalize("NFKC", s).casefold() if c.isalnum())


def amap(city, slug, category):
    url = f"{AMAP}/{slug}/{category}"
    html = page(url)
    for script in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        data = json.loads(script)
        if data.get("@type") == "ItemList":
            break
    else:
        raise ValueError("榜单未公开")
    title = data.get("name", "")
    kind = "美食" if category == "food" else "景点"
    if not title.startswith(city) or not title.endswith(kind):
        raise ValueError("城市或类别不匹配")
    cards = {}
    for link, card in re.findall(r'<a href="(/place/[A-Za-z0-9]+)" class="poi-card"[^>]*>(.*?)</a>', html, re.S):
        name = re.search(r'class="poi-name">([^<]+)', card)
        desc = re.search(r'💡.*?class="detail-text"[^>]*>\s*([^<]+)', card, re.S)
        if name:
            cards[unescape(name[1]).strip()] = ("https://www.amap.com"+link, unescape(desc[1]).strip() if desc else "")
    items = []
    for item in sorted(data["itemListElement"], key=lambda row: row["position"])[:10]:
        name = item["item"]["name"]
        if item["position"] != len(items)+1 or name not in cards:
            raise ValueError("榜单位置或地点链接不完整")
        link, detail = cards[name]
        items.append(dict(name=name, position=item["position"], url=link, detail=detail))
    if not items:
        raise ValueError("空榜")
    return dict(title=title, source="高德", order="原榜名次", url=url, items=items)


def dianping(city, city_id):
    url = f"{DIANPING}?cityid={int(city_id)}&ranktype=3"
    html = page(url)
    marker = "window.__K2_INITIAL_STATE__="
    if marker not in html:
        raise ValueError("页面未提供榜单")
    state, _ = json.JSONDecoder().raw_decode(html.split(marker,1)[1].lstrip())
    base, filters, shops = (state[k] for k in ("baseInfo","filterState","shopInfo"))
    if (base["cityName"] != city or filters["regionName"] != "全城" or
            filters["categoryName"] != "全部美食" or filters["sortName"] != "综合排序"):
        raise ValueError("城市、范围或排序不符")
    by_id = {r["shopId"]:r for r in shops["shopList"] + (shops.get("remainingShopList") or [])}
    items = []
    for position, key in enumerate(shops["shopIds"][:10], 1):
        if key not in by_id:
            break
        row = by_id[key]
        items.append(dict(name=row["shopName"], position=position, url=f"https://www.dianping.com/shop/{key}",
                          detail=" ".join(row.get("recommendReasonList") or []),
                          rating=row.get("fiveScore"), price=row.get("price")))
    if not items:
        raise ValueError("无可读门店")
    return dict(title=f'{city}{base["year"]}大众点评必吃榜', source="大众点评", order="全城综合排序展示位置",
                url=url, items=items, city_id=city_id)


def boards(city, categories, dp_id=None, cache=CACHE):
    import hashlib
    folder = cache / hashlib.sha256(city.encode()).hexdigest()[:24]
    day = date.today().isoformat()
    routing = folder / "routing.json"
    route = json.loads(routing.read_text()) if routing.exists() else {}
    results, errors = {}, {}
    for key in categories:
        path = folder / (day + "-" + key + ".json")
        try:
            if path.exists():
                saved = json.loads(path.read_text())
                if key != "dianping" or dp_id is None or saved.get("city_id") == dp_id:
                    results[key] = saved
                    continue
            if key == "dianping":
                ident = dp_id or route.get("dp_id")
                if not ident:
                    raise ValueError("尚未定位本城大众点评官方榜页")
                board = dianping(city, ident)
                route["dp_id"] = ident
            else:
                if not route.get("slug"):
                    names = dict(re.findall(r"'([^'\n]+)'\s*:\s*'([a-z0-9-]+)'", page(AMAP)))
                    if city not in names:
                        raise ValueError("高德城市目录未收录")
                    route["slug"] = names[city]
                board = amap(city, route["slug"], key)
            save(path, board)
            save(routing, route)
            results[key] = board
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors[key] = str(exc)[:140]
    return dict(city=city, checked=day, boards=results, unavailable=errors)


def shortlist(snapshot):
    pool = {}
    for key, board in snapshot["boards"].items():
        for row in board["items"]:
            category = "玩法" if key == "scenic" else "美食"
            # Only merge cross-platform branches when their complete name, including branch, agrees.
            branch_named = re.search(r"[（(].+?[)）]", row["name"])
            identity = (category, norm(row["name"]), "" if branch_named else row["url"])
            item = pool.setdefault(identity, dict(name=row["name"], topic=category, sources=[]))
            item["sources"].append(dict(platform=board["source"], position=row["position"], order=board["order"], url=row["url"]))
    return sorted(pool.values(), key=lambda r:(-len(r["sources"]), min(s["position"] for s in r["sources"])))


def comparison(snapshot):
    from itertools import zip_longest
    from urllib.parse import urlsplit
    lines = [f"# {snapshot['city']}餐厅双榜", "", f"核对日期 {snapshot['checked']}", ""]
    keys = ("food", "dianping")
    lists = []
    for key in keys:
        board = snapshot["boards"].get(key)
        lists.append(board["items"] if board else [])
        if board:
            lines += [f"[{board['title']}]({board['url']}) · {board['order']}，取 {len(board['items'])} 家。", ""]
    if any(lists):
        lines += ["| 位置 | 高德美食 | 大众点评必吃榜 · 综合排序 |", "|---|---|---|"]
        for position, pair in enumerate(zip_longest(*lists), 1):
            cells = [f"[{r['name']}]({r['url']})" if r else "—" for r in pair]
            lines.append(f"| {position} | " + " | ".join(cells) + " |")
    scenic = snapshot["boards"].get("scenic")
    if scenic:
        lines += ["", "## 去哪里", "", f"[{scenic['title']}]({scenic['url']})", ""]
        lines.extend(f"{r['position']}. [{r['name']}]({r['url']})" for r in scenic["items"])
    linked = [c for c in snapshot["candidates"] if c.get("xhs_url")]
    if linked:
        lines += ["", "## 有同名到访原文的地点", ""]
        for c in linked:
            if urlsplit(c["xhs_url"]).hostname == "www.xiaohongshu.com":
                lines.append(f"- **{c['name']}** · [查看原帖]({c['xhs_url']})")
    return "\n".join(lines) + "\n"


def corroboration(candidate, title, body):
    """Return a literal place-specific passage for editorial review."""
    full = candidate["name"]
    name = norm(full)
    match = re.fullmatch(r"(.+?)[（(]([^()（）]+)[）)]", full)
    core = norm(re.split(r"[·•]", match[1])[0]) if match else ""
    branch = norm(re.sub(r"店$", "", match[2])) if match else ""
    clean = re.sub(r"#[^#\n]*?\[话题\]#", "", body)
    if re.search(r"商务合作|本店开业|团购链接|欢迎探店合作", clean):
        return None
    lines = [s.strip() for s in (title + "\n" + clean).splitlines() if s.strip()]
    for i, line in enumerate(lines):
        signature = norm(line)
        # An abbreviated brand must still name the branch; just naming a district is ambiguous.
        branch_match = (len(core) >= 2 and len(branch) >= 2 and core in signature and
                        branch + "店" in signature and len(line) <= 90 and
                        not re.search(r"[；;、]|[店馆][，,].{2,15}店", line))
        if name not in signature and not branch_match:
            continue
        passage = line
        if i+1 < len(lines) and len(line) < 90 and not re.search(r"[（(].*?店[）)]|📍", lines[i+1]):
            passage += "\n" + lines[i+1]
        near = passage[:320]
        if re.search(r"想去|没去|没吃|下次去|收藏待|听说", near):
            continue
        if re.search(r"点了|我点|我们点|吃了|排了|等了|我去|我们去|逛了|走了|拍了|这次吃|我最喜欢", near):
            return near
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("city")
    parser.add_argument("--dianping-city-id", type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    snap = boards(args.city, ["food", "dianping", "scenic"], args.dianping_city_id)
    snap["candidates"] = shortlist(snap)
    save(args.output, snap)
    print({key:len(v["items"]) for key,v in snap["boards"].items()}, snap["unavailable"])
