"""
Console dashboard using Rich display.
Shows real-time bot status with rebate tracking.
"""

import time
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.columns import Columns
from rich.text import Text


class Dashboard:
    def __init__(self, mode: str = "dry-run"):
        self.console = Console()
        self.mode = mode
        self._states = {}  # per-asset state
        self._global_state = {}

    def update(self, state: dict):
        """Update state for a specific asset.
        
        Uses dict.update() (merge) instead of replacement so that
        real-time fields written in-place by on_live_price (spot_price,
        fair_value) are not discarded between quote cycles.
        """
        asset = state.get("asset", "?")
        if asset not in self._states:
            self._states[asset] = state
        else:
            self._states[asset].update(state)
        self._global_state.update(state)

    def render(self) -> Table:
        """Render status table."""
        mode_color = "yellow" if self.mode == "dry-run" else "green"
        title = f"[bold {mode_color}]POLYMARKET MM — {self.mode.upper()}[/]"

        # If we have multiple assets, show compact view
        if len(self._states) > 1:
            return self._render_multi_asset()

        # Single asset detailed view — read from _states (updated by live callback)
        if self._states:
            asset_key = list(self._states.keys())[0]
            s = self._states[asset_key]
        else:
            s = self._global_state

        if not s:
            table = Table(title=title, expand=True)
            table.add_column("Status", justify="center")
            table.add_row("[dim]Waiting for market data...[/]")
            return table

        table = Table(title=title, show_header=True, header_style="bold cyan",
                      border_style="dim", expand=True, padding=(0, 1))
        table.add_column("Metric", style="dim", width=22)
        table.add_column("Value", justify="right", width=18)
        table.add_column("Metric", style="dim", width=22)
        table.add_column("Value", justify="right", width=18)

        asset = s.get("asset", "—")
        phase = s.get("phase", "—")
        phase_str = {"ACTIVE": "[green]ACTIVE[/]", "WIND_DOWN": "[yellow]WIND_DOWN[/]",
                     "DEAD_ZONE": "[red]DEAD_ZONE[/]",
                     "HALTED": "[red bold]HALTED[/]"}.get(phase, phase)

        slug = s.get("slug", "—") or "—"
        remaining = s.get("time_remaining", 0) or 0
        start_p = s.get("start_price", 0) or 0
        spot = s.get("spot_price", 0) or 0
        fv = s.get("fair_value", 0) or 0
        sigma = s.get("sigma", 0) or 0
        up_buy = s.get("up_buy", 0) or 0
        down_buy = s.get("down_buy", 0) or 0
        up_size = s.get("up_size", 0) or 0
        down_size = s.get("down_size", 0) or 0
        combined = s.get("combined_cost", 0) or 0
        edge = s.get("edge", 0) or 0
        up_shares = s.get("up_shares", 0) or 0
        down_shares = s.get("down_shares", 0) or 0
        up_avg = s.get("up_avg", 0) or 0
        down_avg = s.get("down_avg", 0) or 0

        # P&L / rebates
        net_trade = s.get("net_trading_pnl", 0) or 0
        outcome_pnl = s.get("outcome_pnl", 0) or 0
        est_rebates = s.get("est_rebates", 0) or 0
        net_total = s.get("economic_pnl", s.get("net_pnl", 0)) or 0
        rebates_hr = s.get("rebates_per_hour", 0) or 0
        total_volume = s.get("total_volume", 0) or 0
        total_shares = s.get("total_shares", 0) or 0

        # Market info
        table.add_row("Asset", f"[bold]{asset}[/]",
                       "Phase", phase_str)
        table.add_row("Market", slug[:24] if len(slug) > 24 else slug,
                       "Time Left", f"[bold]{remaining:.0f}s[/]")
        raw = s.get("raw_spot", spot)
        spread = s.get("chainlink_spread", 0)
        table.add_row("Start Price", f"${start_p:,.2f}",
                       "Polymarket Spot", f"${spot:,.4f}")
        
        ws_ticks = s.get("ws_ticks", 0)
        table.add_row("Fair Value P(Up)", f"{fv:.6f}",
                       "Raw Binance Spot", f"${raw:,.2f} (Spread: ${spread:,.2f})")
        table.add_row("Sigma", f"{sigma:.1%}",
                       "WS Ticks", f"{ws_ticks}")

        # Quotes
        table.add_row("UP Buy", f"${up_buy:.2f} x{int(up_size)}",
                       "DOWN Buy", f"${down_buy:.2f} x{int(down_size)}")
        edge_c = "green" if edge > 0 else "red"
        table.add_row("Combined Cost", f"${combined:.4f}",
                       "Edge/Pair", f"[{edge_c}]${edge:.4f}[/]")

        # Inventory
        table.add_row("UP Shares", f"{int(up_shares)} @ ${up_avg:.2f}" if up_shares else "0",
                       "DOWN Shares", f"{int(down_shares)} @ ${down_avg:.2f}" if down_shares else "0")

        # Share imbalance (the metric that drives rebalancing)
        imb = s.get("share_imbalance", 0) or 0
        pairs = s.get("matched_pairs", min(up_shares, down_shares)) or 0
        pair_pnl = s.get("matched_pair_pnl", pairs * edge) or 0
        avg_pair_cost = s.get("avg_pair_cost", 0) or 0
        ic = "green" if abs(imb) < 30 else ("yellow" if abs(imb) < 75 else "red")
        imb_label = f"+{imb:.0f} Up" if imb > 0 else (f"{imb:.0f} Down" if imb < 0 else "Balanced")
        table.add_row("Share Imbalance", f"[{ic}]{imb_label}[/]",
                       "Inv State", s.get("inv_state", "NORMAL"))
        pair_color = "green" if pair_pnl >= 0 and avg_pair_cost < 1.0 else "red"
        table.add_row("Matched Pairs", f"[{pair_color}]{pairs:.0f}[/]",
                       "Avg Pair Cost", f"[{pair_color}]${avg_pair_cost:.4f}[/]")
        table.add_row("Matched Pair P&L", f"[{pair_color}]${pair_pnl:.4f}[/]",
                       "Pair Edge Alert", "[red bold]NEGATIVE[/]" if s.get("negative_pair_edge") else "OK")

        # P&L Section
        tc = "green" if net_trade >= 0 else "red"
        table.add_row("Trading P&L", f"[{tc}]${net_trade:.4f}[/]",
                       "Settled Markets", str(s.get("markets_settled", 0)))

        # Rebates (always green - it's income)
        rc = "green" if est_rebates > 0 else "dim"
        table.add_row("Est. Rebates", f"[{rc}]${est_rebates:.4f}[/]",
                       "Rebates/Hour", f"[{rc}]${rebates_hr:.4f}[/]")

        oc = "green" if outcome_pnl >= 0 else "red"
        table.add_row("Outcome P&L", f"[{oc}]${outcome_pnl:.4f}[/]",
                       "Total Fills", str(s.get("total_fills", 0)))

        # Economic total includes matched-pair/merge, outcome, and rebates.
        nc = "green" if net_total >= 0 else "red"
        table.add_row("[bold]Economic P&L[/]", f"[bold {nc}]${net_total:.4f}[/]",
                       "", "")

        table.add_row("Total Volume", f"${total_volume:.2f}",
                       "Total Shares", f"{total_shares:.0f}")

        table.add_row("Regime", s.get("regime", "STABLE"),
                       "", "")

        # Wallet balance (live mode only or simulated)
        wallet_bal = s.get("wallet_balance", -1)
        if wallet_bal >= 0:
            warn = s.get("balance_warn_threshold", 20)
            merge = s.get("balance_merge_threshold", 10)
            bc = "green" if wallet_bal > warn else (
                "yellow" if wallet_bal > merge else "red"
            )
            auto_merges = s.get("auto_merges", 0)
            merged_usdc = s.get("auto_merged_usdc", 0)
            table.add_row("Wallet USDC", f"[{bc}]${wallet_bal:.2f}[/]",
                           "Auto-Merges",
                           f"{auto_merges} (${merged_usdc:.2f})" if auto_merges else "0")

        # Simulated Capital Tracking (if starting_capital is present)
        start_cap = s.get("starting_capital", 0)
        curr_cap = s.get("current_capital", 0)
        if start_cap > 0:
            cc_color = "green" if curr_cap >= start_cap else "red"
            table.add_row("Starting Capital", f"${start_cap:.2f}",
                           "Current Capital", f"[{cc_color}]${curr_cap:.2f}[/]")
                           
        # Merge Message
        merge_msg = s.get("merge_message", "")
        if merge_msg:
            table.add_row("Auto-Merge Event", f"[yellow bold]{merge_msg}[/]", "", "")

        return table

    def _render_multi_asset(self) -> Table:
        """Compact multi-asset view with rebate column."""
        mode_color = "yellow" if self.mode == "dry-run" else "green"
        title = f"[bold {mode_color}]POLYMARKET MM — {self.mode.upper()}[/]"

        table = Table(title=title, show_header=True, header_style="bold cyan",
                      border_style="dim", expand=True, padding=(0, 1))
        table.add_column("Asset", width=5)
        table.add_column("Phase", width=7)
        table.add_column("Time", width=5, justify="right")
        table.add_column("Spot", width=10, justify="right")
        table.add_column("P(Up)", width=6, justify="right")
        table.add_column("UpBuy", width=9, justify="right")
        table.add_column("DnBuy", width=9, justify="right")
        table.add_column("Fills", width=5, justify="right")
        table.add_column("Rebate", width=9, justify="right")
        table.add_column("Net P&L", width=9, justify="right")

        for asset in sorted(self._states.keys()):
            s = self._states[asset]
            phase = s.get("phase", "—")
            phase_str = {"ACTIVE": "[green]ACT[/]", "WIND_DOWN": "[yellow]WND[/]",
                         "DEAD_ZONE": "[red]DEAD[/]",
                         "HALTED": "[red]HALT[/]"}.get(phase, phase[:4])
            remaining = s.get("time_remaining", 0) or 0
            spot = s.get("spot_price", 0) or 0
            fv = s.get("fair_value", 0) or 0
            up_buy = s.get("up_buy", 0) or 0
            down_buy = s.get("down_buy", 0) or 0
            up_size = s.get("up_size", 0) or 0
            down_size = s.get("down_size", 0) or 0
            fills = s.get("total_fills", 0) or 0
            rebates = s.get("est_rebates", 0) or 0
            net = s.get("economic_pnl", s.get("net_pnl", 0)) or 0

            rc = "green" if rebates > 0 else "dim"
            nc = "green" if net >= 0 else "red"

            table.add_row(
                f"[bold]{asset}[/]",
                phase_str,
                f"{remaining:.0f}s",
                f"${spot:,.2f}" if spot > 100 else f"${spot:.4f}",
                f"{fv:.3f}",
                f"${up_buy:.2f}x{int(up_size)}",
                f"${down_buy:.2f}x{int(down_size)}",
                str(fills),
                f"[{rc}]${rebates:.3f}[/]",
                f"[{nc}]${net:.3f}[/]",
            )

        # Summary row
        total_rebates = sum(s.get("est_rebates", 0) or 0 for s in self._states.values())
        total_fills = sum(s.get("total_fills", 0) or 0 for s in self._states.values())
        total_net = sum(s.get("net_pnl", 0) or 0 for s in self._states.values())
        nc = "green" if total_net >= 0 else "red"

        table.add_row(
            "[bold]TOTAL[/]", "", "", "", "", "", "",
            f"[bold]{total_fills}[/]",
            f"[bold green]${total_rebates:.3f}[/]",
            f"[bold {nc}]${total_net:.3f}[/]",
        )

        return table

    def print_status(self):
        """Print current status to console."""
        self.console.clear()
        self.console.print(self.render())
