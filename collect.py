"""Local feed collector for Shipaton Watch.

Runs on Chris's Windows box (no egress restrictions) a little before the 6am
cloud routine. Pulls every source the cloud sandbox cannot reach, writes one
JSON snapshot per source into raw/, commits and pushes. The cloud routine then
reads raw/ from the repo instead of fetching the open web.

The showcase (apps.shipaton.com/2026) is RevenueCat's own directory of every
published 2026 entrant. Its cards are server-rendered, and each /app/<slug>
detail page carries store link, category, developer, platforms, rating and
description. We crawl the directory for slugs and fetch detail pages only for
slugs we have not seen before, so the first run costs ~6 minutes and every
run after that costs seconds.

Stdlib only. Silent — no windows, no prompts. Task Scheduler runs it through
hidden-run.vbs at 05:30 local.

  python collect.py            # fetch + commit + push
  python collect.py --no-push  # fetch only (debug)
"""
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(ROOT, "raw")
LOG = os.path.join(ROOT, "collect.log")
UA = ("Mozilla/5.0 (compatible; ShipatonWatch/1.0; +https://github.com/MisterSands/shipaton-watch; "
      "contact csands@gmail.com)")
DELAY = 1.1
SHOWCASE = "https://apps.shipaton.com"
KEYWORD = re.compile(r"shipaton", re.I)
GAMEY = re.compile(r"roguelike|roguelite|rogue-like|survivor|bullet[- ]?heaven|bullet[- ]?hell|dungeon|"
                   r"\brpg\b|arena|horde|wave[s]? of|auto[- ]?battler|deckbuild", re.I)

FEEDS = {
    # name: (url, kind)   kind = rss | atom | html
    "hackernoon":       ("https://hackernoon.com/tagged/shipaton/feed", "rss"),
    "devto":            ("https://dev.to/feed/tag/shipaton", "rss"),
    "reddit":           ("https://www.reddit.com/search.rss?q=shipaton&sort=new", "atom"),
    "reddit_shipaton":  ("https://www.reddit.com/r/shipaton/new.rss", "atom"),   # unofficial build-log sub
    "revenuecat_blog":  ("https://www.revenuecat.com/blog/rss.xml", "rss"),
    "shipaton_blog":    ("https://shipaton.com/blog", "html"),
    "medium":           ("https://medium.com/feed/tag/shipaton", "rss"),
}


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
            import gzip
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", "replace")


def text(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def tag(block, name):
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.S | re.I)
    if not m:
        return ""
    v = re.sub(r"^<!\[CDATA\[(.*?)\]\]>$", r"\1", m.group(1).strip(), flags=re.S)
    return text(v)


# ------------------------------------------------------------------ feeds
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
        t = text(body)
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
        try:
            doc = fetch(url)
            if kind == "rss":
                items = parse_rss(doc)
            elif kind == "atom":
                items = parse_atom(doc)
            else:
                items = parse_html_links(doc, re.match(r"https?://[^/]+", url).group(0))
            if name == "revenuecat_blog":
                items = [i for i in items if KEYWORD.search(i["title"] + i["summary"])]
            with open(os.path.join(RAW, f"{name}.json"), "w", encoding="utf-8") as f:
                json.dump({"source": name, "url": url, "fetched_at": stamp, "count": len(items),
                           "items": items}, f, ensure_ascii=False, indent=1)
            status[name] = f"ok {len(items)}"
        except Exception as e:
            status[name] = f"FAIL {type(e).__name__} {getattr(e, 'code', '')}"
        time.sleep(DELAY)


# --------------------------------------------------------------- showcase
def parse_detail(doc, slug):
    """Pull the structured fields off an /app/<slug> page. The page renders a
    plain-text spine: 'Category X Developer Y Ratings Z ... About this app ...'."""
    title = re.search(r"<title>(.*?)\s*·", doc, re.S)
    m = re.search(r"<main[^>]*>(.*?)</main>", doc, re.S)
    body = re.sub(r"\s+", " ", text(m.group(1) if m else doc))
    store = sorted(set(u.rstrip("\\'\"") for u in re.findall(
        r'https://(?:apps\.apple\.com|play\.google\.com|galaxystore\.samsung\.com|galaxy\.store)[^"\'\s<\\]*', doc)))
    cat = re.search(r"Category\s+(.+?)\s+Developer\s", body)
    dev = re.search(r"Developer\s+(.+?)\s+(?:Ratings|iPhone|iPad|Android|About this app)", body)
    rating = re.search(r"Ratings\s+([\d.]+)", body)
    nrat = re.search(r"·\s*([\d,]+)\s+ratings?", body)
    plats = [p for p in ("iPhone", "iPad", "Mac", "Apple Watch", "Android", "Galaxy")
             if re.search(rf"\b{p}\b", body.split("About this app")[0])]
    about = body.split("About this app", 1)[1].strip() if "About this app" in body else ""
    tagline = re.search(r"^(?:.*?)\s{2,}(.+?)\s+View in", body)
    return {
        "slug": slug,
        "name": text(title.group(1)) if title else slug,
        "url": f"{SHOWCASE}/app/{slug}",
        "category": cat.group(1).strip() if cat else "",
        "developer": dev.group(1).strip() if dev else "",
        "platforms": plats,
        "rating": float(rating.group(1)) if rating else None,
        "rating_count": int(nrat.group(1).replace(",", "")) if nrat else None,
        "store_links": store,
        "description": about[:1200],
        "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def collect_showcase(stamp, status):
    path = os.path.join(RAW, "showcase_2026.json")
    known = {}
    if os.path.exists(path):
        try:
            known = {a["slug"]: a for a in json.load(open(path, encoding="utf-8")).get("apps", [])}
        except Exception:
            known = {}
    try:
        index = fetch(f"{SHOWCASE}/2026")
    except Exception as e:
        status["showcase_2026"] = f"FAIL {type(e).__name__} {getattr(e, 'code', '')}"
        return
    slugs = []
    for s in re.findall(r'href="/app/([^"/]+)"', index):
        if s not in slugs:
            slugs.append(s)
    total = re.search(r"Explore all (\d+) published apps", index)
    new = [s for s in slugs if s not in known]
    # Every Games-category entrant is a competitor for Best Game, so re-fetch all
    # of them daily — rating_count growth is the only public traction signal.
    refresh = [s for s in slugs if s in known and known[s].get("category", "").lower() == "games"]
    fetched = failed = refreshed = 0
    for s in new + refresh:
        try:
            rec = parse_detail(fetch(f"{SHOWCASE}/app/{s}"), s)
            if s in known:
                prev = known[s]
                rec["first_seen"] = prev.get("first_seen", rec["first_seen"])
                rec["rating_count_prev"] = prev.get("rating_count")
                refreshed += 1
            else:
                fetched += 1
            known[s] = rec
        except Exception:
            failed += 1
        time.sleep(DELAY)
    apps = [known[s] for s in slugs if s in known] + [a for s, a in known.items() if s not in slugs]
    for a in apps:
        # The QR image's alt text ("QR code for <Name>") occasionally wins the
        # <title> regex; strip it so the stored name is the app's real name.
        a["name"] = re.sub(r"^QR code for\s+", "", a.get("name", "")).strip() or a["slug"]
        a["gamey"] = bool(GAMEY.search(" ".join([a.get("name", ""), a.get("description", "")])))
        # is_game is the store category, full stop. gamey is a separate genre-
        # proximity hint (roguelike/survivor/arena...) and can be true for non-games.
        a["is_game"] = a.get("category", "").lower() == "games"
        a["gaining"] = bool(a.get("rating_count") and a.get("rating_count_prev") is not None
                            and a["rating_count"] > a["rating_count_prev"])
    games = [a for a in apps if a["is_game"]]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"source": "showcase_2026", "url": f"{SHOWCASE}/2026", "fetched_at": stamp,
                   "declared_total": int(total.group(1)) if total else None, "count": len(apps),
                   "new_today": new, "apps": apps}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(RAW, "showcase_games.json"), "w", encoding="utf-8") as f:
        json.dump({"source": "showcase_games", "fetched_at": stamp, "count": len(games),
                   "flagged_roguelike": [a["slug"] for a in games if a["gamey"]], "apps": games},
                  f, ensure_ascii=False, indent=1)
    status["showcase_2026"] = f"ok {len(apps)} apps (+{fetched} new, {failed} failed) · {len(games)} games"


# ------------------------------------------------------------------- main
def collect():
    os.makedirs(RAW, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = {}
    collect_feeds(stamp, status)
    collect_showcase(stamp, status)
    with open(os.path.join(RAW, "_status.json"), "w", encoding="utf-8") as f:
        json.dump({"fetched_at": stamp, "sources": status}, f, indent=1)
    log("collect " + " | ".join(f"{k}={v}" for k, v in status.items()))
    return status


def push():
    def git(*a):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # Commit our raw/ FIRST, then rebase onto whatever the cloud routine pushed
    # (it only ever touches watch/, so the rebase is conflict-free), then push.
    # Pulling before committing fails with "unstaged changes" whenever raw/ is dirty.
    git("add", "raw/")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    committed = git("commit", "-q", "-m", f"raw: {day}").returncode == 0
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
    print(json.dumps(st, indent=1))
