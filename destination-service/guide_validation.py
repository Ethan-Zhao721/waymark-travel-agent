from restaurant_ranking import paired_shortlist, shortlist, xhs_shortlist

import datetime
import json
import math
import os
import re
import tempfile
from urllib.parse import urlsplit

KEY = re.compile(r"[a-zA-Z0-9_-]{1,90}")
CATEGORIES = {"景点", "饭店"}

def write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False, encoding="utf-8") as tmp:
        json.dump(data, tmp, ensure_ascii=False)
    os.replace(tmp.name, path)


def validate(guide, city):
    if guide["cityID"] != city["id"] or len(guide["places"]) > 120:
        raise ValueError("wrong city or shortlist size")
    datetime.date.fromisoformat(guide["checked"])
    version = guide.get("rankingVersion", 1)
    if version == 5:
        if city["countryCode"] != "CN":
            raise ValueError("mainland board outside mainland")
        places = xhs_shortlist([p for p in guide["places"] if any(
            (urlsplit(s["url"]).hostname, urlsplit(s["url"]).path.split("/")[:2]) in
            [("www.amap.com", ["", "place"]), ("www.dianping.com", ["", "shop"])] for s in p["sources"])])
    elif version in (3, 4):
        places = xhs_shortlist(guide["places"], high_rated=version == 4)
    else:
        places = {1: shortlist, 2: paired_shortlist}[version](guide["places"])
    guide = dict(guide, places=places)
    seen = set()
    counts = dict.fromkeys(CATEGORIES, 0)
    for place in guide["places"]:
        identity = place["id"]
        if not KEY.fullmatch(identity) or identity in seen or place["category"] not in CATEGORIES:
            raise ValueError("invalid place identity")
        seen.add(identity)
        counts[place["category"]] += 1
        if place["category"] == "景点" and counts["景点"] > (10 if version >= 2 else 3):
            raise ValueError("too many places")
        lat, lon = place["latitude"], place["longitude"]
        if version not in (3, 5) or lat is not None or lon is not None:
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("invalid coordinate")
            delta = (lon - city["longitude"] + 180) % 360 - 180
            if math.hypot(lat-city["latitude"], delta*math.cos(math.radians(lat))) > 1.5:
                raise ValueError("coordinate outside destination region")
        tier, usd = place.get("priceTier"), place.get("usdPerPerson")
        if tier is not None and (type(tier) is not int or tier not in range(1, 5)):
            raise ValueError("invalid price tier")
        if usd is not None and (type(usd) not in (int, float) or not math.isfinite(usd) or usd <= 0):
            raise ValueError("invalid per-person price")
        if place["category"] == "景点" and (usd is not None or tier is not None):
            raise ValueError("unexpected restaurant price")
        if usd is not None:
            place["priceTier"] = 1 + int(usd >= 15) + int(usd >= 35) + int(usd >= 75)
        hosts = set()
        for source in place["sources"]:
            u = urlsplit(source["url"])
            if u.scheme != "https" or not u.hostname or not source["name"].strip():
                raise ValueError("invalid source")
            hosts.add(u.hostname.lower().removeprefix("www."))
        def family(h):
            for root in ("dianping.com", "xiaohongshu.com", "xhslink.com", "rednote.com", "google.com", "maps.app.goo.gl"):
                if h == root or h.endswith("." + root):
                    return {"xhslink.com":"xiaohongshu.com","rednote.com":"xiaohongshu.com","maps.app.goo.gl":"google.com"}.get(root, root)
            return h
        families = {family(h) for h in hosts}
        if place["category"] == "饭店":
            span = place.get("perPersonUSDRange")
            if span is not None:
                valid = (isinstance(span, dict) and span.get("meal") in ("any", "lunch", "dinner", "breakfast")
                         and all(type(span.get(k)) in (int, float) and math.isfinite(span[k]) for k in ("low", "high"))
                         and 0 < span["low"] <= span["high"])
                if not valid:
                    place["perPersonUSDRange"] = None
            if place.get("priceURL") not in {s["url"] for s in place["sources"]}:
                place.update(priceURL=None, priceNote=None, usdPerPerson=None, priceTier=None, perPersonUSDRange=None)
            continue
        if version in (3,5) and "xiaohongshu.com" in families:
            continue
        if len(families) < 2 or not families.intersection({"dianping.com","xiaohongshu.com","google.com"}):
            raise ValueError("cross-check missing")
    return guide


