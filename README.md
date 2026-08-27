# Truth Social stock-mention alert agent

Monitors the public Truth Social account `@realDonaldTrump` and sends an alert whenever a
post mentions a publicly traded company, by ticker or by name. Runs locally.

## Setup

Requires Python 3.11. The system Python on macOS is 3.9 and will not work.

```bash
uv venv --python 3.11
uv pip install -r requirements.txt
cp .env.example .env      # then fill in the values you want
```

Every credential is optional. With none set, the agent still runs and prints alerts to the
console.

| Variable | Needed for |
| --- | --- |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram delivery. Get the token from @BotFather, then message your bot once and read the numeric chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `GROQ_API_KEY` | The LLM detector arm, the labeling helper, and the bonus sentiment line (bullish/bearish/neutral) added to delivered alerts |

## Running

```bash
# Full end to end demo. No network, no credentials, real archive posts that mention companies.
uv run python agent.py run --once --source demo

# Live monitoring
uv run python agent.py run

# Verify your alert channel in isolation before trusting the agent
uv run python agent.py test-alert

uv run python agent.py health      # heartbeat state and active alarms
uv run python agent.py stats       # store counts
uv run pytest -q                   # full suite, offline
```

### Dashboard

```bash
uv run python scripts/dashboard.py --port 8000 --db data/agent.db
```

A control room for the agent, not just a read only view. One page, built on `http.server`
with no new dependency: a status hero (running, stopped or degraded, with live figures and
a countdown to the next poll), start and stop buttons, poll interval presets with a plain
English guidance line computed from the value you pick, a backfill trigger with a time and
page estimate before you run it, a fetched to alerted pipeline funnel, a ticker filterable
mentions feed, health and latency (reusing `latency_report.py`'s own maths), precision,
recall and F1 read from `data/eval/metrics.md` when it exists, and the ticker lexicon
editor. The page renders once, then a small script polls `GET /api/state` every 10 seconds
and updates in place, so nothing you are mid way through typing gets wiped by a refresh.

`is_running()` checks the recorded pid with `os.kill(pid, 0)` rather than trusting the pid
file, so a stale file left behind by a crash is reported as stopped instead of lying that
the agent is still running. Settings you change (poll interval, backfill window) are saved
to the store and survive a restart of the dashboard itself.

It is local only, binds to 127.0.0.1 by default, and has no authentication, so it must
never be exposed to a network.

Reproducing the data and evaluation:

```bash
uv run python scripts/backfill.py --days 45 --delay 2.5 --out data/history.jsonl
uv run python scripts/build_eval_set.py --history data/history.jsonl \
    --out data/eval/eval_sample.jsonl --seed 42
uv run python scripts/prelabel.py --in data/eval/eval_sample.jsonl \
    --out data/eval/prelabels.jsonl --model openai/gpt-oss-120b
uv run python scripts/label.py --sample data/eval/eval_sample.jsonl \
    --prelabels data/eval/prelabels.jsonl --out data/eval/labeled.jsonl \
    --blind-count 30 --seed 42
uv run python scripts/evaluate.py
uv run python scripts/latency_report.py
```

All sampling is seeded, so reruns are byte identical.

### Docker

```bash
docker build -t tsalert .
docker run --rm tsalert                                  # offline demo, no credentials
docker run --rm --env-file .env -v tsalert-data:/data tsalert run
```

The volume matters: dedup and alert idempotency both live in the SQLite file, so without it
a restarted container re-alerts every post it has already seen. Running twice against the
same volume gives 4 alerts then 0; drop the volume and it is 4 again.

The image is 261MB and the default command needs no network, so `docker run --rm
--network none tsalert` is a complete offline demonstration.

---

# Write-up

## 1. Approach

Truth Social is a Mastodon fork, so its web client already talks to
`/api/v1/accounts/{id}/statuses`, paginated with `max_id` and `min_id`. Clean JSON on a
documented schema. The obstacle is Cloudflare: `requests` and `curl` both get a 403. TLS
fingerprint impersonation gets through, `curl_cffi` with `impersonate="safari17_0"`.
`chrome124` is still blocked, so I found that by trying a few rather than by understanding
their rules, and it is pinned in config before someone tidies it away.

| Approach | Verdict |
| --- | --- |
| Mastodon JSON, TLS impersonation | **Chosen.** `min_id` returns only new posts; documented schema beats scraped markup |
| `trumpstruth.org` RSS mirror | **Fallback.** Exposes `truth:originalId`, the same ids the primary uses, so failover cannot confuse dedup |
| Headless browser | Rejected. Slow and fragile, no gain over JSON |
| Third-party aggregators | Rejected. Added latency plus someone else's uptime |

Impersonation is the best option and the most fragile, since it hangs on a setting somebody
else controls, hence the circuit breaker. The mirror is worse: text and ids only, and it lags.

**Polling** floats between 60 and 300 seconds. He posts in bursts, so quiet polls back off
1.5x and any new post resets to 60. Replaying the real 45 day posting history through it
gives 305 requests a day against 1,440 for a flat 60 second poll.

**Detection** pairs a rule baseline with an LLM arm. The lexicon rates how ambiguous each
ticker is, and riskier ones need more context. That came from the corpus, where *trade*,
*market* and *economy* are ordinary political words, so context terms split into strong
(stock, shares, earnings) and weak. Without that split, his habit of shouting in capitals
turns ALL, BIG and NOW into noise.

## 2. Results

The 45-day archive is 1,260 posts. Three properties shaped everything:

| Finding | Consequence |
| --- | --- |
| Zero cashtags in 1,260 posts | The `$DJT` form is implemented but never exercised by real data |
| 37% of posts have no text | 472 are images or video, invisible to a text detector |
| Every bare `DJT` is his signature | 60 posts contain the token, all 60 are sign-offs, none is the ticker |

With a base rate this low, uniform sampling finds almost no positives, so the set is
stratified and each stratum answers one question:

| Stratum | n | Question | Weight |
| --- | --- | --- | --- |
| candidate | 23 | When it fires, is it right? | 1.0, census, exact precision |
| random | 102 | What gets missed? | 3.62, projected to population |
| hard_negative | 25 | Does suppression hold? | 0.0, excluded from headline |

Hard negatives are picked to be adversarial, so weighting them onto the population would let
one call shift the estimate by sixteen posts. They are reported separately.

**Three arms**, weighted over candidate and random, 15 positives. Combined is the cascade
that actually ships: rules first, LLM only on a rule candidate.

| Arm | Precision | Recall | F1 (bootstrap 95% CI) | Traps |
| --- | --- | --- | --- | --- |
| rules | 0.867 | 0.738 | 0.797 [0.558, 0.968] | 25/25 |
| llm | 1.000 | 1.000 | 1.000 [1.000, 1.000] | 25/25 |
| **combined, ships** | 1.000 | 0.738 | 0.849 [0.603, 1.000] | 25/25 |

Combined costs no tokens to score: it fires exactly where the rules produced a candidate and
the LLM confirmed it, and both are already on disk. Read the recall column. Gating the LLM
on rule candidates lets the cascade delete a rule false positive but never recover a rule
miss, so combined inherits the rule arm's 0.738 exactly, by construction rather than by
luck. The LLM arm gets the bare Google link right on its own and the shipped detector still
misses it. What the cascade buys is precision.

Both misses are known limitations: S&P Global, absent from the 95 lexicon rows, and a bare
link, since URLs are stripped before matching.

**How the labels were made, and why the LLM's 1.000 is not a measurement.** `gpt-oss-120b`
proposes a label for each post, then a stronger model adjudicates every row against a
written rubric for the recurring hard cases: media brands he appears on versus companies as
corporate entities, companies named in passing, private companies, and the DJT sign-off.
The adjudicator changed 8 of 150, and not one of those 8 flipped `is_stock_related`. So
`labeled.jsonl` is identical to the LLM arm's own prediction on 150 rows out of 150. That
1.000 is x == x. The interval of exactly [1.000, 1.000] is the tell: no resample of a set
holding zero errors can produce one. `evaluate.py` now checks agreement per arm and prints
that warning instead of a clean number, and combined's 1.000 precision leans on the same
predictions. A third model, `gpt-oss-safeguard-20b`, relabeled all 150 blind and agreed on
149, which makes the labels consistent, not independent: every step is a language model.
The blind holdout shows a zero gap for that same reason. A trustworthy comparison needs
human labels. That is the biggest weakness here and I would rather say so than report a
perfect score.

**The evaluated model is not the model the agent runs.** Those predictions are
`gpt-oss-120b`. The detector defaults to `qwen3.6-27b`, chosen deliberately so the eval is
not grading a model against labels from its own lineage, which means the combined row above
describes a cascade whose LLM half the agent does not actually run. `evaluate.py` prints the
model id it is scoring, read from prelabels.jsonl, so the report says which one it means.

**Latency**, over a 90 poll live run:

| Stage | Measured |
| --- | --- |
| Published to fetched | 26s, 79s, 154s |
| Fetched to detected | 7.4 ms |
| Detected to delivered | 0.3 ms |

The poll interval is effectively the whole budget, which makes the adaptive backoff a latency
decision as much as a politeness one. A fourth sample read 824s and is excluded as cold start.
Three samples is thin, because no stock-related post arrived during the window.

## 3. Robustness and ethics

Everything rests on a fingerprint continuing to work and those endpoints staying put, both
outside my control.

What worried me more than a 404, which you just see, is Truth Social changing the response
shape underneath me while the parser keeps not erroring and simply comes back empty. So if
over half a page fails to parse the source raises instead of handing back an empty list, and
a `no_new_posts` heartbeat fires when polling keeps working but nothing new ever arrives.
That alarm state is persisted, otherwise a restart resets the clock and buries an outage
that has already been running for hours. Behind those, a circuit breaker onto the mirror and
error-streak alarms, all rate limited so nobody learns to ignore them.

**Politeness** is in code, not a comment promising it: a 2.5 second floor between requests,
an hourly cap that refuses to send past 600, `Retry-After` honoured, strictly sequential
requests. The cap matters most, since backoff stays correct right until a loop bug turns it
into a hammer.

**Legal and ToS.** This reads public pages with no account and keeps only public post text.
The mirror allows crawling; Truth Social publishes no `robots.txt`. I would not oversell
that: automated access probably conflicts with their terms anyway, and impersonating a
browser fingerprint deliberately works around bot protection, which goes past just reading a
page. At one request a minute for a prototype I think that is fine. To run it for real I
would want a licensed feed or permission.

## 4. Limitations and next steps

Media-only posts are invisible; OCR is the obvious answer. The lexicon caps recall, since
ground truth contains `SPGI`, `V`, `TM` and `TMUS`, none among the 95 rows. Fifteen positives
makes every interval wide. And the labels are model-generated, so the ML comparison is not
yet trustworthy.

**More accounts** is scheduling, not architecture. Replace the single loop with a priority
queue keyed on each account's posting rate, so a busy account polls every 60 seconds and a
quiet one drifts to 15 minutes, under one global budget. Dedup already keys on the status id.

**Evaluating in production without labeling everything.** Run both arms over live traffic and
hand-label only where they disagree, concentrating effort on the decision boundary. Turn
delivered alerts into a labeling queue, where a thumbs up or down in Telegram costs almost
nothing. And watch input drift rather than accuracy: candidate rate and ticker distribution
need no labels, and a sharp move in either is usually the first hint something changed.

## Repository layout

```
agent.py                  entry point: run, test-alert, health, stats
src/tsalert/
  sources/                ingestion: the API client, the RSS mirror, failover, shared parser
  detect/                 lexicon and the rule baseline
  alerts/                 channels, formatting, the delivery dispatcher
  store.py                sqlite: dedup, alert idempotency, state, latency
  reliability.py          retries, adaptive interval, circuit breaker
  runner.py monitor.py    the poll loop and health signals
  llm.py                  Groq client with an on-disk cache
  sentiment.py             bonus: bullish/bearish/neutral scoring for delivered alerts
scripts/                  backfill, eval set construction, labeling, evaluation, latency,
                          dashboard (bonus: local read only http.server dashboard)
data/                     the 45 day archive, the lexicon, the evaluation set
tests/                    195 tests, offline, against recorded fixtures
```

## Tests

```bash
uv run pytest -q
```

Everything runs offline against recorded fixtures. No test touches the network.
