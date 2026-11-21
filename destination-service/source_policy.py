"""Destination research rules also bundled with Waymark on iOS."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = json.loads((ROOT / "Waymark/DestinationSources.json").read_text())


def sources(country):
    group = RULES["mainland"] if country == RULES["mainlandRegion"] else RULES["international"]
    return dict(food=[*group["food"], *RULES["regionalFood"].get(country, [])], sights=group["sights"])


def city_record(name, country=None):
    pilots = json.loads((ROOT / "destination-service/pilot-cities.json").read_text())
    aliases = json.loads((ROOT / "Waymark/MapNames.json").read_text())["cities"]
    def matches(c):
        return (not country or c["countryCode"] == country) and name.casefold() in {
            s.casefold() for s in [c["id"], c["name"], *aliases.get(c["id"], [])] if s}
    found = [c for c in pilots if matches(c)]
    if not found:
        found = [c for c in json.loads((ROOT / "Waymark/Geography.json").read_text())["cities"] if matches(c)]
    return found[0] if len(found) == 1 else None


def international_snapshot(city):
    """Use the service verifier for both Mac and phone; retain the observation date of cached metrics."""
    import os
    import server
    from restaurant_ranking import verified_metric
    guide = server.get_cached(city)
    mode = "已保存资料"
    if guide is None and os.getenv("OPENAI_API_KEY"):
        guide = server.validate(server.verify_candidates(city, server.discover_candidates(city)), city)
        mode = "本次查询"
    rows = []
    for place in (guide or {}).get("places", []):
        urls = {s["url"] for s in place["sources"]}
        metric = next((signal for signal in place.get("signals", []) if signal["platform"] == "google"
                       and verified_metric(signal, urls, food=place["category"] == "饭店")
                       and signal.get("rating", 0) >= 4.6), None)
        if metric:
            rows.append(dict(name=place["name"], category=place["category"], google=metric, sources=place["sources"]))
    return dict(country=city["countryCode"], cityID=city["id"], providers=sources(city["countryCode"]),
                checked=(guide or {}).get("checked"), mode=mode if guide else "待核实", places=rows)
