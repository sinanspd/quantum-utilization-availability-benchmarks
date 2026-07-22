# Calibration drift and load-analysis definitions

## Scope and estimand

The collected IBM backend properties are repeated reports of the most recent calibration
result. They are not continuous sensor readings. A row is therefore treated as a calibration
observation only when its `property_date` advances. Repeated polls carrying the same
`property_date` are deduplicated before any metric is calculated.

The available load variable is `pending_jobs`. It measures backend queue pressure at a poll,
not executed QPU time. The dataset contains no circuit-to-physical-qubit mapping, shots,
execution duration, or job history, so it cannot identify physical load for an individual
qubit or edge. Component-level results mean "calibration behavior of this component versus
backend queue pressure," not "calibration behavior versus this component's utilization."

## 1. Property drift

Let `x[i,p,k]` be the value of property `p` for component `i` at its `k`th distinct
calibration timestamp. Properties are identified separately by backend, component type,
property name, gate direction where applicable, and unit.

A qubit's state includes both native qubit properties (`T1`, `T2`, readout error, and so on)
and parameters of one-qubit gates acting on it (`sx`, `x`, `id`, and similar gates). An edge's
state contains parameters of two-qubit gates on that edge.

For two consecutive values, use

```text
g(x) = log(x)       when both consecutive values are positive
       x            otherwise

delta[i,p,k] = g(x[i,p,k]) - g(x[i,p,k-1])
```

The log transform makes a multiplicative increase and reciprocal decrease comparable. For
interpretability, the script also reports absolute change and the bounded symmetric relative
change

```text
relative_change = 2 * |x[k] - x[k-1]| / (|x[k]| + |x[k-1]|)
```

which lies in `[0, 2]` when the denominator is nonzero.

To combine heterogeneous properties, calculate a robust scale for every backend,
component-type, and property family (property name plus unit; gate name is also included for
edges). Directed edge observations remain separate time series, while their scale is pooled
across physical edges so edge scores remain comparable:

```text
s[b,t,p] = 1.4826 * median(|delta - median(delta)|)
```

with IQR, sample-standard-deviation, and numerical-floor fallbacks when the MAD is zero.
The property drift score is

```text
d[i,p,k] = |delta[i,p,k]| / s[b,t,p]
```

This is dimensionless. It says how large the observed move is relative to that property's
historical increment scale; it is not a physical error rate. The signed score, absolute and
symmetric-relative changes, elapsed time, and per-hour rates are retained so the paper need
not rely on one summary alone.

## 2. Component calibration event and aggregate drift

IBM may timestamp different properties of one calibration a few minutes apart. Property
updates for the same component are grouped into one event when they fall within the configured
anchor-based tolerance (15 minutes by default). The anchor does not move, which prevents a
chain of small gaps from merging an arbitrarily long interval.

If event `e` has valid property drift scores `P(e)`, its primary aggregate drift is

```text
D[i,e] = sqrt(mean(d[i,p,e]^2 for p in P(e)))
```

RMS preserves the influence of a large movement without making the maximum the sole result.
Mean, median, maximum, signed mean, and symmetric-relative summaries are also emitted.

For the draft's backend calibration vector, let each coordinate be a distinct component and
property series. Between grid times, signed standardized increments for multiple updates to
the same coordinate are summed. Unchanged coordinates contribute zero. The pipeline reports

```text
D_l2[b,t]  = sqrt(sum_j delta_z[b,j,t]^2)
D_rms[b,t] = sqrt(sum_j delta_z[b,j,t]^2 / J_b)
```

where `J_b` is the full property-series count. It also reports median, p90, and maximum drift
among changed coordinates, the fraction of coordinates changed, and counts of components and
properties that changed. This is the scale-safe implementation of the draft's
`||C[b,t] - C[b,t-1]||`; taking a Euclidean norm of raw `T1`, error-probability, frequency, and
gate-duration values would otherwise be unit-dependent.

"Degrading" is only assigned where quality direction is defensible: increasing error or
infidelity is adverse, while decreasing `T1`, `T2`, or fidelity is adverse. Frequency and
other parameters with no universal good direction remain unclassified rather than being
forced into the degrading count.

## 3. Calibration frequency over multiple windows

For component `i`, grid time `t`, and window `w` in `{1, 2, 8, 24, 48}` hours:

```text
N[i,t,w] = number of component calibration events in (t - w, t]
F[i,t,w] = N[i,t,w] / observed exposure hours
```

The script uses an hourly evaluation grid by default. `window_complete` is false until the
dataset contains the full preceding window; incomplete windows are retained for audit but
excluded from correlation calculations.

The long-run cadence table also reports events/day and the mean, median, standard deviation,
CV, minimum, maximum, and burstiness of inter-calibration intervals. Burstiness is

```text
B = (sigma - mu) / (sigma + mu)
```

with `-1` approaching perfectly regular timing and `+1` strongly bursty timing.

## 4. Calibration concentration

Within each backend, component type, time, and window, let `n_i` be component event counts and
`p_i = n_i / sum(n_i)`. The script reports:

- coverage: fraction of components with at least one event;
- HHI: `sum(p_i^2)`;
- normalized HHI: `(HHI - 1/N) / (1 - 1/N)`;
- normalized Shannon entropy and its effective component count `exp(H)`;
- top-component and top-decile event shares;
- Gini coefficient and coefficient of variation of component event counts.

No-event windows have zero coverage and undefined share-based concentration; they are not
silently treated as perfectly equal allocation.

## 5. Discrepancy between component calibration times

At evaluation time `t`, define calibration age

```text
A[i,t] = t - most recent known calibration event for component i at or before t.
```

Across components, the script reports mean, median, standard deviation, MAD, IQR, p90-p10,
range, Gini, fractions older than every requested window, and

```text
mean_pairwise_gap = 2 / (N * (N - 1)) * sum over i<j of |A[i,t] - A[j,t]|.
```

This pairwise metric directly answers how far apart the individual component calibration
clocks are. `known_last_calibration_fraction` exposes left-censoring rather than imputing an
unknown calibration time.

Backend-wide synchrony episodes additionally group near-simultaneous component events and
report the fraction of qubits or edges covered in each episode.

The draft's property-level staleness is emitted separately. For every property coordinate,
`A[j,t] = t - property_date[j,t]`; each backend/type/time row contains median, p90, maximum,
known-age coverage, and fractions older than 1, 6, and all requested analysis windows.

For the draft's volatility analysis, `component_property_summary.csv.gz` reports
`std(x[i,p,t])`, mean, median, MAD, CV, range, and drift summaries. Spearman and Pearson
associations between component calibration frequency and median property value/volatility
are in `frequency_property_correlations.csv.gz`, covering readout error, one-qubit error,
`T1`, two-qubit error, and every other observed property family.

## 6. Load association metrics

For each event and grid time, queue pressure is represented by the last non-stale
`pending_jobs` observation and the mean, maximum, standard deviation, and sample count over
the strictly preceding 1, 2, 8, 24, and 48 hours. Strictly preceding windows avoid using a
future queue observation to explain a calibration.

The outputs contain Pearson and Spearman associations for:

- event drift versus current and preceding-window queue pressure;
- each component's rolling calibration frequency versus the matching queue window;
- backend-level interval event counts, calibrated-component counts, and drift versus queue
  pressure at lags from -48 to +48 hours.

For lagged results, a positive lag means queue pressure precedes the calibration outcome.
Benjamini-Hochberg q-values are included.

The proposed panel model is also estimated directly:

```text
D[b,t] = alpha[b] + gamma[t] + beta * Q[b,t-1] + u[b,t]
```

Backend and common time effects are removed with alternating projections, which also handles
an unbalanced panel. `fixed_effect_regressions.csv.gz` reports beta for L2 drift, all-property
RMS drift, changed-component count, and changed-property fraction against current and
preceding-window queue pressure. It includes within R-squared and backend-clustered and
two-way backend/time-clustered standard errors. The small number of backends makes cluster
inference fragile, so effect sizes and confidence intervals should be emphasized and a
block-bootstrap sensitivity analysis should accompany final paper claims.

The reported correlations and regressions remain observational: maintenance policy, device
identity, time trends, and scheduled calibration are potential confounders. None supports a
causal utilization-to-drift claim without direct execution telemetry or an intervention.

## 7. Additional validity checks

The outputs retain event property counts, event spans, queue-observation age, load sample
counts, study coverage, known-age coverage, and full-window flags. Analyses should be repeated
with alternative event tolerances, exclude operational outages, stratify by backend/property,
and distinguish scheduled device-wide calibration episodes from isolated component updates.
