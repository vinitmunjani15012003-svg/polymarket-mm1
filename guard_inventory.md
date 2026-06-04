# Guard Inventory

This file tracks legacy guards while the architecture migrates to service-owned decisions.

Status scale: **Extracted** means service/facade owns the decision shape; **Wired** means the live cycler/execution path consumes it; percentages are migration completeness estimates as of 2026-06-04 after the restart/live scenario coverage pass.

| Guard | Current Owner | Target Owner | Status | Completion | Keep / Merge / Delete | Notes |
|---|---|---|---|---:|---|---|
| Stale spot fail-closed | `MarketCycler._quote_cycle` + `RiskCoordinator` input | `services/risk/data_risk.py` + `market_data/feed_health.py` | Extracted, partially wired | 70% | Merge | `feed_freshness_decision` returns fail-closed `RiskDecision`; scenario test covers stale Exness cancel. Next: delete direct stale-age booleans once all cycler branches consume coordinator output. |
| MT5/Exness max quote age | `MarketCycler._max_spot_price_age_seconds` | `services/risk/data_risk.py` | Live guard retained | 55% | Keep | Live quoting should fail closed faster than diagnostic stale tolerance. Next: merge config lookup into data-risk input builder, not the decision itself. |
| FV model confidence | `services/fair_value/confidence.py` | `FairValueEngine` | Extracted | 85% | Keep | Callers can consume `FairValueResult`; legacy exports still support old tests/call sites. Next: remove direct cycler confidence helper calls after FV engine is sole path. |
| Fast-feed confidence floor | `services/fair_value/confidence.py` | `FairValueEngine` | Extracted, legacy alias remains | 80% | Merge | Keep only inside engine. Next delete: scattered `apply_fast_feed_confidence_floor` imports from cycler once dashboard/debug paths read `FairValueResult.confidence`. |
| Tail blend guard | `services/fair_value/blender.py` | `FairValueEngine` | Extracted | 90% | Keep | Prevents thin book pulling tail FV; covered by invariant tests. Next: keep as engine-private calibrated helper. |
| Tradable FV market cap | `services/fair_value/blender.py` | `FairValueEngine` | Extracted | 85% | Keep | Replaces ad hoc optimistic-FV guards. Next: merge any remaining direct `cap_fair_value_to_market` call sites into engine result consumption. |
| Basis/FV-book divergence guard | `services/fair_value/basis_protection.py` + `services/risk/market_risk.py` | `RiskCoordinator` | Extracted, partially wired | 70% | Merge | Scenario test covers `BASIS_GAP` divergence metadata. Next: merge fair-value basis check and market-risk decision into a single coordinator-owned cancel/halt decision. |
| Normal quote atomicity | `MarketCycler._quote_cycle` | `services/quoting/quote_sanity.py` | Extracted, partially wired | 65% | Merge | Must remain until quote policy fully owns plan generation. Next: have `QuotePolicy` produce/validate the final order list before `OrderManager` placement, then delete inline pair-cost/atomicity gates. |
| Small-cap opening/done guards | `orchestration/small_capital.py` via `MarketCycler` delegation | `orchestration/small_capital.py` state machine | Mostly extracted | 85% | Keep | Critical live safety. New scenarios cover restart mid-cycle hold, partial-fill-after-cancel balancing, completed-cycle no-requote. Next: move remaining compatibility wrappers out of `MarketCycler` after quote-loop extraction. |
| Repair pair edge cap | `InventoryBook`/`services/inventory/repair_planner.py` | `InventoryBook`/repair planner | Extracted | 80% | Keep | Prevents guaranteed-loss matched pairs; book exposes service seam. Next: delete direct position-method calls from cycler when repair planner supplies both size and cap. |
| Negative pair edge halt | `services/inventory/pair_tracker.py` + `services/risk/inventory_risk.py` | `RiskCoordinator` | Extracted, partially wired | 75% | Merge | Available as `RiskDecision`; cycler halt/merge call sites remain live. Next: route all negative-edge checks through coordinator audit trail. |
| Crossed bid cancel | `OrderManager` | `services/execution/cancel_manager.py` + fill/reconciliation policy | Partially extracted | 65% | Merge | Execution service should own cancel/fill race policy; existing test covers fill-race defer. Next: merge `_maybe_defer_crossed_bid_cancel` behind cancel manager/reconciliation facade. |
| Duplicate order intent prevention | `OrderIntent` + `OrderTracker` | execution intent tracker | Extracted | 75% | Keep | New scenario asserts identical quote-version retries collapse to one pending intent until order mark. Next: wire tracker into live submit path before considering old per-side active-order guards deletable. |
| Toxicity halt | `src/risk/toxicity.py` | `services/risk/toxicity_monitor.py` | Compatibility exported | 60% | Keep | Coordinator should consume the service monitor decision. Next: convert legacy monitor result to `RiskDecision`. |
| Regime halt | `src/risk/regime_filter.py` | `services/risk/regime_detector.py` | Compatibility exported | 60% | Keep | Coordinator should consume the service detector decision. Next: convert regime detector output to `RiskDecision`. |

## Current cleanup rollup

- Estimated overall guard migration completion: **74%**.
- High-confidence deletes after full quote-loop wiring: legacy fast-feed confidence floor call sites, direct tradable-FV cap call sites, duplicated stale-age booleans, and inline quote atomicity/pair-cost checks.
- High-confidence merges before deletes: basis/FV-book divergence into `RiskCoordinator`, crossed-cancel fill-race handling into execution cancel/reconciliation services, repair price caps into inventory repair planner output.
- Guards that should remain explicit even after migration: Exness live max-age fail-closed, tail blend guard, tradable FV cap, small-cap one-cycle completion/no-requote, repair pair-edge cap, duplicate `OrderIntent` idempotency.
