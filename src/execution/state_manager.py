"""
Durable state manager for crash recovery.
Persists inventory, open orders, and processed fills.
"""
import os
import json
import time
from typing import Dict, Any
from src.monitoring.logger import get_logger

log = get_logger("state_manager")

class StateManager:
    def __init__(self, state_file: str = "data/state.json"):
        self.state_file = state_file
        self.state = {
            "inventory": {},
            "open_orders": {},
            "processed_fills": [],
            "last_updated": 0.0
        }
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        self.load_state()

    def load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self.state.update(data)
                        log.info("state_loaded", file=self.state_file)
            except Exception as e:
                log.error("state_load_error", error=str(e))

    def save_state(self):
        self.state["last_updated"] = time.time()
        try:
            # Atomic write
            temp_file = self.state_file + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            os.replace(temp_file, self.state_file)
        except Exception as e:
            log.error("state_save_error", error=str(e))

    def update_inventory(self, positions_dict: Dict[str, Any]):
        """Store raw dict representation of inventory positions."""
        self.state["inventory"] = positions_dict
        self.save_state()

    def update_open_orders(self, open_orders: Dict[str, dict]):
        self.state["open_orders"] = open_orders
        self.save_state()

    def update_processed_fills(self, processed_fills_list: list):
        self.state["processed_fills"] = processed_fills_list
        self.save_state()

    def clear_state(self):
        self.state = {
            "inventory": {},
            "open_orders": {},
            "processed_fills": [],
            "last_updated": time.time()
        }
        self.save_state()
