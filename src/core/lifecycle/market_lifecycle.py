"""Market lifecycle state machine."""

from __future__ import annotations

from src.core.models.state import LifecycleState


_ALLOWED = {
    LifecycleState.BOOT: {LifecycleState.DISCOVERING, LifecycleState.HALTED},
    LifecycleState.DISCOVERING: {LifecycleState.INITIALIZING, LifecycleState.HALTED},
    LifecycleState.INITIALIZING: {LifecycleState.QUOTING, LifecycleState.HALTED},
    LifecycleState.QUOTING: {LifecycleState.REPAIRING, LifecycleState.WINDDOWN, LifecycleState.SETTLING, LifecycleState.HALTED},
    LifecycleState.REPAIRING: {LifecycleState.QUOTING, LifecycleState.WINDDOWN, LifecycleState.SETTLING, LifecycleState.HALTED},
    LifecycleState.WINDDOWN: {LifecycleState.SETTLING, LifecycleState.HALTED},
    LifecycleState.SETTLING: {LifecycleState.RESETTING, LifecycleState.HALTED},
    LifecycleState.RESETTING: {LifecycleState.DISCOVERING, LifecycleState.HALTED},
    LifecycleState.HALTED: {LifecycleState.RESETTING},
}


class LifecycleManager:
    def __init__(self, state: LifecycleState = LifecycleState.BOOT):
        self.state = state

    def can_transition(self, new_state: LifecycleState) -> bool:
        return new_state in _ALLOWED.get(self.state, set()) or new_state == self.state

    def transition(self, new_state: LifecycleState) -> LifecycleState:
        if not self.can_transition(new_state):
            raise ValueError(f"invalid lifecycle transition {self.state} -> {new_state}")
        self.state = new_state
        return self.state

    def current_market(self):
        return None
