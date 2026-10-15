# AdLift: Ad Incrementality and Targeting

How much do ads actually change what people do, and who should an advertiser spend on?
An analysis of the [Criteo Uplift dataset](https://huggingface.co/datasets/criteo/criteo-uplift):
13,979,592 users randomly assigned to see ads (85%) or not (15%), with visits and conversions recorded.

## Findings (full dataset)

| Question | Answer |
|---|---|
| Is the split as designed? | Yes: 85.00% treated, sample-ratio check p = 0.999 |
| Lift in conversions | **+59.4%** (95% CI +54.4% to +64.7%) |
| Lift in visits | **+27.1%** (95% CI +26.2% to +28.0%) |
| Are the groups comparable on the 12 features? | **No.** All 12 differ at Bonferroni p < 0.05 (largest \|z\| 67), and the features predict treatment with AUC 0.508, about 10 standard errors above chance |
| Can CUPED / CUPAC shorten the test? | Not on this data. Adjusting for the features moved the conversion lift by about 7 standard errors, which a valid adjustment should not do, so the variance reduction cannot be claimed as a sample saving |
| Do uplift models beat targeting likely buyers on **visits**? | **Yes, at shallow depths.** Transformed-outcome uplift captures 12 more points of incremental visits in the top 10% (paired bootstrap CI +9 to +15); Qini area +0.031 (CI +0.006 to +0.053) |
| ...and on **conversions**? | **No.** T-learner: 8 points worse at the top 10% (CI -13 to -3). Transformed outcome: no difference (0 points, CI -4 to +3) |
| Does checking results early break the test? | **Yes, badly.** In 300 A/A replays (labels shuffled, true effect zero), stopping at the first p < 0.05 across 30 checks gave **38.3% (visits) and 40.0% (conversions) false positives** instead of 5%. An always-valid mSPRT held 1.7% and 2.0% |
| How early can a valid test stop? | mSPRT stopped on the real visit lift after 34,859 users (**0.2%** of the sample) with the effect almost exactly right (+0.01032 vs +0.01034 at full sample). On conversions it stopped after 121,521 users (0.9%) but overstated the effect (+0.00158 vs +0.00115), the usual cost of stopping early on a rare outcome |
| What should an advertiser do? | For traffic, rank by uplift. For conversions, target the top 10% by purchase propensity: it captures 76% of incremental conversions (CI 72% to 80%) at 7.6x lower cost per incremental conversion than targeting everyone, assuming equal cost per user |

Full reports: [`results/conversion/report.md`](results/conversion/report.md) and [`results/visit/report.md`](results/visit/report.md).

### Why the imbalance matters
Criteo assembled this dataset from several advertisers' incrementality tests with different treatment ratios.
Version 1 leaked treatment through the features; version 2 subsampled every test to one global ratio to fix it
([Diemert et al.](https://arxiv.org/abs/2111.10106)). The balance checks here show treatment is still slightly
predictable from the features. That leaves two readings: residual mixing across tests (adjusted lift closer to the
truth) or features recorded after exposure began (raw lift correct). The data cannot fully separate them, so the
raw intent-to-treat lift is reported as the headline and the adjusted estimates are reported alongside it.

## Method
1. **Trust checks:** sample-ratio mismatch against the 85/15 design; per-feature Welch tests with Bonferroni
   correction; a cross-fitted classifier predicting treatment (AUC 0.5 expected under clean randomization).
2. **Lift:** difference in means with 95% CIs (delta method for relative lift), plus the effect per exposed user
   (intent-to-treat divided by the exposure gap).
3. **Variance reduction:** CUPED (linear prediction from the 12 features) and CUPAC (cross-fitted gradient
   boosting), each flagged invalid if it moves the estimate by more than 2 standard errors.
4. **Targeting:** response model (likely buyers) vs. two uplift models (T-learner, transformed outcome) on a
   held-out 30%, compared with Qini curves and a paired Poisson bootstrap (the same resamples for every strategy).
5. **Decision:** incremental outcomes and cost per incremental outcome at the top 10/20/30/50/100%.
   Cost uses an assumed CPM (`--cpm`), stated as an assumption.

## Sequential testing: what early peeking costs (`sequential.py`)
Experiment owners often check results daily and stop at the first significant result. `sequential.py`
replays the Criteo data in random arrival order with 30 looks and compares three rules:
naive peeking (stop at the first p < 0.05), a fixed-horizon test (one look at the end), and an
always-valid mSPRT (Johari et al., 2017) whose false-positive rate holds under continuous monitoring.
A/A replays (treatment labels shuffled, so the true effect is zero) measure false positives; the real
data shows how early the valid test can stop. Results: [`results/sequential/report.md`](results/sequential/report.md).

```bash
python sequential.py --data criteo-research-uplift-v2.1.csv.gz --out results/sequential
```

## Run
```bash
pip install -r requirements.txt
curl -L -o criteo-research-uplift-v2.1.csv.gz \
  https://huggingface.co/datasets/criteo/criteo-uplift/resolve/main/criteo-research-uplift-v2.1.csv.gz
python incrementality.py --data criteo-research-uplift-v2.1.csv.gz --outcome conversion --out results/conversion
python incrementality.py --data criteo-research-uplift-v2.1.csv.gz --outcome visit --out results/visit
```
Each run writes `report.md`, `results.json`, `budget_table.csv` and `qini.png` (about 8 to 12 minutes on a laptop).

## Correctness check
`make_synthetic.py` builds data in the same schema with planted effects and clean randomization. On it, the
pipeline recovers the true lift inside its 95% CI, reports a treatment AUC of 0.50, and marks the adjustment
valid, so the imbalance flagged on Criteo comes from the data, not the code.
