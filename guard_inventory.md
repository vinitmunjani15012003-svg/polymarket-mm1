# Guard Inventory

This file tracks legacy guards while the architecture migrates to service-owned decisions.

| Guard | Current Owner | Target Owner | Keep / Merge / Delete | Notes |
|---|---|---|---|---|
| Stale spot fail-closed | `MarketCycler._quote_cycle` | `services/risk/data_risk.py` + `market_data/feed_health.py` | Merge | Centralize feed age decisions. |
| MT5/Exness max quote age | `MarketCycler._max_spot_price_age_seconds` | `services/risk/data_risk.py` | Keep | Live quoting should fail closed faster than diagnostic stale tolerance. |
| FV model confidence | `services/fair_value/confidence.py` | `FairValueEngine` | Keep | Extracted; callers should consume `FairValueResult`. |
| Fast-feed confidence floor | `services/fair_value/confidence.py` | `FairValueEngine` | Merge | Keep only inside engine; remove scattered call sites later. |
| Tail blend guard | `services/fair_value/blender.py` | `FairValueEngine` | Keep | Prevent thin book pulling tail FV. |
| Tradable FV market cap | `services/fair_value/blender.py` | `FairValueEngine` | Keep | Replaces ad hoc optimistic-FV guards. |
| Basis guard | `services/fair_value/basis_protection.py` | `services/risk/market_risk.py` | Merge | Basis check should produce `RiskDecision`. |
| Normal quote atomicity | `MarketCycler._quote_cycle` | `services/quoting/quote_sanity.py` | Merge | Must remain until quote policy fully owns plan generation. |
| Small-cap opening/done guards | `MarketCycler` | future `orchestration/small_capital.py` | Keep | Critical live safety; extract after state machine tests. |
| Repair pair edge cap | `services/inventory/repair_planner.py` | `InventoryBook`/repair planner | Keep | Prevent guaranteed-loss matched pairs. |
| Negative pair edge halt | `services/inventory/pair_tracker.py` | risk coordinator | Merge | Should become `RiskDecision`. |
| Crossed bid cancel | `OrderManager` | `services/execution/cancel_manager.py` | Merge | Execution service should own cancel/fill race policy. |
| Toxicity halt | `src/risk/toxicity.py` | `services/risk/toxicity_monitor.py` | Keep | Compatibility exported; coordinator should consume. |
| Regime halt | `src/risk/regime_filter.py` | `services/risk/regime_detector.py` | Keep | Compatibility exported; coordinator should consume. |

Target reduction: merge scattered boolean guards into `DecisionResult` / `RiskDecision` objects as call sites migrate.
