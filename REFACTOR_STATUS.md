# Refactor Status

Final guard-cleanup snapshot for branch `refactor/agent-guard-delete5`.

| Phase | Status | Evidence |
|---|---|---|
| Bootstrap decomposition | Complete | `src/bootstrap/*` has isolated dependency/startup/recovery helpers; tests guard no imports of concrete service layers. |
| Market-data freshness fail-closed | Wired in live quote loop | `MarketCycler._quote_cycle` calls `decide_stale_spot` directly, which uses `feed_freshness_decision` through `RiskCoordinator`; restart/live scenarios cover stale Exness cancel semantics. Remaining work is moving REST fallback/max-age input assembly behind the data-risk seam. |
| Fair-value service split | Complete for extracted helpers | Engine/confidence/blender/basis helpers are covered by invariant and architecture tests; legacy imports remain for compatibility until final call-site deletion. |
| Quoting policy extraction | Mostly complete | Quote construction, sizing, directional guard, pair-cost precheck, and quote-cycle context have direct tests; final live loop still owns some atomicity gates. |
| Inventory/repair services | Mostly complete, negative-edge wired | InventoryBook, reconciliation, pair tracker, negative-edge risk, and repair planner have service tests; `MarketCycler` now calls `decide_inventory_risk`/`decide_negative_pair_edge` directly. Final merge is routing repair price caps through planner output. |
| Execution services | Mostly complete | Intent idempotency, submitter signature compatibility, cancel manager fallback, reconciliation stray detection, and crossed-cancel fill race are covered. Live submit path can still be tightened around `OrderTracker`. |
| CLOB facade split | Complete for import compatibility | `src.execution.clob.*` helpers and legacy `ClobClientWrapper` static helper compatibility are tested without requiring py-clob-client import at module import time. |
| Settlement split | Complete for import compatibility | Settlement package exports, balance monitor compatibility, collateral inference, gasless/CTF merge selection, and relayer request shapes are tested. |
| Risk coordinator | Complete for aggregation semantics and key quote-loop call sites | Coordinator ranking, audit trail, stale data, basis gap, inventory, capital, and stop/halt precedence are covered; stale, basis, inventory, and negative-pair quote-cycle checks now call coordinator-backed seams directly. |
| Small-cap lifecycle | Complete for critical scenarios | Tests cover restart mid-cycle hold, partial-fill-after-cancel balancing, completed-window no-requote, stale opening repair, and done-state cancellation. |
| Lifecycle state machine | Complete for current enum | Tests cover happy path, halt recovery through resetting, and invalid transition rejection. |

## Phase completion estimates

- Bootstrap decomposition: **100%**
- Market-data freshness fail-closed: **82%**
- Fair-value service split: **86%**
- Quoting policy extraction: **78%**
- Inventory/repair services: **84%**
- Execution services: **80%**
- CLOB facade split: **90%**
- Settlement split: **88%**
- Risk coordinator: **88%**
- Small-cap lifecycle: **90%**
- Lifecycle state machine: **85%**
- Overall roadmap completion estimate: **85%**

## Remaining deletion list

1. Move stale-feed REST fallback and max-age config assembly behind the data-risk input builder; live stale decisions already flow through `quote_cycle`/`RiskCoordinator`.
2. Remove legacy fast-feed confidence-floor and tradable-FV cap call sites once dashboard/debug paths consume `FairValueResult` exclusively.
3. Move basis/FV-book side effects into a coordinator-owned quote-risk handler; direct `MarketCycler` private decision wrappers are gone.
4. Move remaining quote atomicity and pair-cost inline gates behind `QuotePolicy`, then delete cycler-local pair validation branches.
5. Route repair price caps exclusively through inventory repair planner output; negative pair-edge decisions already flow through the coordinator-backed quote-cycle seam.
6. Put `OrderTracker` on the live submit/retry path before deleting old per-side active-order duplicate guards.
7. Move crossed-bid fill-race handling fully behind cancel/reconciliation services, then delete `OrderManager` private compatibility wrappers.
8. After all live call sites use service facades, prune legacy re-export shims only in one compatibility-breaking cleanup pass.
