"""
cooldown_manager.py
--------------------
Thin wrapper around storage.BotStorage's cooldown methods. Kept as its own
module so the cooldown *policy* (2h default, daily reset, manual override)
is easy to find and change independently of the storage implementation.
"""

import logging
import config

log = logging.getLogger("cooldown")


class CooldownManager:
    def __init__(self, storage):
        self.storage = storage

    def can_alert(self, symbol: str) -> bool:
        """True if symbol is NOT currently in cooldown."""
        in_cooldown = self.storage.is_in_cooldown(symbol)
        if in_cooldown:
            log.info(f"[COOLDOWN] {symbol} is in cooldown, skipping alert")
        return not in_cooldown

    def start_cooldown(self, symbol: str):
        self.storage.record_alert_sent(symbol)

    def can_alert_early(self, symbol: str) -> bool:
        """
        Separate cooldown for early/watch signals - independent of the
        confirmed-alert cooldown, using config.EARLY_COOLDOWN_HOURS (shorter
        by default, since a "building" signal is lower-stakes to repeat
        than a full confirmed trade alert).
        """
        in_cooldown = self.storage.is_in_early_cooldown(symbol, config.EARLY_COOLDOWN_HOURS)
        if in_cooldown:
            log.info(f"[COOLDOWN] {symbol} is in EARLY-signal cooldown, skipping watch alert")
        return not in_cooldown

    def start_early_cooldown(self, symbol: str):
        self.storage.record_early_alert_sent(symbol)

    def reset_all(self):
        """Called by the daily reset job. Resets BOTH confirmed and early cooldowns."""
        self.storage.reset_all_cooldowns()
        log.info("[COOLDOWN] All cooldowns (confirmed + early) reset for new trading day")
