"""One shared city catalogue. Run behind the app publisher's HTTPS endpoint."""
import datetime
import json
import os
import re
import unicodedata
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from guide_validation import KEY, validate, write
from restaurant_ranking import normalized, verified_metric

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CITIES = {c["id"]: c for c in json.loads((ROOT / "Waymark/Geography.json").read_text())["cities"]
          + json.loads((HERE / "pilot-cities.json").read_text())}
CACHE = Path(os.getenv("WAYMARK_CACHE", str(HERE / ".cache")))
POOL = ThreadPoolExecutor(max_workers=2)
LOCK = threading.Lock()
pending = set()
phases = {}
failed_until = {}


def get_cached(city):
    for path in (CACHE / (city["id"] + ".json"), HERE / "guides" / (city["id"] + ".json")):
        if not path.exists():
            continue
        try:
            guide = validate(json.loads(path.read_text()), city)
        except (ValueError, KeyError, TypeError):
            continue
        if guide.get("rankingVersion") not in ((4,5) if city.get("countryCode") == "CN" else (4,)) and os.getenv("OPENAI_API_KEY"):
            continue
        age = datetime.date.today() - datetime.date.fromisoformat(guide["checked"])
        if 0 <= age.days <= 1:
            return guide
    return None


def public_url(url):
    p = urlsplit(url)
    return (p.hostname or "").lower().removeprefix("www."), p.path.rstrip("/"), p.query


def fetch_json(request, timeout):
    for attempt in range(3):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise
            delay = error.headers.get("Retry-After") if error.headers else None
            if delay and (not delay.isdecimal() or int(delay) > 120):
                raise
        except (URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
            delay = None
        time.sleep(max(2 ** (attempt + 1), int(delay or 0)))


def search_web(instructions, context, schema):
    payload = dict(model=os.getenv("WAYMARK_MODEL", "gpt-4.1"), store=False,
                   instructions=instructions, input=json.dumps(context, ensure_ascii=False),
                   tools=[{"type":"web_search"}], tool_choice="required",
                   include=["web_search_call.action.sources"],
                   text={"format":{"type":"json_schema","name":"guide","strict":True,"schema":schema}})
    req = Request("https://api.openai.com/v1/responses", data=json.dumps(payload).encode(),
                  headers={"Content-Type":"application/json","Authorization":"Bearer "+os.environ["OPENAI_API_KEY"]})
    body = fetch_json(req, timeout=160)
    urls = set()
    texts = []
    for item in body.get("output", []):
        for source in item.get("action", {}).get("sources", []):
            if source.get("url"):
                urls.add(public_url(source["url"]))
        for part in item.get("content", []):
            if part.get("type") == "output_text":
                texts.append(part["text"])
                urls.update(public_url(a["url"]) for a in part.get("annotations", []) if a.get("url"))
    return json.loads("".join(texts)), urls


def discover_candidates(city):
    from source_policy import sources, RULES
    candidates_from = sources(city.get("countryCode"))
    domestic = city.get("countryCode") == RULES["mainlandRegion"]
    policy = ("先分别查高德全城美食榜、大众点评同城必吃榜的前十，景点查高德全城景点榜。保留榜名年份和顺序，以分店核对。"
              if domestic else "先从该国家可用的当地指南、餐厅评选、景点榜单发现候选，再查同一地点的原帖。")

    board_pool = {}
    if domestic:
        sys.path.insert(0, str(ROOT / "skills/xhs-travel-notes/scripts"))
        from city_boards import boards, shortlist
        labels = json.loads((ROOT / "Waymark/MapNames.json").read_text())["cities"]
        city_name = (labels.get(city["id"]) or [city["name"]])[0]
        snapshot = boards(city_name, ["food", "dianping", "scenic"], RULES["mainlandDianpingCities"].get(city_name))
        board_pool = {("饭店" if c["topic"] == "美食" else "景点", normalized(c["name"])): c
                      for c in shortlist(snapshot)}
    fields = {k: {"type":"string"} for k in ("id", "name", "englishName", "address", "url", "referenceName", "referenceURL", "specialty")}
    fields["category"] = {"type":"string", "enum":["景点", "饭店"]}
    schema = {"type":"object", "properties":{"places":{"type":"array", "items":{
        "type":"object", "properties":fields, "required":list(fields), "additionalProperties":False}}},
        "required":["places"], "additionalProperties":False}
    found, urls = search_web(policy + "各取最多十个当地有特色、有来源的景点和餐厅。referenceName、referenceURL 填该实体的原榜或官方介绍，specialty 填与它对应的招牌菜或玩法。来源不可核实时留空。识别全名、分店和地址，沿用同一地点的旧 ID。"
                             "每行给一条实际读到的原笔记链接。缺证据时少选。", {
                                 "city":city, "sources":candidates_from, "boardCandidates":list(board_pool.values()), "previous":previous_names(city["id"])}, schema)
    seeds = {}
    identities = set()
    for p in found["places"]:
        identity = (p["category"], normalized(p["name"]), normalized(p["address"]))
        if (not KEY.fullmatch(p["id"]) or p["category"] not in ("景点","饭店") or not all(identity)
                or p["id"] in seeds or identity in identities or public_url(p["url"]) not in urls
                or not verified_metric(dict(platform="xhs",url=p["url"]), {p["url"]}, paired=True)
                or sum(v["category"] == p["category"] for v in seeds.values()) >= 10):
            continue
        if board_pool and any(category == p["category"] for category, _ in board_pool):
            listing = board_pool.get((p["category"], normalized(p["name"])))
            if not listing:
                continue
            p = dict(p, boardSources=listing["sources"])
        if p.get("referenceURL") and (public_url(p["referenceURL"]) not in urls or
                                      urlsplit(p["referenceURL"]).scheme != "https"):
            p = dict(p, referenceName="", referenceURL="", specialty="")
        seeds[p["id"]] = p
        identities.add(identity)
    return seeds


def same_street_address(expected, actual):
    # Keep house-number components distinct (1-12 and 11-2 are different branches).
    def street(value):
        value = unicodedata.normalize("NFKC", value).casefold()
        value = re.sub(r"(丁目|番地|番|号)", "-", value)
        return "".join(c if c.isalnum() else " " for c in value).split()
    pieces = street(expected)
    if not pieces:
        return False
    needle = "".join(pieces)
    digits = re.findall(r"[0-9]+", " ".join(pieces))
    haystack = "".join(street(actual))
    if not digits:
        return needle == haystack
    number_groups = re.findall(r"[0-9]+", " ".join(street(actual)))
    return (len(needle) >= 5 and needle in haystack and
            any(number_groups[i:i+len(digits)] == digits for i in range(len(number_groups))))


def lookup_google_place(city, seed, api_key):
    """Look up one named branch. An ambiguous result has no usable rating."""
    fields = ("displayName", "formattedAddress", "shortFormattedAddress", "addressComponents",
              "rating", "userRatingCount", "googleMapsUri", "location", "businessStatus", "priceLevel")
    names = {normalized(name) for name in (seed["name"], seed["englishName"]) if name}
    query = " ".join((seed["name"], seed["address"], city["name"], city["countryCode"]))
    body = {"textQuery": query, "pageSize": 5, "locationBias": {"circle": {"center": {
        "latitude": city["latitude"], "longitude": city["longitude"]}, "radius": 25000}}}
    request = Request("https://places.googleapis.com/v1/places:searchText",
                      data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
                      "X-Goog-Api-Key": api_key, "X-Goog-FieldMask": ",".join("places."+f for f in fields)})
    rows = fetch_json(request, timeout=15).get("places", [])
    matches = []
    for row in rows:
        if normalized(row.get("displayName", {}).get("text", "")) not in names:
            continue
        if not any("country" in c.get("types", []) and c.get("shortText") == city["countryCode"]
                   for c in row.get("addressComponents", [])):
            continue
        if not any(same_street_address(seed["address"], row.get(field, ""))
                   for field in ("formattedAddress", "shortFormattedAddress")):
            continue
        if row.get("businessStatus") in ("CLOSED_TEMPORARILY", "CLOSED_PERMANENTLY"):
            continue
        matches.append(row)
    if len(matches) != 1:
        return None
    row = matches[0]
    link = row.get("googleMapsUri")
    signal = dict(platform="google", url=link, reviews=row.get("userRatingCount"), rating=row.get("rating"))
    coords = row.get("location") or {}
    if link and verified_metric(signal, {link}, food=False) and all(k in coords for k in ("latitude", "longitude")):
        return row
    return None


def verify_candidates(city, seeds):
    guide = dict(cityID=city["id"], rankingVersion=4, checked=str(datetime.date.today()),
                 coverage="小红书原笔记推荐；Google Maps 评分按地点核对。", places=[])
    entries = {}
    for key, seed in seeds.items():
        source = dict(name="小红书原笔记", url=seed["url"])
        note = dict(platform="xhs", url=seed["url"], likes=None, rating=None, reviews=None)
        references = [source]
        if seed.get("referenceURL") and seed.get("referenceName"):
            references.append(dict(name=seed["referenceName"], url=seed["referenceURL"]))
        entries[key] = {**{k: seed[k] for k in ("id", "category", "name", "englishName", "address")},
                        "latitude": None, "longitude": None, "reason": seed.get("specialty") or "", "evidence": "",
                        "sources": references, "signals": [note], "priceTier": None, "usdPerPerson": None}
        if seed.get("boardSources"):
            entries[key]["sources"].extend(dict(name=f"{s['platform']} · {s['order']} {s['position']}", url=s["url"])
                                           for s in seed["boardSources"])
    guide["places"] = list(entries.values())
    if city.get("countryCode") == "CN" and any(seed.get("boardSources") for seed in seeds.values()):
        guide.update(rankingVersion=5, coverage="国内城市榜单与小红书原帖交叉核对")
        guide["places"] = [v for k,v in entries.items() if seeds[k].get("boardSources")]
        return guide
    if not seeds:
        return guide
    places_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if places_key:
        for key, seed in seeds.items():
            match = lookup_google_place(city, seed, places_key)
            if match is None:
                continue
            row = entries[key]
            link = match["googleMapsUri"]
            row["sources"].append(dict(name="Google Maps", url=link))
            row["signals"].append(dict(platform="google", url=link, rating=match["rating"],
                                       reviews=match["userRatingCount"], likes=None))
            row.update(match["location"])
            levels = ["PRICE_LEVEL_INEXPENSIVE", "PRICE_LEVEL_MODERATE",
                      "PRICE_LEVEL_EXPENSIVE", "PRICE_LEVEL_VERY_EXPENSIVE"]
            if seed["category"] == "饭店" and match.get("priceLevel") in levels:
                row.update(priceURL=link, priceTier=1 + levels.index(match["priceLevel"]))
        return guide
    skill = (ROOT / "skills/waymark-destination/SKILL.md").read_text()
    schema = json.loads((HERE / "guide.schema.json").read_text())
    found, urls = search_web(skill + "\n现在只核对给定候选。按城市、本地名及地址逐一查 Google Maps 的具体门店或景点。"
                             "名称和地址须与输入一致，保留输入的 ID、category、name、address。只填原页可核实的评分和评价数。"
                             "无法匹配、只找到搜索页或评价数不可见就跳过；不要添加候选。", {
                                 "city":city, "candidates":list(seeds.values())}, schema)
    for p in found["places"]:
        seed = seeds.get(p["id"])
        if not seed or any(normalized(p.get(k) or "") != normalized(seed[k]) for k in ("category","name","address")):
            continue
        sources = [s for s in p["sources"] if public_url(s["url"]) in urls and s["url"] != seed["url"]]
        urls_for_place = {source["url"] for source in sources}
        google = next((signal for signal in p.get("signals") or []
                       if signal["platform"] == "google" and verified_metric(signal, urls_for_place, food=False)), None)
        if google:
            row = entries[p["id"]]
            p["sources"] = row["sources"] + sources
            p["signals"] = row["signals"] + [google]
            row.update(p)
    return guide


def research(city):
    key = city["id"]
    try:
        with LOCK:
            phases[key] = "discovering"
        seeds = discover_candidates(city)
        with LOCK:
            phases[key] = "verifying"
        guide = verify_candidates(city, seeds)
        write(CACHE / (key + ".json"), validate(guide, city))
    except Exception as exc:
        with LOCK:
            failed_until[key] = time.monotonic() + 300
        print("Research failed for", key, type(exc).__name__, flush=True)
    finally:
        with LOCK:
            with sqlite3.connect(CACHE / "budget.db") as db:
                db.execute("DELETE FROM jobs WHERE city=?", (key,))
            pending.discard(key)
            phases.pop(key, None)


def previous_names(key):
    for path in (CACHE / (key+".json"), HERE / "guides" / (key+".json")):
        if path.exists():
            return [(p["id"],p["name"]) for p in json.loads(path.read_text())["places"]]
    return []


def start_workers():
    CACHE.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(CACHE / "budget.db") as db:
        db.execute("CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY, count INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS jobs(city TEXT PRIMARY KEY)")
        queued = []
        for key, in db.execute("SELECT city FROM jobs ORDER BY rowid").fetchall():
            if key not in CITIES or get_cached(CITIES[key]) is not None:
                db.execute("DELETE FROM jobs WHERE city=?", (key,))
            else:
                queued.append(key)
    if os.getenv("OPENAI_API_KEY"):
        with LOCK:
            for key in queued:
                if key in CITIES and key not in pending:
                    pending.add(key)
                    POOL.submit(research, CITIES[key])


def response_for(key):
    if not KEY.fullmatch(key) or key not in CITIES:
        return 404, {"error":"unknown city"}
    city = CITIES[key]
    with LOCK:
        guide = get_cached(city)
        if guide is not None:
            return 200, guide
        if key in pending:
            return 202, {"state":phases.get(key, "queued")}
        if not os.getenv("OPENAI_API_KEY"):
            return 503, {"error":"research unavailable"}
        if failed_until.get(key, 0) > time.monotonic():
            return 503, {"error":"research failed"}
        if len(pending) >= 4:
            return 429, {"error":"try later"}
        with sqlite3.connect(CACHE / "budget.db") as db:
            day = str(datetime.date.today())
            row = db.execute("SELECT count FROM usage WHERE day=?", (day,)).fetchone()
            if row and row[0] >= int(os.getenv("WAYMARK_DAILY_LIMIT", "20")):
                return 429, {"error":"daily limit"}
            db.execute("INSERT INTO jobs(city) VALUES(?)", (key,))
            db.execute("INSERT INTO usage VALUES(?,1) ON CONFLICT(day) DO UPDATE SET count=count+1", (day,))
        pending.add(key)
        POOL.submit(research, city)
    return 202, {"state":"queued"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        prefix = "/v1/destinations/"
        status, result = response_for(path[len(prefix):]) if path.startswith(prefix) else (404, {})
        data = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600" if status == 200 else "no-store")
        if status == 429:
            self.send_header("Retry-After", "3600" if result.get("error") == "daily limit" else "45")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    start_workers()
    ThreadingHTTPServer((os.getenv("HOST","127.0.0.1"),int(os.getenv("PORT","8080"))), Handler).serve_forever()
