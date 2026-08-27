# Detection evaluation

labeled file: data/eval/labeled.jsonl
labeled rows found: 150
eval sample rows: 150
LLM prelabels: 150 rows from data/eval/predictions_qwen.jsonl
model that produced the scored LLM predictions: qwen/qwen3.6-27b
That is the model the llm and combined arms are scored on here. It is not necessarily the model the agent runs at detection time, which agent.py configures separately.
labeled rows matched to a sample row: 150
sample rows with no label yet: 0 of 150
labeled rows counted toward headline (weighted) metrics: 125

## Headline metrics (candidate + random strata only, hard_negative excluded)
### rules arm
  weighted:   precision=0.875 recall=0.795 f1=0.833 (tp=14.0 fp=2.0 fn=3.6)
  unweighted (on-sample, n=125): precision=0.875 recall=0.933 f1=0.903 (tp=14 fp=2 fn=1 tn=108)
  bootstrap 95% CI (2000 resamples, seed 42): precision=[0.688, 1.000] recall=[0.480, 1.000] f1=[0.602, 1.000]
### llm arm
  weighted:   precision=1.000 recall=0.943 f1=0.971 (tp=16.6 fp=0.0 fn=1.0)
  unweighted (on-sample, n=125): precision=1.000 recall=0.933 f1=0.966 (tp=14 fp=0 fn=1 tn=110)
  bootstrap 95% CI (2000 resamples, seed 42): precision=[1.000, 1.000] recall=[0.800, 1.000] f1=[0.889, 1.000]
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
  precision=0.870 recall=0.653 f1=0.746 (tp=20.0 fp=3.0 fn=10.6)
  exact ticker set: 12/15 stock related posts
### llm arm
  precision=0.922 recall=0.771 f1=0.840 (tp=23.6 fp=2.0 fn=7.0)
  exact ticker set: 8/15 stock related posts
### combined arm
  precision=0.844 recall=0.882 f1=0.862 (tp=27.0 fp=5.0 fn=3.6)
  exact ticker set: 12/15 stock related posts

## Circularity check (arm predictions vs the ground truth labels)
  Agreement over every labeled row, not just the headline strata. High agreement is expected. Total agreement means the labels came from the arm.
  rules: 147/150 rows agree (0.980)
  llm: 149/150 rows agree (0.993)
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
  blind=true  (n=25): precision=1.000 recall=0.500 f1=0.667
  blind=false (n=100): precision=1.000 recall=1.000 f1=1.000
  gap (blind=false f1 minus blind=true f1): 0.333

## Error analysis
### rules arm
  false positives (2):
    stratum=candidate (2):
      [117061655895656555] RT: https://truthsocial.com/users/mrddmia/statuses/117060035123254425Very true! President DJT  My la...
      [116902945158204426] It’s incredible! I win the Election IN A LANDSLIDE against the entire Dumocrat Party, and almost 100...
  false negatives (1):
    stratum=random (1):
      [116986556826955994] https://www.cnbc.com/2026/07/23/google-1-billion-eu-fine-dma.html
### llm arm
  false positives (0):
    none
  false negatives (1):
    stratum=candidate (1):
      [116902264897167883] Maggot Hagerman has covered me incorrectly for ten years. Her book is a joke! 90% of it is Fake News...
### combined arm
  false positives (0):
    none
  false negatives (2):
    stratum=candidate (1):
      [116902264897167883] Maggot Hagerman has covered me incorrectly for ten years. Her book is a joke! 90% of it is Fake News...
    stratum=random (1):
      [116986556826955994] https://www.cnbc.com/2026/07/23/google-1-billion-eu-fine-dma.html
