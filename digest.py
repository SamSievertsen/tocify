"""
tocify — weekly journal ToC digest.

Pipeline:  RSS feeds + PubMed queries -> dedupe -> keyword prefilter
           -> LLM triage (OpenRouter) -> sectioned markdown digest

Backend is OpenRouter (OpenAI-SDK compatible). Set OPENROUTER_API_KEY.
Falls back to OPENAI_API_KEY against api.openai.com if that is what you have.

Useful CLI flags:
    python digest.py --dry-run           # no API calls; keyword-only scores
    python digest.py --list-free-models  # show current OpenRouter free models
    python digest.py --limit 40          # cap items sent to the model (cheap test)
"""

import os, re, sys, json, time, math, html, hashlib, argparse
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import feedparser
import httpx
from dateutil import parser as dtparser
from openai import OpenAI
from openai import APITimeoutError, APIConnectionError, RateLimitError, APIStatusError


# ---------------------------------------------------------------- config
def _env_int(k, d): return int(os.getenv(k, str(d)))
def _env_float(k, d): return float(os.getenv(k, str(d)))

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Free OpenRouter models rotate. This is a fallback CHAIN: the first that works wins.
# Run `python digest.py --list-free-models` to see what is currently available.
DEFAULT_MODEL_CHAIN = [
    "google/gemini-2.0-flash-exp:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "openrouter/free",  # auto-router: picks any free model meeting the request's needs
]
MODEL_CHAIN = [m.strip() for m in os.getenv("MODEL_CHAIN", ",".join(DEFAULT_MODEL_CHAIN)).split(",") if m.strip()]

MAX_ITEMS_PER_FEED   = _env_int("MAX_ITEMS_PER_FEED", 60)
MAX_TOTAL_ITEMS      = _env_int("MAX_TOTAL_ITEMS", 600)
LOOKBACK_DAYS        = _env_int("LOOKBACK_DAYS", 7)
INTERESTS_MAX_CHARS  = _env_int("INTERESTS_MAX_CHARS", 4000)
SUMMARY_MAX_CHARS    = _env_int("SUMMARY_MAX_CHARS", 500)
PREFILTER_KEEP_TOP   = _env_int("PREFILTER_KEEP_TOP", 220)
BATCH_SIZE           = _env_int("BATCH_SIZE", 40)
FEED_TIMEOUT         = _env_int("FEED_TIMEOUT", 45)
HOST_DELAY           = _env_float("HOST_DELAY", 1.5)   # min seconds between same-host hits
PUBMED_RETMAX        = _env_int("PUBMED_RETMAX", 60)
PUBMED_ENABLED       = os.getenv("PUBMED_ENABLED", "1") not in ("0", "false", "False")
NCBI_API_KEY         = os.getenv("NCBI_API_KEY", "").strip()   # optional, raises rate limit
CONTACT_EMAIL        = os.getenv("CONTACT_EMAIL", "").strip()  # polite E-utilities identifier

# Per-section thresholds and caps. Tune without touching code.
SECTIONS = [
    {"id": "suicide",  "title": "Suicide & self-harm",
     "min": _env_float("MIN_SCORE_SUICIDE", 0.55), "max": _env_int("MAX_SUICIDE", 20)},
    {"id": "sensing",  "title": "Intensive longitudinal & sensing",
     "min": _env_float("MIN_SCORE_SENSING", 0.60), "max": _env_int("MAX_SENSING", 15)},
    {"id": "methods",  "title": "ML & dynamical systems methods",
     "min": _env_float("MIN_SCORE_METHODS", 0.65), "max": _env_int("MAX_METHODS", 15)},
    {"id": "adjacent", "title": "Adjacent mental health",
     "min": _env_float("MIN_SCORE_ADJACENT", 0.75), "max": _env_int("MAX_ADJACENT", 8)},
]
SECTION_IDS = [s["id"] for s in SECTIONS]
UA = "tocify/2.0 (+https://github.com/SamSievertsen/tocify)"

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "notes": {"type": "string"},
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id":      {"type": "string"},
                    "section": {"type": "string", "enum": SECTION_IDS},
                    "score":   {"type": "number"},
                    "why":     {"type": "string"},
                    "tags":    {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "section", "score", "why", "tags"],
            },
        },
    },
    "required": ["notes", "ranked"],
}


# ---------------------------------------------------------------- helpers
def sha1(s): return hashlib.sha1(s.encode("utf-8")).hexdigest()

def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def clean(s, limit=None):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0] + "…"
    return s

def load_pairs(path):
    """Parse 'Name | value' lines, skipping blanks and # comments."""
    out = []
    if not os.path.exists(path):
        return out
    for line in read_text(path).splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "|" in s:
            name, val = [x.strip() for x in s.split("|", 1)]
        else:
            name, val = None, s
        if val:
            out.append({"name": name, "value": val})
    return out

def section_of(md, heading):
    m = re.search(rf"(?im)^\s*#{{1,6}}\s+{re.escape(heading)}\s*$", md)
    if not m:
        return ""
    rest = md[m.end():]
    m2 = re.search(r"(?im)^\s*#{1,6}\s+\S", rest)
    return (rest[:m2.start()] if m2 else rest).strip()

def parse_interests(md):
    keywords = []
    for line in section_of(md, "keywords").splitlines():
        line = re.sub(r"^[\-\*\+]\s+", "", line.strip())
        if line and not line.startswith("<!--"):
            keywords.append(line)
    narrative = section_of(md, "narrative").strip()
    if len(narrative) > INTERESTS_MAX_CHARS:
        narrative = narrative[:INTERESTS_MAX_CHARS] + "…"
    return {"keywords": keywords[:250], "narrative": narrative}


# ---------------------------------------------------------------- RSS
def parse_date(entry):
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    for key in ("published", "updated", "created", "dc_date"):
        val = entry.get(key)
        if val:
            try:
                dt = dtparser.parse(val)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass
    return None

_last_hit = {}

def _throttle(url):
    """Space out requests to the same host. Publishers serve an HTML challenge page
    instead of the feed when hit too fast, which silently looks like a dead feed."""
    host = urllib.parse.urlparse(url).netloc
    wait = HOST_DELAY - (time.monotonic() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


def _get(url):
    _throttle(url)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return urllib.request.urlopen(req, timeout=FEED_TIMEOUT).read()


def fetch_one_feed(feed, cutoff):
    url, out = feed["value"], []
    label = feed.get("name") or url
    d = None
    for attempt in range(2):
        try:
            raw = _get(url)
        except Exception as e:
            print(f"  ! {label}: fetch failed ({type(e).__name__}: {str(e)[:60]})")
            return out
        d = feedparser.parse(raw)
        if d.entries:
            break
        head = raw[:400].decode("utf-8", "ignore").lower()
        if attempt == 0 and ("<html" in head or "<!doctype html" in head):
            time.sleep(4)   # probably a rate-limit challenge page; back off once
            continue
        break

    if not d or not d.entries:
        print(f"  ! {label}: 0 entries (run Validate feeds)")
        return out

    source = (feed.get("name") or d.feed.get("title") or url).strip()
    kept = 0
    for e in d.entries[:MAX_ITEMS_PER_FEED]:
        title = clean(e.get("title", ""))
        link = (e.get("link") or "").strip()
        if not (title and link):
            continue
        dt = parse_date(e)
        if dt and dt < cutoff:
            continue
        out.append({
            "id": sha1(f"{title}|{link}"),
            "source": source,
            "title": title,
            "link": link,
            "published_utc": dt.isoformat() if dt else None,
            "summary": clean(e.get("summary") or e.get("description") or "", SUMMARY_MAX_CHARS),
        })
        kept += 1
    print(f"  · {source}: {kept} new / {len(d.entries)} in feed")
    return out

def fetch_rss(feeds):
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    items = []
    print(f"Fetching {len(feeds)} RSS feeds (lookback {LOOKBACK_DAYS}d)…")
    for f in feeds:
        items.extend(fetch_one_feed(f, cutoff))
    return items


# ---------------------------------------------------------------- PubMed
def _eutils(endpoint, params):
    params = dict(params)
    params["tool"] = "tocify"
    if CONTACT_EMAIL:
        params["email"] = CONTACT_EMAIL
    if NCBI_API_KEY:
        params["api_key"] = NCBI_API_KEY
    url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/{endpoint}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=FEED_TIMEOUT).read()

def fetch_pubmed(queries):
    """Run each saved query against PubMed, restricted to the lookback window."""
    if not queries:
        return []
    items = []
    print(f"Querying PubMed ({len(queries)} saved queries, reldate {LOOKBACK_DAYS}d)…")
    for q in queries:
        name = q.get("name") or q["value"][:40]
        try:
            raw = _eutils("esearch.fcgi", {
                "db": "pubmed", "term": q["value"], "retmode": "json",
                "retmax": PUBMED_RETMAX, "reldate": LOOKBACK_DAYS, "datetype": "edat",
            })
            ids = json.loads(raw).get("esearchresult", {}).get("idlist", [])
            if not ids:
                print(f"  · PubMed: {name}: 0 hits")
                continue
            time.sleep(0.4)  # respect NCBI rate limits (3/s without a key)
            raw = _eutils("esummary.fcgi", {
                "db": "pubmed", "id": ",".join(ids), "retmode": "json",
            })
            res = json.loads(raw).get("result", {})
            n = 0
            for pmid in ids:
                rec = res.get(pmid)
                if not isinstance(rec, dict):
                    continue
                title = clean(rec.get("title", ""))
                if not title:
                    continue
                pub = None
                for key in ("sortpubdate", "epubdate", "pubdate"):
                    if rec.get(key):
                        try:
                            dt = dtparser.parse(rec[key])
                            pub = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()
                            break
                        except Exception:
                            pass
                journal = rec.get("fulljournalname") or rec.get("source") or "PubMed"
                link = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
                items.append({
                    "id": sha1(f"{title}|{link}"),
                    "source": f"{journal} (PubMed)",
                    "title": title,
                    "link": link,
                    "published_utc": pub,
                    "summary": "",  # esummary has no abstract; title-only triage
                })
                n += 1
            print(f"  · PubMed: {name}: {n} hits")
            time.sleep(0.4)
        except Exception as e:
            print(f"  ! PubMed query '{name}' failed ({type(e).__name__}: {str(e)[:70]})")
    return items


def dedupe(items):
    """Collapse duplicates by id, then by normalised title (same paper, two sources)."""
    by_id, by_title = {}, {}
    for it in items:
        if it["id"] in by_id:
            continue
        key = re.sub(r"[^a-z0-9]+", "", it["title"].lower())[:120]
        if key in by_title:
            # keep the non-PubMed record (has a summary and a publisher link)
            if "(PubMed)" in it["source"] and "(PubMed)" not in by_title[key]["source"]:
                continue
            if "(PubMed)" not in it["source"] and "(PubMed)" in by_title[key]["source"]:
                by_id.pop(by_title[key]["id"], None)
            else:
                continue
        by_id[it["id"]] = it
        by_title[key] = it
    out = list(by_id.values())
    out.sort(key=lambda x: x["published_utc"] or "", reverse=True)
    return out[:MAX_TOTAL_ITEMS]


# ---------------------------------------------------------------- prefilter
JUNK = re.compile(
    r"^(correction|corrigend|erratum|retraction|editorial|in this issue|masthead|"
    r"issue information|book review|errata|author correction|publisher correction|"
    r"table of contents|front matter|back matter|acknowledg)", re.I)

def keyword_hits(it, kws):
    text = (it.get("title", "") + " " + it.get("summary", "")).lower()
    return sum(1 for k in kws if k in text)

def prefilter(items, keywords, keep_top):
    kws = [k.lower().strip() for k in keywords if k.strip()]
    live = [it for it in items if not JUNK.match(it["title"])]
    dropped = len(items) - len(live)
    if dropped:
        print(f"Dropped {dropped} editorial/correction items")
    scored = sorted(((keyword_hits(it, kws), it) for it in live), key=lambda p: p[0], reverse=True)
    matched = [it for h, it in scored if h > 0]
    if len(matched) >= keep_top:
        return matched[:keep_top]
    # top up with unmatched-but-recent items so we don't miss novel phrasing
    rest = [it for h, it in scored if h == 0]
    return (matched + rest)[:keep_top]


# ---------------------------------------------------------------- LLM
def make_client():
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        base, label = OPENROUTER_BASE, "OpenRouter"
        headers = {"HTTP-Referer": "https://github.com/SamSievertsen/tocify", "X-Title": "tocify"}
    else:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "No API key. Set OPENROUTER_API_KEY (free tier, no card: "
                "https://openrouter.ai/keys) or OPENAI_API_KEY.")
        base, label, headers = None, "OpenAI", {}
    print(f"Backend: {label}")
    http_client = httpx.Client(
        timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
        trust_env=False, headers={"Connection": "close"},
    )
    kw = {"api_key": key, "http_client": http_client, "default_headers": headers}
    if base:
        kw["base_url"] = base
    return OpenAI(**kw), label


def list_free_models():
    """Print OpenRouter models that cost nothing and support structured outputs."""
    req = urllib.request.Request(f"{OPENROUTER_BASE}/models", headers={"User-Agent": UA})
    data = json.loads(urllib.request.urlopen(req, timeout=60).read())
    rows = []
    for m in data.get("data", []):
        p = m.get("pricing", {}) or {}
        free = all(float(p.get(k, 0) or 0) == 0 for k in ("prompt", "completion"))
        if not free:
            continue
        params = m.get("supported_parameters", []) or []
        rows.append((m["id"], "structured_outputs" in params or "response_format" in params,
                     m.get("context_length", 0)))
    rows.sort(key=lambda r: (not r[1], r[0]))
    print(f"{'MODEL ID':<52}{'JSON-SCHEMA':<13}CONTEXT")
    for mid, so, ctx in rows:
        print(f"{mid:<52}{'yes' if so else 'no':<13}{ctx:,}")
    print(f"\n{len(rows)} free models; {sum(1 for r in rows if r[1])} support structured outputs.")
    print("Set MODEL_CHAIN (comma-separated) to override the default chain.")


def extract_json(text):
    """Models sometimes wrap JSON in prose or fences. Recover it."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
    raise ValueError("no parseable JSON in model output")


def call_model(client, model, prompt, use_schema=True):
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    if use_schema:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "weekly_toc_digest", "strict": True, "schema": SCHEMA},
        }
    resp = client.chat.completions.create(**kwargs)
    return extract_json(resp.choices[0].message.content)


def triage_batch(client, prompt):
    """Try each model in the chain; for each, try schema mode then plain-JSON mode."""
    last = None
    for model in MODEL_CHAIN:
        for use_schema in (True, False):
            for attempt in range(3):
                try:
                    out = call_model(client, model, prompt, use_schema)
                    if attempt or not use_schema or model != MODEL_CHAIN[0]:
                        print(f"    (via {model}, schema={use_schema})")
                    return out
                except (APITimeoutError, APIConnectionError, RateLimitError) as e:
                    last = e
                    time.sleep(min(45, 3 * 2 ** attempt))
                except (APIStatusError, ValueError, KeyError) as e:
                    last = e
                    break  # schema unsupported or bad output — change mode/model
    raise RuntimeError(f"All models in MODEL_CHAIN failed. Last error: {last}")


def triage(client, interests, items, template):
    total = math.ceil(len(items) / BATCH_SIZE)
    ranked, notes = [], []
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i:i + BATCH_SIZE]
        print(f"  Triage batch {i // BATCH_SIZE + 1}/{total} ({len(batch)} items)")
        lean = [{"id": it["id"], "source": it["source"], "title": it["title"],
                 "summary": it["summary"][:SUMMARY_MAX_CHARS]} for it in batch]
        prompt = (template
                  .replace("{{KEYWORDS}}", json.dumps(interests["keywords"], ensure_ascii=False))
                  .replace("{{NARRATIVE}}", interests["narrative"])
                  .replace("{{ITEMS}}", json.dumps(lean, ensure_ascii=False)))
        res = triage_batch(client, prompt)
        if res.get("notes", "").strip():
            notes.append(res["notes"].strip())
        ranked.extend(res.get("ranked", []))

    best = {}
    for r in ranked:
        rid = r.get("id")
        if not rid:
            continue
        try:
            r["score"] = float(r.get("score", 0))
        except (TypeError, ValueError):
            r["score"] = 0.0
        if r.get("section") not in SECTION_IDS:
            r["section"] = "adjacent"
        if rid not in best or r["score"] > best[rid]["score"]:
            best[rid] = r
    return {"notes": " ".join(dict.fromkeys(notes))[:800], "ranked": list(best.values())}


def dry_run_triage(interests, items):
    """Keyword-only scoring so the pipeline can be tested with no API key."""
    kws = [k.lower() for k in interests["keywords"]]
    sec_kw = {
        "suicide": ["suicid", "self-harm", "self harm", "self-injur", "nssi", "crisis"],
        "sensing": ["ecological momentary", "ema", "experience sampling", "digital phenotyp",
                    "passive sensing", "wearable", "actigraph", "smartphone", "sensor",
                    "just-in-time", "micro-randomiz", "intensive longitudinal"],
        "methods": ["machine learning", "deep learning", "dynamical", "time series",
                    "prediction model", "predictive model", "network analysis",
                    "idiographic", "state space", "early warning", "calibration"],
    }
    ranked = []
    for it in items:
        text = (it["title"] + " " + it["summary"]).lower()
        sec = "adjacent"
        for s, terms in sec_kw.items():
            if any(t in text for t in terms):
                sec = s
                break
        h = keyword_hits(it, kws)
        ranked.append({"id": it["id"], "section": sec,
                       "score": round(min(0.99, 0.30 + 0.12 * h), 2),
                       "why": f"DRY RUN — keyword-only score ({h} keyword matches). No model was called.",
                       "tags": ["dry-run"]})
    return {"notes": "**DRY RUN** — scores are keyword counts, not model judgements.", "ranked": ranked}


# ---------------------------------------------------------------- render
def render(result, items_by_id, stats):
    week_of = datetime.now(timezone.utc).date().isoformat()
    ranked = result.get("ranked", [])
    notes = result.get("notes", "").strip()

    buckets = {s["id"]: [] for s in SECTIONS}
    for r in ranked:
        if r["id"] in items_by_id:
            buckets[r["section"]].append(r)

    kept = {}
    for s in SECTIONS:
        rows = sorted(buckets[s["id"]], key=lambda x: x["score"], reverse=True)
        kept[s["id"]] = [r for r in rows if r["score"] >= s["min"]][:s["max"]]

    total_kept = sum(len(v) for v in kept.values())
    L = [f"# Weekly ToC Digest — week of {week_of}", ""]
    if notes:
        L += [f"> {notes}", ""]
    L += ["| Section | Kept | Threshold |", "|---|---:|---:|"]
    for s in SECTIONS:
        L.append(f"| {s['title']} | {len(kept[s['id']])} | ≥ {s['min']:.2f} |")
    L += ["", f"*{total_kept} items kept from {stats['scored']} scored "
              f"({stats['fetched']} fetched across {stats['feeds']} feeds "
              f"and {stats['queries']} PubMed queries, {LOOKBACK_DAYS}-day window).*", "", "---", ""]

    if total_kept == 0:
        L += ["_Nothing met threshold this week._", ""]
        return "\n".join(L)

    for s in SECTIONS:
        rows = kept[s["id"]]
        if not rows:
            continue
        L += [f"## {s['title']}", ""]
        for r in rows:
            it = items_by_id[r["id"]]
            L += [f"### [{it['title']}]({it['link']})", ""]
            meta = [f"*{it['source']}*", f"**{r['score']:.2f}**"]
            if it.get("published_utc"):
                meta.append(it["published_utc"][:10])
            L += [" · ".join(meta), ""]
            if r.get("tags"):
                L += ["`" + "` `".join(t for t in r["tags"][:6]) + "`", ""]
            L += [clean(r.get("why", "")), ""]
            if it.get("summary"):
                L += ["<details><summary>Abstract snippet</summary>", "",
                      it["summary"], "", "</details>", ""]
        L += ["---", ""]
    return "\n".join(L)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no API calls; keyword-only scoring")
    ap.add_argument("--list-free-models", action="store_true", help="list free OpenRouter models")
    ap.add_argument("--limit", type=int, default=0, help="cap items sent to the model")
    args = ap.parse_args()

    if args.list_free_models:
        list_free_models()
        return

    interests = parse_interests(read_text("interests.md"))
    print(f"Interests: {len(interests['keywords'])} keywords, "
          f"{len(interests['narrative'])} chars of narrative")

    feeds = load_pairs("feeds.txt")
    queries = load_pairs("pubmed_queries.txt") if PUBMED_ENABLED else []

    items = fetch_rss(feeds)
    items += fetch_pubmed(queries)
    items = dedupe(items)
    print(f"\n{len(items)} unique items after dedupe")

    week_of = datetime.now(timezone.utc).date().isoformat()
    if not items:
        with open("digest.md", "w", encoding="utf-8") as f:
            f.write(f"# Weekly ToC Digest — week of {week_of}\n\n"
                    f"_No items found in the last {LOOKBACK_DAYS} days. "
                    f"If this repeats, run the **Validate feeds** workflow — "
                    f"feed URLs rot._\n")
        print("No items; wrote digest.md")
        return

    items = prefilter(items, interests["keywords"], PREFILTER_KEEP_TOP)
    if args.limit:
        items = items[:args.limit]
    print(f"{len(items)} items to triage\n")

    items_by_id = {it["id"]: it for it in items}
    stats = {"fetched": len(items_by_id), "scored": len(items),
             "feeds": len(feeds), "queries": len(queries)}

    if args.dry_run:
        result = dry_run_triage(interests, items)
    else:
        client, _ = make_client()
        result = triage(client, interests, items, read_text("prompt.txt"))

    md = render(result, items_by_id, stats)
    with open("digest.md", "w", encoding="utf-8") as f:
        f.write(md)
    print(f"\nWrote digest.md ({len(md):,} chars)")


if __name__ == "__main__":
    main()
