# Truth Social stock-mention alert agent

Monitors the public Truth Social account `@realDonaldTrump` and sends an alert whenever a
post mentions a publicly traded company, by ticker or by name. Runs locally.

## How it works

```mermaid
flowchart LR
  A["Truth Social<br/>JSON API"] -->|primary| D["dedup<br/><i>sqlite, survives restart</i>"]
  B["trumpstruth.org<br/>RSS mirror"] -.->|"fallback after<br/>3 failures"| D
  D --> R["rule detector<br/><i>531 row lookup table</i>"]
  R -->|"candidate,<br/>about 1.3% of posts"| L["LLM confirm<br/><i>Groq</i>"]
  R -->|"no candidate"| X["ignored"]
  L --> S["alert<br/><i>Telegram + console</i>"]
  D --> H["health monitor"]
  H -.->|"nothing new<br/>for N hours"| S
```

Both sources return the same Truth Social status ids, so switching between them cannot
re-alert a post that already went out. The LLM only ever sees posts the rules already
flagged, which keeps cost and added latency small.

### Reading the numbers

The write-up reports two scores per detector. **Precision** is how many alerts were real,
**recall** how many real mentions got caught, and **F1** balances the two. The ticker columns
score whether the right company was named, not just whether the post was a mention. **Exact
set** counts how many stock posts got their full ticker list right.

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
| Telegram down | Console still delivers. One alert probes the channel, the rest of the poll skips it, and they queue for the next poll. Delivery is tracked per channel, so one channel being down cannot hold up another |
| Process dies mid delivery | The alert is re-sent on the next poll. The claim is keyed on post and channel, so a restart cannot repeat what already completed. Delivery is at least once, not exactly once |
| Process dies before detection | The next poll re-checks anything left undetected, so it is still caught |

## Demo and write-up

- [docs/demo_log.md](docs/demo_log.md) captures three real runs: the offline replay, the
  same command again showing dedup holds, and a live delivery to Telegram with the alert
  records behind it.
- [docs/index.html](docs/index.html) is a short page explaining the system for someone who
  has not read the code.
- The write-up starts at [Write-up](#write-up) below.

## Setup

Requires Python 3.11. The system Python on macOS is 3.9 and will not work.

```bash
uv venv --python 3.11
uv pip install -r requirements.txt
cp .env.example .env      # then fill in the values you want
```

Every credential is optional. With none set, the agent still runs and prints alerts to the
console.

Telegram is blocked outright on some networks, including whole countries. If
`setup_telegram.py` reports that it cannot reach the API, that is a blocked port rather than a
bad token, and the console channel needs no network at all.

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

# Pick a detector. Defaults to combined when GROQ_API_KEY is set, rules otherwise.
uv run python agent.py run --detector rules

# Verify your alert channel in isolation before trusting the agent
uv run python agent.py test-alert

uv run python agent.py health      # heartbeat state and active alarms
uv run python agent.py stats       # store counts
uv run pytest -q                   # full suite, offline, nothing touches the network
```

### Dashboard

```bash
uv run python scripts/dashboard.py --port 8000 --db data/agent.db
```

One page on `http.server`, no new dependency. It shows:

- a status hero (running, stopped or degraded) with live figures and a countdown to the next poll
- start and stop buttons
- poll interval presets, each with a line explaining what that value costs
- a backfill trigger, with a time and page estimate before you run it
- a fetched to alerted pipeline funnel
- a ticker filterable mentions feed
- health and latency, reusing `latency_report.py`'s own maths
- precision, recall and F1 read from `data/eval/metrics.md` when it exists
- the ticker lexicon editor

The page renders once, then a small script polls `GET /api/state` every 10 seconds and updates
in place, so nothing you are mid way through typing gets wiped by a refresh.

`is_running()` checks the recorded pid with `os.kill(pid, 0)` rather than trusting the pid
file, so a stale file left behind by a crash is reported as stopped instead of lying that
the agent is still running. Settings you change (poll interval, backfill window) are saved
to the store and survive a restart of the dashboard itself.

It is local only, binds to 127.0.0.1 by default, and has no authentication, so it must
never be exposed to a network.

### Reproducing the evaluation

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

Sampling is seeded, so a rebuild is deterministic. The committed sample was frozen before
the lookup table grew to 531 rows, so rebuilding today draws a different candidate stratum.
That is deliberate: the labels belong to the frozen sample, and re-drawing it would throw
them away.

`latency_report.py` reads timings recorded by live polls, so on a fresh clone it has nothing
to report and prints zeros. The numbers in the write-up come from a run against the real
account.

### Docker

```bash
docker build -t tsalert .
docker run --rm tsalert                                  # offline demo, nothing persisted
docker run --rm -v tsalert-data:/data tsalert            # same demo, state kept
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

Truth Social runs a Mastodon fork, so its web client calls
`/api/v1/accounts/{id}/statuses`: clean JSON, and `min_id` returns only what is new. The
obstacle is Cloudflare, which 403s both `requests` and `curl`. Impersonating a browser's TLS
fingerprint gets through, `curl_cffi` set to `safari17_0`. Chrome is still blocked, so that came
from trial, not from understanding.

| Option | Verdict |
| --- | --- |
| Mastodon JSON + TLS impersonation | **Chosen.** Latency is bounded by the poll interval. Reliable while the fingerprint holds |
| `trumpstruth.org` RSS mirror | **Fallback.** Same status ids, so failover cannot double-alert |
| Headless browser, aggregators | Rejected. Slower and more brittle, no gain over JSON |

It is also the most fragile, resting on a Cloudflare setting I neither control nor get warned
about, hence the mirror behind it.

**Polling** floats 30 to 60 seconds. Replaying the real posting history gives a median wait of
21 seconds and a worst case of 71, since the jitter can overshoot the cap. That costs volume: roughly 1,450 requests a day against 316
for the 60 to 300 range I started with. Latency won, and the throttle and hourly cap still
bound the worst case.

**Detection** pairs a rule baseline with an LLM arm over a 531 row table: the S&P 500, eight
ETFs, and companies he names outside the index. Each row rates its ticker's ambiguity, and
riskier ones need more context, because *trade* and *economy* are ordinary political words
here. Context splits into strong (stock, shares, earnings) and weak. Without that split,
capitals turn ALL and NOW into noise, and so do names like Ball and Progressive.

## 2. Results

Three facts from the 1,260 post archive shaped everything. No cashtags at all, so `$DJT` is
implemented but never exercised. 472 posts, 37 percent, carry no text. And all 61 bare `DJT`
tokens are his sign-off.

Mentions are rare enough that a random 150 would find almost none, so the set is stratified:
candidates (23, every post the rules flagged, labelled completely so precision is exact),
random (102, reweighted by 3.62 so recall is measurable), and traps (25, lookalikes scored
separately). Together they hold 15 real mentions.

| Arm | Class P / R / F1 | Ticker P / R / F1 | Exact set | Traps |
| --- | --- | --- | --- | --- |
| rules | 0.875 / 0.795 / 0.833 | 0.870 / 0.653 / 0.746 | 12/15 | 25/25 |
| llm | 1.000 / 0.943 / 0.971 | 0.922 / 0.771 / 0.840 | 8/15 | 25/25 |
| **combined, ships** | 1.000 / 0.738 / 0.849 | 0.844 / 0.882 / 0.862 | 12/15 | 25/25 |

Resampling puts the shipping arm's F1 between 0.603 and 1.000. **I optimised for precision:** a
miss costs one alert, a false alarm costs trust in every alert after it, and at one real
mention a week a feed that cries wolf gets muted. Both rule false positives are one shape, a
media outlet cited rather than discussed as a business (Fox News; New York Times and NBC),
which the LLM arm gets right.

The ticker column is where the arms separate, and not the way the headline suggests. The LLM
wins the yes/no call and loses on naming the companies: 8 of 15 exact sets against the rules'
12. It agrees a post is about stocks, then lists the wrong one. Gating the LLM on rule
candidates means combined can drop a false positive but never recover a miss, so it inherits
the rules' recall and ticker accuracy while keeping the LLM's precision.

**The labels, and the trade-off.** `gpt-oss-120b` proposed a label per post, a second model
labelled the same posts independently and agreed with the finished set on 149 of 150, and I
read all 150 and made the final call. Thirty of them I reviewed blind, without seeing either
proposal, to check I was reading rather than rubber-stamping.

The honest part is the override count: zero. Reviewing a proposal anchors you in a way
labelling cold does not, and both models come from one family, so their agreeing is weaker
evidence than two people agreeing. Read the numbers as resting on labels one person checked.
One thing is kept clean. The scored LLM arm is `qwen3.6-27b`, the model the agent runs, and it
never touched the labels. An earlier version scored the labelling model and returned 1.000 on all 150 rows,
measuring lineage rather than skill. `evaluate.py` now warns when an arm agrees with the labels
completely. The rule arm predates the labelling entirely.

**Latency** over a 90 poll run: 26, 79 and 154 seconds from post to fetch, then 7.4 ms to
decide and 0.3 ms to reach the console. The poll interval is the whole budget. Three samples
only, a fourth at 824 seconds being a cold start.

## 3. Robustness and ethics

In the failure table above, the quiet ones matter most: a changed schema and a stalled poll
both look like nothing is wrong, so both raise.

Delivery is at least once. A post and channel pair is claimed in sqlite before sending, so
restarts and repeat polls cannot resend, but a crash between Telegram accepting and that write
will resend. Telegram has no idempotency key, and a duplicate beats a miss.

The one bug I would not have found by reasoning came from my own network blocking Telegram.
Nothing was lost, but every alert in the poll spent the full retry budget alone, four timeouts
and backoff each, so a poll that should take a second took nearly six minutes. A channel that
exhausts its budget is down, not flaky, so it is skipped for the rest of the poll and probed
once, cheaply, on the next. Skipped alerts stay queued rather than counted as failures, since
spending their budget on sends that never happened would discard them. After: 88 seconds.

Politeness is in code, not a comment promising it: a 2.5 second floor between requests, an
hourly cap that refuses past 600, `Retry-After` honoured, requests strictly sequential. The cap
matters most, since backoff stays correct right up until a loop bug turns it into a hammer.

This reads public pages with no account and keeps only public post text. The mirror's
`robots.txt` allows crawling, Truth Social publishes none. Automated access likely conflicts with
their terms anyway, and impersonating a browser fingerprint works around bot protection, which
goes past reading a page. Proportionate for a prototype at a request a minute; for real use I
would want a licensed feed.

## 4. Limitations and next steps

Media-only posts are invisible; OCR is the answer. The lexicon caps recall: the S&P 500 and
eight ETFs, so a foreign or small cap name is unreachable, `TM` for Toyota being the one left
in the labels. Fifteen positives limit every interval above.

**More accounts** is mostly scheduling now. Sources are per account, posts record which one,
and dedup keys on the status id; the cursor needed fixing, since one global value let a second
account overwrite the first. What is left is a priority queue keyed on posting rate, so a busy
account polls every minute and a quiet one drifts to fifteen.

**Evaluating in production** without labelling everything: run both arms over live traffic and
hand-label only where they disagree, putting the effort on the decision boundary. Watch input
drift too. Candidate rate and ticker distribution need no labels, and a sharp move in either is
the first hint something changed.

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
  sentiment.py            bonus: bullish/bearish/neutral scoring on alerts
scripts/                  backfill, eval set construction, labeling, evaluation, latency,
                          dashboard (bonus: local control panel on http.server)
data/                     the 45 day archive, the lexicon, the evaluation set
tests/                    238 tests, offline, against recorded fixtures
```

