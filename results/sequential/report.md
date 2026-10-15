# Sequential testing on Criteo Uplift (13,979,592 users, random arrival order)

30 looks on a geometric schedule from 10,000 users; alpha = 0.05; mSPRT mixing scale = 10% of the base rate (fixed before looking at the effect).

## False positives when checking early (A/A replays, true effect = 0)

| outcome | users per replay | replays | naive peeking | fixed horizon | mSPRT (always valid) |
|---|---|---|---|---|---|
| visit | 2,000,000 | 300 | 38.3% [33.0, 43.9] | 5.0% [3.1, 8.1] | 1.7% [0.7, 3.8] |
| conversion | 2,000,000 | 300 | 40.0% [34.6, 45.6] | 5.7% [3.6, 8.9] | 2.0% [0.9, 4.3] |

## How early a valid test can stop on the real effect

| outcome | effect at full sample | mSPRT stops at (users) | share of sample | effect at stop |
|---|---|---|---|---|
| visit | +0.01034 | 34,859 | 0.2% | +0.01032 |
| conversion | +0.00115 | 121,521 | 0.9% | +0.00158 |

Naive peeking stops early too, but its false-positive rate above shows those stops cannot be trusted.
