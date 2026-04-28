# Bug Report — polymarket-mm

Date: 2026-04-28
Scope: runtime bugs, production blockers, and operational risks for live deployment.

## Assumptions / excluded items
- **Live credentials leak removed from this report** per owner request.
- **BUY-only behavior is treated as an intentional design constraint**, not a bug.

---

## Executive summary
The repo is a decent strategy prototype, but it is **not yet production-safe for unattended live trading**.

### Top blockers
1. Runtime bug in live fill processing (`time` not imported)
2. Missing strict live config validation / fail-fast startup checks
3. No mandatory startup order cleanup (`cancel_all`) before quoting
4. Weak toxicity / adverse-fill shutdown behavior
5. Near-expiry protections are still too soft for hostile live markets
6. No production deployment / observability / recovery layer

---

## 1) Runtime bug: `time` not imported in live fill processing
**Severity:** High  
**File:** `src/execution/clob_client.py`

### Issue
`process_fills()` uses `time.time()` but the module does not import `time`.

### Impact
Can throw:
```python
NameError: name 'time' is not defined
```
when live fills are processed.

### Recommended fix
Add:
```python
import time
```
at the top of `src/execution/clob_client.py`.

---

## 2) Missing strict validation for live config / env vars
**Severity:** High  
**File:** `src/config.py`

### Issue
Environment variable substitution silently converts missing env vars to empty strings:
```python
return os.environ.get(env_var, "")
```
There is no hard validation that required live-mode fields are present.

### Impact
The bot can start in a partially invalid state and fail later in confusing ways.

### Recommended fix
For `mode == live`, fail fast if any required field is empty:
- private key
- api key
- api secret
- api passphrase
- builder creds (if merge path required)
- RPC URL (if on-chain ops are required)

---

## 3) No mandatory startup safety routine before quoting
**Severity:** High  
**Files:** `src/main.py`, `src/execution/order_manager.py`

### Issue
No clearly enforced startup sequence that guarantees a clean live state before quoting.

### Risk
If stale orders are left from a previous run/crash, the bot may start quoting on top of old inventory/orders.

### Recommended fix
Before live quoting begins:
1. authenticate
2. fetch current open orders
3. cancel all existing strategy orders
4. verify balances / connectivity / market metadata
5. only then enable quoting

---

## 4) Weak adverse-fill / toxicity kill switch
**Severity:** High  
**Files:** `src/risk/toxicity.py`, `src/orchestration/market_cycler.py`

### Issue
The repo has toxicity-related components, but it does not appear to have a strong enough **hard stop** for getting picked off in hostile live conditions.

### Impact
The bot may continue quoting when it should immediately stop after toxic fills or one-way flow.

### Recommended fix
Add hard shutdown / cooldown conditions such as:
- repeated adverse fills in short window
- one-sided fills while opposite side does not fill
- immediate post-fill move against quote
- fast fair-value drift beyond threshold
- stale feed / stale book / stale fair-value detection

---

## 5) Near-expiry protection is still too soft
**Severity:** High  
**Files:** `src/strategy/quote_engine.py`, `src/risk/risk_engine.py`, `src/orchestration/market_cycler.py`

### Issue
Spreads widen near expiry, but the bot can still remain active in very toxic end-of-window conditions.

### Impact
Short-dated binary markets become much more dangerous near expiry:
- adverse selection increases sharply
- displayed book can become deceptive
- one-sided inventory becomes harder to repair

### Recommended fix
Introduce stronger phase behavior:
- reduce size earlier
- stop quoting earlier in the final window
- optionally allow only repair-side quoting in defensive mode
- flatten / halt if imbalance + time-to-expiry combination becomes unsafe

---

## 6) Broad exception handling in critical paths
**Severity:** Medium  
**Files:** multiple (`market_cycler.py`, `price_feed.py`, `clob_client.py`, `ctf_ops.py`, etc.)

### Issue
Many critical paths use broad exception blocks:
```python
except Exception as e:
```

### Impact
Can hide logic errors, integration failures, and partial state corruption while allowing the bot to keep running.

### Recommended fix
- narrow exception types where possible
- distinguish fatal vs retryable failures
- escalate fatal trading-state failures instead of only logging them

---

## 7) Terminal/dashboard logic is mixed into runtime path
**Severity:** Medium  
**File:** `src/main.py`

### Issue
The runtime loop uses a console-clearing dashboard:
```python
os.system('cls' if os.name == 'nt' else 'clear')
```

### Impact
This is fragile in non-interactive/headless production environments and makes clean supervision harder.

### Recommended fix
Split modes:
- interactive dashboard mode
- headless production mode

Headless mode should run without terminal UI assumptions.

---

## 8) Live-mode confirmation prompt blocks automation
**Severity:** Medium  
**File:** `src/main.py`

### Issue
Live mode requires manual input:
```python
confirm = input("Type 'CONFIRM' to proceed: ")
```

### Impact
Prevents clean unattended startup under process managers.

### Recommended fix
Keep manual confirmation for local interactive runs, but support an explicit flag/env switch for supervised production runs, e.g.:
- `--yes-live-risk`
- `LIVE_MODE_CONFIRMED=1`

---

## 9) No exact dependency pinning
**Severity:** Medium  
**File:** `requirements.txt`

### Issue
Dependencies are specified with `>=` ranges.

### Impact
Fresh installs can pull different versions over time, creating unstable production behavior.

### Recommended fix
Pin exact versions and use a lockfile / reproducible build process.

---

## 10) No deployment artifacts / production runbook
**Severity:** Medium  
**Missing:** Dockerfile, compose, systemd unit, restart policy, healthcheck, ops notes

### Issue
Repo is missing a standard production deployment layer.

### Impact
- inconsistent environments
- weak crash recovery
- manual ops burden
- harder reproducibility

### Recommended fix
Add:
- Dockerfile
- `.env.example`
- service/run script
- systemd or supervisor example
- healthcheck and startup self-test
- deployment runbook

---

## 11) Logging exists, but alerting / observability is incomplete
**Severity:** Medium  
**Files:** `src/monitoring/logger.py`, monitoring layer overall

### Issue
Structured JSON logs are present, but there is no full production observability path.

### Missing coverage
- stale feed alerting
- trading halt alerting
- inventory emergency alerting
- merge failure alerting
- repeated order reject alerting
- drawdown alerting

### Recommended fix
Add metrics + alerts to Discord/Telegram for key failure modes.

---

## 12) Settlement / merge / redeem path is operationally fragile
**Severity:** Medium  
**Files:** `src/orchestration/market_cycler.py`, `src/execution/ctf_ops.py`

### Issue
Settlement depends on multiple live components:
- market metadata
- condition IDs
- builder relayer availability
- on-chain fallback
- resolution polling

### Impact
If one piece fails, capital/accounting can drift from expected bot state.

### Recommended fix
Add stronger reconciliation:
- explicit settlement state machine
- durable state persistence for pending merges/redeems
- retry/reporting for incomplete settlement operations

---

## 13) No visible automated test suite for key invariants
**Severity:** Medium

### Issue
No visible tests for:
- config validation
- quote invariants
- inventory state transitions
- startup safety
- settlement accounting

### Recommended fix
Add tests for at least:
- `yes_price + no_price < 1.0`
- emergency inventory behavior
- missing live env vars must fail startup
- startup order cleanup path
- settlement PnL accounting

---

## Non-bug but important design constraints
These are not listed as bugs in this report, but they are still important operational realities:

1. **BUY-only design** means inventory repair is constrained to opposite-side accumulation and merge/redeem workflows.
2. **Pair-building logic** works best in balanced/two-sided fill environments.
3. **Fast one-way markets** remain strategically hostile even if the code is stable.

---

## Recommended remediation order
### Immediate
1. Fix `time` import bug
2. Add strict live config validation
3. Add startup `cancel_all` safety routine
4. Add hard toxicity kill switch
5. Tighten near-expiry shutdown logic

### Next
6. Add headless production mode
7. Pin dependencies
8. Add deployment artifacts
9. Add alerting / observability
10. Add tests for critical invariants

---

## Final verdict
This repo is a **credible strategy prototype**, but it is **not yet safe for unattended live production**.

Best use right now:
- code review
- paper trading
- supervised micro-live testing after fixes

Not recommended yet for:
- unattended production deployment
- larger live capital allocation
