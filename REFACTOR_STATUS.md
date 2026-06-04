# Refactor Status

Final compatibility-pruning snapshot for branch `refactor/agent-compat-smoke6`.

## Final phase table

| Phase | Status | Final assessment |
|---|---|---|
| Bootstrap decomposition | Complete | Dependency/startup/recovery helpers are isolated under `src/bootstrap/*`; architecture tests guard module import direction. |
| Market-data freshness fail-closed | Ready with follow-up cleanup | Live quote loop calls coordinator-backed stale-spot decisions through `quote_cycle`; remaining work is only moving REST fallback/max-age input assembly fully behind the data-risk seam. |
| Fair-value service split | Public compatibility kept | Service package exports engine/model/blender/confidence/basis helpers plus calibration constants; legacy `MarketCycler` imports remain compatible while call sites continue migrating. |
| Quoting policy extraction | Ready with follow-up cleanup | Quote construction, sizing, directional guard, pair-cost precheck, and quote-cycle context are covered; final live-loop atomicity gates can move later without blocking merge. |
| Inventory/repair services | Ready with follow-up cleanup | InventoryBook, reconciliation, pair tracker, negative-edge risk, and repair planner are covered; remaining work is routing repair price caps exclusively through planner output. |
| Execution services | Ready with guarded compatibility | Intent idempotency, submitter compatibility, cancel fallback, reconciliation stray detection, and crossed-cancel race handling are covered. Removed one unused private `OrderManager._needs_reprice` shim; retained public/private wrappers still used by live flow or explicit compatibility tests. |
| CLOB facade split | Public compatibility kept | `src.execution.clob.*` helpers and legacy `ClobClientWrapper` helper compatibility are tested without requiring SDK import at module import time. |
| Settlement split | Public compatibility kept | Settlement package exports and legacy `src.execution.ctf_ops` import boundaries remain tested for balance monitor/collateral compatibility. |
| Risk coordinator | Ready | Coordinator ranking, audit trail, stale data, basis gap, inventory, capital, and stop/halt precedence are covered; key quote-loop gates use coordinator-backed seams. |
| Small-cap lifecycle | Ready | Restart mid-cycle hold, partial-fill-after-cancel balancing, completed-window no-requote, stale opening repair, and done-state cancellation are covered. |
| Lifecycle state machine | Ready | Happy path, halt recovery through resetting, and invalid transition rejection are covered. |

## Branch readiness

- **Merge readiness:** ready for main merge after the checklist below is completed on the integration target.
- **Compatibility stance:** public import compatibility is intentionally preserved. Compatibility-breaking shim deletion should be a separate cleanup pass after live soak.
- **Pruning performed:** removed only the unused private `OrderManager._needs_reprice` compatibility wrapper. Kept `_reprice_decision`, `_order_still_open`, `ClobClientWrapper` SDK helpers, CLOB facades, settlement exports, and service package exports because they are either live-used or public import boundaries.
- **Dry-run readiness:** deterministic no-network service-wired dry-run smoke exists in `tests/test_final_roadmap_hardening.py` and places intent-tracked orders through `QuoteEngine` → `RiskCoordinator` → `QuotePolicy` → `OrderManager` → `DryRunExecutor`.
- **Safe local script smoke:** no standalone script was run because available scripts either require network/service reachability (`polymarket_sdk_smoke.py`, `check_mt5_bridge.py`) or wait for real 15-minute windows and market discovery (`run_two_window_dryrun_and_report.py`).

## Verification — 2026-06-04

- Targeted hardening tests: `/root/.openclaw/workspace/polymarket-mm/.venv/bin/python -m pytest tests/test_final_roadmap_hardening.py -q` passed (`8 passed`).
- Compileall: `/root/.openclaw/workspace/polymarket-mm/.venv/bin/python -m compileall -q src tests` passed.
- Full pytest: `/root/.openclaw/workspace/polymarket-mm/.venv/bin/python -m pytest -q` passed (`214 passed, 1 warning`).
- CLI inspection: `/root/.openclaw/workspace/polymarket-mm/.venv/bin/python -m src.main --help` confirmed dry-run mode still routes through live market discovery/window-oriented runtime rather than a deterministic no-network smoke command.

## Main-merge checklist

1. Confirm `git status --short` has only the intentional compatibility/test/status changes.
2. Run `/root/.openclaw/workspace/polymarket-mm/.venv/bin/python -m compileall -q src tests` on the merge target.
3. Run `/root/.openclaw/workspace/polymarket-mm/.venv/bin/python -m pytest -q` on the merge target.
4. Review conflicts against concurrent refactor branches touching `src/services/fair_value/__init__.py`, `src/execution/order_manager.py`, `tests/test_final_roadmap_hardening.py`, or `REFACTOR_STATUS.md`.
5. Keep public imports from `src.execution.clob`, `src.execution.ctf_ops`, `src.execution.settlement`, and `src.services.*` intact during conflict resolution.
6. Do not run live SDK/setup/approval smoke commands unless explicitly approved with credentials and a live-action window.
7. After merge, schedule a separate compatibility-breaking cleanup only after live soak confirms all callers use service facades.

## Remaining deletion list

1. Move stale-feed REST fallback and max-age config assembly behind the data-risk input builder; live stale decisions already flow through `quote_cycle`/`RiskCoordinator`.
2. Remove legacy fast-feed confidence-floor and tradable-FV cap call sites once dashboard/debug paths consume `FairValueResult` exclusively.
3. Move basis/FV-book side effects into a coordinator-owned quote-risk handler; direct `MarketCycler` private decision wrappers are gone.
4. Move remaining quote atomicity and pair-cost inline gates behind `QuotePolicy`, then delete cycler-local pair validation branches.
5. Route repair price caps exclusively through inventory repair planner output; negative pair-edge decisions already flow through the coordinator-backed quote-cycle seam.
6. Delete old per-side active-order duplicate guard compatibility once live `OrderTracker` behavior has more runtime soak.
7. Continue moving crossed-bid fill-race handling behind cancel/reconciliation services, then delete `OrderManager` private compatibility wrappers.
8. After all live call sites use service facades, prune legacy public re-export shims only in one compatibility-breaking cleanup pass.
