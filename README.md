# Truth Social stock-mention alert agent

Monitors the public Truth Social account `@realDonaldTrump` and sends an alert whenever a
post mentions a publicly traded company, by ticker or by name. Runs locally.

## How it works

```mermaid
flowchart LR
  A["Truth Social<br/>JSON API"] -->|primary| D["dedup<br/><i>sqlite, survives restart</i>"]
  B["trumpstruth.org<br/>RSS mirror"] -.->|"fallback after<br/>3 failures"| D
  D --> R["rule detector<br/><i>95 ticker lexicon</i>"]
  R -->|"candidate,<br/>about 1% of posts"| L["LLM confirm<br/><i>Groq</i>"]
  R -->|"no candidate"| X["ignored"]
  L --> S["alert<br/><i>Telegram + console</i>"]
  D --> H["health monitor"]
  H -.->|"nothing new<br/>for N hours"| S
```

Both sources return the same Truth Social status ids, so switching between them cannot
re-alert a post that already went out. The LLM only ever sees posts the rules already
flagged, which keeps cost and added latency small.

### When something breaks

Every failure below was tested by forcing it, not just handled in principle.

| Failure | What happens |
| --- | --- |
| Primary API blocked or erroring | Circuit breaker opens after 3 consecutive failures and the RSS mirror takes over. It probes the primary again after a cooldown |
| Rate limited with `Retry-After` | Honoured inside the retry and again between polls, so the next request waits as long as the server asked |
| Endpoint changes shape | A page more than half unparseable raises rather than returning an empty list, so a silent schema change is loud |
| Polls succeed but nothing arrives | `no_new_posts` heartbeat fires. State is on disk, so a restart cannot reset the clock |
| Groq unavailable | The LLM arm falls back to the rule verdict and the agent keeps alerting |
| Sentiment model fails | The alert goes out without the sentiment line |
| Telegram down | Console still delivers, and the alert is retried on later polls |
| Process dies mid delivery | The alert is re-sent on the next poll. Idempotency keys on post and channel, so nothing goes twice |
| Process dies before detection | The post is picked up by a backlog pass and detected on the next poll |

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
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram delivery. Get the token from @BotFather, message your bot once, then run `uv run python scripts/setup_telegram.py` to fill in the chat id |
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

Truth Social runs a Mastodon fork, so its own web client calls
`/api/v1/accounts/{id}/statuses`. Clean JSON, and `min_id` returns only what is new. The
obstacle is Cloudflare: `requests` and `curl` both get 403. Impersonating a browser's TLS
fingerprint gets through, `curl_cffi` set to `safari17_0`. Chrome is still blocked, so I
found the working one by trying a few rather than understanding their rules.

| Option | Verdict |
| --- | --- |
| Mastodon JSON + TLS impersonation | **Chosen.** Structured and incremental |
| `trumpstruth.org` RSS mirror | **Fallback.** Same status ids, so failover cannot double-alert |
| Headless browser, third-party aggregators | Rejected. Slower and more brittle, no gain over JSON |

It is also the most fragile, depending on a Cloudflare setting I neither control nor get
warning about, which is why the mirror sits behind it.

**Polling** floats 60 to 300 seconds plus jitter, so real delays land near 48 to 360. Quiet
polls back off, any new post resets to base. Replaying the real history gives 305 requests a
day against 1,440 for a flat 60 second poll.

**Detection** pairs a rule baseline with an LLM arm. Each lexicon row rates its ticker's
ambiguity and riskier ones need more context, because here *trade* and *economy* are ordinary
political words. Context splits into strong (stock, shares, earnings) and weak. Without that
split, his habit of shouting in capitals turns ALL, BIG and NOW into noise.

## 2. Results

Three facts from the 1,260 post archive shaped everything. Zero cashtags appear, so `$DJT` is
implemented but never exercised by real data. 472 posts, 37 percent, carry no text and are
invisible to a text detector. And all 60 bare `DJT` tokens are his sign-off, none the ticker.

Mentions are rare enough that a random 150 would turn up almost none, so the set uses three
groups: candidates (23, every post the rules flagged, labelled completely so precision is
exact), random (102, reweighted by 3.62 to stand for the archive, which makes recall
measurable), and traps (25, lookalikes a rule picked out, scored separately). Together they
hold 15 real mentions.

Precision is how many alerts were real, recall how many real mentions got caught, F1 balances
the two. Ticker scores the same on whether the right company was named. Exact set counts how
many of the 15 got their whole ticker list right.

| Arm | Class P / R / F1 | Ticker P / R / F1 | Exact set | Traps |
| --- | --- | --- | --- | --- |
| rules | 0.867 / 0.738 / 0.797 | 0.857 / 0.588 / 0.697 | 11/15 | 25/25 |
| llm | 1.000 / 1.000 / 1.000 | 1.000 / 0.967 / 0.983 | 14/15 | 25/25 |
| **combined, ships** | 1.000 / 0.738 / 0.849 | 0.897 / 0.849 / 0.872 | 13/15 | 25/25 |

Resampling the rule arm 2,000 times puts its true F1 between 0.558 and 0.968 at 95 percent
confidence. Gating the LLM on rule candidates lets combined drop a false positive but never
recover a miss, so it inherits 0.738 recall by construction. What it buys is precision. Both
misses are known: S&P Global is outside the lexicon, and a bare link, since URLs are
stripped.

**The LLM's 1.000 is not a measurement.** `gpt-oss-120b` proposed every label and a stronger
model adjudicated all 150, changing 8 without flipping a verdict, so the labels match that
arm's own predictions 150 out of 150. A third model relabelled blind and agreed on 149, which
makes them consistent rather than independent, since every step is a language model.
`evaluate.py` detects this and warns. The scored model is `gpt-oss-120b`; the agent runs
`qwen3.6-27b`.

**Latency** over a 90 poll run: 26, 79 and 154 seconds from a post appearing to the agent
fetching it, then 7.4 ms to decide and 0.3 ms to send. The poll interval is the whole budget,
which makes backoff a latency decision as much as a politeness one. A fourth sample at 824
seconds is dropped as a cold start, leaving three, because no other stock post arrived during
the run.

## 3. Robustness and ethics

The failure table above lists what breaks and what catches it. The one worth naming is the
quiet one: a 404 is obvious, but a 200 with a changed shape is not, and the parser just
returns empty while everything looks fine. Hence an unparseable page raising rather than
returning nothing, and a heartbeat watching for polls that succeed while nothing arrives.

Politeness is in code rather than in a comment promising it: a 2.5 second floor between
requests, an hourly cap that refuses past 600, `Retry-After` honoured, strictly sequential
requests. The cap matters most, since backoff stays correct right until a loop bug turns it
into a hammer.

This reads public pages with no account and keeps only public post text. The mirror allows
crawling; Truth Social publishes no `robots.txt`. I would not oversell that: automated access
likely conflicts with their terms anyway, and impersonating a browser fingerprint works
around bot protection, which goes past reading a page. One request a minute seems
proportionate for a prototype. For real use I would want a licensed feed.

## 4. Limitations and next steps

Media-only posts are invisible; OCR is the answer. The lexicon caps recall, since the labels
contain `SPGI`, `V`, `TM` and `TMUS`, none among the 95 rows. Fifteen positives limit every
interval here. And the labels are model generated, so the ML comparison is not trustworthy.

**More accounts** is mostly scheduling now. Sources are per account, posts carry it, dedup
keys on the status id, and the polling cursor is namespaced, which it was not until I ran two
accounts against one database and watched the second overwrite the first. Namespacing then
orphaned the cursor on the existing database and the agent quietly refetched three weeks, so
it reads the old key once and carries it forward. What is left is the
loop: a priority queue keyed on each account's posting rate, so a busy one polls every minute
and a quiet one drifts to fifteen, under one shared budget.

**Evaluating in production** without labeling everything: run both arms over live traffic and
hand-label only where they disagree, which puts effort on the decision boundary and turns each
delivered alert into a labeling chance, since a thumbs up or down in Telegram costs nothing.
Alongside that, watch input drift rather than accuracy. Candidate rate and ticker distribution
need no labels, and a sharp move in either is the first hint something changed.

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
tests/                    203 tests, offline, against recorded fixtures
```

## Tests

```bash
uv run pytest -q
```

Everything runs offline against recorded fixtures. No test touches the network.
