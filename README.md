# tocify

Pulls new papers from journal RSS feeds and PubMed every Monday, ranks them against your
research interests with an LLM, and publishes the result to GitHub Pages.

Live digest: <https://samsievertsen.github.io/tocify/>

## Quick start

Five minutes, assuming you forked this repo.

1. **Get an API key.** Sign up at <https://openrouter.ai>, then create a key at
   <https://openrouter.ai/keys>. The free tier needs no card.

2. **Add it as a secret.** Settings, then Secrets and variables, then Actions, then New
   repository secret. Name it `OPENROUTER_API_KEY`. Paste the key.

3. **Turn on Pages.** Settings, then Pages, then set Source to **GitHub Actions**. Do not
   pick "Deploy from a branch". The publish workflow also tries to enable this for you,
   but set it by hand if you can.

4. **Check the feeds.** Actions, then Validate feeds, then Run workflow. Feed URLs rot and
   publishers block bots. Act on the report before your first real run.

5. **Run it.** Actions, then Weekly ToC Digest, then Run workflow. Tick *dry run* the
   first time. That skips the API and proves the feeds and rendering work. Then run it for
   real.

After that it runs every Monday at 15:00 UTC. That is 08:00 PDT, 07:00 PST. GitHub cron
ignores daylight saving.

## Configuration

Four text files. You should not need to touch the Python.

| File | What it does |
|---|---|
| `interests.md` | Keywords, a narrative paragraph, section definitions |
| `prompt.txt` | Scoring rubric, meaning what gets up- and down-weighted |
| `feeds.txt` | RSS feeds, `Name \| URL` per line |
| `pubmed_queries.txt` | PubMed queries, `Name \| query` per line |

The narrative paragraph in `interests.md` does most of the work. Write it in prose.

Thresholds and caps are env vars in `.github/workflows/weekly-digest.yml`:
`MIN_SCORE_SUICIDE`, `MIN_SCORE_SENSING`, `MIN_SCORE_METHODS`, `MIN_SCORE_ADJACENT`, and
the matching `MAX_*`. Raise them if the digest gets noisy.

## What it currently tracks

| Section | Contents |
|---|---|
| Suicide & self-harm | Ideation, behavior, self-injury, crisis services, prevention and intervention trials. Pediatric and adolescent samples score higher. |
| Intensive longitudinal & sensing | EMA/ESM, digital phenotyping, passive and wearable sensing, actigraphy, JITAI, micro-randomized trials. |
| ML & dynamical systems methods | Prediction models, nonlinear time series, idiographic models, early warning signals, network psychometrics, calibration and external validation. Included even outside psychiatry if the method transfers. |
| Adjacent mental health | Nearby work worth a glance. Kept small on purpose. |

The rubric rewards external validation, prospective designs, and honest handling of rare
events. It penalizes cross-sectional self-report studies, single-site models sold as
clinically actionable, editorials, and corrections.

Scores come from a model reading a title and an abstract snippet. It has not read the
paper. Treat a high score as "worth opening" and nothing more.

## Local use

```bash
pip install -r requirements.txt

python digest.py --dry-run             # no API calls, keyword-only scores
python digest.py --list-free-models    # what's free on OpenRouter today
python digest.py --limit 40            # small live test
python validate_feeds.py               # feed health report

export OPENROUTER_API_KEY=sk-or-v1-...
python digest.py
quarto preview
```

Run `--list-free-models` when triage starts failing. OpenRouter's free model list changes.
It prints which free models support strict JSON schema output. Put working ones in the
`MODEL_CHAIN` repository variable, comma-separated.

## How it works

```
feeds.txt ─────────┐
                   ├─► fetch ─► dedupe ─► keyword prefilter ─► LLM triage ─► digest.md ─► Quarto ─► Pages
pubmed_queries.txt ┘                                              ▲
                                              interests.md + prompt.txt
```

Two sources, because neither is enough alone. RSS gives you table-of-contents coverage
with abstracts, but feed URLs break and you only get journals you thought to list. PubMed
E-utilities searches all of MEDLINE and survives publisher changes. *Computational
Psychiatry* moved publishers and its feed died, but the PubMed query still works. Papers
found twice get deduplicated on normalized title, keeping the publisher copy because it
has the abstract.

PubMed also covers journals whose publishers block GitHub runners. Wiley, Taylor &
Francis, and psychiatryonline.org all return 403 from Actions, so those journals live in
`pubmed_queries.txt` as `"J Abbrev"[Journal]` searches.

A full run is about six API requests against a 50-per-day free allowance.

Triage tries each model in `MODEL_CHAIN` in order. Per model it asks for strict
`json_schema` output first, then falls back to plain JSON and digs the object out of the
response if the model ignores the schema. Timeouts and rate limits back off. The run only
fails if every model fails.

## Optional settings

| Type | Name | Purpose |
|---|---|---|
| Secret | `NCBI_API_KEY` | Raises PubMed rate limit from 3/s to 10/s. Free from your NCBI account |
| Variable | `CONTACT_EMAIL` | Identifies you to NCBI E-utilities |
| Variable | `MODEL_CHAIN` | Comma-separated model override |

Secrets are encrypted. They never appear in the repo, the logs, or the published site,
including on a public repo. Do not commit the key to a file.

## Retargeting this to your field

Nothing here is specific to suicide research except the text files.

1. Rewrite `interests.md`. Describe what you care about in prose.
2. Rewrite the section definitions and the up/down-weight lists in `prompt.txt`. Keep the
   section IDs in sync with `interests.md` and `digest.py`.
3. Replace `feeds.txt` and `pubmed_queries.txt`. Run Validate feeds before your first real
   run.
4. Start with high thresholds. Broad sections make the digest unreadable and you stop
   reading it.

## Feeds break constantly

Run Validate feeds monthly. It runs automatically on the 1st. The report uses five
statuses:

| Status | Meaning | What to do |
|---|---|---|
| `OK` | Parsed, has entries, recent | Nothing |
| `STALE` | Works, nothing in 120 days | Probably discontinued. Drop it |
| `BLOCKED` | 403, or an HTML challenge page | Publisher refuses runner IPs. Delete the feed and add `"J Abbrev"[Journal]` to `pubmed_queries.txt` |
| `EMPTY` | Parsed, no entries, twice | Fine if the journal is quiet |
| `FETCH_FAIL` | 404, DNS failure, timeout | Wrong URL or dead host. Find the real one or drop it |

The validator fetches one URL at a time per host with a delay between them. Without that,
sites like nature.com return an HTML challenge page and a working feed looks dead. Tune
with `HOST_DELAY` and `HOST_WORKERS` if you still see false failures.

Two patterns worth knowing. ScienceDirect feeds work from Actions, so use
`https://rss.sciencedirect.com/publication/science/{ISSN}` for Elsevier titles rather than
the journal-branded sites like thelancet.com or cell.com, which return 403. And no amount
of URL fixing gets past a `BLOCKED` result, so go to PubMed instead of retrying.

## Troubleshooting

**`deploy` fails with 404.** Pages is not enabled. Settings, then Pages, then Source,
then GitHub Actions. Re-run.

**Render fails.** Quarto renders every `.qmd` and `.md` in the project unless you list
files explicitly. The `render:` list in `_quarto.yml` exists to stop that. Add new pages
there.

**Digest is empty or thin.** Run Validate feeds. Feeds die quietly.

**Triage batches fail, or the log shows `via openrouter/free`.** The named models in
`MODEL_CHAIN` are dead. OpenRouter retires free model IDs regularly. Run Validate feeds
and read the model list at the bottom of the report, then set the `MODEL_CHAIN`
repository variable to an ID marked `yes` under JSON-SCHEMA.

Pinning a specific model matters for more than speed. `openrouter/free` is an auto-router
that picks a different model per request, so scores are not comparable across batches and
your section thresholds stop meaning one thing. Pin a model once you find one that works.

A model marked `no` under JSON-SCHEMA can still work. The pipeline falls back to asking
for plain JSON and recovering the object from the response. That path is less reliable at
40 items per batch, so prefer a `yes` model, but a much larger `no` model is worth testing
against a `yes` one before you decide.

**Nature feeds come back BLOCKED, and a different set each run.** That is nature.com rate
limiting, not dead feeds. Roughly eight of the twelve succeed per run and which eight
varies. `HOST_DELAY` controls the spacing. The PubMed safety-net query covers those
journals when a feed does get dropped, so a rotating handful is tolerable rather than
urgent.

**A batch fails but the digest still publishes.** That is intended. A failed batch is
skipped, a note goes at the top of the digest saying how many items went unscored, and
the run continues. Only a total failure of every batch stops the run.

**Node.js 20 deprecation warnings.** Cosmetic. They come from GitHub's own Pages actions
and will go away when GitHub ships updated versions.

## Credit

Forked from [voytek/tocify](https://github.com/voytek/tocify) by Bradley Voytek, with
earlier work by [Renee (Hui Xin) Ng](https://github.com/nghuixin). The original idea and
scaffolding are theirs. This fork swaps in different interests, sources, scoring, and
model backend.

Maintained by Sam Sievertsen.

## License

See `LICENSE`.
