"""
storage.py
----------
Persistence layer. Render.com's free/standard web services do NOT reliably
keep a local filesystem across deploys/restarts, so this uses SQLite as the
default (fast, zero-config, ships with Python) but is written so you can
swap in Postgres (Render's free managed Postgres) later by only touching
this file.

Mirrors the 3-sheet structure you asked for:
  Settings  -> symbol registry, active flag, cooldown, fail counters
  AlertLog  -> every alert sent
  ErrorLog  -> every error encountered

One SQLite file per bot (matches "separate Google Sheets file per bot").
"""

import sqlite3
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta

log = logging.getLogger("storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    symbol TEXT PRIMARY KEY,
    sector TEXT DEFAULT '',
    active INTEGER DEFAULT 1,
    last_alert TEXT DEFAULT '',
    last_early_alert TEXT DEFAULT '',
    cooldown_hours REAL DEFAULT 2,
    alert_count INTEGER DEFAULT 0,
    fail_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ACTIVE',
    manual_reset INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alert_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    symbol TEXT,
    signal TEXT,
    price REAL,
    vwap REAL,
    rsi REAL,
    adx REAL,
    ema9 REAL,
    ema21 REAL,
    volume REAL,
    confidence TEXT,
    score INTEGER,
    message_id TEXT,
    signal_type TEXT DEFAULT 'CONFIRMED'
);

CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT,
    symbol TEXT,
    error_type TEXT,
    error_message TEXT,
    retry_count INTEGER
);
"""


class BotStorage:
    """One instance per bot, backed by its own SQLite file."""

    def __init__(self, db_path: str, symbols: list):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._init_db()
        self._migrate()
        self._seed_symbols(symbols)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    def _migrate(self):
        """
        Idempotent, additive-only migration for databases created before
        the early-signal feature existed. CREATE TABLE IF NOT EXISTS won't
        add new columns to an already-existing table, so we do that here.
        Safe to run every startup - duplicate-column errors are swallowed.
        """
        with self._conn() as conn:
            for stmt in [
                "ALTER TABLE settings ADD COLUMN last_early_alert TEXT DEFAULT ''",
                "ALTER TABLE alert_log ADD COLUMN signal_type TEXT DEFAULT 'CONFIRMED'",
            ]:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e).lower():
                        raise  # only swallow the expected "already migrated" case

    def _seed_symbols(self, symbols: list):
        """Insert any symbol not already present (Active=TRUE by default)."""
        with self._conn() as conn:
            for sym in symbols:
                conn.execute(
                    "INSERT OR IGNORE INTO settings (symbol, active, status) VALUES (?, 1, 'ACTIVE')",
                    (sym,),
                )

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    def get_active_symbols(self):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM settings WHERE active = 1 AND status != 'DISABLED'"
            ).fetchall()
            return [dict(r) for r in rows]

    def is_in_cooldown(self, symbol: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT last_alert, cooldown_hours, manual_reset FROM settings WHERE symbol=?",
                (symbol,),
            ).fetchone()
        if not row or not row["last_alert"]:
            return False
        if row["manual_reset"]:
            return False
        last_alert = datetime.fromisoformat(row["last_alert"])
        cooldown = timedelta(hours=row["cooldown_hours"] or 2)
        return datetime.now() < last_alert + cooldown

    def record_alert_sent(self, symbol: str):
        with self._conn() as conn:
            conn.execute(
                """UPDATE settings
                   SET last_alert=?, alert_count = alert_count + 1, fail_count = 0
                   WHERE symbol=?""",
                (datetime.now().isoformat(), symbol),
            )

    def is_in_early_cooldown(self, symbol: str, early_cooldown_hours: float) -> bool:
        """
        Separate, independent cooldown for early/watch signals - deliberately
        NOT tied to the confirmed-alert cooldown, so an early signal and a
        later confirmed alert for the same symbol don't suppress each other.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT last_early_alert, manual_reset FROM settings WHERE symbol=?",
                (symbol,),
            ).fetchone()
        if not row or not row["last_early_alert"]:
            return False
        if row["manual_reset"]:
            return False
        last_early = datetime.fromisoformat(row["last_early_alert"])
        return datetime.now() < last_early + timedelta(hours=early_cooldown_hours)

    def record_early_alert_sent(self, symbol: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE settings SET last_early_alert=? WHERE symbol=?",
                (datetime.now().isoformat(), symbol),
            )

    def record_failure(self, symbol: str, broken_at: int, disable_at: int):
        with self._conn() as conn:
            conn.execute(
                "UPDATE settings SET fail_count = fail_count + 1 WHERE symbol=?",
                (symbol,),
            )
            row = conn.execute(
                "SELECT fail_count FROM settings WHERE symbol=?", (symbol,)
            ).fetchone()
            fails = row["fail_count"] if row else 0
            if fails >= disable_at:
                conn.execute(
                    "UPDATE settings SET status='DISABLED', active=0 WHERE symbol=?",
                    (symbol,),
                )
            elif fails >= broken_at:
                conn.execute(
                    "UPDATE settings SET status='BROKEN' WHERE symbol=?", (symbol,)
                )

    def reset_all_cooldowns(self):
        """Called daily at market close / before open. Clears both the
        confirmed-alert cooldown AND the early-signal cooldown."""
        with self._conn() as conn:
            conn.execute("UPDATE settings SET last_alert='', last_early_alert='', manual_reset=0")

    def reset_fail_counts_if_healthy(self, symbol: str):
        with self._conn() as conn:
            conn.execute(
                "UPDATE settings SET fail_count=0, status='ACTIVE' WHERE symbol=? AND status != 'DISABLED'",
                (symbol,),
            )

    # ------------------------------------------------------------------ #
    # AlertLog
    # ------------------------------------------------------------------ #
    def log_alert(self, symbol, signal, price, vwap, rsi, adx, ema9, ema21,
                  volume, confidence, score, message_id, signal_type="CONFIRMED"):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO alert_log
                   (timestamp, symbol, signal, price, vwap, rsi, adx, ema9, ema21,
                    volume, confidence, score, message_id, signal_type)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (datetime.now().isoformat(), symbol, signal, price, vwap, rsi,
                 adx, ema9, ema21, volume, confidence, score, str(message_id), signal_type),
            )

    def count_alerts_today(self):
        today = datetime.now().date().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM alert_log WHERE timestamp LIKE ? AND signal_type='CONFIRMED'",
                (f"{today}%",),
            ).fetchone()
            return row["c"]

    def count_early_signals_today(self):
        today = datetime.now().date().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) c FROM alert_log WHERE timestamp LIKE ? AND signal_type='EARLY'",
                (f"{today}%",),
            ).fetchone()
            return row["c"]

    def top_symbols_today(self, limit=5):
        today = datetime.now().date().isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT symbol, COUNT(*) c FROM alert_log
                   WHERE timestamp LIKE ? AND signal_type='CONFIRMED'
                   GROUP BY symbol ORDER BY c DESC LIMIT ?""",
                (f"{today}%", limit),
            ).fetchall()
            return [(r["symbol"], r["c"]) for r in rows]

    # ------------------------------------------------------------------ #
    # ErrorLog
    # ------------------------------------------------------------------ #
    def log_error(self, symbol, error_type, error_message, retry_count=0):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO error_log (timestamp, symbol, error_type, error_message, retry_count)
                   VALUES (?,?,?,?,?)""",
                (datetime.now().isoformat(), symbol, error_type, str(error_message)[:500], retry_count),
            )

    def errors_today_summary(self):
        today = datetime.now().date().isoformat()
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT error_type, COUNT(*) c FROM error_log
                   WHERE timestamp LIKE ? GROUP BY error_type ORDER BY c DESC""",
                (f"{today}%",),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) c FROM error_log WHERE timestamp LIKE ?", (f"{today}%",)
            ).fetchone()["c"]
            return total, [(r["error_type"], r["c"]) for r in rows]
