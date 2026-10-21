#!/usr/bin/env python3
"""LOBSTERIA automatic pars.

Runs every night at 3 am Miami time (GitHub Actions). Reads Square orders for
both locations, works out what each service night used (recipe map in
config.json), forecasts the coming nights with an 8-week weighted average per
weekday, subtracts the latest staff counts and writes pars.json for the
kitchen iPad.

Env vars:
  SQUARE_TOKEN   read-only use; never printed
  COUNTS_CSV_URL published CSV of the Google Form responses (optional)
  FIXTURE        path to a JSON list of Square orders (testing, no network)
"""
import csv, io, json, math, os, re, sys, urllib.request
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))
TZ = ZoneInfo(CFG["timezone"])
CUT = CFG["service_day_cutoff_hour"]
SQUARE = "https://connect.squareup.com/v2"
G_PER_OZ = 28.3495
COUNT_KEYS = sorted(set(list(CFG["count_fields"]) + [c["frozen_count"] for c in CFG["products"].values() if c.get("frozen_count")]
                        + [k for c in CFG["products"].values() for k in c.get("extra_counts", {})] + ["burrata"]), key=len, reverse=True)


def service_day(ts):
    t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)
    return (t - timedelta(hours=CUT)).date()


def fetch_orders(start, end):
    token = os.environ["SQUARE_TOKEN"]
    body = {"location_ids": CFG["location_ids"], "limit": 500,
            "query": {"filter": {"state_filter": {"states": ["COMPLETED"]},
                                 "date_time_filter": {"closed_at": {"start_at": start.isoformat(), "end_at": end.isoformat()}}},
                      "sort": {"sort_field": "CLOSED_AT", "sort_order": "ASC"}}}
    out = []
    while True:
        req = urllib.request.Request(SQUARE + "/orders/search", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                                              "Square-Version": "2025-01-23"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        out += d.get("orders", [])
        if not d.get("cursor"):
            return out
        body["cursor"] = d["cursor"]


def line_use(li):
    """Prep used by one order line (quantity included)."""
    name, var = li.get("name") or "", li.get("variation_name") or ""
    qty = float(li.get("quantity") or 0)
    use = {}
    if any(x in name for x in CFG["exclude_items"]):
        return use, "event_box"
    for r in CFG["recipes"]:
        if re.search(r["item"], name, re.I) and ("variation" not in r or re.search(r["variation"], var, re.I)):
            for k, v in r["use"].items():
                use[k] = use.get(k, 0) + v * qty
            break
    for m in li.get("modifiers", []) or []:
        mn, mq = (m.get("name") or "").lower(), float(m.get("quantity") or 1)
        for r in CFG["modifier_rules"]:
            if re.search(r["modifier"], mn):
                for k, v in r["use"].items():
                    use[k] = use.get(k, 0) + v * mq * qty
                break
    return use, ("custom" if li.get("item_type") == "CUSTOM_AMOUNT" else None)


def nightly(orders):
    nights = {}
    for o in orders:
        d = service_day(o.get("closed_at") or o["created_at"])
        n = nights.setdefault(d, {"orders": 0, "use": {}, "custom": 0, "boxes": 0})
        n["orders"] += 1
        if o.get("location_id") != CFG["lobsteria_location_id"]:
            continue
        for li in o.get("line_items", []) or []:
            u, flag = line_use(li)
            if flag == "custom":
                n["custom"] += 1
            if flag == "event_box":
                n["boxes"] += 1
            for k, v in u.items():
                n["use"][k] = n["use"].get(k, 0) + v
    return nights


def read_counts():
    url = os.environ.get("COUNTS_CSV_URL")
    if not url:
        return []
    with urllib.request.urlopen(url, timeout=60) as r:
        rows = list(csv.DictReader(io.StringIO(r.read().decode("utf-8"))))
    return rows


def norm(s):
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def latest_count(rows):
    """Latest form row, keys normalised. Form question titles must contain the field key (e.g. 'lobster_clean_oz')."""
    best = None
    for r in rows:
        rr = {norm(k): v for k, v in r.items()}
        ts = rr.get("timestamp") or rr.get("marca_temporal") or ""
        try:
            t = datetime.strptime(ts, "%m/%d/%Y %H:%M:%S")
        except ValueError:
            continue
        if best is None or t > best[0]:
            best = (t, rr)
    if not best:
        return None, {}
    vals = {}
    for k, v in best[1].items():
        for field in COUNT_KEYS:
            if field in k:
                try:
                    vals[field] = float(str(v).replace(",", "."))
                except ValueError:
                    pass
                break
    return best[0], vals


def stockouts_by_night(rows):
    """Every count row's 'ran out' answer, attached to the service night it was counted on."""
    out = {}
    for r in rows:
        rr = {norm(k): v for k, v in r.items()}
        try:
            t = datetime.strptime(rr.get("timestamp") or rr.get("marca_temporal") or "", "%m/%d/%Y %H:%M:%S")
        except ValueError:
            continue
        night = (t - timedelta(hours=CUT)).date()
        for k, v in rr.items():
            if "ran_out" in k:
                for x in re.split(r"[,;/]", v or ""):
                    x = norm(x)
                    pid = next((p for p, c in CFG["products"].items() if x and (x == p or x in norm(c["en"]))), None)
                    if pid:
                        out.setdefault(night, set()).add(pid)
    return out


def forecast(nights, target, prod):
    w = CFG["week_weights"]
    num = den = 0.0
    for k, wk in enumerate(w, start=1):
        d = target - timedelta(days=7 * k)
        n = nights.get(d)
        if not n or n["orders"] == 0:          # closed: no orders in either location
            continue
        if prod in n.get("stockout", []):    # sold out that night: real demand unknown
            continue
        num += wk * n["use"].get(prod, 0)
        den += wk
    if den == 0:
        return None
    return num / den


def next_rd_days(today, n=2):
    out, d = [], today
    while len(out) < n:
        d += timedelta(days=1)
        if d.isoweekday() % 7 in CFG["restaurant_depot_weekdays"]:
            out.append(d)
    return out


def main():
    now = datetime.fromisoformat(os.environ['TODAY']).replace(tzinfo=TZ) if os.environ.get('TODAY') else datetime.now(TZ)
    tonight = now.date()   # the 3 am run plans this coming evening
    start = datetime.combine(tonight - timedelta(weeks=CFG["history_weeks"]), datetime.min.time(), TZ) + timedelta(hours=CUT)
    end = datetime.combine(tonight, datetime.min.time(), TZ) + timedelta(hours=CUT)
    orders = json.load(open(os.environ["FIXTURE"])) if os.environ.get("FIXTURE") else fetch_orders(start, end)
    nights = nightly(orders)

    count_rows = read_counts()
    counted_at, counts = latest_count(count_rows) if count_rows else (None, {})
    # staff count weights in grams; the recipe map works in oz
    for k in list(counts):
        if k.endswith("_oz"):
            counts[k] = counts[k] / G_PER_OZ
    last = tonight - timedelta(days=1)
    for night, prods in stockouts_by_night(count_rows).items():
        if night in nights:
            nights[night]["stockout"] = prods

    P = CFG["products"]
    days = [tonight + timedelta(days=i) for i in range(7)]
    fc = {p: [forecast(nights, d, p) for d in days] for p in P}
    out = {"generated": now.isoformat(timespec="minutes"), "service_date": tonight.isoformat(),
           "counted_at": counted_at.isoformat(timespec="minutes") if counted_at else None,
           "tonight": [], "week": {}, "buy": None, "alerts": []}

    for p, c in P.items():
        f0 = fc[p][0]
        if f0 is None:
            f0 = 0
        par = max(c.get("min", 0), math.ceil(f0 * (1 + c["buffer"]) - 1e-9)) if (f0 or c.get("min")) else 0
        out["week"][p] = [None if x is None else round(x, 1) for x in fc[p]]
        if c["kind"] == "info" or (par == 0 and not c.get("min")):
            continue
        row = {"id": p, "en": c["en"], "es": c["es"], "unit": c["unit"], "kind": c["kind"], "forecast": round(f0, 1), "par": par}
        have_key = next((k for k, v in CFG["count_fields"].items() if v == p), None)
        have = counts.get(have_key) if have_key else None
        if have is not None:
            row["on_hand"] = have
            need = max(0, par - have)
            row["todo"] = need
            if c["kind"] == "batch" and need > 0:
                b = math.ceil(need / c["batch"])
                row["todo_en"] = f"{c['batch_en']} × {b}" if b > 1 else c["batch_en"]
                row["todo_es"] = f"{c['batch_es']} × {b}" if b > 1 else c["batch_es"]
        if c["unit"] == "oz":   # show weights in grams on the iPad
            g10 = lambda x: int(math.ceil(x * G_PER_OZ / 10.0 - 1e-9) * 10)
            row.update(unit="g", forecast=round(f0 * G_PER_OZ), par=g10(par))
            if "on_hand" in row:
                row["on_hand"] = round(row["on_hand"] * G_PER_OZ)
                row["todo"] = max(0, row["par"] - row["on_hand"])
        out["tonight"].append(row)

    # Restaurant Depot list (runs every night; shown the night before a trip)
    rd1, rd2 = next_rd_days(tonight - timedelta(days=1))
    cover = [d for d in (tonight + timedelta(days=i) for i in range(14)) if rd1 <= d < rd2 + timedelta(days=1)]
    buy, make = [], []
    for p, c in P.items():
        if c["kind"] not in ("batch", "thaw", "frozen", "fresh", "fresh_simple"):
            continue
        need = sum((forecast(nights, d, p) or 0) for d in cover) * (1 + c.get("reserve_buffer", c["buffer"]))
        stock = 0.0
        if c.get("frozen_count") and counts.get(c["frozen_count"]) is not None:
            stock += counts[c["frozen_count"]] * c.get("frozen_unit_oz", 1)
        for ek, eoz in c.get("extra_counts", {}).items():
            if counts.get(ek) is not None:
                stock += counts[ek] * eoz
        hk = next((k for k, v in CFG["count_fields"].items() if v == p), None)
        if hk and counts.get(hk) is not None:
            stock += counts[hk]
        short = need - stock
        unit = c.get("buy_unit_oz") or c.get("frozen_unit_oz") or 1
        if short > 0 and need > 0:
            (make if c.get("make") else buy).append({"id": p, "en": c.get("buy_en", c["en"]), "es": c.get("buy_es", c["es"]), "qty": math.ceil(short / unit),
                        "unit_en": c.get("buy_unit_en", ""), "unit_es": c.get("buy_unit_es", ""),
                        "need": round(need, 1), "stock_counted": bool(counts)})
    out["buy"] = {"trip": rd1.isoformat(), "covers_until": rd2.isoformat(), "items": buy}
    out["make"] = make

    y = nights.get(last)
    if y and y["custom"]:
        out["alerts"].append({"en": f"Last night {y['custom']} sale(s) were rung as Custom Amount: ring the real menu item so the pars stay right.",
                              "es": f"Anoche {y['custom']} venta(s) se cobraron como Custom Amount: marcar el producto real para que los pars sean correctos."})
    if not counts:
        out["alerts"].append({"en": "No staff count received: pars shown, but no 'to do' amounts.",
                              "es": "No hay conteo del personal: se muestran los pars, sin cantidades a preparar."})
    # what ran out last night (from the close count) and the owner's dated notes, for the iPad "Today" briefing
    so = nights.get(last, {}).get("stockout") or set()
    out["ran_out_last_night"] = [{"id": p, "en": P[p]["en"], "es": P[p]["es"]} for p in sorted(so) if p in P]
    out["notes"] = [n for n in CFG.get("notes", []) if n.get("from", "0000") <= tonight.isoformat() <= n.get("to", "9999")]
    json.dump(out, open(os.path.join(HERE, "pars.json"), "w"), indent=1, ensure_ascii=False)
    print(f"pars.json written for {tonight} ({len(orders)} orders, {sum(1 for n in nights.values() if n['orders'])} open nights)")


if __name__ == "__main__":
    main()
