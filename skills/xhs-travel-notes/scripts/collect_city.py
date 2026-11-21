"""Collect a personal city brief with a separately installed MediaCrawler client."""
import argparse
import asyncio
from datetime import date, datetime
import json
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlencode

from city_report import period, prepare
from city_boards import boards, shortlist, corroboration, comparison

PROJECT = Path(__file__).resolve().parents[3]
HOME = Path.home() / "Library/Application Support/Waymark/MediaCrawler"
TOPICS = json.loads((PROJECT / "Waymark/TravelTopics.json").read_text())
NOTE = re.compile(r"[a-fA-F0-9]{24}")
PRICE = re.compile(r"人均|订的|床位|[¥￥元]|\d+块|百[一二三四五六七八九十]+多")


def seed_queries(topic, keywords, limit):
    queries = [topic["query"], *topic.get("discoveryQueries", [])]
    if keywords:
        return [(keywords, limit or 2)]
    budget = limit if limit is not None else (6 if topic.get("discoveryQueries") else 2)
    queries = queries[:budget]
    quotient, remainder = divmod(budget, len(queries))
    return [(query, quotient + (index < remainder)) for index, query in enumerate(queries)]


def local_date(timestamp):
    return datetime.fromtimestamp(int(timestamp) / 1000).date().isoformat()


def write_json(path, data):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(f.name, path)


def comment_set(rows, count, author):
    unique = {}
    pending = list(rows)
    while pending:
        row = pending.pop()
        pending.extend(row.get("sub_comments") or [])
        cid = row.get("id")
        uid = (row.get("user_info") or {}).get("user_id")
        if cid and uid and row.get("content") and row.get("create_time"):
            likes = str(row.get("like_count", ""))
            unique[cid] = dict(id=cid, author_id=uid, date=local_date(row["create_time"]),
                               text=row["content"], likes=int(likes) if likes.isdecimal() else None,
                               basis="author" if uid == author else "unconfirmed")
    if count is None or len(unique) != count:
        return "unverified", []
    return "all", list(unique.values())


def plan_activities(sources, attempted):
    node = shutil.which("node")
    if not node:
        import playwright
        node = Path(playwright.__file__).parent / "driver/node"
    response = subprocess.run([node, PROJECT / "Waymark/ActivityDiscovery.js"],
                              input=json.dumps(dict(sources=sources, attempted=attempted)),
                              text=True, capture_output=True, check=True)
    return json.loads(response.stdout)


async def collect(args, folder):
    # Import the pinned upstream installation outside the iOS bundle and repository.
    from playwright.async_api import async_playwright
    import config
    from media_platform.xhs import XiaoHongShuCrawler
    from media_platform.xhs.help import get_search_id
    from tools import utils
    utils.logger.setLevel(logging.CRITICAL)
    config.ENABLE_GET_SUB_COMMENTS = True
    topics = [t for t in TOPICS if not args.topic or t["name"] == args.topic]
    board_keys = []
    if not args.keywords:
        if any(t["name"] == "美食" for t in topics):
            board_keys.extend(["food", "dianping"])
        if any(t["name"] == "玩法" for t in topics):
            board_keys.append("scenic")
    sys.path.insert(0, str(PROJECT / "destination-service"))
    from source_policy import city_record, international_snapshot, RULES
    destination = city_record(args.city, args.country)
    country = destination["countryCode"] if destination else args.country
    overseas_places = []
    if country and country != RULES["mainlandRegion"]:
        board_keys = []
        if destination and not args.keywords:
            overseas = international_snapshot(destination)
            overseas_places = overseas["places"]
            write_json(folder / "海外地点核对.json", overseas)
            if overseas["places"]:
                lines = [f"# {args.city} · Google Maps", "", "核对日期 " + overseas["checked"], ""]
                for row in overseas["places"]:
                    g = row["google"]
                    lines.append(f"- **{row['name']}** · {g['rating']:.1f} 分 · {g['reviews']:,} 条评价。[Google Maps]({g['url']})")
                (folder / "海外地点核对.md").write_text("\n".join(lines) + "\n")
    dp_id = args.dianping_city_id or RULES["mainlandDianpingCities"].get(args.city)
    snapshot = boards(args.city, board_keys, dp_id)
    candidates = shortlist(snapshot)
    for candidate in candidates:
        candidate["xhs_status"] = "未检索"
    snapshot["candidates"] = candidates
    board_file = folder / "城市双榜.json"
    def checkpoint_boards():
        write_json(board_file, snapshot)
        (folder / "双榜对照.md").write_text(comparison(snapshot))

    if board_keys:
        checkpoint_boards()
    async with async_playwright() as pw:
        if args.session_dir:
            port = int((args.session_dir / "DevToolsActivePort").read_text().splitlines()[0])
            browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
        else:
            ctx = await pw.chromium.launch_persistent_context(str(HOME / "profile"), channel="chrome", headless=False)
        page = await ctx.new_page()
        notes, seen = [], set()
        packets = folder / "sources"
        packets.mkdir()
        try:
            await page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded", timeout=40000)
            if not args.session_dir:
                first = args.city + " " + (args.keywords or args.topic or TOPICS[0]["query"])
                await page.goto("https://www.xiaohongshu.com/search_result?" + urlencode({"keyword": first}),
                                wait_until="domcontentloaded")
                print("首次使用请在这个独立窗口完成小红书登录，之后会复用该窗口的登录状态。", flush=True)
                await page.locator("section.note-item a.cover").first.wait_for(timeout=180000)
            crawler = XiaoHongShuCrawler()
            crawler.context_page, crawler.browser_context = page, ctx
            client = await crawler.create_xhs_client(None)
            activity_sources, attempted = [], []
            board_sources = {}
            topic_authors = {t["name"]: set() for t in topics}
            async def read_query(topic, query, limit, targeted=False, candidate=None):
                term = args.city + " " + query
                data = await client.get_note_by_keyword(keyword=term, search_id=get_search_id(), page=1)
                if not isinstance(data.get("items"), list):
                    raise RuntimeError("站内搜索未返回结果列表，请检查独立窗口的登录状态")
                added = 0
                rows = data["items"]
                if topic["name"] == "玩法":
                    def overview_score(row):
                        title = (row.get("note_card") or {}).get("display_title") or ""
                        specific = sum(word.lower() in title.lower() for word in query.split() if word not in {"游玩", "实测", "线路", "区别"}) if targeted else 0
                        overview = sum(word in title for word in ("攻略", "天", "路线", "玩法", "合集", "指南"))
                        return specific, overview
                    rows = sorted(rows, key=overview_score, reverse=True)
                if topic.get("discoveryQueries"):
                    known = topic_authors[topic["name"]]
                    rows = sorted(rows, key=lambda row: ((row.get("note_card") or {}).get("user") or {}).get("user_id") in known)
                if candidate:
                    rows = [row for row in rows if row.get("note_card")][:3]
                for row in rows:
                    key = row.get("id", "")
                    if not NOTE.fullmatch(key) or not row.get("note_card") or key in seen or not row.get("xsec_token"):
                        continue
                    token = row["xsec_token"]
                    detail = await client.get_note_by_id(key, row.get("xsec_source") or "pc_search", token)
                    if detail.get("note_id") != key or not detail.get("time"):
                        continue
                    day = local_date(detail["time"])
                    if not period(date.today()).isoformat() <= day <= date.today().isoformat():
                        continue
                    author = detail.get("user") or {}
                    body = detail.get("desc", "").strip()
                    if not author.get("user_id") or not body: continue
                    if topic.get("discoveryQueries") and not args.keywords and not candidate:
                        if author["user_id"] in topic_authors[topic["name"]]:
                            continue
                        plain = re.sub(r"#[^#\n]*?\[话题\]#", "", body).strip()
                        if len(plain) < 18:
                            continue
                    title = detail.get("title") or body[:80]
                    quote = corroboration(candidate, title, body) if candidate else None
                    if candidate and not quote:
                        continue
                    count_str = str((detail.get("interact_info") or {}).get("comment_count", ""))
                    count = int(count_str) if count_str.isdecimal() else None
                    mode, comments = "unverified", []
                    if count == 0:
                        mode = "all"
                    elif not candidate and count and count <= 20 and (topic["name"] in {"青旅", "酒店", "避坑"} or PRICE.search(body)):
                        rows = await client.get_note_all_comments(key, token, crawl_interval=2, max_count=20)
                        mode, comments = comment_set(rows, count, author["user_id"])
                    link = "https://www.xiaohongshu.com/explore/" + key
                    source = dict(city=args.city, url=link, author_id=author["user_id"], author=author.get("nickname",""),
                                  title=title, body=body, date=day, date_basis="原帖发布时间", comments_mode=mode,
                                  comments=comments, comment_total=count, search_query=term,
                                  search_origin="平台榜单核对" if candidate else ("指定关键词" if args.keywords else ("原文补搜" if targeted else "城市盲搜")))
                    prepare(source)
                    write_json(packets / (key+".json"), {"source":source})
                    if candidate:
                        board_sources[key] = source
                        candidate.update(xhs_status="同名到访线索", xhs_url=link, quote=quote, note_date=day)
                    seen.add(key)
                    topic_authors[topic["name"]].add(author["user_id"])
                    if any(t["name"] == "玩法" for t in topics) and not args.keywords:
                        activity_sources.append(dict(id=key, author=author["user_id"], text=title+"\n"+body))

                    quotes = [c["text"] for c in comments if c["basis"]=="author" and PRICE.search(c["text"])]
                    notes.append(dict(id=key, title=title, category=topic["name"], excerpt=body[:3200] if topic.get("discoveryQueries") else body[:240],
                                      authorID=author["user_id"], authorReply=quotes[0][:160] if quotes else None))
                    write_json(folder/"waymark-plan.json", dict(version=1, city=args.city, notes=notes))
                    added += 1
                    if added >= limit: break
                    await asyncio.sleep(2)
                await asyncio.sleep(2)
                return added

            for topic in topics:
                if country and country != RULES["mainlandRegion"] and not args.keywords and topic["name"] in ("美食", "玩法"):
                    from source_policy import sources
                    food = topic["name"] == "美食"
                    known = [row for row in overseas_places if row["category"] == ("饭店" if food else "景点")]
                    queries = [row["name"] for row in known]
                    if not queries:
                        queries = [ref + (" 当地特色餐厅 实吃" if food else " 游玩实录")
                                   for ref in sources(country)["food" if food else "sights"]]
                    for query in queries[:args.per_topic]:
                        await read_query(topic, query, 1)
                    continue
                ranked = [c for c in candidates if c["topic"] == topic["name"]]
                if ranked:
                    for candidate in ranked[:args.per_topic]:
                        existing = next(((src, quote) for src in board_sources.values()
                                         if (quote := corroboration(candidate, src["title"], src["body"]))), None)
                        if existing:
                            src, quote = existing
                            candidate.update(xhs_status="同名到访线索", xhs_url=src["url"], quote=quote, note_date=src["date"])
                            checkpoint_boards()
                            continue
                        candidate["xhs_status"] = "检索中"
                        checkpoint_boards()
                        try:
                            await read_query(topic, candidate["name"], 1, candidate=candidate)
                        except Exception as exc:
                            candidate.update(xhs_status="查询中断", error=type(exc).__name__)
                            checkpoint_boards()
                            raise
                        if "xhs_url" not in candidate:
                            candidate["xhs_status"] = "未取得同店或同地点的到访原文"
                        checkpoint_boards()
                    if topic["name"] == "美食":
                        continue
                    await read_query(topic, topic["query"], min(args.per_topic or 2, 2))
                    continue
                for query, limit in seed_queries(topic, args.keywords, args.per_topic):
                    await read_query(topic, query, limit)
            activity_topic = next((topic for topic in topics if topic["name"] == "玩法"), None)
            if activity_topic and not args.keywords:
                while True:
                    coverage = plan_activities(activity_sources, attempted)
                    write_json(folder / "玩法检索.json", dict(city=args.city, attempted=attempted, **coverage))
                    # Keep the originating source id with every lead through extraction/reporting.
                    by_note = {s["id"]: [] for s in activity_sources}
                    for candidates in coverage["leads"].values():
                        for candidate in candidates:
                            for evidence in candidate["evidence"]:
                                by_note[evidence["id"]].append(evidence["term"])
                    for key, terms in by_note.items():
                        path = packets / (key + ".json")
                        packet = json.loads(path.read_text())
                        packet["source"]["activity_leads"] = list(dict.fromkeys(terms))
                        write_json(path, packet)
                    if not coverage["next"] or len(attempted) >= 3:
                        break
                    follow = coverage["next"][0]
                    before = len(activity_sources)
                    try:
                        added = await read_query(activity_topic, follow["query"], 1, targeted=True)
                    except Exception as exc:
                        write_json(folder / "玩法检索.json", dict(city=args.city, attempted=attempted,
                                   interrupted=dict(**follow, error=type(exc).__name__), **coverage))
                        raise
                    attempted.append(dict(**follow, added=added, ids=[s["id"] for s in activity_sources[before:]]))
            if not notes and not candidates: raise RuntimeError("没有取得可核实的原帖")
        finally:
            await page.close()
            if not args.session_dir: await ctx.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("city")
    p.add_argument("--output",type=Path,required=True)
    p.add_argument("--per-topic",type=int,choices=range(1,11),help="每类收录上限；默认玩法、美食各6篇，其余各2篇")
    p.add_argument("--topic",choices=[t["name"] for t in TOPICS])
    p.add_argument("--keywords",help="补查当前分类时使用的词")
    p.add_argument("--session-dir",type=Path,help="已手动登录的独立 Waymark 浏览器配置目录")
    p.add_argument("--country", type=str.upper, help="同名城市可指定 ISO 两位地区代码")
    p.add_argument("--dianping-city-id", type=int, help="首次定位后可复用的点评官方城市编号")
    p.add_argument("--iphone", help="可选：把本次计划传到指定的已配对 iPhone")
    args=p.parse_args()
    if not args.city.strip() or (args.keywords and not args.topic): p.error("请输入城市，定向补查请指定分类")
    upstream=Path(os.environ.get("MEDIACRAWLER_HOME", HOME/"upstream")).resolve()
    if not (upstream/"media_platform/xhs/client.py").is_file(): p.error("未找到独立安装的 MediaCrawler")
    sys.path.insert(0,str(upstream))
    args.output=args.output.resolve()
    args.output.mkdir(exist_ok=True,parents=True)
    folder=Path(tempfile.mkdtemp(prefix="research-",dir=args.output))
    print(folder,flush=True)
    os.chdir(upstream)
    try:
        asyncio.run(collect(args,folder))
    except Exception as exc:
        print(f"查询未完成 ({type(exc).__name__})。已取得的原帖保存在本次目录。",file=sys.stderr)
        sys.exit(1)
    if args.iphone:
        plan = folder / "waymark-plan.json"
        city_key = hashlib.sha256(args.city.encode()).hexdigest()
        version = hashlib.sha256(plan.read_bytes()).hexdigest()[:16]
        subprocess.run(["xcrun","devicectl","device","copy","to","--device",args.iphone,
                        "--domain-type","appDataContainer","--domain-identifier","com.waymark.footprints",
                        "--source",str(plan),"--destination",f"Documents/waymark-plan-{city_key}-{version}.json"],
                       check=True)


if __name__=="__main__":
    main()
