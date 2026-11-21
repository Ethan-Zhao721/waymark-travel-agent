"""Normalize branch-level evidence before taking each provider's shortlist."""
import re
import unicodedata
from urllib.parse import urlsplit, parse_qs

PROVIDERS = ("google", "xhs", "dianping")


def domain_matches(host, domain):
    return host == domain or host.endswith("." + domain)


def verified_metric(s, source_urls, *, paired=False, food=True):
    url = s["url"]
    u = urlsplit(url)
    host = u.hostname or ""
    if u.scheme != "https" or url not in source_urls:
        return False
    platform = s["platform"]
    count, rating, likes = s.get("reviews"), s.get("rating"), s.get("likes")
    count_ok = type(count) is int and count > 0
    rating_ok = type(rating) in (int, float) and 0 < rating <= 5
    if platform == "google":
        maps_link = (host == "maps.app.goo.gl" and len(u.path) > 2 or
                     domain_matches(host, "google.com") and (u.path.startswith("/maps") or host == "maps.google.com") and
                     ("/place/" in u.path or bool({"cid","query_place_id"} & parse_qs(u.query).keys())))
        return maps_link and count_ok and rating_ok and (not food or count >= 300 and rating >= 4.6)
    if platform == "xhs":
        return (domain_matches(host, "xiaohongshu.com") and
                re.fullmatch(r"/(?:explore|discovery/item|search_result)/[a-f0-9]{24}/?", u.path) and
                (paired or type(likes) is int and likes >= 0))
    return (platform == "dianping" and domain_matches(host, "dianping.com") and
            re.match(r"/shop/[a-zA-Z0-9]+(?:/|$)", u.path) and count_ok and
            (rating is None or rating_ok))


def order(signal):
    if signal["platform"] == "xhs":
        return signal["likes"], 0
    return signal["reviews"], signal.get("rating") or 0


def normalized(value):
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def shortlist(places):
    branches = {}
    aliases = {}
    sights = []
    for row in places:
        if row["category"] != "饭店":
            sights.append(row)
            continue
        urls = {s["url"] for s in row["sources"]}
        signals = [s for s in row.get("signals") or [] if verified_metric(s, urls)]
        address = normalized(row.get("address") or "")
        if not signals or not address:
            continue
        keys = [("id", row["id"])]
        keys += [(address, normalized(row[k])) for k in ("name","englishName") if row.get(k)]
        keys += [(s["platform"], s["url"]) for s in signals if s["platform"] != "xhs"]
        branch = next((aliases[k] for k in keys if k in aliases), row["id"])
        for k in keys:
            aliases[k] = branch
        if branch not in branches:
            branches[branch] = dict(row, sources=[], signals=[])
        target = branches[branch]
        price_fields = ("priceNote", "usdPerPerson", "priceTier", "perPersonUSDRange")
        if row.get("priceURL") in urls and any(row.get(key) for key in price_fields):
            target.update({key: row.get(key) for key in (*price_fields, "priceURL")})
        target["sources"] = list({s["url"]:s for s in target["sources"] + row["sources"]}.values())
        target["signals"].extend(signals)
    ranked = {key:[] for key in branches}
    for provider in PROVIDERS:
        matches = []
        for key, row in branches.items():
            best = max((s for s in row["signals"] if s["platform"] == provider), key=order, default=None)
            if best:
                matches.append((key,best))
        matches.sort(key=lambda item: (-order(item[1])[0], -order(item[1])[1],item[0]))
        for rank,(key,signal) in enumerate(matches[:6], 1):
            ranked[key].append(dict(signal, rank=rank))
    return sights + [dict(branches[key], signals=signals) for key,signals in ranked.items() if signals]

def paired_shortlist(places):
    result = []
    for category in ("景点", "饭店"):
        candidates = []
        for row in places:
            if row["category"] != category or not (row.get("address") or "").strip():
                continue
            urls = {s["url"] for s in row["sources"]}
            eligible = {s["platform"]: s for s in row.get("signals") or []
                        if verified_metric(s, urls, paired=True, food=category == "饭店")}
            if "google" in eligible and "xhs" in eligible:
                candidates.append((row, eligible["google"], eligible["xhs"]))
        candidates.sort(key=lambda item: (-item[1]["reviews"], -item[1]["rating"], item[0]["id"]))
        seen = set()
        rank = 0
        for row, google, xhs in candidates:
            keys = {("google", google["url"]), ("id", row["id"]),
                    ("name-address", normalized(row["name"]), normalized(row["address"]))}
            if keys & seen:
                continue
            seen.update(keys)
            rank += 1
            result.append(dict(row, signals=[dict(xhs, rank=rank, likes=None), dict(google, rank=rank)]))
            if rank == 10:
                break
    return result

def xhs_shortlist(places, *, high_rated=False):
    """Preserve discovery order and attach only metrics observed for this place."""
    result = []
    for category in ("景点", "饭店"):
        seen_branches, seen_maps = set(), set()
        rank = 0
        for row in places:
            if row["category"] != category:
                continue
            urls = {s["url"] for s in row["sources"]}
            signals = {s["platform"]: s for s in row.get("signals") or []
                       if verified_metric(s, urls, paired=True, food=False)}
            xhs, google = signals.get("xhs"), signals.get("google")
            if high_rated and (not google or google["rating"] < 4.6 or
                               (category == "饭店" and google["reviews"] < 300)):
                continue
            branch = (normalized(row["name"]), normalized(row.get("address") or ""))
            if not xhs or not all(branch) or branch in seen_branches or (google and google["url"] in seen_maps):
                continue
            seen_branches.add(branch)
            rank += 1
            evidence = [dict(xhs, rank=rank, likes=None)]
            if google:
                seen_maps.add(google["url"])
                evidence.append(dict(google, rank=rank))
            result.append(dict(row, signals=evidence))
            if rank == 10:
                break
    return result
