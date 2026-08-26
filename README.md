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
| `GROQ_API_KEY` | The LLM detector arm and the labeling helper |

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

---

# Write-up

## 1. Approach

**Reading without an API.** Truth Social runs a Mastodon fork, so it exposes
Mastodon-compatible JSON endpoints that its own web client uses. The timeline is at
`/api/v1/accounts/{id}/statuses`, paginated via the RFC 5988 `Link` header with `max_id`
and `min_id`. The obstacle is Cloudflare: plain `requests` or `curl` gets a 403. TLS
fingerprint impersonation works. `curl_cffi` with `impersonate="safari17_0"` returns 200
and clean JSON, while `chrome124` is still blocked, so the exact fingerprint matters and is
pinned in config.

Four options considered:

| Approach | Verdict |
| --- | --- |
| Mastodon JSON with TLS impersonation | **Chosen.** Structured, `min_id` returns only new posts, documented schema rather than scraped markup |
| `trumpstruth.org` RSS mirror | **Fallback.** Carries `truth:originalId`, the same status ids as the primary, so dedup survives failover with no reconciliation |
| Headless browser on the HTML | Rejected. Slow and brittle, no gain over an endpoint returning JSON |
| Third-party aggregators | Rejected. Adds latency and depends on someone else's uptime and editorial choices |

The trade-off: TLS impersonation is the lowest-latency, highest-fidelity route and also the
most fragile, since it depends on a bot-protection setting that can change without notice.
Hence the mirror behind a circuit breaker. The mirror is a degraded mode, not an equal: it
carries text and ids but loses repost, quote and media structure, and it lags the source.

**Polling.** The interval adapts between 60 and 300 seconds. He posts in bursts, so a quiet
poll multiplies the interval by 1.5 up to a cap and any new post snaps it back to 60
seconds. Against a fixed 60 second poll that cuts volume from about 1,440 to about 500
requests per day, costing latency only on the post that opens a burst.

**Detection.** A rule baseline and an LLM arm, compared head to head. The rules use a 95-row
lexicon where each row carries an ambiguity rating: the higher it is, the more context a
match must earn. The design turns on one observation, that here political and financial
vocabulary overlap heavily, since *trade*, *market*, *deal* and *economy* are everyday
political words. Context terms therefore split into strong evidence (stock, shares, NASDAQ,
earnings) and weak (market, trade, economy), and high-ambiguity tickers require the strong
kind. Without that split, his habit of writing in capitals turns ALL, BIG and NOW into
constant false positives.

## 2. Results

The 45-day archive is 1,260 posts, about 28 per day. Three properties shaped everything
downstream:

- **Zero cashtags** in 1,260 posts. The `$DJT` form the brief leads with is implemented and
  unit tested but never exercised by real data.
- **37% of posts have no text at all.** 472 are images or video, invisible to a text
  detector. That is a ceiling on recall no model quality can lift.
- **235 posts, 30% of text posts, sign off "President DONALD J. TRUMP" or "President DJT".**
  A naive DJT match would fire on a third of everything he writes.

**Labeled set.** With a base rate near zero, a uniform sample of 150 posts would hold almost
no positives and recall would be unmeasurable. The set is stratified, each stratum answering
one question:

| Stratum | n | Question it answers | Weight |
| --- | --- | --- | --- |
| candidate | 30 | When it fires, is it right? | 1.0, a census, so precision is exact |
| random | 95 | What does it miss? | 3.85, projected to the population |
| hard_negative | 25 | Does suppression work? | 0.0, excluded from headline metrics |

Hard negatives are hand-picked to be adversarial, making them a purposive rather than
probability sample. Projecting them onto the population would let one labeling call swing
the estimate by sixteen posts, which against roughly ten true positives would decide the
result by itself. They are reported separately as a suppression stress test.

Labels were proposed by `gpt-oss-120b` then reviewed by a human on every row. Since that
partly grades the LLM arm against its own output, 30 posts are labeled blind with no proposal
shown, drawn proportionally across strata. Comparing the arm on the blind subset against the
reviewed subset makes its score falsifiable rather than self-graded. The models are from
different families on purpose: `gpt-oss-120b` labels, `qwen3.6-27b` detects.

*Metrics and the measured latency are filled in once the labeled set is complete. This
section is deliberately left empty rather than populated with placeholder numbers.*

## 3. Robustness and ethics

The primary route depends on a TLS fingerprint continuing to pass Cloudflare and on the
Mastodon-compatible endpoints staying. Either could change without warning. Mitigations,
ordered by how quietly the failure would otherwise happen:

- **Silent schema change is the dangerous one.** A 404 is loud. What hurts is HTTP 200 with a
  changed shape: you parse nothing, alert on nothing, and the dashboard says healthy forever.
  If over half a page fails to parse, the source raises rather than returning an empty list.
- **A `no_new_posts` heartbeat.** Polls succeeding while nothing new arrives for N hours is
  what a silently emptied response looks like from outside. Alarm state persists, so a
  restart cannot reset the clock and hide an outage already hours old.
- **A circuit breaker with automatic failover** to the mirror after repeated failures, with
  the transition logged and alerted, plus consecutive-error and stale-poll alarms, all rate
  limited so an operator does not learn to ignore them.

**Politeness** is enforced in code, not promised in a comment. A monotonic throttle holds a
floor of 2.5 seconds between any two requests, a rolling hourly cap refuses to send past 600
rather than merely warning, `Retry-After` is honored on 429, and requests are strictly
sequential. The cap matters most: backoff and retry are both correct right up until a loop
bug turns them into a hammer, and the cap bounds that worst case.

**Legal and terms of service.** This reads public pages with no account and no access to
anything a browser is not already served, is read-only, and stores nothing beyond public post
text. The mirror's `robots.txt` permits all crawling; Truth Social serves none. Still,
undisclosed automated access plausibly conflicts with their terms even where technically
permitted, and TLS impersonation deliberately circumvents a bot-protection measure, which is
a real step beyond reading a public page. At one request per minute for a prototype that is
proportionate; productionizing it would warrant a licensed feed or explicit permission rather
than a heavier version of the same trick. Scale changes the ethics even when the mechanism
does not.

## 4. Limitations and next steps

**Limitations.** The 37% of posts that are pure media are invisible; OCR is the obvious fix.
The lexicon is a hard recall ceiling: ground truth already contains `SPGI`, `V`, `TM` and
`TMUS`, none in the 95 rows, so the rule arm cannot find them at any threshold while the LLM
has no such limit. The positive class is small, so intervals are wide and reported as such.

**Multiple accounts.** Ingestion is already per-account, so this is scheduling, not
architecture. Replace the single loop with a priority queue keyed on each account's observed
posting rate, so a busy account polls at 60 seconds while a quiet one drifts to 15 minutes,
all under one global rate budget. Dedup and idempotency already key on the status id, so
storage is unchanged.

**Production evaluation without hand-labeling everything.** Three things compound. Run both
arms on all live traffic and hand-label only where they disagree, concentrating attention on
the decision boundary. Treat delivered alerts as a labeling queue, so a thumbs up or down in
Telegram returns labels at nearly zero marginal cost. And monitor input drift rather than
accuracy: candidate generation rate and ticker distribution over time need no labels at all,
and a sharp move in either is usually the first sign that the world or the scraper changed.

---

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
scripts/                  backfill, eval set construction, labeling, evaluation, latency
data/                     the 45 day archive, the lexicon, the evaluation set
tests/                    140 tests, offline, against recorded fixtures
```

## Tests

```bash
uv run pytest -q
```

Everything runs offline against recorded fixtures. No test touches the network.
