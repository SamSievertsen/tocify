# tocify

A weekly journal table-of-contents digest. A GitHub Action pulls new items from RSS
feeds and PubMed, has an LLM triage them against your research interests, writes a
sectioned `digest.md`, and publishes it to GitHub Pages.

**Current focus:** suicidality (especially pediatric/adolescent), intensive
longitudinal data and biosensing, and machine learning / dynamical systems methods.

---

## Setup

### 1. Get an OpenRouter API key (free, no payment info)

1. Sign up at <https://openrouter.ai> with email or GitHub.
2. Create a key at <https://openrouter.ai/keys>.

The free tier allows 50 requests/day. This pipeline uses roughly **6 requests per
week**, so you are at ~2% of the daily cap on the one day it runs.

### 2. Add repository secrets

**Settings → Secrets and variables → Actions → New repository secret**

| Name | Required | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | yes | From step 1 |
| `NCBI_API_KEY` | no | Raises PubMed rate limit from 3/s to 10/s. Free from your NCBI account |
| `OPENAI_API_KEY` | no | Only if you want to keep using OpenAI instead |

Optionally add **Variables** (same page, Variables tab):

| Name | Notes |
|---|---|
| `CONTACT_EMAIL` | Polite identifier sent to NCBI E-utilities |
| `MODEL_CHAIN` | Comma-separated model override, e.g. `deepseek/deepseek-chat-v3-0324:free,openrouter/free` |

### 3. Enable GitHub Pages

**Settings → Pages → Build and deployment → Source: `GitHub Actions`**

Do not pick "Deploy from a branch" — the workflow uploads a Pages artifact directly.
Your site will appear at `https://samsievertsen.github.io/tocify/`.

### 4. Kick it off

**Actions → Weekly ToC Digest → Run workflow.** Tick *dry run* the first time to
confirm the feeds and rendering work without spending a request. Then run it for real.

After that it runs automatically **every Monday at 15:00 UTC** (08:00 PDT / 07:00 PST —
GitHub cron does not observe DST).

---

## Customising

Everything you would normally want to change lives in plain text files:

| File | What it controls |
|---|---|
| `interests.md` | Your keywords, narrative, and the section definitions |
| `prompt.txt` | The triage rubric — what gets up-weighted and down-weighted |
| `feeds.txt` | RSS feed list (`Name \| URL`) |
| `pubmed_queries.txt` | PubMed E-utilities queries (`Name \| query`) |

Thresholds are environment variables, so you can tune them in `weekly-digest.yml`
without touching Python: `MIN_SCORE_SUICIDE`, `MIN_SCORE_SENSING`,
`MIN_SCORE_METHODS`, `MIN_SCORE_ADJACENT`, and the matching `MAX_*` caps.

### Why both RSS and PubMed?

RSS gives you table-of-contents coverage of journals you care about, with abstracts.
But publisher feed URLs rot constantly, and a feed only covers journals you thought to
list. PubMed E-utilities is a stable NCBI API that searches everything in MEDLINE, so
it catches relevant papers in journals you never subscribed to — and it keeps working
when a journal changes publisher (as *Computational Psychiatry* did). Items found by
both routes are deduplicated by title, keeping the publisher record because it has the
abstract.

---

## Feeds rot — check them

**Actions → Validate feeds → Run workflow.** It fetches every URL in `feeds.txt`,
every query in `pubmed_queries.txt`, and prints a status report to the run summary.
No API key needed. It also runs automatically on the 1st of each month.

Anything reported `EMPTY` or `FETCH_FAIL` should be fixed or deleted. `STALE` means
the feed works but has published nothing in 120 days.

The feed list currently ships with a number of best-effort URL patterns that have not
been confirmed from a GitHub runner. **Run this workflow first and prune accordingly.**

---

## Local use

```bash
pip install -r requirements.txt

python digest.py --dry-run             # keyword-only scores, no API calls
python digest.py --list-free-models    # what is free on OpenRouter right now
python digest.py --limit 40            # small live test
python validate_feeds.py               # feed health report

export OPENROUTER_API_KEY=sk-or-v1-...
python digest.py                       # full run
quarto preview                         # view the site
```

`--list-free-models` matters because OpenRouter's free model roster changes. It prints
which free models support strict JSON schema output; put working ones in `MODEL_CHAIN`.

---

## How triage degrades gracefully

`MODEL_CHAIN` is tried in order. For each model the pipeline first requests strict
`json_schema` structured output, and if the model does not support it, retries in plain
JSON mode and recovers the object from the response (including markdown-fenced or
prose-wrapped output). Rate limits and timeouts back off exponentially. Only if every
model in the chain fails does the run error out. This matters on a free tier where
model availability is not guaranteed.

---

## A caveat worth stating

Scores are a language model's judgement from a title and a short abstract snippet.
They are a triage aid, not an appraisal of quality, and the model has not read the
paper. Treat a high score as "worth opening", nothing more.

---

## License

See `LICENSE`.
