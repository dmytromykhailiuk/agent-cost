from . import core

PRICING_PATH = core.SKILL_DIR / "config" / "pricing.json"
TOKEN_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")

_cache = None


def load_pricing():
    global _cache
    if _cache is None:
        data = core.read_json(PRICING_PATH, {})
        _cache = data if isinstance(data, dict) else {}
    return _cache


def resolve_model(name):
    if not name or not isinstance(name, str):
        return None
    p = load_pricing()
    models = p.get("models") or {}
    aliases = p.get("aliases") or {}
    base = name.split("[", 1)[0].strip()
    for candidate in (name, base):
        if candidate in models:
            return candidate
        if candidate in aliases:
            return aliases[candidate]
    for alias, full in aliases.items():
        if base.startswith(f"claude-{alias}-"):
            return full
    for full in models:
        if base.startswith(full + "-"):
            return full
    return None


def _numbers(usage):
    if all(k in usage for k in TOKEN_KEYS):
        return usage
    from .transcripts import usage_numbers

    return usage_numbers(usage)


def request_cost(model, usage, long_context=True):
    full = resolve_model(model)
    if full is None or not isinstance(usage, dict):
        return None
    n = _numbers(usage)
    p = load_pricing()
    rates = (p.get("models") or {}).get(full) or {}
    lc = rates.get("long_context")
    context = n["input"] + n["cache_write_5m"] + n["cache_write_1h"] + n["cache_read"]
    if long_context and isinstance(lc, dict) and context > lc.get("threshold", float("inf")):
        rates = lc
    per = float(p.get("per_tokens") or 1_000_000)
    return sum(n[k] * float(rates.get(k, 0) or 0) for k in TOKEN_KEYS) / per


def price_by_model(by_model):
    total = 0.0
    known = False
    unknown = []
    priced = {}
    for model, row in (by_model or {}).items():
        row = dict(row or {})
        cost = row.get("cost_usd")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            cost = request_cost(model, {k: int(row.get(k) or 0) for k in TOKEN_KEYS}, long_context=False)
        if cost is None:
            unknown.append(model)
            row["cost_usd"] = None
        else:
            known = True
            total += cost
            row["cost_usd"] = round(cost, 8)
        priced[model] = row
    if by_model and not known:
        return None, priced, unknown
    return round(total, 8), priced, unknown
