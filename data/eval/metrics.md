# Detection evaluation

labeled file: data/eval/labeled.jsonl
labeled rows found: 150
eval sample rows: 150
LLM prelabels: 150 rows from data/eval/prelabels.jsonl
model that produced the scored LLM predictions: openai/gpt-oss-120b
That is the model the llm and combined arms are scored on here. It is not necessarily the model the agent runs at detection time, which agent.py configures separately.
labeled rows matched to a sample row: 150
sample rows with no label yet: 0 of 150
labeled rows counted toward headline (weighted) metrics: 125

## Headline metrics (candidate + random strata only, hard_negative excluded)
### rules arm
  weighted:   precision=0.867 recall=0.738 f1=0.797 (tp=13.0 fp=2.0 fn=4.6)
  unweighted (on-sample, n=125): precision=0.867 recall=0.867 f1=0.867 (tp=13 fp=2 fn=2 tn=108)
  bootstrap 95% CI (2000 resamples, seed 42): precision=[0.667, 1.000] recall=[0.432, 1.000] f1=[0.558, 0.968]
### llm arm
  weighted:   precision=1.000 recall=1.000 f1=1.000 (tp=17.6 fp=0.0 fn=0.0)
  unweighted (on-sample, n=125): precision=1.000 recall=1.000 f1=1.000 (tp=15 fp=0 fn=0 tn=110)
  bootstrap 95% CI (2000 resamples, seed 42): precision=[1.000, 1.000] recall=[1.000, 1.000] f1=[1.000, 1.000]
  WARNING: this arm's predictions match the ground truth labels on 150/150 labeled rows, without a single disagreement.
  That is not a measurement of the arm. The labels derive from these very
  predictions, so precision and recall here are computing x == x and can only
  come out perfect. A bootstrap CI of exactly [1.000, 1.000] is the same fact
  restated: no resample of a set containing zero errors can produce one.
  Treat these numbers as a consistency check on the label file, not as a score.
### combined arm
  weighted:   precision=1.000 recall=0.738 f1=0.849 (tp=13.0 fp=0.0 fn=4.6)
  unweighted (on-sample, n=125): precision=1.000 recall=0.867 f1=0.929 (tp=13 fp=0 fn=2 tn=110)
  bootstrap 95% CI (2000 resamples, seed 42): precision=[1.000, 1.000] recall=[0.432, 1.000] f1=[0.603, 1.000]
  note: this is the arm the agent actually ships. It fires only where the rule arm
  produced a candidate and the LLM then confirmed it, so the cascade can only remove
  a rule positive, never recover a rule false negative. Its recall is therefore
  capped at the rule arm's by construction, and its precision inherits whatever the
  LLM arm's predictions carry, including whatever the circularity check below finds.

## Ticker extraction (which stocks, not just whether)
  Micro averaged over ticker sets, weighted like the headline numbers.
  Exact set counts posts where the predicted tickers match the labels exactly,
  over the posts that really are stock related.

### rules arm
  precision=0.857 recall=0.588 f1=0.697 (tp=18.0 fp=3.0 fn=12.6)
  exact ticker set: 11/15 stock related posts
### llm arm
  precision=1.000 recall=0.967 f1=0.983 (tp=29.6 fp=0.0 fn=1.0)
  exact ticker set: 14/15 stock related posts
### combined arm
  precision=0.897 recall=0.849 f1=0.872 (tp=26.0 fp=3.0 fn=4.6)
  exact ticker set: 13/15 stock related posts

## Circularity check (arm predictions vs the ground truth labels)
  Agreement over every labeled row, not just the headline strata. High agreement is expected. Total agreement means the labels came from the arm.
  rules: 146/150 rows agree (0.973)
  llm: 150/150 rows agree (1.000)
    WARNING: this arm's predictions match the ground truth labels on 150/150 labeled rows, without a single disagreement.
    That is not a measurement of the arm. The labels derive from these very
    predictions, so precision and recall here are computing x == x and can only
    come out perfect. A bootstrap CI of exactly [1.000, 1.000] is the same fact
    restated: no resample of a set containing zero errors can produce one.
    Treat these numbers as a consistency check on the label file, not as a score.
  combined: 148/150 rows agree (0.987)

## Suppression report (hard_negative trap stratum)
### rules arm
  hard_negative rows labeled: 25
  confirmed actually negative (true traps): 25
  correctly stayed quiet: 25/25
### llm arm
  hard_negative rows labeled: 25
  confirmed actually negative (true traps): 25
  correctly stayed quiet: 25/25
### combined arm
  hard_negative rows labeled: 25
  confirmed actually negative (true traps): 25
  correctly stayed quiet: 25/25

## Blind subset comparison (LLM arm only)
  blind=true  (n=25): precision=1.000 recall=1.000 f1=1.000
  blind=false (n=100): precision=1.000 recall=1.000 f1=1.000
  gap (blind=false f1 minus blind=true f1): 0.000
  This split cannot show anything while the LLM arm matches the labels on every row. Both halves are perfect for the same reason, so the gap of zero measures nothing.

## Error analysis
### rules arm
  false positives (2):
    stratum=candidate (2):
      [117061655895656555] RT: https://truthsocial.com/users/mrddmia/statuses/117060035123254425Very true! President DJT  My la...
      [116902945158204426] It’s incredible! I win the Election IN A LANDSLIDE against the entire Dumocrat Party, and almost 100...
  false negatives (2):
    stratum=candidate (1):
      [117156902282061140] S&P Global: ‘US Business Is Booming’: https://www.breitbart.com/economy/2026/08/21/biz-is-booming/
    stratum=random (1):
      [116986556826955994] https://www.cnbc.com/2026/07/23/google-1-billion-eu-fine-dma.html
### llm arm
  false positives (0):
    none
  false negatives (0):
    none
### combined arm
  false positives (0):
    none
  false negatives (2):
    stratum=candidate (1):
      [117156902282061140] S&P Global: ‘US Business Is Booming’: https://www.breitbart.com/economy/2026/08/21/biz-is-booming/
    stratum=random (1):
      [116986556826955994] https://www.cnbc.com/2026/07/23/google-1-billion-eu-fine-dma.html
