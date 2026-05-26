#!/usr/bin/env python3
"""Wait for the next 15m Polymarket crypto window, run an N-window BTC dry-run, and write report.md.

Set MM_DRYRUN_TARGET_WINDOWS to choose N. This is intentionally self-contained
so an OpenClaw background exec can run it unattended.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"
RUNS_DIR = ROOT / "runs"
REPORT = ROOT / "report.md"
BOT_LOG = LOG_DIR / "bot.log"
STATE = DATA_DIR / "state.json"
ASSETS = os.environ.get("MM_DRYRUN_ASSETS", "BTC").split()
TARGET_WINDOWS = int(os.environ.get("MM_DRYRUN_TARGET_WINDOWS", "2"))
PYTHON = ROOT / ".venv" / "bin" / "python"


def iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def wait_until_next_window() -> float:
    now = time.time()
    next_boundary = math.floor(now / 900) * 900 + 900
    # Small cushion so market discovery sees the new slug rather than the expired window.
    start_at = next_boundary + 2
    delay = max(0, start_at - now)
    print(f"[{iso()}] Waiting {delay:.1f}s for next 15m window ({iso(start_at)})", flush=True)
    time.sleep(delay)
    return start_at


def backup_path(path: Path, run_id: str) -> Path | None:
    if not path.exists():
        return None
    dst = path.with_name(f"{path.name}.pre_two_window_{run_id}.bak")
    shutil.copy2(path, dst)
    return dst


def reset_state():
    DATA_DIR.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({
        "inventory": {},
        "open_orders": {},
        "processed_fills": [],
        "pending_resolutions": [],
        "last_updated": time.time(),
    }, indent=2), encoding="utf-8")


def parse_events(log_path: Path):
    events = []
    if not log_path.exists():
        return events
    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get("event"):
            events.append(obj)
    return events


def short_market(obj: dict) -> str:
    return str(obj.get("market") or obj.get("market_id") or "")[:8]


def money(v: float) -> str:
    return f"${v:.6f}"


def generate_report(run_dir: Path, log_path: Path, stdout_path: Path, run_started_at: float, run_ended_at: float, returncode: int):
    events = parse_events(log_path)
    windows: list[dict] = []
    current_by_asset: dict[str, dict] = {}
    slug_to_window: dict[str, dict] = {}
    market_to_window: dict[str, dict] = {}

    for e in events:
        ev = e.get("event")
        asset = e.get("asset") or ""
        if ev == "market_started":
            w = {
                "asset": asset,
                "slug": e.get("slug", ""),
                "question": e.get("question", ""),
                "started_log_ts": e.get("timestamp", ""),
                "fills": [],
                "rebates": [],
                "end_up": None,
                "end_down": None,
                "matched_pairs_at_settle": None,
                "merge_pnl": 0.0,
                "final_pair_pnl": 0.0,
                "outcome_pnl": 0.0,
                "winner": None,
                "market_short": None,
            }
            windows.append(w)
            current_by_asset[asset] = w
            if w["slug"]:
                slug_to_window[w["slug"]] = w
        elif ev == "fill_recorded":
            # Fills do not include slug; associate to the current active window for the asset.
            w = current_by_asset.get(asset) or (windows[-1] if windows else None)
            if not w:
                continue
            m = short_market(e)
            if m:
                w["market_short"] = m
                market_to_window[m] = w
            if "pair_pnl" in e:
                # fill_recorded carries the inventory module's cumulative FIFO
                # pair PnL for this market. Use it as a fallback for windows
                # where matched pairs remain at expiry but no explicit merge
                # event was emitted before the target-window shutdown.
                w["final_pair_pnl"] = float(e.get("pair_pnl") or 0)
            w["fills"].append(e)
        elif ev == "fill_rebate":
            w = current_by_asset.get(asset) or (windows[-1] if windows else None)
            if w:
                w["rebates"].append(e)
        elif ev == "market_settling":
            w = slug_to_window.get(e.get("slug")) or current_by_asset.get(asset)
            if w:
                w["end_up"] = float(e.get("up_shares") or 0)
                w["end_down"] = float(e.get("down_shares") or 0)
                w["matched_pairs_at_settle"] = float(e.get("matched_pairs") or 0)
        elif ev == "pnl_pair_merge":
            m = short_market(e)
            w = market_to_window.get(m)
            if w:
                w["merge_pnl"] += float(e.get("pnl") or 0)
        elif ev == "outcome_pnl_recorded":
            m = short_market(e)
            w = market_to_window.get(m)
            if w:
                w["outcome_pnl"] += float(e.get("pnl") or 0)
        elif ev == "dry_run_actual_resolution":
            w = slug_to_window.get(e.get("slug"))
            if w:
                w["winner"] = e.get("winner")
                # Fallback if market-short mapping was unavailable.
                if not w.get("outcome_pnl"):
                    w["outcome_pnl"] = float(e.get("outcome_pnl") or 0)

    completed = [w for w in windows if w.get("winner") is not None or w.get("end_up") is not None]
    selected = completed[:TARGET_WINDOWS]
    if len(selected) < TARGET_WINDOWS:
        selected = windows[:TARGET_WINDOWS]

    lines = []
    lines.append(f"# Polymarket MM {TARGET_WINDOWS}-Window Dry-Run Report")
    lines.append("")
    lines.append(f"Generated: {iso()}")
    lines.append(f"Mode: dry-run")
    lines.append(f"Assets: {' '.join(ASSETS)}")
    lines.append(f"Target windows: {TARGET_WINDOWS}")
    lines.append(f"Run directory: `{run_dir}`")
    lines.append(f"Bot return code: `{returncode}`")
    lines.append("")

    total_net = 0.0
    for i, w in enumerate(selected, 1):
        fills = w["fills"]
        yes_fills = [f for f in fills if f.get("side") in ("yes", "up")]
        no_fills = [f for f in fills if f.get("side") in ("no", "down")]
        yes_qty = sum(float(f.get("size") or 0) for f in yes_fills)
        no_qty = sum(float(f.get("size") or 0) for f in no_fills)
        yes_cost = sum(float(f.get("size") or 0) * float(f.get("price") or 0) for f in yes_fills)
        no_cost = sum(float(f.get("size") or 0) * float(f.get("price") or 0) for f in no_fills)
        matched = min(yes_qty, no_qty)
        avg_pair_cost = ((yes_cost / yes_qty) + (no_cost / no_qty)) if yes_qty and no_qty else 0.0
        rebate_pnl = sum(float(r.get("est_rebate") or 0) for r in w["rebates"])
        merge_pnl = float(w.get("merge_pnl") or 0)
        final_pair_pnl = float(w.get("final_pair_pnl") or 0)
        if abs(final_pair_pnl) > abs(merge_pnl):
            merge_pnl = final_pair_pnl
        outcome_pnl = float(w.get("outcome_pnl") or 0)
        net_pnl = merge_pnl + outcome_pnl + rebate_pnl
        total_net += net_pnl

        lines.append(f"## Window {i}: `{w.get('slug') or 'unknown'}`")
        if w.get("question"):
            lines.append(f"- Question: {w['question']}")
        if w.get("winner"):
            lines.append(f"- Winner: {w['winner']}")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|---|---:|")
        lines.append(f"| Fills | {len(fills)} |")
        lines.append(f"| YES/UP end inventory | {(w.get('end_up') if w.get('end_up') is not None else yes_qty):.4f} shares |")
        lines.append(f"| NO/DOWN end inventory | {(w.get('end_down') if w.get('end_down') is not None else no_qty):.4f} shares |")
        lines.append(f"| Matched pair quantity | {matched:.4f} shares |")
        lines.append(f"| Average pair cost | {money(avg_pair_cost)} |")
        lines.append(f"| Merge PnL | {money(merge_pnl)} |")
        lines.append(f"| Outcome PnL | {money(outcome_pnl)} |")
        lines.append(f"| Rebate PnL | {money(rebate_pnl)} |")
        lines.append(f"| Net PnL | {money(net_pnl)} |")
        lines.append("")

    lines.append("## Totals")
    lines.append("")
    lines.append(f"- Windows reported: {len(selected)}/{TARGET_WINDOWS}")
    lines.append(f"- Net PnL: {money(total_net)}")
    lines.append("")
    lines.append("## Source files")
    lines.append(f"- Bot JSON log: `{log_path}`")
    lines.append(f"- Stdout/stderr: `{stdout_path}`")

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    shutil.copy2(REPORT, run_dir / "report.md")
    print(f"[{iso()}] Wrote {REPORT}", flush=True)


def main() -> int:
    if not PYTHON.exists():
        print(f"Missing venv python: {PYTHON}", file=sys.stderr)
        return 2

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS_DIR / f"{TARGET_WINDOWS}_window_dryrun_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)

    wait_until_next_window()
    run_started_at = time.time()

    backup_tag = f"{TARGET_WINDOWS}window_{run_id}"
    backup_log = backup_path(BOT_LOG, backup_tag)
    backup_state = backup_path(STATE, backup_tag)
    if backup_log:
        print(f"[{iso()}] Backed up bot.log -> {backup_log}", flush=True)
    if backup_state:
        print(f"[{iso()}] Backed up state.json -> {backup_state}", flush=True)
    if BOT_LOG.exists():
        BOT_LOG.unlink()
    reset_state()

    stdout_path = run_dir / "bot_stdout.log"
    cmd = [
        str(PYTHON), "-m", "src.main",
        "--mode", "dry-run",
        "--assets", *ASSETS,
        "--headless",
        "--target-windows", str(TARGET_WINDOWS),
        "--progress-heartbeat-minutes", "2",
    ]
    print(f"[{iso()}] Starting dry-run: {' '.join(cmd)}", flush=True)
    with stdout_path.open("w", encoding="utf-8") as out:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, text=True)
        try:
            rc = proc.wait(timeout=int(os.environ.get("MM_DRYRUN_TIMEOUT_SECONDS", "7200")))
        except subprocess.TimeoutExpired:
            print(f"[{iso()}] Timeout; sending SIGINT", flush=True)
            proc.send_signal(signal.SIGINT)
            try:
                rc = proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()

    run_ended_at = time.time()
    run_log = run_dir / "bot.log"
    if BOT_LOG.exists():
        shutil.copy2(BOT_LOG, run_log)
    else:
        run_log.write_text("", encoding="utf-8")

    if STATE.exists():
        shutil.copy2(STATE, run_dir / "state.final.json")

    generate_report(run_dir, run_log, stdout_path, run_started_at, run_ended_at, rc)
    print(f"[{iso()}] Done; return code {rc}; report at {REPORT}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
