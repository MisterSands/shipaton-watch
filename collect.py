"""Local collector for Shipaton Watch.

Runs on Chris's Windows box at 05:30 (Task Scheduler via hidden-run.vbs, catch-up
enabled). The cloud routine's sandbox cannot reach any of these hosts, so this is
the only thing that can see the field.

Two jobs:
  1. Feeds  - HackerNoon, dev.to, Reddit, RevenueCat, shipaton.com, Medium -> raw/*.json
  2. Games  - EVERY 2026 game on apps.shipaton.com, re-fetched EVERY run, with rating
              history -> watch/games.json (what hallpass.cc/watcher.html renders) and
              watch/competitors.md. Deterministic: no LLM touches the games list.

Ratings come from the stores, not the showcase (which prints one number of unstated
origin): App Store via Apple's iTunes Lookup API (one batched call, US storefront),
Google Play via the public app detail page's ld+json. Play's robots.txt allows
/store/apps/details and disallows /store/getreviews, so reviews are never fetched there.
rating_count = App Store + Google Play; per-store series live in raw/store_history.json.

Games = slugs on /games (all years, server-rendered) intersected with slugs on /2026.
Detail pages stream their body through React Suspense, so fields are parsed from the
whole document's text, not <main>. A run that finds zero games never overwrites a
good list: it keeps the last one and flags the page stale.

  python collect.py            # fetch + commit + push
  python collect.py --no-push  # debug
"""
import gzip
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(ROOT, "raw")
WATCH = os.path.join(ROOT, "watch")
LOG = os.path.join(ROOT, "collect.log")
UA = ("Mozilla/5.0 (compatible; ShipatonWatch/1.0; +https://github.com/MisterSands/shipaton-watch; "
      "contact csands@gmail.com)")
DELAY = 1.1
SHOWCASE = "https://apps.shipaton.com"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
KEYWORD = re.compile(r"shipaton", re.I)
PROXIMITY = re.compile(
    r"roguelike|roguelite|rogue-like|survivor|bullet[- ]?heaven|bullet[- ]?hell|dungeon|\brpg\b|"
    r"role[- ]playing|arena|horde|waves? of|auto[- ]?battler|deck[- ]?build|hack[- ]and[- ]slash|"
    r"shooter|brawler|slash", re.I)
GENRES = [
    "Games", "Action", "Adventure", "Arcade", "Board", "Card", "Casino", "Casual", "Dice", "Educational",
    "Education", "Family", "Kids", "Music", "Music & Audio", "Puzzle", "Racing", "Role Playing",
    "Roleplaying", "Simulation", "Sports", "Strategy", "Trivia", "Word", "Entertainment", "Lifestyle",
    "Productivity", "Utilities", "Health & Fitness", "Social Networking", "Social", "Photo & Video",
    "Books", "Books & Reference", "Business", "Graphics & Design", "Navigation", "Maps & Navigation",
    "Travel", "Travel & Local", "Food & Drink", "Finance", "News", "News & Magazines",
    "Magazines & Newspapers", "Reference", "Shopping", "Weather", "Medical", "Developer Tools", "Stickers",
    "Communication", "Tools", "Personalization", "Art & Design", "Auto & Vehicles", "Beauty", "Comics",
    "Dating", "Events", "House & Home", "Libraries & Demo", "Parenting", "Photography",
    "Video Players & Editors"]
GENRE_RE = "(?:%s)\\b" % "|".join(re.escape(g) for g in sorted(GENRES, key=len, reverse=True))
STORE = re.compile(r"https://(?:apps\.apple\.com|play\.google\.com|galaxystore\.samsung\.com|galaxy\.store)"
                   r"[^\"'\s<\\]*")

FEEDS = {
    "hackernoon":       ("https://hackernoon.com/tagged/shipaton/feed", "rss"),
    "devto":            ("https://dev.to/feed/tag/shipaton", "rss"),
    "reddit":           ("https://www.reddit.com/search.rss?q=shipaton&sort=new", "atom"),
    "reddit_shipaton":  ("https://www.reddit.com/r/shipaton/new.rss", "atom"),
    "revenuecat_blog":  ("https://www.revenuecat.com/blog/rss.xml", "rss"),
    "shipaton_blog":    ("https://shipaton.com/blog", "html"),
    "medium":           ("https://medium.com/feed/tag/shipaton", "rss"),
}


# --------------------------------------------------------------------- utils
def log(msg):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{stamp} {msg}\n")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=40) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def text_of(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def tag(block, name):
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S | re.I)
    if not m:
        return ""
    v = re.sub(r"^<!\[CDATA\[(.*?)\]\]>$", r"\1", m.group(1).strip(), flags=re.S)
    return text_of(v)


# --------------------------------------------------------------------- feeds
def parse_rss(doc):
    out = []
    for it in re.findall(r"<item>(.*?)</item>", doc, re.S | re.I):
        out.append({"title": tag(it, "title"), "link": tag(it, "link"),
                    "date": tag(it, "pubDate") or tag(it, "dc:date"),
                    "author": tag(it, "dc:creator") or tag(it, "author"),
                    "summary": tag(it, "description")[:400]})
    return out


def parse_atom(doc):
    out = []
    for it in re.findall(r"<entry>(.*?)</entry>", doc, re.S | re.I):
        link = re.search(r'<link[^>]*href="([^"]+)"', it)
        out.append({"title": tag(it, "title"), "link": html.unescape(link.group(1)) if link else "",
                    "date": tag(it, "updated") or tag(it, "published"), "author": tag(it, "name"),
                    "summary": (tag(it, "content") or tag(it, "summary"))[:400]})
    return out


def parse_html_links(doc, base):
    out, seen = [], set()
    for href, body in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', doc, re.S | re.I):
        t = text_of(body)
        if not (KEYWORD.search(t) or KEYWORD.search(href)):
            continue
        if href.startswith("/"):
            href = base + href
        if href in seen or not href.startswith("http"):
            continue
        seen.add(href)
        out.append({"title": t[:160], "link": href, "date": "", "author": "", "summary": ""})
    return out


def collect_feeds(stamp, status):
    for name, (url, kind) in FEEDS.items():
        # Reddit throttles hard per-IP; two reddit.com calls 1.1s apart earn a 429.
        if "reddit.com" in url:
            time.sleep(4)
        try:
            try:
                doc = fetch(url)
            except Exception as e:
                if getattr(e, "code", None) == 429:
                    time.sleep(15)
                    doc = fetch(url)
                else:
                    raise
            if kind == "rss":
                items = parse_rss(doc)
            elif kind == "atom":
                items = parse_atom(doc)
            else:
                items = parse_html_links(doc, re.match(r"https?://[^/]+", url).group(0))
            if name == "revenuecat_blog":
                items = [i for i in items if KEYWORD.search(i["title"] + i["summary"])]
            write_json(os.path.join(RAW, f"{name}.json"),
                       {"source": name, "url": url, "fetched_at": stamp, "count": len(items), "items": items})
            status[name] = f"ok {len(items)}"
        except Exception as e:
            status[name] = f"FAIL {type(e).__name__} {getattr(e, 'code', '')}"
        time.sleep(DELAY)


# --------------------------------------------------------------------- games
def page_text(doc):
    doc = re.sub(r"<(script|style|noscript|template)\b[^>]*>.*?</\1>", " ", doc, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", doc))).strip()


def app_slugs(doc):
    out = []
    for s in re.findall(r'href="/app/([^"/?#]+)"', doc):
        if s not in out:
            out.append(s)
    return out


def parse_detail(doc, slug):
    """Fields from the page's plain-text spine, which reads:
    'Category X Developer Y Ratings 5.0 ★★★★★ 26 ratings ... About this app <desc>
     Information Seller Y Compatibility iPhone Genres Games, Action, Racing You might also like'"""
    t = page_text(doc)
    og = re.search(r'property="og:title"\s+content="([^"]*)"', doc)
    ti = re.search(r"<title>(.*?)\s*·", doc, re.S)
    name = html.unescape((og or ti).group(1)).strip() if (og or ti) else slug

    # Any of Seller / Compatibility / Genres can be blank, and "You might also like" is
    # sometimes absent, so each field is cut separately and genres match a known list.
    j = t.find("Information Seller")
    seg = t[j:j + 600] if j >= 0 else ""
    sm = re.search(r"Seller (.{1,80}?)(?= Compatibility\b)", seg)
    cm = re.search(r"Compatibility(.{0,80}?)(?= Genres\b| You might also like\b| More from\b|$)", seg)
    gm = re.search(r"Genres (%s(?:, %s)*)" % (GENRE_RE, GENRE_RE), seg)
    compat = cm.group(1).strip() if cm else ""
    genres = gm.group(1).split(", ") if gm else []

    cat = re.search(r"Category (.{1,40}?) Developer (.{1,80}?) (?=Ratings\b|About this app\b)", t)
    category = cat.group(1).strip() if cat else (genres[0] if genres else "")
    seller = (sm.group(1).strip() if sm else "") or (cat.group(2).strip() if cat else "")

    rm = re.search(r"Ratings ([\d.]+) ★+ ([\d,]+) ratings?", t)
    store = sorted(set(u.rstrip("\\'\"") for u in STORE.findall(doc)))

    plats = [p for p in ("iPhone", "iPad", "Mac", "Apple Watch", "Apple TV", "Android")
             if re.search(r"\b%s\b" % p, compat)]
    if not plats:
        if any("apple.com" in u for u in store):
            plats.append("iPhone")
        if any("play.google" in u for u in store):
            plats.append("Android")
    if any("galaxy" in u for u in store):
        plats.append("Galaxy")

    j = t.find("Information Seller")
    k = t.rfind("About this app", 0, j if j > 0 else len(t))
    about = t[k + len("About this app"):j].strip() if (k >= 0 and j > k) else ""
    tagline = re.split(r"(?<=[.!?])\s", about, maxsplit=1)[0][:160] if about else ""

    apple = next((u for u in store if "apple.com" in u), "")
    play = next((u for u in store if "play.google" in u), "")
    return {
        "slug": slug,
        "name": name,
        "url": f"{SHOWCASE}/app/{slug}",
        "tagline": tagline,
        "description": about[:600],
        "category": category,
        "genres": genres,
        "seller": seller,
        "platforms": plats,
        "rating": float(rm.group(1)) if rm else None,
        "rating_count": int(rm.group(2).replace(",", "")) if rm else 0,
        "store": apple or play or (store[0] if store else ""),
        "store_links": store,
    }


def count_on_or_before(hist, day):
    days = sorted(d for d in hist if d <= day)
    return hist[days[-1]] if days else None


APPLE_ID = re.compile(r"apps\.apple\.com/[^\s\"']*?id(\d+)")
PLAY_ID = re.compile(r"play\.google\.com/store/apps/details\?id=([A-Za-z0-9._]+)")


def store_ids(rec, old):
    # yesterday's store urls ride along so a blank or failed showcase fetch doesn't drop a store
    links = " ".join((rec.get("store_links") or []) + [rec.get("store") or ""]
                     + [(old.get(k) or {}).get("url", "") for k in ("ios", "play")])
    a, g = APPLE_ID.search(links), PLAY_ID.search(links)
    return (a.group(1) if a else None), (g.group(1) if g else None)


def apple_ratings(ids):
    """Every App Store id in one iTunes Lookup call per 150 (Apple's documented public API).
    US storefront. Ids missing from the result are not sold in the US."""
    out, ids = {}, sorted(set(ids))
    for i in range(0, len(ids), 150):
        data = json.loads(fetch("https://itunes.apple.com/lookup?country=us&id=" + ",".join(ids[i:i + 150])))
        for a in data.get("results", []):
            avg = a.get("averageUserRating")
            out[str(a.get("trackId"))] = {
                "rating": round(avg, 2) if avg else None,
                "count": int(a.get("userRatingCount") or 0),
                "released": (a.get("releaseDate") or "")[:10],
                "version": a.get("version") or "",
                "url": (a.get("trackViewUrl") or "").split("?")[0],
            }
        time.sleep(DELAY)
    return out


def play_ratings(pkg):
    """Rating, rating count and install band from the public Google Play detail page."""
    url = f"https://play.google.com/store/apps/details?id={pkg}"
    doc = fetch(url + "&hl=en_US&gl=US")
    app = None
    for m in re.finditer(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', doc, re.S):
        try:
            j = json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(j, dict) and ("aggregateRating" in j or j.get("@type") == "SoftwareApplication"):
            app = j
            break
    if app is None:
        raise ValueError("no ld+json app block")
    agg = app.get("aggregateRating") or {}
    inst = re.search(r"([0-9][0-9.,]*[KMB]?\+)\s*Downloads", page_text(doc))
    val = float(agg.get("ratingValue") or 0)
    return {"rating": round(val, 2) if val else None, "count": int(agg.get("ratingCount") or 0),
            "installs": inst.group(1) if inst else "", "url": url}


def store_delta(sh, key, n, day):
    """Growth of one store's count since its last reading on or before `day`; None without one."""
    if n is None:
        return None
    days = sorted(d for d, v in sh.items() if d <= day and v.get(key) is not None)
    return (n - sh[days[-1]][key]) if days else None


def add(*vals):
    got = [v for v in vals if v is not None]
    return sum(got) if got else None


def backfill_history():
    """First run only: rebuild per-day rating counts from this repo's git history of
    raw/showcase_2026.json, so the page has real 1-day and 7-day deltas immediately."""
    hist = {}
    r = subprocess.run(["git", "log", "--format=%H %cs", "--", "raw/showcase_2026.json"], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", creationflags=NOWIN)
    for line in reversed([x for x in r.stdout.split("\n") if x.strip()]):
        sha, day = line.split()[:2]
        show = subprocess.run(["git", "show", f"{sha}:raw/showcase_2026.json"], cwd=ROOT,
                              capture_output=True, creationflags=NOWIN)
        try:
            data = json.loads(show.stdout.decode("utf-8", "replace"))
        except Exception:
            continue
        for a in data.get("apps", []):
            n = a.get("rating_count")
            if isinstance(n, int) and n > 0:
                hist.setdefault(a["slug"], {})[day] = n
    return hist


def mark_stale(prev, path, stamp, err):
    if prev.get("games"):
        prev.update({"stale": True, "error": err, "checked_at": stamp})
        write_json(path, prev)
    else:
        write_json(path, {"generated_at": stamp, "stale": True, "error": err, "count": 0, "games": []})


def fmt_delta(n):
    if n is None:
        return "·"
    return f"+{n}" if n > 0 else str(n)


def md(s):
    return str(s or "").replace("|", "\\|")


def write_competitors(p):
    rows = []
    for g in p["games"]:
        mark = "⚠ " if g["proximity"] else ""
        new = " 🆕" if g["new"] else ""
        def cell(st):
            if not st:
                return "·"
            return f"[{st['rating'] or '–'} ({st['count']})]({st['url']})" if st.get("url") else f"({st['count']})"
        links = f"[showcase]({g['url']})"
        rows.append(
            f"| {g['rank']} | {mark}{md(g['name'])}{new} | {md(', '.join(g['genres']) or g['category'])} | "
            f"{md(g['seller'])} | {cell(g.get('ios'))} | {cell(g.get('play'))} | {g.get('installs') or '·'} | "
            f"{g.get('rating_count') or 0} | {fmt_delta(g['delta_1d'])} | "
            f"{fmt_delta(g['delta_7d'])} | {g['first_seen']} | {links} |")
    head = (
        "# Shipaton 2026: every game in the field\n\n"
        f"Generated {p['generated_at']} by the local collector from {SHOWCASE}/games ∩ /2026. "
        f"**{p['count']} games** · {p['rated']} rated · +{len(p['new_today'])} new today · "
        f"{len(p['gaining'])} gaining. Ratings read from the stores (App Store US + Google Play), "
        "sorted by their total. ⚠ = genre proximity to HALL PASS. "
        "🆕 = first seen in the last 3 days. Regenerated every run. Do not edit by hand.\n\n"
        "| # | Game | Genres | Developer | App Store ★ (n) | Google Play ★ (n) | Installs | Total | "
        "Δ 1d | Δ 7d | First seen | Links |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    with open(os.path.join(WATCH, "competitors.md"), "w", encoding="utf-8") as f:
        f.write(head + "\n".join(rows) + "\n")


def collect_games(stamp, today, status):
    games_path = os.path.join(WATCH, "games.json")
    hist_path = os.path.join(RAW, "ratings_history.json")
    all_path = os.path.join(RAW, "showcase_2026.json")
    prev = load_json(games_path, {})
    prev_by = {g["slug"]: g for g in prev.get("games", [])}
    roster = {a["slug"]: a for a in load_json(all_path, {}).get("apps", [])}
    history = load_json(hist_path, None)
    if history is None:
        history = backfill_history()
    shist_path = os.path.join(RAW, "store_history.json")
    shist = load_json(shist_path, {})

    try:
        idx = fetch(SHOWCASE + "/2026")
        time.sleep(DELAY)
        gdoc = fetch(SHOWCASE + "/games")
        time.sleep(DELAY)
    except Exception as e:
        msg = f"FAIL index fetch {type(e).__name__} {getattr(e, 'code', '')}"
        status["showcase_games"] = msg
        mark_stale(prev, games_path, stamp, msg)
        return

    y26 = app_slugs(idx)
    total = re.search(r"Explore all (\d+) published apps", idx)
    gall = app_slugs(gdoc)
    gdecl = re.search(r"\b(\d+) games\b", page_text(gdoc))
    in26 = set(y26)
    slugs = [s for s in gall if s in in26]

    # all-apps roster: new slugs recorded with first_seen; cheap, no detail fetch
    new_apps = [s for s in y26 if s not in roster]
    for s in new_apps:
        roster[s] = {"slug": s, "url": f"{SHOWCASE}/app/{s}", "first_seen": today}
    write_json(all_path, {"source": "showcase_2026", "url": SHOWCASE + "/2026", "fetched_at": stamp,
                          "declared_total": int(total.group(1)) if total else len(y26), "count": len(y26),
                          "new_today": new_apps, "apps": [roster[s] for s in y26 if s in roster]})
    status["showcase_2026"] = f"ok {len(y26)} apps (+{len(new_apps)} new)"

    if not slugs:
        msg = (f"FAIL 0 games in /games ∩ /2026 ({len(gall)} game slugs all-years, {len(y26)} 2026 apps) "
               "(showcase layout may have changed)")
        status["showcase_games"] = msg
        mark_stale(prev, games_path, stamp, msg)
        return

    t0 = date.fromisoformat(today)
    yday, wk, new_cut = ((t0 - timedelta(days=n)).isoformat() for n in (1, 7, 2))
    games, failed = [], 0
    for s in slugs:
        try:
            rec = parse_detail(fetch(f"{SHOWCASE}/app/{s}"), s)
            if not (rec["genres"] or rec["seller"] or rec["category"]):
                # the streamed body occasionally arrives empty; one retry, then keep
                # yesterday's fields rather than blanking a row
                time.sleep(3)
                rec = parse_detail(fetch(f"{SHOWCASE}/app/{s}"), s)
            old = prev_by.get(s, {})
            for k in ("tagline", "description", "category", "genres", "seller", "platforms", "store"):
                if not rec.get(k) and old.get(k):
                    rec[k] = old[k]
            rec["stale"] = False
        except Exception:
            failed += 1
            rec = dict(prev_by[s], stale=True) if s in prev_by else None
        time.sleep(DELAY)
        if rec is None:
            continue
        seen = prev_by.get(s) or roster.get(s) or {}
        rec["first_seen"] = seen.get("first_seen") or today
        if not rec["stale"]:
            history.setdefault(s, {})[today] = rec.get("rating_count") or 0   # showcase's own number
        rec["new"] = rec["first_seen"] >= new_cut
        rec["new_today"] = rec["first_seen"] == today
        rec["proximity"] = bool(PROXIMITY.search(
            " ".join([rec["name"], rec.get("description", ""), " ".join(rec.get("genres", []))])))
        games.append(rec)

    # Store ratings. App Store: one batched lookup. Google Play: one page per app.
    ids = {g["slug"]: store_ids(g, prev_by.get(g["slug"], {})) for g in games}
    try:
        apple, apple_ok = apple_ratings(a for a, _ in ids.values() if a), True
    except Exception as e:
        apple, apple_ok = {}, False
        log(f"apple lookup FAIL {type(e).__name__} {getattr(e, 'code', '')}")
    play_tried = play_failed = 0
    for g in games:
        s, old = g["slug"], prev_by.get(g["slug"], {})
        a_id, p_id = ids[s]
        ios = apple.get(a_id) if a_id else None
        ios_fresh = ios is not None
        if a_id and not apple_ok:
            ios = old.get("ios")
        play, play_fresh = None, False
        if p_id:
            play_tried += 1
            try:
                play, play_fresh = play_ratings(p_id), True
            except Exception:
                play_failed += 1
                play = old.get("play")
            time.sleep(DELAY)

        sh = shist.get(s)
        if sh is None:
            # single-store games inherit the showcase series: it could only have been that store
            sh = shist[s] = {}
            only = "ios" if (a_id and not p_id) else "play" if (p_id and not a_id) else None
            if only:
                for d, n in history.get(s, {}).items():
                    if d < today:
                        sh[d] = {only: n}

        ic = ios["count"] if ios else None
        pc = play["count"] if play else None
        g["showcase_count"] = g.get("rating_count") or 0
        if ic is not None or pc is not None:
            g["rating_count"] = (ic or 0) + (pc or 0)
            w = [(x["rating"], x["count"]) for x in (ios, play) if x and x.get("rating") and x.get("count")]
            g["rating"] = round(sum(r * c for r, c in w) / sum(c for _, c in w), 2) if w else None
        fresh = {"ios": ic if ios_fresh else None, "play": pc if play_fresh else None}
        g["delta_1d_ios"] = store_delta(sh, "ios", fresh["ios"], yday)
        g["delta_1d_play"] = store_delta(sh, "play", fresh["play"], yday)
        g["delta_1d"] = add(g["delta_1d_ios"], g["delta_1d_play"])
        g["delta_7d"] = add(store_delta(sh, "ios", fresh["ios"], wk), store_delta(sh, "play", fresh["play"], wk))
        g["gaining"] = bool(g["delta_1d"] and g["delta_1d"] > 0)
        row = {k: v for k, v in fresh.items() if v is not None}
        if row:
            sh[today] = dict(sh.get(today, {}), **row)
        g["ios"], g["play"] = ios, play
        g["installs"] = (play or {}).get("installs", "")

    parsed = [g for g in games if not g["stale"]]
    empties = sum(1 for g in parsed if not g.get("genres") and not g.get("seller"))
    warning = ""
    if parsed and empties > len(parsed) * 0.5:
        warning = (f"{empties}/{len(parsed)} games parsed without genres or developer: "
                   "showcase detail layout may have changed")

    games.sort(key=lambda g: (-(g.get("rating_count") or 0), g["name"].lower()))
    keep = ("rank", "slug", "name", "url", "tagline", "description", "category", "genres", "seller",
            "platforms", "rating", "rating_count", "ios", "play", "installs", "showcase_count",
            "delta_1d", "delta_7d", "delta_1d_ios", "delta_1d_play", "first_seen", "new",
            "new_today", "gaining", "proximity", "store", "stale")
    for i, g in enumerate(games, 1):
        g["rank"] = i
    payload = {
        "generated_at": stamp, "date": today, "stale": False, "error": "", "warning": warning,
        "source": SHOWCASE + "/games",
        "apps_2026": int(total.group(1)) if total else len(y26),
        "games_all_years": int(gdecl.group(1)) if gdecl else len(gall),
        "count": len(games),
        "rated": sum(1 for g in games if g.get("rating_count")),
        "new_today": [g["slug"] for g in games if g["new_today"]],
        "gaining": [g["slug"] for g in games if g["gaining"]],
        "failed": failed,
        "stores": {"app_store_ok": apple_ok, "app_store": len(apple), "play_tried": play_tried,
                   "play_failed": play_failed},
        "games": [{k: g.get(k) for k in keep} for g in games],
    }
    write_json(games_path, payload)
    write_json(os.path.join(RAW, "showcase_games.json"), dict(payload, apps=games))
    write_json(hist_path, history)
    write_json(shist_path, shist)
    write_competitors(payload)
    status["showcase_games"] = (
        f"ok {len(games)} games · {payload['rated']} rated · +{len(payload['new_today'])} new · "
        f"{len(payload['gaining'])} gaining" + (f" · {failed} failed" if failed else "")
        + f" · app store {'ok' if apple_ok else 'FAIL'} {len(apple)} · play {play_tried - play_failed}/{play_tried}"
        + (f" · WARNING {warning}" if warning else ""))


# ---------------------------------------------------------------------- main
def collect():
    os.makedirs(RAW, exist_ok=True)
    now = datetime.now(timezone.utc)
    stamp, today = now.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%d")
    status = {}
    collect_feeds(stamp, status)
    collect_games(stamp, today, status)
    write_json(os.path.join(RAW, "_status.json"), {"fetched_at": stamp, "sources": status})
    log("collect " + " | ".join(f"{k}={v}" for k, v in status.items()))
    return status


def push():
    def git(*a):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, creationflags=NOWIN)
    # Commit first, then rebase onto whatever the cloud routine pushed (it only writes
    # watch/*.md report files and seen.txt, so this never conflicts), then push.
    git("add", "raw/", "watch/games.json", "watch/competitors.md")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    committed = git("commit", "-q", "-m", f"collect: {day}").returncode == 0
    r = git("pull", "-q", "--rebase", "origin", "master")
    if r.returncode != 0:
        log("pull --rebase FAIL " + r.stderr.strip()[:200])
        git("rebase", "--abort")
    if committed:
        p = git("push", "-q", "origin", "master")
        log("push " + ("ok" if p.returncode == 0 else "FAIL " + p.stderr.strip()[:200]))
    else:
        log("push skipped (nothing new)")


if __name__ == "__main__":
    st = collect()
    if "--no-push" not in sys.argv:
        push()
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(st, indent=1, ensure_ascii=False))
