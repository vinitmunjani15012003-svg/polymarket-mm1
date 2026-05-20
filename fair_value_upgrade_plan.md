# Fair Value Upgrade Plan

Date: 2026-04-30
Repo: `polymarket-mm`
Goal: materially improve fair value quality for 15-minute Polymarket crypto binaries without overcomplicating the stack.

---

## Executive summary
The biggest fair value gains will **not** come from swapping one elegant formula for another.
They will come from:

1. **better sigma inputs**
2. **market-implied probability blending**
3. **historical calibration**
4. **microstructure adjustments**
5. **clean separation between true fair value and quote center**

The target end-state is:

```text
p_final = calibrated(
  blend(
    p_model,
    p_book,
    p_flow
  )
)
```

Where:
- `p_model` = your internal model probability
- `p_book` = market-implied probability from Polymarket orderbook
- `p_flow` = short-horizon momentum / microstructure adjustment

---

# What we are doing

We are upgrading fair value from a mostly raw model into a **blended, calibrated probability engine**.

Current fair value is directionally decent, but still too dependent on:
- simplistic volatility assumptions
- single-model outputs
- insufficient market anchoring
- no calibration correction layer

That means it can look smart while still being mispriced in live conditions.

---

# Phase plan

## Phase 1 — Highest ROI

### 1) Upgrade sigma to regime-aware sigma
**Priority:** P0

### Problem
Current fair value is only as good as its volatility estimate.
Short binaries are extremely sensitive to short-term vol shifts.

### Goal
Use an effective sigma that adapts to market regime.

### Implement
Compute:
- `sigma_30s`
- `sigma_2m`
- `sigma_window`
- optional burst multiplier when recent returns spike

Suggested first-pass logic:

```python
sigma_eff = max(
    sigma_30s,
    0.85 * sigma_2m,
    0.75 * sigma_window,
    default_sigma
)
```

Optional burst adjustment:

```python
if recent_abs_return_zscore > threshold:
    sigma_eff *= 1.15
```

### Files
- `src/strategy/volatility.py`
- `src/orchestration/market_cycler.py`

### Acceptance criteria
- fair value uses `sigma_eff`, not a single raw sigma source
- log sigma components for debugging
- confirm sigma reacts faster during volatility spikes

---

### 2) Add market-implied probability anchor
**Priority:** P0

### Problem
Internal model alone can be stale relative to the live market.
The orderbook often contains useful information.

### Goal
Blend internal model with Polymarket-implied probability when the book is trustworthy.

### Implement
Derive `p_book` from top-of-book or book mid.

Suggested first-pass:

```python
p_book = implied_prob_from_orderbook(...)
```

Only trust `p_book` when:
- spread is not too wide
- book is fresh
- some minimum depth exists
- toxicity state is not extreme

Add a confidence score:

```python
book_confidence = f(spread, depth, freshness, toxicity)
```

### Files
- `src/data/orderbook.py`
- `src/orchestration/market_cycler.py`
- optionally new helper: `src/strategy/fair_value_blend.py`

### Acceptance criteria
- `p_book` computed and logged
- confidence-weighted use of orderbook signal
- fair value does not blindly follow a thin or stale book

---

### 3) Blend model and book, don’t replace one with the other
**Priority:** P0

### Goal
Create a stable blended fair value.

### Suggested formula

```python
p_raw = (
    model_confidence * p_model +
    book_confidence * p_book
) / max(1e-9, model_confidence + book_confidence)
```

Later:

```python
p_raw = (
    model_confidence * p_model +
    book_confidence * p_book +
    flow_confidence * p_flow
) / (model_confidence + book_confidence + flow_confidence)
```

### Files
- `src/orchestration/market_cycler.py`
- new helper recommended: `src/strategy/fair_value_blend.py`

### Acceptance criteria
- `p_model`, `p_book`, `p_raw` all visible in logs/debug state
- blend weights are explicit and configurable

---

## Phase 2 — Turn smart-looking into accurate

### 4) Add probability calibration layer
**Priority:** P1

### Problem
Even good raw models are often poorly calibrated.
Example:
- raw `0.62` may behave like `0.56`
- raw `0.80` may behave like `0.72`

### Goal
Correct raw probability outputs using historical outcomes.

### Implement
Log for every market:
- timestamp
- asset
- time remaining bucket
- `p_model`
- `p_book`
- `p_raw`
- final result (`UP` / `DOWN`)

Build either:
- isotonic regression, or
- simple bucket-based correction table

Example bucket approach:

```python
0.55-0.60 raw  -> 0.53 calibrated
0.60-0.65 raw  -> 0.57 calibrated
0.65-0.70 raw  -> 0.62 calibrated
```

### Files
- new data log path, e.g. `data/fair_value_training.jsonl`
- new helper: `src/strategy/calibration.py`
- `src/orchestration/market_cycler.py`

### Acceptance criteria
- every market logs prediction + outcome
- calibration can be turned on/off
- live fair value can use `p_calibrated`

---

### 5) Add microstructure adjustment term
**Priority:** P1

### Problem
Short binaries are heavily influenced by short-horizon momentum and microstructure.
Raw model + book blend still misses some live signal.

### Goal
Add a bounded correction term for short-term flow.

### Inputs
Use some combination of:
- 5s return z-score
- 15s return z-score
- orderbook imbalance
- spread widening
- fill toxicity state
- recent adverse selection

### Suggested first-pass

```python
p_flow_adj = (
    a * short_return_z +
    b * book_imbalance -
    c * toxicity_score
)
p_flow_adj = clip(p_flow_adj, -0.03, 0.03)
```

Then:

```python
p_final = clip(p_raw + p_flow_adj, 0.01, 0.99)
```

### Files
- `src/risk/toxicity.py`
- `src/orchestration/market_cycler.py`
- optional helper: `src/strategy/microstructure.py`

### Acceptance criteria
- flow adjustment is bounded and logged
- fair value responds slightly faster to real short-term changes
- no wild jumps from noisy inputs

---

## Phase 3 — Clean architecture and operations

### 6) Separate true fair value from quote center
**Priority:** P1

### Problem
Model truth and quoting behavior should not be the same variable.

### Goal
Maintain:
- `p_true` = best estimate of real probability
- `p_quote_center` = inventory/toxicity-adjusted center used for orders

### Why
This prevents execution logic from corrupting model quality.

### Suggested structure

```python
p_true = calibrated_blended_probability(...)
p_quote_center = apply_inventory_and_execution_offsets(p_true, ...)
```

### Files
- `src/orchestration/market_cycler.py`
- `src/strategy/quote_engine.py`

### Acceptance criteria
- logs show both `p_true` and `p_quote_center`
- inventory skew changes quote behavior, not model truth

---

### 7) Add fair value diagnostics logging
**Priority:** P1

### Goal
Make it obvious why fair value moved.

### Log per quote cycle
- spot
- start price
- sigma components
- `p_model`
- `p_book`
- blend weights
- `p_flow_adj`
- `p_final`
- `p_quote_center`
- time remaining
- toxicity flags

### Files
- `src/orchestration/market_cycler.py`
- `src/monitoring/logger.py`
- optional dashboard additions in `src/monitoring/dashboard.py`

### Acceptance criteria
- debugging fair value no longer feels like guesswork

---

# Recommended implementation order

## Sprint 1
1. regime-aware sigma
2. market-implied `p_book`
3. blended `p_raw`

## Sprint 2
4. calibration logging
5. calibration application
6. microstructure adjustment

## Sprint 3
7. split `p_true` vs `p_quote_center`
8. expand diagnostics/dashboard

---

# Exact target architecture

```python
sigma_eff = compute_effective_sigma(...)

p_model = internal_probability_model(
    current_price=current_price,
    start_price=start_price,
    sigma=sigma_eff,
    time_remaining=time_remaining,
)

p_book, book_confidence = market_implied_probability(...)
model_confidence = model_quality_score(...)

p_raw = weighted_blend(
    p_model=p_model,
    p_book=p_book,
    model_confidence=model_confidence,
    book_confidence=book_confidence,
)

p_calibrated = calibrate_probability(
    p_raw=p_raw,
    asset=asset,
    time_remaining=time_remaining,
)

p_flow_adj = bounded_microstructure_adjustment(...)

p_true = clip(p_calibrated + p_flow_adj, 0.01, 0.99)
p_quote_center = execution_adjusted_center(p_true, inventory, toxicity, phase)
```

---

# Risks and how to avoid them

## Risk 1: overfitting
If calibration is built on tiny sample sizes, it lies.

### Mitigation
- bucket by asset and time remaining only if enough data exists
- fall back to global calibration when sample size is low

## Risk 2: blindly following a toxic book
Book-implied probability is useful until it isn’t.

### Mitigation
- reduce book weight when spread is wide, depth is thin, or toxicity is high

## Risk 3: making fair value too twitchy
Too many corrections can create noise.

### Mitigation
- bound adjustments tightly
- smooth inputs
- log everything

## Risk 4: mixing model truth with execution logic
This corrupts learning.

### Mitigation
- keep `p_true` and `p_quote_center` separate

---

# Success criteria
A successful fair value upgrade should produce:

1. **better calibration**
   - predicted probabilities match realized outcomes more closely

2. **better live stability**
   - fewer obviously stale or lagging quotes

3. **better inventory quality**
   - less toxic imbalance
   - more informative imbalance

4. **better post-trade behavior**
   - improved fill quality
   - less adverse selection

---

# Final recommendation
If only three things get done first, do these:

## 1. regime-aware sigma
## 2. market-implied blending
## 3. historical calibration

That is the shortest realistic path to a meaningfully better fair value engine.
