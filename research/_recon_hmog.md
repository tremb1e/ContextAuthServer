# Recon: HMOG methodology reference (for research/ parity)

Read-only study of `/data/paper/sp/hmog_exp` to extract mirror-able idioms for the
ContextAuth `research/` layer. **The reference code is real, dense, and directly
reusable** (not sparse). Two parallel families exist:

1. **Central pipeline** — `260630/hmog_exp/hmog_protocol/hmog_protocol/pipeline.py`
   (3613 lines): the authoritative source for EER, bootstrap CI, Holm, matched-impostor
   splits, threshold selection, paired stats.
2. **Per-model `evaluation/` packages** — e.g.
   `hmog_1dcnn_exp/src/hmog_cnn/evaluation/{sample_metrics,bootstrap}.py` and
   `.../scripts/31_aggregate_final.py`: a cleaner, sklearn/scipy-based restatement of the
   same methodology (this is the easiest to mirror).

Newest copy = `260630/`; `260619/` and `backup/` are older snapshots of the same tree.
`hmog_exp_old/.venv/...` is just installed site-packages — ignore.

Line refs below point at the `260630` tree.

---

## 1. EER computation — TWO reference implementations (both correct)

### 1a. Vectorised sweep (pipeline.py:1979 `threshold_at_eer`)
The production one. Score = larger-is-genuine. Sweeps unique score thresholds via
`searchsorted` (O(n log n)), computes FAR/FRR at each, picks the crossing:

```python
def threshold_at_eer(y_true, scores):          # returns (thr, eer, far@eer, frr@eer)
    y = y_true.astype(int); s = scores.astype(float)
    if len(np.unique(y)) < 2 or len(s) == 0:
        return (np.median(s) if len(s) else 0.0), nan, nan, nan
    thr = np.unique(s)
    if thr.size > 5000:                        # cap for tens-of-millions of rows
        thr = np.quantile(thr, np.linspace(0, 1, 5000))
    thr = np.r_[thr[0]-1e-6, thr, thr[-1]+1e-6]
    imp = np.sort(s[y == 0]); gen = np.sort(s[y == 1])
    far = (imp.size - np.searchsorted(imp, thr, side="left")) / imp.size   # P(imp >= thr)
    frr =              np.searchsorted(gen, thr, side="left")  / gen.size   # P(gen  < thr)
    i = int(np.nanargmin(np.abs(far - frr)))   # ROC crossing FAR==FRR
    return float(thr[i]), float((far[i]+frr[i])/2), float(far[i]), float(frr[i])
```
Key formulas: `FAR(t)=frac(impostor scores >= t)`, `FRR(t)=frac(genuine scores < t)`,
`EER=(FAR+FRR)/2` at `argmin|FAR-FRR|`. Convention: **label 1 = genuine, 0 = impostor**.

### 1b. ROC-interpolation + brentq (sample_metrics.py:12 `compute_eer_threshold`)
Cleaner, uses sklearn `roc_curve` + scipy `brentq` root-find where `fnr(x)=x`:

```python
fpr, tpr, thr = roc_curve(labels, scores); fnr = 1 - tpr
f   = interp1d(fpr, fnr, bounds_error=False, fill_value=(fnr[0], fnr[-1]))
eer = brentq(lambda x: f(x) - x, 0., 1.)                 # crossing FPR==FNR
eer_thr = float(interp1d(fpr, thr, ...)(eer))
# fallback on ValueError/RuntimeError: i = argmin|fpr-fnr|; eer=(fpr[i]+fnr[i])/2
```
Guards `len(unique(labels))<2 -> nan`. Same file also has `far_frr_at_threshold`,
`compute_eer_auc_from_scores` (returns both **per-user mean** and **global** EER/AUC/HTER;
HTER = `0.5*(FAR+FRR)` at each user's EER threshold).

**Mirror recommendation:** copy 1b (sample_metrics.py) verbatim into research/ — it is
self-contained (numpy+scipy+sklearn) and returns the per-user/global split you need.

## 2. By-user bootstrap 95% CI — resample UNIT = user

There is **no fancy cluster bootstrap**; the trick is the *input* is already one EER value
per user, so resampling those values with replacement IS a by-user bootstrap.

Two identical implementations:
- pipeline.py:2298 `bootstrap_ci(x, n_boot=1000, seed=0) -> (lo, hi)` using
  `rng.choice(x, size=len(x), replace=True)` then `np.percentile(means, [2.5, 97.5])`.
- bootstrap.py:9 `bootstrap_ci(values, n_iters=1000, alpha=0.05, seed=20260520)
  -> (mean, lo, hi)` using `rng.integers`; quantiles `alpha/2, 1-alpha/2`.

```python
def bootstrap_ci(x, n_boot=1000, seed=0):
    x = np.asarray(x, float); x = x[np.isfinite(x)]
    if len(x) == 0: return nan, nan
    rng = np.random.default_rng(seed)
    means = [rng.choice(x, size=len(x), replace=True).mean() for _ in range(n_boot)]
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))
```
Callers **always pass per-user vectors**: `aggregate()` in 31_aggregate_final.py:158 does
`groupby(paradigm,ctx,variant)` then `bootstrap_ci(sub["eer_test"].dropna().values)` — one
row per user in the group. Percentile (not BCa) CI. Deterministic seed derived per
comparison via `stable_int_seed(...)` for reproducibility.

## 3. Holm-Bonferroni — TWO implementations

### 3a. Hand-rolled (pipeline.py:2326 `holm_correction`)
Step-down, NaN-safe, preserves input order, enforces monotone non-decreasing adjusted p:
```python
order = finite_idx[np.argsort(p[finite_idx])]; m = len(order); prev = 0.0
for rank, idx in enumerate(order):
    adj = min(1.0, (m - rank) * p[idx])        # multiplier m, m-1, ..., 1
    prev = max(prev, adj); adjusted[rank] = prev
```
### 3b. statsmodels (31_aggregate_final.py:141)
`rej, p_corr, _, _ = multipletests(pvals, method="holm")` — use this when statsmodels is
available.

## 4. Paired significance testing structure (pipeline.py:2237 `paired_stats`; 31_aggregate_final.py:94 `run_stat_tests`)

The canonical "compare two configs" recipe the research/ layer should mirror:
1. **Pair by user (× context)**: build a per-user pivot / merge on
   `[target_user, context, window, model]`; `diff = EER_a - EER_b` over matched pairs.
2. Per comparison compute: `delta_mean`, `bootstrap_ci(diff)`, **Wilcoxon signed-rank**
   (`scipy.stats.wilcoxon(diff, alternative=..., zero_method="wilcox")`), a **paired
   sign-flip permutation p** (pipeline.py:2311, `n_perm=10000`,
   `p=(count+1)/(n_perm+1)`), Cohen's d (`mean/std_ddof1`), win-rate `mean(diff>0)`.
3. If >50% ties, fall back to **sign test** `binomtest(min(wins,losses), wins+losses, .5)`.
4. Collect all comparison p-values into ONE array, apply Holm across the family, store
   `holm_p_*` / `reject_holm_0.05`.

## 5. Matched-impostor sampling (pipeline.py:788 `make_segments`)

Per target user:
- `non_targets = users != target`; deterministic order via
  `rng = default_rng(stable_int_seed(target, seed)); rng.permutation(non_targets)`.
- **Disjoint impostor pools**: first 24 -> validation, next 24 -> test, rest -> train
  (`>=48` non-targets; graceful thirds split when fewer). val/test/train impostor users
  never overlap (asserted in `write_split_sanity_checks`, pipeline.py:943).
- Genuine sessions fixed by index: sessions `[0,1]`=train, `[2]`=val, `[3]`=test.
- Impostor segment = a **seeded random quarter** of the session's longest-valid interval:
  `seg_len=dur/4; offset=rng.uniform(0, 0.75*dur)` with
  `seed=stable_int_seed(SEED_IMPOSTOR_SEGMENT, target, source, role, context, session)`.
- Sanity: target never in own impostor pool; one-class training never sees impostors.

## 6. Split families (session / context) — pipeline.py

No LOSO/day/app-out helper functions exist by those names. The realized protocol is:
- **Leave-session-out by fixed index** (train sessions 0-1, val 2, test 3) inside
  `make_segments`.
- **Leave-user-out for tuning**: `select_tuning_users` (pipeline.py:737) holds out N=5
  users; group label `heldout` vs tuning drives `paired_stats` (filters
  `target_user_group=="heldout"`). Analog for "app/scenario" = the `CONTEXTS` / scenario
  dimension carried through every groupby (`context_config` in per-model code).
- **Leave-one-out + bootstrap stability of a selection** (window length):
  `grid_selection_stability` (pipeline.py:2820) — for each tuning user drop them and
  re-select; plus 1000 bootstrap resamples of tuning users, tally chosen window. This is
  the pattern to reuse if research/ needs "is the chosen config stable across users".

## 7. Publication matplotlib style block (scripts/plot_publication_figures.py:34)

**Copy this rcParams block verbatim** — it satisfies every stated requirement (Times New
Roman, STIX mathtext, large fonts, no titles, 300 dpi, tight bbox):
```python
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["Times New Roman", "Nimbus Roman", "Liberation Serif", "DejaVu Serif"],
    "font.size": 16, "axes.labelsize": 18,
    "xtick.labelsize": 14, "ytick.labelsize": 14, "legend.fontsize": 13,
    "mathtext.fontset": "stix", "axes.linewidth": 1.2,
    "figure.dpi": 300, "savefig.dpi": 300, "savefig.bbox": "tight",
})
def save(fig, name, FIG):                 # ALWAYS emit BOTH pdf + png
    for ext in ("pdf", "png"): fig.savefig(FIG / f"{name}.{ext}")
    plt.close(fig)
```
Conventions to mirror: **no `ax.set_title`** on publication figs; all in-figure text
English; read CSV with stdlib `csv`/pandas, **no seaborn**; skip-with-message when a CSV is
missing/empty; keep a stable `DISPLAY`/model-ordering dict.
(The simpler variant hmog_protocol/plot_publication_figures.py:23 uses
`"font.family": "Times New Roman"` directly and the same keys.)

## 8. Pareto / EER-vs-cost plot (hmog_protocol/plot_publication_figures.py:226 `fig_pareto`)

Minimal but present. Mirror-able sketch:
```python
fig, ax = plt.subplots(figsize=(6, 4))
ax.scatter(cost_xs, eer_ys, color="#F58518", alpha=0.7)   # x=train_time proxy (or #params)
ax.set_xlabel("Train time proxy (s)"); ax.set_ylabel("EER"); ax.grid(alpha=0.25)
save(fig, "eer_latency_pareto")
```
Cost proxy = mean `train_time_sec` per (model, system) from a run manifest. For a true
Pareto frontier, additionally sort by cost and keep points with strictly-decreasing EER.
Per-context EER **boxplots** (fig_eer_boxplot_by_context, figures.py:18) use
`ax.boxplot(data, labels=order, showmeans=True)` over per-user EER arrays.

---

## Mirror checklist for research/ (parity, minimal risk)
1. Copy `sample_metrics.py` (EER §1b, HTER, per-user+global) verbatim.
2. Copy `bootstrap_ci` (§2) — feed it **per-user EER vectors** (one value/user/cell).
3. Copy `holm_correction` (§3a) or use `multipletests(method="holm")`; apply once per
   comparison family.
4. For each RQ: pivot per-user EER, `diff=a-b`, Wilcoxon (+sign-test fallback on ties),
   bootstrap CI on diff, Cohen's d, win-rate; then Holm across the family (§4).
5. Reuse `stable_int_seed(...)`-style deterministic seeding for every bootstrap/permutation.
6. Drop in the rcParams block (§7) + always save pdf+png, no titles, English labels.
7. Matched impostors: disjoint val/test/train impostor user pools, target excluded, seeded
   quarter-segment sampling (§5).

## Verbatim source paths
- EER (vectorised): `.../hmog_protocol/hmog_protocol/pipeline.py:1979`
- EER (roc+brentq) + HTER: `.../hmog_1dcnn_exp/src/hmog_cnn/evaluation/sample_metrics.py:12,43`
- bootstrap CI: `pipeline.py:2298`, `.../hmog_cnn/evaluation/bootstrap.py:9`
- Holm: `pipeline.py:2326`; statsmodels: `.../hmog_1dcnn_exp/scripts/31_aggregate_final.py:141`
- Permutation p: `pipeline.py:2311`
- Paired stats family: `pipeline.py:2237`; `.../31_aggregate_final.py:94`
- Matched impostor / splits: `pipeline.py:788` (+ sanity `:943`), tuning holdout `:737`,
  selection stability `:2820`
- Pub style + save: `.../hmog_exp/scripts/plot_publication_figures.py:34,73`
- Pareto: `.../hmog_protocol/hmog_protocol/plot_publication_figures.py:226`
- Per-context boxplot: `.../hmog_1dcnn_exp/src/hmog_cnn/reports/figures.py:18`
