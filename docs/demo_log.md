# Demo log

Captured runs. The first two need no credentials or network and reproduce on a
clean clone. The third is a real delivery to Telegram.

## 1. Offline end to end, replaying real archive posts

```
$ uv run python agent.py run --once --source demo
Source: demo
Detector: rules
Active channels: console
STOCK MENTION: NVDA
companies: Nvidia
posted: 2026-07-27 23:20 UTC
detected: rules (confidence 0.85)

NVIDIA: Building in America, for America: https://www.nvidia.com/en-us/made-in-usa/

https://truthsocial.com/@realDonaldTrump/116994500400281844
STOCK MENTION: CVX
companies: Chevron
posted: 2026-08-03 13:50 UTC
detected: rules (confidence 0.85)

Mike Wirth, Chairman and CEO of Chevron, just gave, in an interview with the fabulous Maria Bartiromo, all of the reasons that his company is doing so well. The only thing he conveniently forgot to mention is that, without the genius, foresight, strength, and stability, of the TRUMP Administration, the Oil Industry, and our Country itself, would be DEAD! As an example, they threw Mike and Chevron out of Venezuela, but now they’re back, far bigger and stronger than ever before, expecting to make a fortune! That goes for other Oil Companies as well…and get your consumer (retail!) Oil Prices DOWN, NOW! Thank you for your attention to this matter. President DJT

https://truthsocial.com/@realDonaldTrump/117031897808226413
STOCK MENTION: GOOGL, GS, C, BAC, WMT, CMCSA
companies: Alphabet, Goldman Sachs, Citigroup, Bank of America, Walmart, Comcast
posted: 2026-08-11 02:31 UTC
detected: rules (confidence 0.85)

Chandler Hall, representing, on Television, the foolish Center for American Lack of Progress, stated that adding the National Guard to Cities, including our now Great Again, Washington, D.C., had “NO impact on Crime.” How crazy is that. Hall was met with furious dissent. The report is just another Radical Left SCAM, as are the people who fund this gaggle of Lunatics, including George Soros, Bill and Melinda Gates, Google, Apple, Visa, Goldman Sachs, Citigroup, WellsFargo, Bank of America, Walmart, Toyota, T-Mobile, and NBC Universal. Foreign support includes the Embassy of Japan, the Korea Foundation, Taipei Economic and Cultural Representative Office (Taiwan), and the Embassy of the United Arab Emirates. The Dumocrats love it, and are against anything “TRUMP.” These people, and others like them, are so bad for our Country. Their stated course is anything to hate or demean “TRUMP.” This will be met with a lawsuit, which is being drawn now. I am also strongly considering adding some of the contributors to this Fake Organization. Crime is way down since I took Office, and they know it. Liars, at this level, must be held accountable! President DONALD J. TRUMP

https://truthsocial.com/@realDonaldTrump/117074526504264990
STOCK MENTION: WMT
companies: Walmart
posted: 2026-08-12 22:58 UTC
detected: rules (confidence 0.85)

I am pleased to announce the nomination of Lee Rudofsky to the United States Court of Appeals for the Eighth Circuit! Lee is currently a distinguished Federal District Court Judge in Arkansas. He previously was Solicitor General of Arkansas, and Assistant General Counsel of Walmart. He is a Graduate of Harvard Law School and Cornell University. Lee will be a rock solid defender of the Constitution on the Eighth Circuit. Congratulations Lee! President DONALD J. TRUMP

https://truthsocial.com/@realDonaldTrump/117085013643913800
poll 1: 8 new post(s), 4 alert(s) sent
stopped after 1 poll(s), 8 new post(s), 4 alert(s) sent
```

Eight real posts from the 45 day archive. Four name a company and alert. The other
four mention a company word in a sense that is not the company (Intel as
intelligence, ABC News and Fox News as broadcasters he is appearing on or
complaining about, New York Times as a bestseller list) and are correctly suppressed.

## 2. Dedup across a restart

Same command, same database. Nothing is re-alerted.

```
$ uv run python agent.py run --once --source demo
Detector: rules
Active channels: console
poll 1: 0 new post(s), 0 alert(s) sent
stopped after 1 poll(s), 0 new post(s), 0 alert(s) sent
```

## 3. Real delivery to Telegram

Same pipeline with credentials set. Both channels are attempted per post and each
is claimed separately, so one failing cannot suppress the other.

```
$ uv run python agent.py test-alert
Active channels: console, telegram
  console: delivered
  telegram: delivered

$ uv run python agent.py run --once --source demo
poll 1: 8 new post(s), 4 alert(s) sent

$ sqlite3 agent.db "select channel, post_id, status, attempts from alerts"
console|116994500400281844|delivered|1
console|117031897808226413|delivered|1
console|117074526504264990|delivered|1
console|117085013643913800|delivered|1
telegram|116994500400281844|delivered|1
telegram|117031897808226413|delivered|1
telegram|117074526504264990|delivered|1
telegram|117085013643913800|delivered|1
```

Eight records, four posts across two channels, all delivered on the first attempt.
