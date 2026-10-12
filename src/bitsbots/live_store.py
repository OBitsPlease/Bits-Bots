"""SQLite persistence for live and dry-run trading.

Money, prices, and quantities are stored as decimal strings. Every order
intent is written before any network call and every state change is
appended to an event log, so execution can be reconstructed after a crash.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .exchange import to_decimal
from .risk import RiskLimits

ACTIVE_INTENT_STATES = (
    "pending",
    "submitting",
    "open",
    "partially_filled",
    "submission_unknown",
    "cancel_requested",
    "needs_review",
)
UNRESOLVED_INTENT_STATES = ("submitting", "submission_unknown", "needs_review")
TERMINAL_INTENT_STATES = ("filled", "cancelled", "rejected", "not_found")
# Matches the "<status> executed <qty>/<total>" detail written by order updates.
_EXECUTED_PATTERN = re.compile(r"\bexecuted ([0-9][0-9.eE+-]*)/")


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_day_start_ms(timestamp_ms: int | None = None) -> int:
    moment = datetime.fromtimestamp((timestamp_ms or now_ms()) / 1000, tz=timezone.utc)
    start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _dec(value: Any) -> Decimal:
    return to_decimal(value)


class LiveStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_settings (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                live_enabled INTEGER NOT NULL DEFAULT 0,
                enabled_at INTEGER,
                kill_switch INTEGER NOT NULL DEFAULT 0,
                limits_json TEXT NOT NULL DEFAULT '{}',
                ip_address TEXT,
                ip_confirmed_at INTEGER,
                permissions_confirmed_at INTEGER,
                risk_acknowledged_at INTEGER,
                app_version TEXT,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_previews (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                mode TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price TEXT NOT NULL,
                quantity TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                allowed INTEGER NOT NULL,
                risk_json TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                consumed_at INTEGER,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                mode TEXT NOT NULL,
                client_id TEXT UNIQUE,
                preview_id TEXT UNIQUE,
                bot_id INTEGER,
                slot_id INTEGER,
                purpose TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price TEXT NOT NULL,
                quantity TEXT NOT NULL,
                state TEXT NOT NULL,
                exchange_id TEXT,
                executed_quantity TEXT NOT NULL DEFAULT '0',
                exchange_status TEXT,
                error TEXT,
                created_at INTEGER NOT NULL,
                submitted_at INTEGER,
                updated_at INTEGER NOT NULL,
                last_reconciled_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS live_intents_user_state
                ON live_intents(user_id, mode, state);
            CREATE INDEX IF NOT EXISTS live_intents_bot
                ON live_intents(bot_id, state);
            CREATE TABLE IF NOT EXISTS live_intent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id INTEGER NOT NULL REFERENCES live_intents(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                mode TEXT NOT NULL,
                name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                lower_price TEXT NOT NULL,
                upper_price TEXT NOT NULL,
                levels INTEGER NOT NULL,
                order_value TEXT NOT NULL,
                max_allocation TEXT NOT NULL,
                stop_price TEXT NOT NULL,
                trailing_enabled INTEGER NOT NULL DEFAULT 0,
                trailing_trigger TEXT NOT NULL DEFAULT '0.01',
                trailing_confirm_seconds INTEGER NOT NULL DEFAULT 300,
                trailing_max_recenters INTEGER NOT NULL DEFAULT 3,
                trailing_ceiling TEXT,
                status TEXT NOT NULL,
                halt_reason TEXT,
                ladder_version INTEGER NOT NULL DEFAULT 1,
                recenter_count INTEGER NOT NULL DEFAULT 0,
                trigger_started_at INTEGER,
                realized_pnl TEXT NOT NULL DEFAULT '0',
                consecutive_errors INTEGER NOT NULL DEFAULT 0,
                last_tick_at INTEGER,
                last_reconciled_at INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_grid_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER NOT NULL REFERENCES live_bots(id) ON DELETE CASCADE,
                ladder_version INTEGER NOT NULL,
                level_index INTEGER NOT NULL,
                buy_price TEXT NOT NULL,
                sell_price TEXT NOT NULL,
                state TEXT NOT NULL,
                buy_intent_id INTEGER,
                sell_intent_id INTEGER,
                held_quantity TEXT NOT NULL DEFAULT '0',
                cost_basis TEXT NOT NULL DEFAULT '0',
                updated_at INTEGER NOT NULL,
                UNIQUE (bot_id, ladder_version, level_index)
            );
            CREATE TABLE IF NOT EXISTS live_bot_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id INTEGER NOT NULL REFERENCES live_bots(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS live_pnl_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                mode TEXT NOT NULL,
                bot_id INTEGER,
                amount TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dry_run_orders (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                client_id TEXT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price TEXT NOT NULL,
                quantity TEXT NOT NULL,
                executed TEXT NOT NULL DEFAULT '0',
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dry_run_balances (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                asset TEXT NOT NULL,
                available TEXT NOT NULL,
                held TEXT NOT NULL,
                PRIMARY KEY (user_id, asset)
            );
            """
        )
        existing = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(live_bots)")
        }
        for column, definition in (
            ("stop_mode", "TEXT"),
            ("liquidation_intent_id", "INTEGER"),
            ("last_notice", "TEXT"),
        ):
            if column not in existing:
                self.connection.execute(
                    f"ALTER TABLE live_bots ADD COLUMN {column} {definition}"
                )
        self.connection.commit()

    # ------------------------------------------------------------------ meta
    def install_id(self) -> str:
        row = self.connection.execute(
            "SELECT value FROM app_meta WHERE key = 'install_id'"
        ).fetchone()
        if row:
            return str(row["value"])
        value = secrets.token_hex(3)
        self.connection.execute(
            "INSERT OR IGNORE INTO app_meta (key, value) VALUES ('install_id', ?)",
            (value,),
        )
        self.connection.commit()
        return self.install_id()

    # -------------------------------------------------------------- settings
    def settings(self, user_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM live_settings WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            self.connection.execute(
                "INSERT OR IGNORE INTO live_settings (user_id, limits_json, updated_at) "
                "VALUES (?, ?, ?)",
                (user_id, json.dumps(RiskLimits().to_dict()), now_ms()),
            )
            self.connection.commit()
            return self.settings(user_id)
        data = dict(row)
        data["live_enabled"] = bool(data["live_enabled"])
        data["kill_switch"] = bool(data["kill_switch"])
        data["limits"] = RiskLimits.from_dict(json.loads(data.pop("limits_json") or "{}"))
        return data

    def update_settings(self, user_id: int, **values: Any) -> dict[str, Any]:
        self.settings(user_id)
        if "limits" in values:
            values["limits_json"] = json.dumps(values.pop("limits").to_dict())
        for key in ("live_enabled", "kill_switch"):
            if key in values:
                values[key] = int(bool(values[key]))
        values["updated_at"] = now_ms()
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.connection.execute(
            f"UPDATE live_settings SET {assignments} WHERE user_id = ?",
            (*values.values(), user_id),
        )
        self.connection.commit()
        return self.settings(user_id)

    # -------------------------------------------------------------- previews
    def save_preview(self, preview: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO live_previews
                (id, user_id, mode, symbol, side, price, quantity, fingerprint,
                 allowed, risk_json, expires_at, created_at)
            VALUES (:id, :user_id, :mode, :symbol, :side, :price, :quantity,
                    :fingerprint, :allowed, :risk_json, :expires_at, :created_at)
            """,
            preview,
        )
        self.connection.commit()

    def get_preview(self, user_id: int, preview_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM live_previews WHERE id = ? AND user_id = ?",
            (preview_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    # --------------------------------------------------------------- intents
    def create_intent(
        self,
        user_id: int,
        mode: str,
        purpose: str,
        symbol: str,
        side: str,
        price: Decimal,
        quantity: Decimal,
        bot_id: int | None = None,
        slot_id: int | None = None,
        preview_id: str | None = None,
    ) -> dict[str, Any]:
        """Creates a durable intent and its unique client ID atomically. When
        a preview ID is supplied, it is consumed in the same transaction so a
        duplicate confirmation cannot create a second intent."""
        timestamp = now_ms()
        try:
            with self.connection:
                if preview_id is not None:
                    cursor = self.connection.execute(
                        "UPDATE live_previews SET consumed_at = ? "
                        "WHERE id = ? AND user_id = ? AND consumed_at IS NULL",
                        (timestamp, preview_id, user_id),
                    )
                    if cursor.rowcount != 1:
                        raise ValueError("This preview was already used")
                cursor = self.connection.execute(
                    """
                    INSERT INTO live_intents
                        (user_id, mode, preview_id, bot_id, slot_id, purpose, symbol,
                         side, price, quantity, state, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        user_id, mode, preview_id, bot_id, slot_id, purpose, symbol,
                        side, str(price), str(quantity), timestamp, timestamp,
                    ),
                )
                intent_id = int(cursor.lastrowid or 0)
                client_id = f"bb{self.install_id()}{intent_id:x}"
                if len(client_id) >= 32:
                    raise ValueError("Generated client order ID is too long")
                self.connection.execute(
                    "UPDATE live_intents SET client_id = ? WHERE id = ?",
                    (client_id, intent_id),
                )
                self._event(intent_id, user_id, "created", f"{purpose} {side} {quantity} {symbol} @ {price}")
        except sqlite3.IntegrityError as exc:
            raise ValueError("This preview was already used") from exc
        intent = self.get_intent(user_id, intent_id)
        assert intent is not None
        return intent

    def _event(self, intent_id: int, user_id: int, event: str, detail: str) -> None:
        self.connection.execute(
            "INSERT INTO live_intent_events (intent_id, user_id, event, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (intent_id, user_id, event, detail[:500], now_ms()),
        )

    def update_intent(
        self, intent: dict[str, Any], event: str, detail: str, **values: Any
    ) -> dict[str, Any]:
        values["updated_at"] = now_ms()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.connection:
            self.connection.execute(
                f"UPDATE live_intents SET {assignments} WHERE id = ?",
                (*[str(v) if isinstance(v, Decimal) else v for v in values.values()], intent["id"]),
            )
            self._event(intent["id"], intent["user_id"], event, detail)
        updated = self.get_intent(intent["user_id"], intent["id"])
        assert updated is not None
        return updated

    def get_intent(self, user_id: int, intent_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM live_intents WHERE id = ? AND user_id = ?",
            (intent_id, user_id),
        ).fetchone()
        return dict(row) if row else None

    def list_intents(
        self,
        user_id: int,
        mode: str | None = None,
        states: tuple[str, ...] | None = None,
        bot_id: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if mode is not None:
            clauses.append("mode = ?")
            params.append(mode)
        if states:
            clauses.append(f"state IN ({','.join('?' * len(states))})")
            params.extend(states)
        if bot_id is not None:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        rows = self.connection.execute(
            f"SELECT * FROM live_intents WHERE {' AND '.join(clauses)} "
            "ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def intents_in_states(self, states: tuple[str, ...]) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            f"SELECT * FROM live_intents WHERE state IN ({','.join('?' * len(states))})",
            states,
        ).fetchall()
        return [dict(row) for row in rows]

    def intent_events(self, user_id: int, intent_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT event, detail, created_at FROM live_intent_events "
            "WHERE intent_id = ? AND user_id = ? ORDER BY id",
            (intent_id, user_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def fill_times(self, user_id: int, intents: list[dict[str, Any]]) -> dict[int, int]:
        """Returns when each intent first reached its final executed quantity,
        using the order-status events recorded at each reconciliation."""
        targets = {
            int(intent["id"]): _dec(intent["executed_quantity"])
            for intent in intents
            if _dec(intent["executed_quantity"]) > 0
        }
        if not targets:
            return {}
        ids = list(targets)
        found: dict[int, int] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows = self.connection.execute(
                "SELECT intent_id, detail, created_at FROM live_intent_events "
                f"WHERE user_id = ? AND intent_id IN ({','.join('?' * len(chunk))}) ORDER BY id",
                (user_id, *chunk),
            ).fetchall()
            for row in rows:
                intent_id = int(row["intent_id"])
                if intent_id in found:
                    continue
                match = _EXECUTED_PATTERN.search(str(row["detail"]))
                if match and _dec(match.group(1)) >= targets[intent_id]:
                    found[intent_id] = int(row["created_at"])
        for intent in intents:
            intent_id = int(intent["id"])
            if intent_id in targets and intent_id not in found:
                found[intent_id] = int(intent["updated_at"])
        return found

    def open_order_count(self, user_id: int, mode: str) -> int:
        row = self.connection.execute(
            f"SELECT COUNT(*) AS n FROM live_intents WHERE user_id = ? AND mode = ? "
            f"AND state IN ({','.join('?' * len(ACTIVE_INTENT_STATES))})",
            (user_id, mode, *ACTIVE_INTENT_STATES),
        ).fetchone()
        return int(row["n"])

    def daily_order_count(self, user_id: int, mode: str) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) AS n FROM live_intents WHERE user_id = ? AND mode = ? "
            "AND submitted_at >= ?",
            (user_id, mode, utc_day_start_ms()),
        ).fetchone()
        return int(row["n"])

    def account_exposure(self, user_id: int, mode: str) -> Decimal:
        rows = self.connection.execute(
            f"SELECT price, quantity, executed_quantity FROM live_intents "
            f"WHERE user_id = ? AND mode = ? AND side = 'buy' "
            f"AND state IN ({','.join('?' * len(ACTIVE_INTENT_STATES))})",
            (user_id, mode, *ACTIVE_INTENT_STATES),
        ).fetchall()
        open_buys = sum(
            (_dec(r["price"]) * (_dec(r["quantity"]) - _dec(r["executed_quantity"])) for r in rows),
            Decimal(0),
        )
        held = self.connection.execute(
            """
            SELECT s.cost_basis FROM live_grid_slots s JOIN live_bots b ON b.id = s.bot_id
            WHERE b.user_id = ? AND b.mode = ? AND s.state IN ('holding', 'sell_open')
            """,
            (user_id, mode),
        ).fetchall()
        return open_buys + sum((_dec(r["cost_basis"]) for r in held), Decimal(0))

    # ------------------------------------------------------------------ P&L
    def record_pnl(self, user_id: int, mode: str, bot_id: int | None, amount: Decimal) -> None:
        self.connection.execute(
            "INSERT INTO live_pnl_events (user_id, mode, bot_id, amount, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, mode, bot_id, str(amount), now_ms()),
        )
        self.connection.commit()

    def daily_realized_pnl(self, user_id: int, mode: str) -> Decimal:
        rows = self.connection.execute(
            "SELECT amount FROM live_pnl_events WHERE user_id = ? AND mode = ? AND created_at >= ?",
            (user_id, mode, utc_day_start_ms()),
        ).fetchall()
        return sum((_dec(r["amount"]) for r in rows), Decimal(0))

    # ------------------------------------------------------------------ bots
    def create_bot(self, user_id: int, values: dict[str, Any]) -> dict[str, Any]:
        timestamp = now_ms()
        record = {
            **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in values.items()},
            "user_id": user_id,
            "status": "armed",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        columns = ", ".join(record)
        placeholders = ", ".join(f":{key}" for key in record)
        cursor = self.connection.execute(
            f"INSERT INTO live_bots ({columns}) VALUES ({placeholders})", record
        )
        bot_id = int(cursor.lastrowid or 0)
        self.bot_event(bot_id, user_id, "created", "Bot created in armed state")
        self.connection.commit()
        bot = self.get_bot(user_id, bot_id)
        assert bot is not None
        return bot

    def get_bot(self, user_id: int, bot_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM live_bots WHERE id = ? AND user_id = ?", (bot_id, user_id)
        ).fetchone()
        return dict(row) if row else None

    def list_bots(
        self, user_id: int | None = None, statuses: tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if statuses:
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM live_bots {where} ORDER BY id", params
        ).fetchall()
        return [dict(row) for row in rows]

    def update_bot(self, bot: dict[str, Any], **values: Any) -> dict[str, Any]:
        values["updated_at"] = now_ms()
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.connection.execute(
            f"UPDATE live_bots SET {assignments} WHERE id = ? AND user_id = ?",
            (*[str(v) if isinstance(v, Decimal) else v for v in values.values()], bot["id"], bot["user_id"]),
        )
        self.connection.commit()
        updated = self.get_bot(bot["user_id"], bot["id"])
        assert updated is not None
        return updated

    def bot_event(self, bot_id: int, user_id: int, event: str, detail: str) -> None:
        self.connection.execute(
            "INSERT INTO live_bot_events (bot_id, user_id, event, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (bot_id, user_id, event, detail[:500], now_ms()),
        )
        self.connection.commit()

    def bot_events(self, user_id: int, bot_id: int, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT event, detail, created_at FROM live_bot_events "
            "WHERE bot_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
            (bot_id, user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_bot(self, user_id: int, bot_id: int) -> None:
        self.connection.execute(
            "DELETE FROM live_bots WHERE id = ? AND user_id = ?", (bot_id, user_id)
        )
        self.connection.commit()

    # ----------------------------------------------------------------- slots
    def create_slots(
        self, bot_id: int, ladder_version: int, levels: list[tuple[Decimal, Decimal]]
    ) -> None:
        timestamp = now_ms()
        self.connection.executemany(
            """
            INSERT OR IGNORE INTO live_grid_slots
                (bot_id, ladder_version, level_index, buy_price, sell_price, state, updated_at)
            VALUES (?, ?, ?, ?, ?, 'idle', ?)
            """,
            [
                (bot_id, ladder_version, index, str(buy), str(sell), timestamp)
                for index, (buy, sell) in enumerate(levels)
            ],
        )
        self.connection.commit()

    def slots(self, bot_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM live_grid_slots WHERE bot_id = ? ORDER BY ladder_version, level_index",
            (bot_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_slot(self, slot: dict[str, Any], **values: Any) -> dict[str, Any]:
        values["updated_at"] = now_ms()
        assignments = ", ".join(f"{key} = ?" for key in values)
        self.connection.execute(
            f"UPDATE live_grid_slots SET {assignments} WHERE id = ?",
            (*[str(v) if isinstance(v, Decimal) else v for v in values.values()], slot["id"]),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM live_grid_slots WHERE id = ?", (slot["id"],)
        ).fetchone()
        return dict(row)

    def delete_idle_slots(self, bot_id: int, ladder_version: int) -> None:
        self.connection.execute(
            "DELETE FROM live_grid_slots WHERE bot_id = ? AND ladder_version = ? AND state = 'idle'",
            (bot_id, ladder_version),
        )
        self.connection.commit()

    # --------------------------------------------------------------- dry run
    def dry_run_balance(self, user_id: int, asset: str) -> tuple[Decimal, Decimal]:
        row = self.connection.execute(
            "SELECT available, held FROM dry_run_balances WHERE user_id = ? AND asset = ?",
            (user_id, asset),
        ).fetchone()
        if row is None:
            return Decimal(0), Decimal(0)
        return _dec(row["available"]), _dec(row["held"])

    def set_dry_run_balance(
        self, user_id: int, asset: str, available: Decimal, held: Decimal
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO dry_run_balances (user_id, asset, available, held) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, asset) DO UPDATE SET available = excluded.available,
                held = excluded.held
            """,
            (user_id, asset, str(available), str(held)),
        )
        self.connection.commit()

    def dry_run_balances(self, user_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT asset, available, held FROM dry_run_balances WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def save_dry_run_order(self, order: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO dry_run_orders
                (id, user_id, client_id, symbol, side, price, quantity, executed, status,
                 created_at, updated_at)
            VALUES (:id, :user_id, :client_id, :symbol, :side, :price, :quantity, :executed,
                    :status, :created_at, :updated_at)
            ON CONFLICT(id) DO UPDATE SET executed = excluded.executed,
                status = excluded.status, updated_at = excluded.updated_at
            """,
            order,
        )
        self.connection.commit()

    def dry_run_order(self, user_id: int, order_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM dry_run_orders WHERE user_id = ? AND (id = ? OR client_id = ?) "
            "ORDER BY created_at LIMIT 1",
            (user_id, order_id, order_id),
        ).fetchone()
        return dict(row) if row else None

    def dry_run_orders(
        self, user_id: int, symbol: str | None = None, active_only: bool = False
    ) -> list[dict[str, Any]]:
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if active_only:
            clauses.append("status IN ('open', 'partially_filled')")
        rows = self.connection.execute(
            f"SELECT * FROM dry_run_orders WHERE {' AND '.join(clauses)} ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
