"""
Validate every URL in feeds.txt and every query in pubmed_queries.txt.

Run locally:            python validate_feeds.py
Run on GitHub:          Actions -> Validate feeds -> Run workflow

Publisher feed URLs rot constantly. Run this whenever the digest looks thin.
Anything reported EMPTY or FETCH_FAIL should be fixed or deleted from feeds.txt.
"""
import os, sys, json, time, urllib.parse, urllib.request
import concurrent.futures as cf
from datetime import datetime, timezone

import feedparser

UA = "tocify-validator/2.0 (+https://github.com/SamSievertsen/tocify)"
TIMEOUT = int(os.getenv("FEED_TIMEOUT", "45"))
STALE_DAYS = int(os.getenv("STALE_DAYS", "120"))


def load_pairs(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            name, val = ([x.strip() for x in s.split("|", 1)] if "|" in s else (None, s))
            if val:
                out.append((name or val[:40], val))
    return out


def newest(d):
    best = None
    for e in d.entries:
        for attr in ("published_parsed", "updated_parsed"):
            t = getattr(e, attr, None)
            if t:
                try:
                    dt = datetime(*t[:6], tzinfo=timezone.utc)
                except Exception:
                    continue
                if best is None or dt > best:
                    best = dt
    return best


def check_feed(pair):
    name, url = pair
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        })
        raw = urllib.request.urlopen(req, timeout=TIMEOUT).read()
    except Exception as e:
        return (name, url, "FETCH_FAIL", 0, None, f"{type(e).__name__}: {str(e)[:80]}")

    d = feedparser.parse(raw)
    if not d.entries:
        head = raw[:300].decode("utf-8", "ignore").lower()
        why = "returned an HTML page, not a feed" if "<html" in head or "<!doctype html" in head else "parsed but 0 entries"
        return (name, url, "EMPTY", 0, None, why)

    nd = newest(d)
    age = (datetime.now(timezone.utc) - nd).days if nd else None
    status = "STALE" if (age is not None and age > STALE_DAYS) else "OK"
    sample = (d.entries[0].get("title") or "")[:64].replace("\n", " ")
    return (name, url, status, len(d.entries), age, sample)


def check_pubmed(pair):
    name, term = pair
    params = {"db": "pubmed", "term": term, "retmode": "json", "retmax": 1, "reldate": 365, "datetype": "edat", "tool": "tocify"}
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(params)
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=TIMEOUT).read()
        res = json.loads(raw).get("esearchresult", {})
        if res.get("errorlist") or res.get("warninglist"):
            detail = json.dumps({k: v for k, v in res.items() if k in ("errorlist", "warninglist")})[:80]
            return (name, term[:60], "WARN", int(res.get("count", 0)), None, detail)
        count = int(res.get("count", 0))
        status = "OK" if count else "EMPTY"
        return (name, term[:60], status, count, None, f"{count} hits in last 365d")
    except Exception as e:
        return (name, term[:60], "FETCH_FAIL", 0, None, f"{type(e).__name__}: {str(e)[:80]}")


def report(title, rows, order):
    print(f"\n{'=' * 118}\n{title}\n{'=' * 118}")
    print(f"{'STATUS':<12}{'N':>6}{'AGEd':>6}  {'NAME':<32} DETAIL")
    print("-" * 118)
    rows.sort(key=lambda r: (order.get(r[2], 9), r[0].lower()))
    for name, _u, status, n, age, detail in rows:
        print(f"{status:<12}{n:>6}{(str(age) if age is not None else '-'):>6}  {name[:32]:<32} {detail}")
    counts = {}
    for r in rows:
        counts[r[2]] = counts.get(r[2], 0) + 1
    print(f"\nSUMMARY: {counts}")
    return counts


def main():
    order = {"OK": 0, "STALE": 1, "WARN": 2, "EMPTY": 3, "FETCH_FAIL": 4}

    feeds = load_pairs("feeds.txt")
    print(f"Checking {len(feeds)} RSS feeds…")
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        feed_rows = list(ex.map(check_feed, feeds))
    fc = report("RSS FEEDS", feed_rows, order)

    queries = load_pairs("pubmed_queries.txt")
    print(f"\nChecking {len(queries)} PubMed queries…")
    q_rows = []
    for q in queries:
        q_rows.append(check_pubmed(q))
        time.sleep(0.4)
    qc = report("PUBMED QUERIES", q_rows, order)

    bad = fc.get("EMPTY", 0) + fc.get("FETCH_FAIL", 0) + qc.get("EMPTY", 0) + qc.get("FETCH_FAIL", 0)
    print(f"\n{bad} entries need attention. Edit feeds.txt / pubmed_queries.txt and re-run.")
    # Exit 0 regardless: this is a report, not a gate.
    return 0


if __name__ == "__main__":
    sys.exit(main())
