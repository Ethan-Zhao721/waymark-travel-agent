"""Import evidence-checked XHS note extractions and render a dated city brief."""
import argparse
from collections import defaultdict
from datetime import date
from html import escape
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import unicodedata
from urllib.request import Request, urlopen

from rank_notes import identify

PROMPT = Path(__file__).resolve().parents[1] / "references/extract-v1.md"
ACTIVITY_SECTIONS = ("游览线路", "景点与体验")
SECTIONS = (*ACTIVITY_SECTIONS, "避坑", "交通", "青旅", "酒店", "美食")
STANCES = {"推荐", "避雷", "有争议", "信息"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def period(day):
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # Feb 29 one year earlier.
        return day.replace(year=day.year - 1, day=28)


def normalized(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def prepare(source):
    source = dict(source)
    key, ok = identify(source["url"])
    if not ok or not source["author_id"] or not source["city"] or not source["body"].strip():
        raise ValueError("原帖链接、作者、城市与原文都是必填项")
    if not source["title"].strip():
        raise ValueError("原帖标题缺失")
    date.fromisoformat(source["date"])
    mode = source["comments_mode"]
    if mode not in {"all", "top_likes", "unverified"}:
        raise ValueError("评论排序状态无效")
    comments = list(source.get("comments", []))
    if mode == "unverified":
        comments = []
    elif mode == "all":
        if source.get("comment_total") != len(comments):
            raise ValueError("完整评论数量和原帖不一致")
        if len(comments) > 20 and any(type(c.get("likes")) is not int for c in comments):
            raise ValueError("无法核对最高赞的 20 条评论")
        comments.sort(key=lambda c: c["likes"] if c.get("likes") is not None else -1, reverse=True)
    source["comments"] = comments[:20]
    ids = set()
    for c in source["comments"]:
        if c["id"] in ids or c["id"] == "body" or not c["author_id"] or not c["text"].strip():
            raise ValueError("评论缺少独立标识或内容")
        if c["basis"] not in {"author", "firsthand", "unconfirmed"}:
            raise ValueError("评论证据类型无效")
        if c["basis"] == "author" and c["author_id"] != source["author_id"]:
            raise ValueError("评论作者与原作者不符")
        date.fromisoformat(c["date"])
        ids.add(c["id"])
    return key, source


def validate(key, source, extraction):
    if extraction["note_id"] != key or extraction["ad"] not in {"organic", "ad", "uncertain"}:
        raise ValueError("笔记 ID 或商业状态无效")
    if not extraction["ad_reason"].strip() or not isinstance(extraction["items"], list):
        raise ValueError("缺少商业判断说明或条目列表")
    passages = {"body": source["title"] + "\n" + source["body"]}
    passages.update({c["id"]: c["text"] for c in source["comments"]})
    for item in extraction["items"]:
        passage = passages.get(item["source"])
        if not passage or (item["area"] is not None and not isinstance(item["area"], str)):
            raise ValueError("条目出处或区域格式无效")
        if item["dimension"] not in SECTIONS or item["stance"] not in STANCES:
            raise ValueError("无效的维度或态度")
        if not all(item[k].strip() for k in ("name", "reason", "evidence")) or item["evidence"] not in passage:
            raise ValueError(f'{key} 引文无法在来源中定位')
        if item["update"] not in {None, "price", "closure", "transport"} or (item["update"] and item["source"] == "body"):
            raise ValueError("更正必须来自指定评论")
        price = item["price"]
        if price is not None and (not price["label"].strip() or not price["quote"].strip() or price["quote"] not in passage):
            raise ValueError("价格缺少同一出处的逐字引文")
        if item["update"] == "price" and not price:
            raise ValueError("评论补价缺少金额")
    return extraction


def call_claude(source, prompt, model):
    token = os.environ.get("ANTHROPIC_API_KEY")
    if not token or not model:
        raise ValueError("调用 Claude 需要 ANTHROPIC_API_KEY 和 --model")
    payload = {"model": model, "max_tokens": 7500, "system": prompt,
               "messages": [{"role": "user", "content": json.dumps(source, ensure_ascii=False)}]}
    request = Request("https://api.anthropic.com/v1/messages", data=json.dumps(payload).encode(),
                      headers={"content-type": "application/json", "anthropic-version": "2023-06-01", "x-api-key": token})
    with urlopen(request, timeout=120) as response:
        answer = json.load(response)
    if answer.get("stop_reason") != "end_turn":
        raise ValueError("模型没有返回完整提取")
    return json.loads("".join(b["text"] for b in answer["content"] if b["type"] == "text"))


def ingest(db, folder, api, model, day):
    prompt = PROMPT.read_text()
    counts = defaultdict(int)
    for path in sorted(folder.glob("*.json")):
        packet = json.loads(path.read_text())
        key, source = prepare(packet["source"])
        fingerprint = digest([source, prompt])
        cached = db.execute("select signature, result from notes where id=?", (key,)).fetchone()
        if cached and cached[0] == fingerprint and (api or json.loads(cached[1]) == packet["extraction"]):
            counts["缓存命中"] += 1
            continue
        if api and not period(day) <= date.fromisoformat(source["date"]) <= day:
            counts["不在时间窗"] += 1
            continue
        result = call_claude(source, prompt, model) if api else packet["extraction"]
        validate(key, source, result)
        db.execute("insert or replace into notes values (?,?,?,?,?,?)",
                   (key, source["city"], fingerprint, json.dumps(source, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False), model if api else packet.get("extractor", "manual")))
        counts["写入"] += 1
    return dict(counts)


def aggregate(db, city, day, aliases):
    groups = defaultdict(list)
    stats = defaultdict(int)
    for src, result in db.execute("select source,result from notes where city=? order by id", (city,)):
        src, result = json.loads(src), json.loads(result)
        published = date.fromisoformat(src["date"])
        if published < period(day):
            stats["可能过期"] += 1
            continue
        if published > day:
            continue
        if result["ad"] != "organic":
            stats["广告或商业待核"] += 1
            continue
        stats["原帖"] += 1
        if src["comments_mode"] != "unverified":
            stats["已核评论集"] += 1
        comments = {c["id"]: c for c in src["comments"]}
        for item in result["items"]:
            dim, name = item["dimension"], item["name"]
            name = aliases.get(f'{city}|{dim}|{name}', name)
            comment = comments.get(item["source"])
            when = comment["date"] if comment else src["date"]
            if date.fromisoformat(when) > day:
                continue
            row = dict(item, name=name, author=comment["author_id"] if comment else src["author_id"],
                       date=when, url=src["url"], id=result["note_id"], basis=comment["basis"] if comment else "body")
            groups[dim, normalized(name)].append(row)
    return groups, stats


def summarize(rows, *, concise=False):
    ordered = sorted(rows, key=lambda r: r["date"], reverse=True)
    votes = defaultdict(set)
    for r in ordered:
        if r["basis"] != "unconfirmed":
            votes[r["author"]].add(r["stance"])
    yes = no = mixed = 0
    for stances in votes.values():
        if "有争议" in stances or {"推荐", "避雷"} <= stances:
            mixed += 1
        elif "推荐" in stances:
            yes += 1
        elif "避雷" in stances:
            no += 1
    def reliable(r):
        return r["basis"] in {"author", "firsthand", "body"}
    support = [r for r in ordered if reliable(r)]
    primary = next((r for r in support if r["basis"] == "body"), None)
    tip = primary or (support or ordered)[0]
    detail = tip["reason"] if support else "评论待核：" + tip["reason"]
    # Newer, attributable updates take precedence within the same topic.
    changes = {}
    for r in ordered:
        if r["update"] and r["basis"] in {"author", "firsthand"} and (not primary or r["date"] >= primary["date"]):
            changes.setdefault(r["update"], r)
    for field in ("closure", "transport"):
        if field in changes:
            detail = "评论更新：" + changes[field]["reason"] + "（此前：" + detail + "）"
    if (no and yes) or mixed:
        other = next((r for r in support if r["stance"] != tip["stance"] and r["stance"] != "信息"), None)
        if other:
            detail += "；另一条记录：" + other["reason"]
    prices = sorted((r for r in support if r["price"]), key=lambda r: (r["date"], r["update"] == "price"), reverse=True)
    if prices:
        p = prices[0]
        detail += " · " + p["price"]["label"] + ("（评论补充）" if p["basis"] != "body" else "")
    if any(not reliable(r) for r in ordered):
        detail += " · 评论线索待核"
    tags = []
    if len({r["id"] for r in ordered}) == 1:
        tags.append("单一来源")
    elif len(votes) == 1:
        tags.append("同一作者")
    if mixed or (yes and no):
        tags.append("有争议")
    area = next((r.get("area") for r in ordered if r.get("area")), None)
    name = tip["name"] + (f'（{area}）' if area and area not in tip["name"] else "")
    refs = dict.fromkeys((r["date"], r["url"], r["basis"] != "body") for r in ordered)
    links = " ".join(f'[{d}{" 评论" if c else ""}]({url})' for d, url, c in refs)
    count = f'提及 {len(votes)} 人 · 推荐 {yes} / 避雷 {no}' + (f' / 分歧 {mixed}' if mixed else "")
    line = f'**{name}** · {detail}。{count}' + (" · " + "、".join(tags) if tags else "") + "。" + links
    score = (no if tip["dimension"] == "避坑" else yes, len(votes), bool(prices), ordered[0]["date"])
    if concise:
        line = f"**{name}** · {detail}。[原帖]({tip['url']})"
        if prices and prices[0]["url"] != tip["url"]:
            line += f" [价格出处]({prices[0]['url']})"
    return score, line


def report(db, city, day, aliases, *, evidence=False):
    groups, stats = aggregate(db, city, day, aliases)
    section_rows = defaultdict(list)
    for (dim, _), rows in groups.items():
        section_rows[dim].append(summarize(rows))
    if not evidence:
        lines = [f"# {city}怎么玩", ""]
        for dim in (*ACTIVITY_SECTIONS, "美食", "青旅", "酒店", "避坑"):
            ranked = [summarize(verified, concise=True)
                      for (category, _), rows in groups.items() if category == dim
                      if (verified := [row for row in rows if row["basis"] in {"body", "firsthand", "author"}])]
            if ranked:
                lines += [f"## {dim}", ""]
                lines += ["- " + line for _, line in sorted(ranked, reverse=True)[:(len(ranked) if dim in ACTIVITY_SECTIONS else 4)]]
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"
    best = []
    for dim in (*ACTIVITY_SECTIONS, "美食", "青旅", "酒店"):
        choices = [rows for (d, _), rows in groups.items() if d == dim]
        if choices:
            chosen = max(choices, key=lambda rows: summarize(rows)[0])
            if summarize(chosen)[0][0]:
                best.append(chosen[0]["name"])
    overview = "本次样本可先看" + "、".join(best) + "，详情见各分类。" if best else "本轮样本可支持的结论见下表。"
    lines = [f'# {city}旅行 · 小红书样本报告', '',
             overview, '', f'{day} 汇总。{stats["原帖"]} 篇原帖覆盖 {len(section_rows)} 个维度，共提取 {len(groups)} 个不同条目。'
             f'已核评论集 {stats["已核评论集"]} 篇，排除广告或商业待核 {stats["广告或商业待核"]} 篇、可能过期 {stats["可能过期"]} 篇。', '']
    for dim in SECTIONS:
        label = {"避坑": "避坑提醒", "青旅": "住宿 · 青旅", "酒店": "住宿 · 酒店"}.get(dim, dim)
        rows = sorted(section_rows[dim], key=lambda pair: pair[0], reverse=True)
        lines += [f'## {label}', '']
        lines += [f'{i}. {line}' for i, (_, line) in enumerate(rows if dim in ACTIVITY_SECTIONS else rows[:10 if dim == "避坑" else 4], 1)]
        if not rows:
            lines.append("本次样本未覆盖。")
        lines.append("")
    # Discovery candidates must never become votes. Flag evidenced omissions explicitly.
    covered = "\n".join(row["name"] + " " + row["evidence"] for (dim, _), group in groups.items()
                        if dim in ACTIVITY_SECTIONS for row in group)
    missing = {}
    for src, result in db.execute("select source,result from notes where city=? order by id", (city,)):
        src, result = json.loads(src), json.loads(result)
        if result["ad"] != "organic" or not period(day) <= date.fromisoformat(src["date"]) <= day:
            continue
        for term in src.get("activity_leads", []):
            if term in (src["title"] + "\n" + src["body"]) and term not in covered:
                missing.setdefault(term, src["url"])
    if missing:
        lines += ["## 原帖已提到、仍待核实的玩法", ""]
        lines += [f"- {escape(term)} · [原帖]({url})" for term, url in missing.items()]
        lines.append("")
    lines += ["<details><summary>查看逐条原文依据</summary>", ""]
    for rows in groups.values():
        for r in rows:
            evidence = escape(r["evidence"])
            if r["price"] and r["price"]["quote"] != r["evidence"]:
                evidence += " / " + escape(r["price"]["quote"])
            lines.append(f'- {r["dimension"]} · {r["name"]}：「{evidence}」。[{r["date"]} 原帖]({r["url"]})')
    lines += ["", "</details>", ""]
    return "\n".join(lines).rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["import", "extract", "report"])
    parser.add_argument("target", help="packet directory or city name")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--aliases", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--evidence", action="store_true", help="导出完整逐项证据")
    args = parser.parse_args()
    with sqlite3.connect(args.db) as db:
        db.execute("create table if not exists notes (id text primary key, city text, signature text, source text, result text, model text)")
        if args.action == "report":
            aliases = json.loads(args.aliases.read_text()) if args.aliases else {}
            print(report(db, args.target, args.as_of, aliases, evidence=args.evidence), end="")
        else:
            folder = Path(args.target)
            if not folder.is_dir() or not any(folder.glob("*.json")):
                parser.error("样本目录中没有 JSON")
            print(ingest(db, folder, args.action == "extract", args.model, args.as_of))


if __name__ == "__main__":
    main()
