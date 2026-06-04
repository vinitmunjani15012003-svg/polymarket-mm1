# Refactor Status

Final roadmap hardening snapshot for branch `refactor/agent-final-tests4`.

| Phase | Status | Evidence |
|---|---|---|
| Bootstrap decomposition | Complete | `src/bootstrap/*` has isolated dependency/startup/recovery helpers; tests guard no imports of concrete service layers. |
| Market-data freshness fail-closed | Complete for decision layer, partially wired live | `feed_freshness_decision` and restart/live scenarios cover stale Exness cancel semantics; remaining work is deleting duplicate cycler age branches after coordinator is sole live input. |
| Fair-value service split | Complete for extracted helpers | Engine/confidence/blender/basis helpers are covered by invariant and architecture tests; legacy imports remain for compatibility until final call-site deletion. |
| Quoting policy extraction | Mostly complete | Quote construction, sizing, directional guard, pair-cost precheck, and quote-cycle context have direct tests; final live loop still owns some atomicity gates. |
| Inventory/repair services | Mostly complete | InventoryBook, reconciliation, pair tracker, negative-edge risk, and repair planner have service tests; final merge is routing all cycler repair price caps through planner output. |
| Execution services | Mostly complete | Intent idempotency, submitter signature compatibility, cancel manager fallback, reconciliation stray detection, and crossed-cancel fill race are covered. Live submit path can still be tightened around `OrderTracker`. |
| CLOB facade split | Complete for import compatibility | `src.execution.clob.*` helpers and legacy `ClobClientWrapper` static helper compatibility are tested without requiring py-clob-client import at module import time. |
| Settlement split | Complete for import compatibility | Settlement package exports, balance monitor compatibility, collateral inference, gasless/CTF merge selection, and relayer request shapes are tested. |
| Risk coordinator | Complete for aggregation semantics | Coordinator ranking, audit trail, stale data, basis gap, inventory, capital, and stop/halt precedence are covered. |
| Small-cap lifecycle | Complete for critical scenarios | Tests cover restart mid-cycle hold, partial-fill-after-cancel balancing, completed-window no-requote, stale opening repair, and done-state cancellation. |
| Lifecycle state machine | Complete for current enum | Tests cover happy path, halt recovery through resetting, and invalid transition rejection. |

## Remaining deletion list

1. Delete duplicated stale-age booleans in the live quote loop after all data-risk checks feed `RiskCoordinator`.
2. Remove legacy fast-feed confidence-floor and tradable-FV cap call sites once dashboard/debug paths consume `FairValueResult` exclusively.
3. Merge basis/FV-book divergence into a single coordinator-owned market-risk input, then delete direct basis checks outside that path.
4. Move remaining quote atomicity and pair-cost inline gates behind `QuotePolicy`, then delete cycler-local pair validation branches.
5. Route repair price caps and negative pair-edge checks exclusively through inventory/risk service outputs, then delete direct position-method checks from the cycler.
6. Put `OrderTracker` on the live submit/retry path before deleting old per-side active-order duplicate guards.
7. Move crossed-bid fill-race handling fully behind cancel/reconciliation services, then delete `OrderManager` private compatibility wrappers.
8. After all live call sites use service facades, prune legacy re-export shims only in one compatibility-breaking cleanup pass.
