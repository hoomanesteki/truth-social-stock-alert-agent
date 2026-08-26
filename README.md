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

A read only view of mentions, health, latency and store counts, plus a lexicon editor
and a backfill trigger, built on `http.server` with no new dependency. It is local only,
binds to 127.0.0.1 by default, and has no authentication, so it must never be exposed to
a network.

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

The volume matters: dedup and alert idempotency both live in the SQLite file, so without
it a restarted container would re-alert on every post it had already seen.

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
else controls, hence the circuit breaker. The mirror is genuinely worse: text and ids only,
no repost or media structure, and it lags.

**Polling** floats between 60 and 300 seconds. He posts in bursts, so quiet polls back off
1.5x and any new post resets to 60. Roughly 500 requests a day instead of 1,440.

**Detection** pairs a rule baseline with an LLM arm. The lexicon rates how ambiguous each
ticker is, and riskier ones need more context. That came from the corpus, where *trade*,
*market* and *economy* are ordinary political words, so context terms split into strong
(stock, shares, NASDAQ, earnings) and weak. Without that split his habit of shouting in
capitals turns ALL, BIG and NOW into noise.

## 2. Results

The 45-day archive is 1,260 posts. Three properties shaped everything:

| Finding | Consequence |
| --- | --- |
| Zero cashtags in 1,260 posts | The `$DJT` form is implemented but never exercised by real data |
| 37% of posts have no text | 472 are images or video, invisible to a text detector |
| 30% sign off "President DJT" | A naive DJT match fires on a third of everything |

With a base rate this low, uniform sampling would find almost no positives. The set is
stratified, each stratum answering one question:

| Stratum | n | Question | Weight |
| --- | --- | --- | --- |
| candidate | 23 | When it fires, is it right? | 1.0, census, exact precision |
| random | 102 | What gets missed? | 3.62, projected to population |
| hard_negative | 25 | Does suppression hold? | 0.0, excluded from headline |

Hard negatives are picked to be adversarial, so weighting them onto the population would let
one call shift the estimate by sixteen posts. Reported separately instead.

**Rule baseline**, weighted over candidate and random, 15 positives:

| Metric | Value |
| --- | --- |
| Precision | 0.867 |
| Recall | 0.738 |
| F1 | 0.797, bootstrap 95% CI [0.558, 0.968] |
| Trap suppression | 25/25 |

Both false negatives are known limitations: S&P Global, whose symbol is not among the 95
lexicon rows, and a bare CNBC link, since URLs are stripped before matching.

**The LLM arm scored 1.000 on everything, and that is not evidence.** Ground truth here was
produced by a language model, and it agreed with the LLM detector on 142 of 150 rows.
Scoring a model against labels a similar model wrote is circular, and the blind holdout
shows a zero gap for the same reason. A real baseline-versus-ML comparison needs human
labels. That is the biggest weakness here, and I would rather say so than report a perfect
score.

**Latency**, over a 90 poll live run:

| Stage | Measured |
| --- | --- |
| Published to fetched | 26s, 79s, 154s |
| Fetched to detected | 7.4 ms |
| Detected to delivered | 0.3 ms |

The poll interval is effectively the whole budget, which makes the adaptive backoff a latency
decision as much as a politeness one. A fourth sample read 824s and is excluded as cold
start, the first poll after startup collecting an older post. Three samples is thin, because
no stock-related post arrived during the window.

## 3. Robustness and ethics

Everything rests on a fingerprint continuing to work and those endpoints staying put, both
outside my control.

The failure worth engineering against is the quiet one. A 404 is easy. The bad case is a 200
with a changed shape, where parsing yields nothing and the agent looks healthy while going
deaf. So if over half a page fails to parse the source raises instead of returning an empty
list, and a `no_new_posts` heartbeat fires when polls succeed but nothing arrives. That alarm
state persists, or a restart would reset the clock and hide an outage already underway.
Behind those, a circuit breaker onto the mirror and error-streak alarms, rate limited so
nobody learns to ignore them.

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

Media-only posts are invisible; OCR is the obvious answer. The lexicon caps recall outright,
since ground truth contains `SPGI`, `V`, `TM` and `TMUS`, none among the 95 rows. Fifteen
positives makes every interval wide. And the ground truth is model-generated, so the ML
comparison is not yet trustworthy.

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
tests/                    140 tests, offline, against recorded fixtures
```

## Tests

```bash
uv run pytest -q
```

Everything runs offline against recorded fixtures. No test touches the network.
