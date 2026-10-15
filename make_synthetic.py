"""Synthetic data in the Criteo Uplift schema with a known ground truth, used only to check that
incrementality.py recovers what was planted. Numbers from this file are never results.

python make_synthetic.py --n 1500000 --out synthetic.csv.gz
"""
import argparse
import json

import numpy as np
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1_500_000)
    ap.add_argument("--out", default="synthetic.csv.gz")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    n = a.n
    X = rng.normal(size=(n, 12))
    d = (rng.random(n) < 0.85).astype(int)
    exposed = d * (rng.random(n) < 1 / (1 + np.exp(-(-3.3 + 0.8 * X[:, 4]))))
    sig = lambda z: 1 / (1 + np.exp(-z))
    # baseline (organic) propensity driven by f0, f3; ad effect driven by f1, f2 and only when exposed
    p_visit0 = sig(-3.3 + 0.9 * X[:, 0] + 0.5 * X[:, 3])
    p_conv0 = sig(-6.4 + 1.0 * X[:, 0] + 0.6 * X[:, 3])
    tau_visit = 0.25 * sig(1.5 * X[:, 1] - 1.0 * X[:, 2])        # added visit prob when exposed
    tau_conv = 0.04 * sig(2.0 * X[:, 1] - 1.5 * X[:, 2] - 1.0)   # added conversion prob when exposed
    p_visit = np.clip(p_visit0 + exposed * tau_visit, 0, 1)
    p_conv = np.clip(p_conv0 + exposed * tau_conv, 0, 1)
    visit = (rng.random(n) < p_visit).astype(int)
    conv = ((rng.random(n) < p_conv) & (visit | (rng.random(n) < 0.3))).astype(int)
    df = pd.DataFrame(X, columns=[f"f{i}" for i in range(12)])
    df["treatment"], df["conversion"], df["visit"], df["exposure"] = d, conv, visit, exposed
    df.to_csv(a.out, index=False)
    # true ITT effects on the visit outcome (exact expectation over the drawn X)
    p_exp_t = 1 / (1 + np.exp(-(-3.3 + 0.8 * X[:, 4])))
    truth = {"visit_itt": float((p_exp_t * tau_visit).mean()),
             "visit_relative": float((p_exp_t * tau_visit).mean() / p_visit0.mean())}
    print(json.dumps(truth, indent=1))


if __name__ == "__main__":
    main()
