"""Ad incrementality on the Criteo Uplift dataset (randomized ad exposure, 13.9M users).

Questions, in the order an ads measurement team would ask them:
  1. Is the experiment trustworthy?        sample-ratio check, covariate balance
  2. How much do ads lift visits and conversions?
                                            ITT lift with 95% CIs, plus the effect on users actually
                                            exposed (Wald / complier estimate)
  3. Could the same answer come from a shorter test?
                                            CUPED-style regression adjustment on pre-treatment
                                            features, and CUPAC (ML-predicted covariate, cross-fitted)
  4. Who should an advertiser target?       uplift models vs. targeting likely buyers, Qini curves,
                                            Poisson-bootstrap CIs
  5. What is the decision?                  budget table: incremental conversions and cost per
                                            incremental conversion at each targeting depth

Data: criteo-research-uplift-v2.1.csv.gz (columns f0..f11, treatment, conversion, visit, exposure)
  download: https://ailab.criteo.com/criteo-uplift-prediction-dataset/
  or:       from sklift.datasets import fetch_criteo

Usage:
  python incrementality.py --data criteo-research-uplift-v2.1.csv.gz --out results
  python incrementality.py --data ... --sample-frac 0.1      # quick run on 10% of users
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import train_test_split

FEATURES = [f"f{i}" for i in range(12)]
Z = stats.norm.ppf(0.975)


# --------------------------------------------------------------------------- 1. trust
def randomization_checks(df: pd.DataFrame, expected_share: float, seed: int) -> dict:
    n, n_t = len(df), int(df["treatment"].sum())
    chi2, p_srm = stats.chisquare([n_t, n - n_t], [n * expected_share, n * (1 - expected_share)])
    t, c = df[df["treatment"] == 1], df[df["treatment"] == 0]
    feats = {}
    for f in FEATURES:
        mt, mc, vt, vc = t[f].mean(), c[f].mean(), t[f].var(), c[f].var()
        z = (mt - mc) / np.sqrt(vt / len(t) + vc / len(c))          # Welch z for the mean difference
        feats[f] = {"smd": float((mt - mc) / np.sqrt((vt + vc) / 2)), "z": float(z),
                    "p_bonferroni": float(min(1.0, 12 * 2 * stats.norm.sf(abs(z))))}
    # can the features predict who was treated? Under valid randomization AUC is 0.5 up to noise.
    sub = df.sample(n=min(len(df), 1_000_000), random_state=seed)
    X, d = sub[FEATURES].to_numpy(), sub["treatment"].to_numpy()
    fold = np.random.default_rng(seed).integers(0, 2, len(d))
    p = np.empty(len(d))
    for k in (0, 1):
        m = HistGradientBoostingClassifier(max_iter=200, random_state=seed).fit(X[fold != k], d[fold != k])
        p[fold == k] = m.predict_proba(X[fold == k])[:, 1]
    from sklearn.metrics import roc_auc_score
    auc = roc_auc_score(d, p)
    se_auc = np.sqrt((1 / d.sum() + 1 / (1 - d).sum()) / 12)       # null SE of AUC (Mann-Whitney)
    return {"n_users": n, "treated_share": n_t / n, "expected_share": expected_share,
            "srm_p_value": float(p_srm), "features": feats,
            "max_abs_smd": max(abs(v["smd"]) for v in feats.values()),
            "max_abs_z": max(abs(v["z"]) for v in feats.values()),
            "n_imbalanced_bonferroni": sum(v["p_bonferroni"] < 0.05 for v in feats.values()),
            "treatment_auc": float(auc), "treatment_auc_null_se": float(se_auc),
            "control_exposure_rate": float(c["exposure"].mean()),
            "treated_exposure_rate": float(t["exposure"].mean())}


# --------------------------------------------------------------------------- 2. lift
def diff_in_means(y: np.ndarray, d: np.ndarray) -> dict:
    yt, yc = y[d == 1], y[d == 0]
    mt, mc = yt.mean(), yc.mean()
    vt, vc = yt.var(ddof=1) / len(yt), yc.var(ddof=1) / len(yc)
    diff, se = mt - mc, np.sqrt(vt + vc)
    # relative lift via the delta method on log(mt / mc)
    se_log = np.sqrt(vt / mt ** 2 + vc / mc ** 2) if mt > 0 and mc > 0 else np.nan
    rel = mt / mc - 1 if mc > 0 else np.nan
    return {"rate_treated": float(mt), "rate_control": float(mc), "ate": float(diff), "se": float(se),
            "ci": [float(diff - Z * se), float(diff + Z * se)],
            "p_value": float(2 * stats.norm.sf(abs(diff / se))),
            "relative_lift": float(rel),
            "relative_ci": [float(np.exp(np.log1p(rel) - Z * se_log) - 1), float(np.exp(np.log1p(rel) + Z * se_log) - 1)]}


def complier_effect(y, d, exposure) -> dict:
    """Effect on users who were actually shown an ad: ITT / (exposure rate in treated - in control)."""
    itt = diff_in_means(y, d)
    first = exposure[d == 1].mean() - exposure[d == 0].mean()
    return {"exposure_gap": float(first), "effect_on_exposed": float(itt["ate"] / first),
            "ci": [float(itt["ci"][0] / first), float(itt["ci"][1] / first)]}


# --------------------------------------------------------------------------- 3. faster tests
def adjusted_ate(y, d, cov) -> dict:
    """CUPED: y_adj = y - theta * (cov - mean(cov)); cov must not depend on treatment."""
    cov = cov - cov.mean()
    theta = np.cov(y, cov)[0, 1] / cov.var(ddof=1)
    return diff_in_means(y - theta * cov, d)


def variance_reduction(y, d, X, seed: int) -> dict:
    raw = diff_in_means(y, d)
    # CUPED with raw pre-treatment features (linear prediction, fit on both arms, treatment excluded)
    lin = LinearRegression().fit(X, y).predict(X)
    cuped = adjusted_ate(y, d, lin)
    # CUPAC: gradient-boosted prediction, cross-fitted so no user's covariate uses its own outcome
    folds = np.random.default_rng(seed).integers(0, 2, len(y))
    pred = np.empty(len(y))
    for k in (0, 1):
        m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.1, random_state=seed)
        m.fit(X[folds != k], y[folds != k])
        pred[folds == k] = m.predict(X[folds == k])
    cupac = adjusted_ate(y, d, pred)
    out = {"raw": raw}
    for name, r in (("cuped_linear", cuped), ("cupac_gbm", cupac)):
        vr = 1 - (r["se"] / raw["se"]) ** 2
        shift = (r["ate"] - raw["ate"]) / raw["se"]
        out[name] = {**r, "variance_reduction": float(vr), "shift_in_raw_se": float(shift),
                     "valid": bool(abs(shift) < 2),
                     "ci_width_reduction": float(1 - r["se"] / raw["se"]),
                     "equivalent_sample_saving": float(vr)}
    return out


# --------------------------------------------------------------------------- 4. targeting
def fit_scores(train: pd.DataFrame, test: pd.DataFrame, outcome: str, seed: int) -> dict[str, np.ndarray]:
    Xtr, Xte = train[FEATURES].to_numpy(), test[FEATURES].to_numpy()
    y, d = train[outcome].to_numpy(), train["treatment"].to_numpy()
    clf = lambda: HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, random_state=seed)
    scores = {}
    # baseline: target the users most likely to convert when shown an ad
    resp = clf().fit(Xtr[d == 1], y[d == 1])
    p1 = resp.predict_proba(Xte)[:, 1]
    scores["likely buyers (response model)"] = p1
    # T-learner: separate models for treated and control, uplift = difference
    ctrl = clf().fit(Xtr[d == 0], y[d == 0])
    scores["uplift: T-learner"] = p1 - ctrl.predict_proba(Xte)[:, 1]
    # transformed outcome: E[y * (d/e - (1-d)/(1-e)) | X] = uplift(X) under randomization
    e = d.mean()
    zt = y * (d / e - (1 - d) / (1 - e))
    reg = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, random_state=seed).fit(Xtr, zt)
    scores["uplift: transformed outcome"] = reg.predict(Xte)
    return scores


def qini_curve(score, y, d, w=None, grid=None):
    """Qini(k) = Y_T(k) - Y_C(k) * N_T(k) / N_C(k) over the top-k fraction ranked by score."""
    order = np.argsort(-score, kind="stable")
    y, d = y[order], d[order]
    w = np.ones(len(y)) if w is None else w[order]
    yt, nt = np.cumsum(w * y * d), np.cumsum(w * d)
    yc, nc = np.cumsum(w * y * (1 - d)), np.cumsum(w * (1 - d))
    grid = np.linspace(0.01, 1.0, 100) if grid is None else grid
    idx = np.clip(np.ceil(grid * len(y)).astype(int) - 1, 0, len(y) - 1)
    q = yt[idx] - yc[idx] * nt[idx] / np.maximum(nc[idx], 1e-12)
    return grid, q


def evaluate_targeting(scores, test, outcome, n_boot, seed, depths, baseline):
    y, d = test[outcome].to_numpy().astype(float), test["treatment"].to_numpy().astype(float)
    rng = np.random.default_rng(seed)
    grid = np.linspace(0.01, 1.0, 100)
    ix = [int(round(k * 100)) - 1 for k in depths]
    point, curves = {}, {}
    for name, s in scores.items():
        _, q = qini_curve(s, y, d, grid=grid)
        point[name] = {"area": float(np.trapezoid(q / q[-1] - grid, grid)), "share": q[ix] / q[-1]}
        curves[name] = q / q[-1]
    boot = {name: {"area": [], "share": []} for name in scores}
    for _ in range(n_boot):            # Poisson bootstrap over users; same weights for every strategy (paired)
        w = rng.poisson(1.0, len(y)).astype(float)
        qs = {name: qini_curve(s, y, d, w=w, grid=grid)[1] for name, s in scores.items()}
        if any(q[-1] <= 0 for q in qs.values()):
            continue
        for name, q in qs.items():
            boot[name]["area"].append(np.trapezoid(q / q[-1] - grid, grid))
            boot[name]["share"].append(q[ix] / q[-1])
    ci = lambda a: [float(np.percentile(a, 2.5)), float(np.percentile(a, 97.5))]
    res = {}
    for name in scores:
        A, S = np.array(boot[name]["area"]), np.array(boot[name]["share"])
        r = {"qini_area_above_random": point[name]["area"], "qini_area_ci": ci(A),
             "share_of_incremental": {f"top_{int(k * 100)}pct": {"estimate": float(point[name]["share"][j]),
                                                                 "ci": ci(S[:, j])} for j, k in enumerate(depths)}}
        if name != baseline:
            A0, S0 = np.array(boot[baseline]["area"]), np.array(boot[baseline]["share"])
            r["vs_baseline"] = {
                "qini_area_diff": float(point[name]["area"] - point[baseline]["area"]), "qini_area_diff_ci": ci(A - A0),
                "share_diff": {f"top_{int(k * 100)}pct": {
                    "estimate": float(point[name]["share"][j] - point[baseline]["share"][j]),
                    "ci": ci(S[:, j] - S0[:, j])} for j, k in enumerate(depths)}}
        res[name] = r
    return res, grid, curves


def budget_table(scores, test, outcome, depths, cpm, population):
    """Per 1M users in the population: incremental outcomes and cost per incremental outcome when
    targeting the top-k fraction. Cost assumes one impression per targeted user at `cpm` (an assumption)."""
    y, d = test[outcome].to_numpy().astype(float), test["treatment"].to_numpy().astype(float)
    rows = []
    for name, s in scores.items():
        grid, q = qini_curve(s, y, d, grid=np.array(depths))
        for k, qk in zip(grid, q):
            inc_per_m = qk / len(y) * population
            cost = k * population * cpm / 1000
            rows.append({"strategy": name, "target_top": f"{int(k * 100)}%",
                         f"incremental_{outcome}s_per_1M_users": round(inc_per_m, 1),
                         f"cost_per_incremental_{outcome}_usd": round(cost / inc_per_m, 2) if inc_per_m > 0 else None})
    return pd.DataFrame(rows)


def plot_qini(grid, curves, path, outcome):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = ["#eb6834", "#2a78d6", "#1baf7a"]       # validated categorical slots (fixed order)
    fig, ax = plt.subplots(figsize=(7, 4.5), dpi=150)
    fig.patch.set_facecolor("#fcfcfb"); ax.set_facecolor("#fcfcfb")
    ax.plot([0, 1], [0, 1], color="#9a9a94", lw=1.5, ls="--", label="random targeting")
    for (name, c), col in zip(curves.items(), colors):
        ax.plot(np.r_[0, grid], np.r_[0, c], color=col, lw=2, label=name)
    ax.set_xlabel("share of users targeted (ranked by score)", color="#55554f")
    ax.set_ylabel(f"share of all incremental {outcome}s captured", color="#55554f")
    ax.set_title("Who to target: uplift vs. likely buyers (Qini, test set)", loc="left", color="#1f1f1d")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(color="#e6e5df", lw=0.8); ax.tick_params(colors="#55554f")
    ax.legend(frameon=False, loc="lower right")
    fig.tight_layout(); fig.savefig(path); plt.close(fig)


# --------------------------------------------------------------------------- report
def fmt_pct(x, d=1):
    return f"{100 * x:+.{d}f}%"


def write_report(R, out: Path, outcome: str):
    rc, lift, vr, tg = R["randomization"], R["lift"], R["variance_reduction"], R["targeting"]
    L = [f"# Ad incrementality: Criteo Uplift ({rc['n_users']:,} users)", "",
         "## 1. Is the experiment trustworthy?",
         f"- Treated share {rc['treated_share']:.4f} vs. design {rc['expected_share']}: SRM p = {rc['srm_p_value']:.3g}",
         f"- Feature balance: largest |SMD| {rc['max_abs_smd']:.4f}, largest |z| {rc['max_abs_z']:.1f}; "
         f"{rc['n_imbalanced_bonferroni']} of 12 features differ between arms at Bonferroni p < 0.05",
         f"- Predicting treatment from the 12 features: cross-fitted AUC {rc['treatment_auc']:.4f} "
         f"(0.5 expected under clean randomization; null SE {rc['treatment_auc_null_se']:.4f})",
         f"- Exposure rate: treated {rc['treated_exposure_rate']:.4f}, control {rc['control_exposure_rate']:.4f}", "",
         "## 2. How much do ads lift outcomes? (intent-to-treat)", "",
         "| outcome | treated | control | relative lift [95% CI] | absolute lift [95% CI] | p |", "|---|---|---|---|---|---|"]
    for o, r in lift.items():
        L.append(f"| {o} | {r['itt']['rate_treated']:.5f} | {r['itt']['rate_control']:.5f} | "
                 f"{fmt_pct(r['itt']['relative_lift'])} [{fmt_pct(r['itt']['relative_ci'][0])}, {fmt_pct(r['itt']['relative_ci'][1])}] | "
                 f"{r['itt']['ate']:+.5f} [{r['itt']['ci'][0]:+.5f}, {r['itt']['ci'][1]:+.5f}] | {r['itt']['p_value']:.2g} |")
    L += ["", "Effect on users actually shown an ad (ITT / exposure gap):", ""]
    for o, r in lift.items():
        c = r["exposed"]
        L.append(f"- {o}: {c['effect_on_exposed']:+.5f} per exposed user [{c['ci'][0]:+.5f}, {c['ci'][1]:+.5f}]")
    L += ["", f"## 3. Could the same answer come from a shorter test? ({outcome})", "",
          "| estimator | lift [95% CI] | CI width vs. raw | variance reduction | estimate shift vs. raw (raw SEs) | valid? |",
          "|---|---|---|---|---|---|"]
    raw = vr["raw"]
    L.append(f"| difference in means | {raw['ate']:+.6f} [{raw['ci'][0]:+.6f}, {raw['ci'][1]:+.6f}] | - | - | - | - |")
    for name, label in (("cuped_linear", "CUPED, linear on 12 features"), ("cupac_gbm", "CUPAC, cross-fitted GBM")):
        r = vr[name]
        L.append(f"| {label} | {r['ate']:+.6f} [{r['ci'][0]:+.6f}, {r['ci'][1]:+.6f}] | "
                 f"{fmt_pct(-r['ci_width_reduction'])} | {100 * r['variance_reduction']:.1f}% | "
                 f"{r['shift_in_raw_se']:+.1f} | {'yes' if r['valid'] else 'NO: estimate moved; covariates not balanced'} |")
    L += ["", "Under valid randomization, adjustment narrows the CI without moving the estimate. A shift beyond "
          "2 raw SEs means the features differ between arms, so the variance reduction cannot be claimed as a sample saving."]
    L += ["", f"## 4. Who should be targeted? ({outcome}, held-out test set)", "",
          "| strategy | Qini area above random [95% CI] | " + " | ".join(
              f"share of incremental, top {k}" for k in tg["depths_pct"]) + " |",
          "|---|---|" + "---|" * len(tg["depths_pct"])]
    for name, r in tg["models"].items():
        cells = [f"{100 * v['estimate']:.0f}% [{100 * v['ci'][0]:.0f}, {100 * v['ci'][1]:.0f}]"
                 for v in r["share_of_incremental"].values()]
        L.append(f"| {name} | {r['qini_area_above_random']:.3f} [{r['qini_area_ci'][0]:.3f}, {r['qini_area_ci'][1]:.3f}] | "
                 + " | ".join(cells) + " |")
    L += ["", "Paired bootstrap difference vs. likely-buyer targeting (same resamples for every strategy):", ""]
    for name, r in tg["models"].items():
        if "vs_baseline" in r:
            v = r["vs_baseline"]
            cells = ", ".join(f"top {k[4:-3]}%: {100 * x['estimate']:+.0f} pts [{100 * x['ci'][0]:+.0f}, {100 * x['ci'][1]:+.0f}]"
                              for k, x in v["share_diff"].items())
            L.append(f"- {name}: Qini area {v['qini_area_diff']:+.3f} [{v['qini_area_diff_ci'][0]:+.3f}, "
                     f"{v['qini_area_diff_ci'][1]:+.3f}]; share captured {cells}")
    L += ["", "![Qini curves](qini.png)", "", "## 5. Budget decision", "",
          f"Cost assumes ${R['cpm']:.2f} CPM and one impression per targeted user (an assumption, not data).", "",
          R["budget_table_md"], ""]
    (out / "report.md").write_text("\n".join(L))


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="results")
    ap.add_argument("--outcome", default="conversion", choices=["conversion", "visit"])
    ap.add_argument("--sample-frac", type=float, default=1.0)
    ap.add_argument("--expected-treat-share", type=float, default=0.85)
    ap.add_argument("--test-size", type=float, default=0.3)
    ap.add_argument("--n-boot", type=int, default=200)
    ap.add_argument("--cpm", type=float, default=5.0)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    df = pd.read_csv(a.data, dtype={**{f: np.float32 for f in FEATURES}, "treatment": np.int8,
                                     "conversion": np.int8, "visit": np.int8, "exposure": np.int8})
    if a.sample_frac < 1:
        df = df.sample(frac=a.sample_frac, random_state=a.seed)
    df = df.reset_index(drop=True)
    for c in ("treatment", "conversion", "visit", "exposure"):
        df[c] = df[c].astype(np.int8)
    print(f"loaded {len(df):,} rows in {time.time() - t0:.0f}s")

    R = {"cpm": a.cpm, "outcome": a.outcome}
    R["randomization"] = randomization_checks(df, a.expected_treat_share, a.seed)
    d = df["treatment"].to_numpy()
    R["lift"] = {o: {"itt": diff_in_means(df[o].to_numpy().astype(float), d),
                     "exposed": complier_effect(df[o].to_numpy().astype(float), d, df["exposure"].to_numpy())}
                 for o in ("visit", "conversion")}
    print("lift done", f"{time.time() - t0:.0f}s")
    R["variance_reduction"] = variance_reduction(df[a.outcome].to_numpy().astype(float), d,
                                                 df[FEATURES].to_numpy(), a.seed)
    print("variance reduction done", f"{time.time() - t0:.0f}s")

    strat = df["treatment"].astype(str) + "_" + df[a.outcome].astype(str)
    train, test = train_test_split(df, test_size=a.test_size, stratify=strat, random_state=a.seed)
    scores = fit_scores(train, test, a.outcome, a.seed)
    depths = [0.1, 0.2, 0.3, 0.5]
    models, grid, curves = evaluate_targeting(scores, test, a.outcome, a.n_boot, a.seed, depths,
                                              baseline="likely buyers (response model)")
    R["targeting"] = {"depths_pct": [f"{int(k * 100)}%" for k in depths], "models": models,
                      "n_test": len(test)}
    print("targeting done", f"{time.time() - t0:.0f}s")
    bt = budget_table(scores, test, a.outcome, [0.1, 0.2, 0.3, 0.5, 1.0], a.cpm, 1_000_000)
    R["budget_table_md"] = bt.to_markdown(index=False)
    bt.to_csv(out / "budget_table.csv", index=False)
    plot_qini(grid, curves, out / "qini.png", a.outcome)
    (out / "results.json").write_text(json.dumps({k: v for k, v in R.items() if k != "budget_table_md"}, indent=2))
    write_report(R, out, a.outcome)
    print((out / "report.md").read_text())
    print(f"total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
