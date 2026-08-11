# tocify

A weekly research digest. Every Monday a GitHub Action pulls new papers from journal
RSS feeds and PubMed, has a language model triage them against a written statement of
research interests, and publishes a ranked, sectioned digest to GitHub Pages.

**Live digest:** <https://samsievertsen.github.io/tocify/>

Maintained by Sam Sievertsen. Forked from [voytek/tocify](https://github.com/voytek/tocify)
by Bradley Voytek, with earlier contributions from [Renee (Hui Xin) Ng](https://github.com/nghuixin).
The triage pipeline, scoring rubric, source coverage, and backend have since been
substantially rewritten, but the original idea and scaffolding are theirs.

---

## What this digest tracks

Four sections, each scored and thresholded independently:

| Section | What lands here |
|---|---|
| **Suicide & self-harm** | Suicidal ideation and behavior, self-injury, crisis services, means safety, prevention and intervention trials. Pediatric and adolescent samples are up-weighted. |
| **Intensive longitudinal & sensing** | EMA/ESM, digital phenotyping, passive smartphone and wearable sensing, biosensors, actigraphy, measurement-burst designs, JITAI and micro-randomized trials. |
| **ML & dynamical systems methods** | Prediction modeling, deep learning, nonlinear time series, idiographic and person-specific models, early warning signals, network psychometrics, calibration and external validation. Included even when the application sits outside psychiatry, if the method transfers. |
| **Adjacent mental health** | Nearby psychiatry and child/adolescent work worth a glance. Deliberately kept small. |

The rubric up-weights external validation, prospective designs, and honest handling of
rare events and class imbalance; it down-weights cross-sectional self-report studies,
single-site models framed as clinically actionable, and editorials and corrections.

### A caveat worth stating plainly

Scores are a language model's judgement from a title and a short abstract snippet. The
model has not read the paper. A high score means *worth opening*, not *methodologically
sound*. This is a triage aid, not an appraisal.

---

## How it works

```
feeds.txt ─────────┐
                   ├─► fetch ─► dedupe ─► keyword prefilter ─► LLM triage ─► digest.md ─► Quarto ─► Pages
pubmed_queries.txt ┘                                              ▲
                                              interests.md + prompt.txt
```

**Why two sources.** RSS gives table-of-contents coverage with abstracts, but publisher
feed URLs rot constantly and a feed only covers journals you thought to list. PubMed
E-utilities is a stable NCBI API that searches all of MEDLINE, so it catches relevant
work in journals you never subscribed to and keeps working when a journal changes
publisher — as *Computational Psychiatry* did. Papers found by both routes are
deduplicated on normalized title, keeping the publisher record because it carries the
abstract.

**Cost.** The triage backend is [OpenRouter](https://openrouter.ai), whose free tier
needs no payment information. A full weekly run is roughly six API requests against a
50-requests-per-day free allowance.

---

## Configuration

Everything you would normally want to change is plain text. No Python edits required.

| File | Controls |
|---|---|
| `interests.md` | Keywords, narrative statement, section definitions |
| `prompt.txt` | The triage rubric — what gets up- and down-weighted |
| `feeds.txt` | RSS feeds, one per line as `Name \| URL` |
| `pubmed_queries.txt` | PubMed queries, one per line as `Name \| query` |

Score thresholds and result caps are environment variables set in
`.github/workflows/weekly-digest.yml`: `MIN_SCORE_SUICIDE`, `MIN_SCORE_SENSING`,
`MIN_SCORE_METHODS`, `MIN_SCORE_ADJACENT`, and the matching `MAX_*` caps.

### Feeds rot — check them periodically

**Actions → Validate feeds → Run workflow.** It fetches every feed URL and runs every
PubMed query, then prints a status report to the run summary. No API key needed. It
also runs automatically on the 1st of each month.

Fix or delete anything reported `EMPTY` or `FETCH_FAIL`. `STALE` means the feed works
but has published nothing in 120 days.

---

## Local use

```bash
pip install -r requirements.txt

python digest.py --dry-run             # keyword-only scoring, no API calls
python digest.py --list-free-models    # what's free on OpenRouter right now
python digest.py --limit 40            # small live test
python validate_feeds.py               # feed health report

export OPENROUTER_API_KEY=sk-or-v1-...
python digest.py                       # full run
quarto preview                         # view the site locally
```

`--list-free-models` matters because OpenRouter's free roster changes. It prints which
free models support strict JSON schema output; put working ones in `MODEL_CHAIN`.

### Graceful degradation

`MODEL_CHAIN` is tried in order. For each model the pipeline first requests strict
`json_schema` structured output; if the model doesn't support it, it retries in plain
JSON mode and recovers the object from the response, including markdown-fenced or
prose-wrapped output. Rate limits and timeouts back off exponentially. The run only
fails if every model in the chain fails. This matters on a free tier where model
availability isn't guaranteed.

---

## Fork this for your own field

Nothing here is specific to suicide research except the text files. To retarget it:

1. **Fork the repo**, then rewrite `interests.md` — the narrative paragraph does most
   of the work, so describe what you actually care about in prose.
2. **Rewrite `prompt.txt`**, especially the section definitions and the up-weight /
   down-weight lists. Redefine the four sections for your field, keeping the IDs in
   `interests.md` and `digest.py` in sync.
3. **Replace `feeds.txt` and `pubmed_queries.txt`** with your journals and queries,
   then run **Validate feeds** before your first real run.
4. **Add an `OPENROUTER_API_KEY` secret** and enable Pages (see below).
5. **Run with `--dry-run` first.** It exercises fetching, deduplication, filtering, and
   rendering without spending an API request, which is the cheapest way to find a
   broken feed list.

Start with thresholds set high. Broad sections are how these digests become unreadable
and get abandoned.

---

## Setup

### 1. Add your OpenRouter API key

**Settings → Secrets and variables → Actions → New repository secret**

- Name: `OPENROUTER_API_KEY`
- Secret: your key from <https://openrouter.ai/keys>

Repository secrets are encrypted and are never readable from the repository, the Actions
logs, or the published site — including when the repo is public. GitHub masks the value
in logs automatically. Never put the key in a file in the repo.

Optional extras:

| Type | Name | Purpose |
|---|---|---|
| Secret | `NCBI_API_KEY` | Raises the PubMed rate limit from 3/s to 10/s. Free from your NCBI account |
| Variable | `CONTACT_EMAIL` | Polite identifier sent to NCBI E-utilities |
| Variable | `MODEL_CHAIN` | Comma-separated model override |

### 2. Enable GitHub Pages

**Settings → Pages → Build and deployment → Source: `GitHub Actions`**

Not "Deploy from a branch" — the workflow uploads a Pages artifact directly.

### 3. Run it

**Actions → Weekly ToC Digest → Run workflow**, with *dry run* ticked the first time.
Then run it for real. After that it runs every Monday at 15:00 UTC (08:00 PDT / 07:00
PST — GitHub cron does not observe daylight saving).

---

## License

See `LICENSE`.
