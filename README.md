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

**Getting the posts.** Truth Social is a Mastodon fork and never hid it. The web client
talks to `/api/v1/accounts/{id}/statuses`, paginated with `max_id` and `min_id` via the
standard `Link` header, so it comes back as clean JSON on a documented schema. The catch is
Cloudflare: plain `requests` gets a 403, and so does `curl`.

TLS fingerprint impersonation got through: `curl_cffi` with `impersonate="safari17_0"`
returns 200. Worth flagging how arbitrary that is, since `chrome124` is still blocked. I
found the working one by trying a few, not by understanding Cloudflare's rules, so the
exact string is pinned in config before someone "cleans it up".

Four options I weighed:

| Approach | Verdict |
| --- | --- |
| Mastodon JSON with TLS impersonation | **Chosen.** Structured, `min_id` returns only what is new, documented schema instead of scraped markup |
| `trumpstruth.org` RSS mirror | **Fallback.** It exposes `truth:originalId`, the same status ids the primary uses, so failing over does not confuse dedup |
| Headless browser on the HTML | Rejected. Slow and fragile, and buys nothing over an endpoint that already returns JSON |
| Third-party aggregators | Rejected. Extra latency, plus a dependency on someone else's uptime and editorial judgement |

The shared ids were luck as much as judgement. Had the mirror minted its own, failing over
would risk re-alerting everything already sent.

The trade-off: impersonation gives the best latency and fidelity and is also likeliest to
break, since it hangs on a setting somebody else controls. Hence the circuit breaker. The
mirror is genuinely worse though: text and ids only, no repost, quote or media structure,
and it runs behind.

**Polling.** The interval floats between 60 and 300 seconds. He posts in bursts, so each
quiet poll multiplies the wait by 1.5 up to the cap and any new post drops it back to 60.
That is roughly 500 requests a day instead of 1,440. The cost falls on whichever post opens
a burst, which can wait up to five minutes.

**Detection.** Two arms, compared. The rules run off a 95-row lexicon rating how ambiguous
each ticker is, so a riskier one needs more supporting context before it counts. That came
straight out of reading the corpus: political and financial vocabulary overlap badly here,
since *trade*, *market*, *deal* and *economy* are ordinary political words in this account.
Context terms split into strong (stock, shares, NASDAQ, earnings) and weak (market, trade,
economy), and risky tickers need the strong kind. Skip that split and his habit of shouting
in capitals turns ALL, BIG and NOW into a stream of garbage.

The LLM arm (`qwen/qwen3.6-27b` on Groq) only runs once the rules find a candidate. About 4%
of posts get that far, which bounds both cost and added latency and points the model at the
cases the rules genuinely cannot settle.

## 2. Results

The 45-day archive is 1,260 posts, roughly 28 a day. Three things about it shaped everything
after:

- **Not one cashtag.** Zero `$TICKER` occurrences in 1,260 posts. The `$DJT` form the brief
  opens with is implemented and unit tested, and real data never exercises it once.
- **37% of posts have no text.** 472 are images or video. A text detector cannot see them at
  all, and no amount of model quality fixes that.
- **235 posts, 30% of the ones with text, end with "President DONALD J. TRUMP" or
  "President DJT".** Match DJT naively and you fire on a third of everything he writes.

**The labeled set.** With a base rate this low, sampling 150 posts uniformly would have
turned up almost no positives and left recall unmeasurable. So the set is stratified, and
each stratum is there to answer one question:

| Stratum | n | Question | Weight |
| --- | --- | --- | --- |
| candidate | 23 | When it fires, is it right? | 1.0, a census, so precision is exact |
| random | 102 | What gets missed? | 3.62, projected back to the population |
| hard_negative | 25 | Does suppression hold? | 0.0, kept out of the headline numbers |

Hard negatives are picked by hand to be nasty, which makes them purposive. Weighting them
back onto the population would let one labeling call move the estimate by sixteen posts, and
with roughly ten true positives in the corpus that call would decide the answer. They get
reported separately as a suppression check.

Labels were drafted by `gpt-oss-120b`, then reviewed by hand on every row. That partly grades
the LLM arm against its own homework, so 30 posts are labeled blind with no proposal visible,
drawn proportionally from each stratum. Comparing those 30 against the other 120 is what
makes the number checkable. The models differ by family on purpose: `gpt-oss-120b` drafts,
`qwen3.6-27b` detects.

`scripts/evaluate.py` produces the metrics and writes `data/eval/metrics.md`: weighted
precision, recall and F1 for both arms, with bootstrap intervals, since a bare F1 on a
positive class this small is mostly noise.

## 3. Robustness and ethics

Everything rests on a TLS fingerprint continuing to work and those endpoints staying put.
Both are outside my control.

I spent most of the time on the failure that does not announce itself. A 404 is easy. The bad
case is a 200 with a changed shape, where parsing yields nothing and the agent looks healthy
while going deaf. Two guards: if over half a page fails to parse the source raises instead of
returning an empty list, and a `no_new_posts` heartbeat fires when polls keep succeeding but
nothing arrives for hours. That alarm state persists, or restarting would reset the clock and
paper over an outage already underway.

Behind those, a circuit breaker fails over to the mirror, plus stale-poll and error-streak
alarms, rate limited so nobody learns to ignore them.

**Politeness** is in the code, not a comment promising good behaviour: a 2.5 second floor
between requests, a rolling hourly cap that refuses to send past 600 rather than just logging
about it, `Retry-After` respected on 429, strictly sequential requests. The cap is the one I
care about. Backoff and retry stay correct right up until a loop bug turns them into a
hammer, and the cap is what bounds that.

**Legal and ToS.** This reads public pages with no account and takes nothing a browser is not
already handed. Read-only, and it keeps only public post text. The mirror's `robots.txt`
allows everything; Truth Social does not publish one.

I would rather not oversell that. Automated access probably conflicts with their terms even
though the pages are public, and impersonating a browser's TLS fingerprint is deliberately
working around bot protection, which goes past just reading a page. At one request a minute
for a prototype I think it is fine. To run this for real I would want a licensed feed or
actual permission instead of a heavier version of the same trick.

## 4. Limitations and next steps

**Limitations.** The 37% of posts that are pure media are invisible; OCR is the obvious
answer. The lexicon caps recall outright, since ground truth already includes `SPGI`, `V`,
`TM` and `TMUS`, none among the 95 rows, so the rule arm cannot reach them at any threshold
while the LLM has no such ceiling. The positive class is small enough that intervals are
wide, which is reported rather than smoothed over.

**More accounts.** Ingestion is already per-account, so this is scheduling rather than
architecture. Swap the single loop for a priority queue keyed on each account's observed
posting rate: a busy account polls every 60 seconds while a quiet one drifts to 15 minutes,
under one global request budget. Dedup and idempotency already key on the status id, so
storage is untouched.

**Evaluating in production without labeling everything.** Three things stack. Run both arms
over live traffic and hand-label only where they disagree, putting the effort on the decision
boundary. Turn delivered alerts into a labeling queue, where a thumbs up or down in Telegram
returns a label for almost nothing. And watch input drift rather than accuracy: candidate
rate and ticker distribution need no labels at all, and a sharp move in either is usually the
first hint something changed, in the world or in the scraper.

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
