"""
Validate every URL in feeds.txt and every query in pubmed_queries.txt.

Run locally:   python validate_feeds.py
On GitHub:     Actions -> Validate feeds -> Run workflow

Publishers rate-limit and bot-block. This script fetches one URL at a time per host,
spaces out hits on the same host, and retries with a growing backoff before calling a
feed dead. Without that, sites like nature.com return an HTML challenge page and a
working feed looks broken.

Read BLOCKED carefully. A 403 is a real block and no URL change fixes it. An HTML
challenge page is usually rate limiting, so if the same host has other feeds passing
in the same run, raise HOST_DELAY and re-run before deleting anything.

Statuses:
  OK          parsed, has entries, published something recently
  STALE       works, but nothing new in STALE_DAYS
  BLOCKED     403, or an HTML challenge page after every retry
  EMPTY       parsed, no entries, on every attempt
  FETCH_FAIL  404, DNS failure, or timeout. Wrong URL, or the host is gone.
"""
import os, sys, json, time, random, urllib.parse, urllib.request
import concurrent.futures as cf
from collections import defaultdict
from datetime import datetime, timezone

import feedparser

UA = "tocify-validator/2.1 (+https://github.com/SamSievertsen/tocify)"
TIMEOUT = int(os.getenv("FEED_TIMEOUT", "45"))
STALE_DAYS = int(os.getenv("STALE_DAYS", "120"))
# Keep this at or above digest.py's HOST_DELAY. digest.py also interleaves hosts, so it
# spaces same-host requests further apart than this script does. If the validator is the
# more aggressive of the two it reports BLOCKED for feeds the digest reads fine, which
# is the worst kind of false alarm because it makes you delete working feeds.
HOST_DELAY = float(os.getenv("HOST_DELAY", "10.0"))  # seconds between hits on one host
HOST_WORKERS = int(os.getenv("HOST_WORKERS", "6"))   # distinct hosts in parallel
ATTEMPTS = int(os.getenv("FEED_RETRIES", "3"))       # tries before calling a feed dead


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


def looks_like_html(raw):
    head = raw[:400].decode("utf-8", "ignore").lower()
    return "<html" in head or "<!doctype html" in head


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return urllib.request.urlopen(req, timeout=TIMEOUT).read()


def check_feed(name, url):
    last_err = None
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(5 * attempt + random.random() * 3)
        try:
            raw = fetch(url)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):
                return (name, url, "BLOCKED", 0, None, f"HTTP {e.code}. Publisher refuses runner IPs")
            last_err = f"HTTP {e.code}"
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:60]}"
            continue

        d = feedparser.parse(raw)
        if d.entries:
            nd = newest(d)
            age = (datetime.now(timezone.utc) - nd).days if nd else None
            status = "STALE" if (age is not None and age > STALE_DAYS) else "OK"
            sample = (d.entries[0].get("title") or "")[:60].replace("\n", " ")
            return (name, url, status, len(d.entries), age, sample)

        if looks_like_html(raw):
            last_err = "HTML page, not a feed"
        else:
            last_err = "parsed but 0 entries"

    if last_err == "HTML page, not a feed":
        return (name, url, "BLOCKED", 0, None,
                f"HTML challenge page on all {ATTEMPTS} tries. If other feeds on this "
                f"host passed, this is rate limiting. Raise HOST_DELAY")
    if last_err == "parsed but 0 entries":
        return (name, url, "EMPTY", 0, None, f"0 entries on all {ATTEMPTS} tries")
    return (name, url, "FETCH_FAIL", 0, None, last_err or "unknown")


def check_host_group(item):
    """Feeds sharing a host are fetched one at a time, spaced out."""
    _host, pairs = item
    rows = []
    for i, (name, url) in enumerate(pairs):
        if i:
            time.sleep(HOST_DELAY + random.random() * 0.5)
        rows.append(check_feed(name, url))
    return rows


def check_pubmed(name, term):
    params = {"db": "pubmed", "term": term, "retmode": "json", "retmax": 1,
              "reldate": 365, "datetype": "edat", "tool": "tocify"}
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(params)
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=TIMEOUT).read()
        res = json.loads(raw).get("esearchresult", {})
        warn = res.get("errorlist") or res.get("warninglist")
        count = int(res.get("count", 0))
        if warn:
            bad = json.dumps(warn)[:70]
            return (name, term[:60], "WARN", count, None, f"{count} hits; check terms: {bad}")
        return (name, term[:60], "OK" if count else "EMPTY", count, None, f"{count} hits in last 365d")
    except Exception as e:
        return (name, term[:60], "FETCH_FAIL", 0, None, f"{type(e).__name__}: {str(e)[:60]}")


ORDER = {"OK": 0, "STALE": 1, "WARN": 2, "BLOCKED": 3, "EMPTY": 4, "FETCH_FAIL": 5}


def report(title, rows):
    print(f"\n{'=' * 112}\n{title}\n{'=' * 112}")
    print(f"{'STATUS':<12}{'N':>6}{'AGEd':>6}  {'NAME':<32} DETAIL")
    print("-" * 112)
    rows.sort(key=lambda r: (ORDER.get(r[2], 9), r[0].lower()))
    for name, _u, status, n, age, detail in rows:
        print(f"{status:<12}{n:>6}{(str(age) if age is not None else '-'):>6}  {name[:32]:<32} {detail}")
    counts = defaultdict(int)
    for r in rows:
        counts[r[2]] += 1
    print(f"\nSUMMARY: {dict(counts)}")
    return counts


def main():
    feeds = load_pairs("feeds.txt")
    groups = defaultdict(list)
    for name, url in feeds:
        groups[urllib.parse.urlparse(url).netloc].append((name, url))
    print(f"Checking {len(feeds)} RSS feeds across {len(groups)} hosts "
          f"({HOST_WORKERS} hosts in parallel, {HOST_DELAY}s between same-host requests)…")

    feed_rows = []
    with cf.ThreadPoolExecutor(max_workers=HOST_WORKERS) as ex:
        for rows in ex.map(check_host_group, groups.items()):
            feed_rows.extend(rows)
    fc = report("RSS FEEDS", feed_rows)

    queries = load_pairs("pubmed_queries.txt")
    print(f"\nChecking {len(queries)} PubMed queries…")
    q_rows = []
    for name, term in queries:
        q_rows.append(check_pubmed(name, term))
        time.sleep(0.4)
    qc = report("PUBMED QUERIES", q_rows)

    print("\n" + "=" * 112)
    print("WHAT TO DO")
    print("=" * 112)
    print("BLOCKED     If the detail says HTTP 403, the publisher refuses runner IPs.")
    print("            Delete the feed and add \"J Abbrev Name\"[Journal] to")
    print("            pubmed_queries.txt instead. No URL change gets past a 403.")
    print("            If the detail says HTML challenge page and other feeds on the")
    print("            same host passed, that is rate limiting. Raise HOST_DELAY and")
    print("            re-run before you delete anything.")
    print("FETCH_FAIL  Wrong URL or dead host. Find the real feed URL, or drop it.")
    print("EMPTY       Feed is real but had nothing. Fine if the journal is quiet.")
    print("STALE       Works, but nothing published recently. Probably discontinued.")
    bad = sum(fc[k] for k in ("BLOCKED", "EMPTY", "FETCH_FAIL")) + \
          sum(qc[k] for k in ("EMPTY", "FETCH_FAIL", "WARN"))
    print(f"\n{bad} entries need attention.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
