"""Sequential testing on the Criteo Uplift data: what happens when people check results early.

Two questions an experimentation team asks:
  1. If we look at the A/B result repeatedly and stop at the first p < 0.05, how often do we
     declare a winner when there is no effect?          -> A/A replays (treatment labels shuffled)
  2. How early can a test that is valid under continuous monitoring stop on a real effect?
                                                        -> always-valid mSPRT on the real data

Methods
  naive peeking   two-sample z-test at every look, stop at the first p < alpha
  fixed horizon   one z-test at the final look only
  mSPRT           mixture sequential probability ratio test with a normal mixing prior
                  (Johari et al., "Always Valid Inference", 2017); stop when Lambda >= 1/alpha.
                  Valid at every look, so peeking cannot inflate false positives.

Looks are on a geometric schedule (users arriving in random order), from --first-look users to all.

Usage:
  python sequential.py --data criteo-research-uplift-v2.1.csv.gz --out results/sequential
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats


def look_points(n: int, n_looks: int, first: int) -> np.ndarray:
    pts = np.unique(np.geomspace(min(first, n), n, n_looks).astype(int))
    pts[-1] = n
    return pts


def cumulative_stats(y: np.ndarray, d: np.ndarray, pts: np.ndarray):
    """Treated/control sums at each look (binary outcome, so sum y^2 = sum y)."""
    cy_t, cn_t = np.cumsum(y * d)[pts - 1], np.cumsum(d)[pts - 1]
    cy_c, cn_c = np.cumsum(y * (1 - d))[pts - 1], np.cumsum(1 - d)[pts - 1]
    return cy_t, cn_t, cy_c, cn_c


def look_tests(cy_t, cn_t, cy_c, cn_c, tau: float):
    pt, pc = cy_t / np.maximum(cn_t, 1), cy_c / np.maximum(cn_c, 1)
    v = pt * (1 - pt) / np.maximum(cn_t, 1) + pc * (1 - pc) / np.maximum(cn_c, 1)
    v = np.maximum(v, 1e-18)
    theta = pt - pc
    z = theta / np.sqrt(v)
    p = 2 * stats.norm.sf(np.abs(z))
    t2 = tau ** 2
    log_lam = 0.5 * np.log(v / (v + t2)) + theta ** 2 * t2 / (2 * v * (v + t2))
    return theta, p, log_lam


def first_true(mask: np.ndarray) -> int:
    idx = np.flatnonzero(mask)
    return int(idx[0]) if idx.size else -1


def wilson(k: int, n: int):
    if n == 0:
        return [float("nan")] * 2
    z = 1.96
    c = (k + z * z / 2) / (n + z * z)
    h = z * np.sqrt(k * (n - k) / n + z * z / 4) / (n + z * z)
    return [float(c - h), float(c + h)]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="results/sequential")
    ap.add_argument("--outcomes", nargs="+", default=["visit", "conversion"])
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--looks", type=int, default=30)
    ap.add_argument("--first-look", type=int, default=10_000)
    ap.add_argument("--tau-rel", type=float, default=0.10,
                    help="mixing-prior scale as a fraction of the outcome's base rate (pre-specified)")
    ap.add_argument("--aa-reps", type=int, default=300)
    ap.add_argument("--aa-sample", type=int, default=2_000_000)
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args(argv)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    df = pd.read_csv(a.data, usecols=["treatment"] + a.outcomes,
                     dtype={c: np.int8 for c in ["treatment"] + a.outcomes})
    rng = np.random.default_rng(a.seed)
    df = df.iloc[rng.permutation(len(df))].reset_index(drop=True)      # random arrival order
    d_all = df["treatment"].to_numpy().astype(float)
    print(f"loaded {len(df):,} rows in {time.time() - t0:.0f}s")
    R = {"alpha": a.alpha, "looks": a.looks, "first_look": a.first_look, "tau_rel": a.tau_rel,
         "n_users": len(df), "outcomes": {}}

    for o in a.outcomes:
        y_all = df[o].to_numpy().astype(float)
        tau = a.tau_rel * y_all.mean()            # prior scale fixed from the base rate, not the effect
        thr = np.log(1 / a.alpha)

        # 1. real data: when does each method stop?
        pts = look_points(len(y_all), a.looks, a.first_look)
        theta, p, log_lam = look_tests(*cumulative_stats(y_all, d_all, pts), tau)
        k_seq, k_naive = first_true(log_lam >= thr), first_true(p < a.alpha)
        real = {"final_effect": float(theta[-1]), "fixed_horizon_p": float(p[-1]),
                "sequential_stop_users": int(pts[k_seq]) if k_seq >= 0 else None,
                "sequential_stop_share": float(pts[k_seq] / len(y_all)) if k_seq >= 0 else None,
                "sequential_effect_at_stop": float(theta[k_seq]) if k_seq >= 0 else None,
                "naive_stop_users": int(pts[k_naive]) if k_naive >= 0 else None}

        # 2. A/A replays: shuffle treatment labels (keeps the 85/15 split), so the true effect is 0
        n = min(a.aa_sample, len(y_all))
        y, d0 = y_all[:n], d_all[:n]
        pts_aa = look_points(n, a.looks, a.first_look)
        rej = {"naive_peeking": 0, "fixed_horizon": 0, "msprt": 0}
        for _ in range(a.aa_reps):
            d = rng.permutation(d0)
            _, pp, ll = look_tests(*cumulative_stats(y, d, pts_aa), tau)
            rej["naive_peeking"] += bool((pp < a.alpha).any())
            rej["fixed_horizon"] += bool(pp[-1] < a.alpha)
            rej["msprt"] += bool((ll >= thr).any())
        aa = {k: {"false_positive_rate": v / a.aa_reps, "ci95": wilson(v, a.aa_reps)} for k, v in rej.items()}
        aa["n_users"], aa["reps"], aa["n_looks"] = n, a.aa_reps, len(pts_aa)
        R["outcomes"][o] = {"tau": tau, "real": real, "aa": aa}
        print(f"{o} done {time.time() - t0:.0f}s")

    (out / "results.json").write_text(json.dumps(R, indent=2))
    L = [f"# Sequential testing on Criteo Uplift ({R['n_users']:,} users, random arrival order)", "",
         f"{a.looks} looks on a geometric schedule from {a.first_look:,} users; alpha = {a.alpha}; "
         f"mSPRT mixing scale = {a.tau_rel:.0%} of the base rate (fixed before looking at the effect).", "",
         "## False positives when checking early (A/A replays, true effect = 0)", "",
         "| outcome | users per replay | replays | naive peeking | fixed horizon | mSPRT (always valid) |",
         "|---|---|---|---|---|---|"]
    for o, r in R["outcomes"].items():
        aa = r["aa"]
        cell = lambda k: f"{100 * aa[k]['false_positive_rate']:.1f}% [{100 * aa[k]['ci95'][0]:.1f}, {100 * aa[k]['ci95'][1]:.1f}]"
        L.append(f"| {o} | {aa['n_users']:,} | {aa['reps']} | {cell('naive_peeking')} | {cell('fixed_horizon')} | {cell('msprt')} |")
    L += ["", "## How early a valid test can stop on the real effect", "",
          "| outcome | effect at full sample | mSPRT stops at (users) | share of sample | effect at stop |",
          "|---|---|---|---|---|"]
    for o, r in R["outcomes"].items():
        x = r["real"]
        if x["sequential_stop_users"] is None:
            L.append(f"| {o} | {x['final_effect']:+.5f} | did not stop | - | - |")
        else:
            L.append(f"| {o} | {x['final_effect']:+.5f} | {x['sequential_stop_users']:,} | "
                     f"{100 * x['sequential_stop_share']:.1f}% | {x['sequential_effect_at_stop']:+.5f} |")
    L += ["", "Naive peeking stops early too, but its false-positive rate above shows those stops cannot be trusted."]
    (out / "report.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
