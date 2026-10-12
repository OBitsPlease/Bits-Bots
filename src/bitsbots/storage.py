import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from .models import Candle, PaperBot, PaperPosition, Signal, User


class Storage:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _inserted_id(cursor: sqlite3.Cursor) -> int:
        if cursor.lastrowid is None:
            raise sqlite3.DatabaseError("Insert completed without a row ID")
        return cursor.lastrowid

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL,
                period INTEGER NOT NULL,
                timestamp INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                PRIMARY KEY (symbol, period, timestamp)
            );
            CREATE INDEX IF NOT EXISTS candles_lookup
                ON candles(symbol, period, timestamp DESC);
            CREATE TABLE IF NOT EXISTS signals (
                symbol TEXT NOT NULL,
                generated_at INTEGER NOT NULL,
                score REAL NOT NULL,
                price REAL NOT NULL,
                action TEXT NOT NULL,
                reasons TEXT NOT NULL,
                history_quality TEXT NOT NULL,
                PRIMARY KEY (symbol, generated_at)
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
            CREATE TABLE IF NOT EXISTS paper_bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                status TEXT NOT NULL,
                entry_score REAL NOT NULL,
                max_allocation REAL NOT NULL,
                lower_price REAL,
                upper_price REAL,
                grid_slices INTEGER,
                grid_stop_price REAL,
                last_price REAL,
                created_at INTEGER NOT NULL,
                UNIQUE(user_id, name),
                UNIQUE(user_id, symbol)
            );
            CREATE INDEX IF NOT EXISTS paper_bots_user
                ON paper_bots(user_id, status);
            CREATE TABLE IF NOT EXISTS paper_account (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                cash REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paper_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                quantity REAL NOT NULL,
                entry_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                target_price REAL NOT NULL,
                highest_price REAL NOT NULL,
                opened_at INTEGER NOT NULL,
                status TEXT NOT NULL,
                closed_at INTEGER,
                exit_price REAL,
                exit_reason TEXT,
                realized_pnl REAL,
                entry_reasons TEXT NOT NULL,
                bot_id INTEGER,
                strategy TEXT NOT NULL DEFAULT 'Multi-TF',
                grid_level REAL
            );
            CREATE TABLE IF NOT EXISTS exchange_credentials (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                access_key TEXT NOT NULL,
                encrypted_secret TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS grid_bot_pnl_history (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                recorded_at INTEGER NOT NULL,
                total_pnl REAL NOT NULL,
                grid_profit REAL NOT NULL,
                unrealized REAL NOT NULL,
                PRIMARY KEY (user_id, symbol, recorded_at)
            );
            -- NOTE: "symbol" stores a real bot's stable bot_id (a hex
            -- identifier) when available, not always a market symbol -
            -- see the "label" column added below for the human-readable
            -- market pair to display alongside it.
            CREATE INDEX IF NOT EXISTS grid_bot_pnl_history_lookup
                ON grid_bot_pnl_history(user_id, symbol, recorded_at DESC);
            CREATE TABLE IF NOT EXISTS learner_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                strategy TEXT NOT NULL,
                market TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('champion', 'challenger', 'retired')),
                settings_json TEXT NOT NULL,
                parent_id INTEGER REFERENCES learner_versions(id),
                created_at INTEGER NOT NULL,
                retired_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS learner_versions_scope
                ON learner_versions(user_id, strategy, market, role);
            CREATE UNIQUE INDEX IF NOT EXISTS learner_one_champion
                ON learner_versions(user_id, strategy, market)
                WHERE role = 'champion';
            CREATE TABLE IF NOT EXISTS learner_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                version_id INTEGER NOT NULL REFERENCES learner_versions(id) ON DELETE CASCADE,
                market TEXT NOT NULL,
                started_at INTEGER,
                ended_at INTEGER,
                completed_trades INTEGER NOT NULL,
                net_return REAL NOT NULL,
                max_drawdown REAL NOT NULL,
                objective REAL NOT NULL,
                eligible INTEGER NOT NULL,
                reason TEXT NOT NULL,
                evaluated_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS learner_evaluations_scope
                ON learner_evaluations(user_id, version_id, evaluated_at DESC);
            CREATE TABLE IF NOT EXISTS learner_promotions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                strategy TEXT NOT NULL,
                market TEXT NOT NULL,
                prior_version_id INTEGER NOT NULL REFERENCES learner_versions(id),
                promoted_version_id INTEGER NOT NULL REFERENCES learner_versions(id),
                reason TEXT NOT NULL,
                promoted_at INTEGER NOT NULL
            );
            """
        )
        self._ensure_column("paper_bots", "lower_price", "REAL")
        self._ensure_column("paper_bots", "upper_price", "REAL")
        had_label_column = self._table_has_column("grid_bot_pnl_history", "label")
        self._ensure_column("grid_bot_pnl_history", "label", "TEXT")
        if not had_label_column:
            # Rows recorded before this migration were keyed by market
            # symbol alone (the old, now-abandoned per-symbol reconstruction
            # model). Under the new per-bot model the "symbol" column holds
            # a real bot_id instead, so that old data is no longer
            # meaningful here and would otherwise linger forever as bogus
            # "bots" (e.g. every manually-traded market) in the P&L chart.
            self.connection.execute("DELETE FROM grid_bot_pnl_history")
        self._ensure_column("paper_bots", "grid_slices", "INTEGER")
        self._ensure_column("paper_bots", "grid_stop_price", "REAL")
        self._ensure_column("paper_bots", "last_price", "REAL")
        self._ensure_column("paper_positions", "bot_id", "INTEGER")
        self._ensure_column(
            "paper_positions", "strategy", "TEXT NOT NULL DEFAULT 'Multi-TF'"
        )
        self._ensure_column("paper_positions", "grid_level", "REAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS exchange_accounts (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                exchange TEXT NOT NULL,
                access_key TEXT NOT NULL,
                encrypted_secret TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, exchange)
            );
            """
        )
        # One-time move of legacy single-exchange NonKYC credentials. The
        # legacy rows are removed so no secret remains in two places.
        self.connection.execute(
            """
            INSERT OR IGNORE INTO exchange_accounts
                (user_id, exchange, access_key, encrypted_secret, created_at, updated_at)
            SELECT user_id, 'nonkyc', access_key, encrypted_secret, created_at, updated_at
            FROM exchange_credentials
            """
        )
        self.connection.execute("DELETE FROM exchange_credentials")
        self.connection.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        if not self._table_has_column(table, column):
            self.connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
            )

    def _table_has_column(self, table: str, column: str) -> bool:
        columns = {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})")
        }
        return column in columns

    def user_count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"])

    def create_user(
        self, email: str, display_name: str, password_hash: str
    ) -> User:
        created_at = int(time.time() * 1000)
        try:
            cursor = self.connection.execute(
                """
                INSERT INTO users (email, display_name, password_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (email.strip().lower(), display_name.strip(), password_hash, created_at),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("An account with that email already exists") from exc
        return User(
            id=self._inserted_id(cursor),
            email=email.strip().lower(),
            display_name=display_name.strip(),
            created_at=created_at,
        )

    def user_with_password(self, email: str) -> tuple[User, str] | None:
        row = self.connection.execute(
            """
            SELECT id, email, display_name, created_at, password_hash
            FROM users WHERE email = ? COLLATE NOCASE AND is_active = 1
            """,
            (email.strip(),),
        ).fetchone()
        if row is None:
            return None
        user = User(
            id=int(row["id"]),
            email=str(row["email"]),
            display_name=str(row["display_name"]),
            created_at=int(row["created_at"]),
        )
        return user, str(row["password_hash"])

    def get_user(self, user_id: int) -> User | None:
        row = self.connection.execute(
            """
            SELECT id, email, display_name, created_at
            FROM users WHERE id = ? AND is_active = 1
            """,
            (user_id,),
        ).fetchone()
        return User(**dict(row)) if row else None

    def list_user_ids(self) -> list[int]:
        rows = self.connection.execute(
            "SELECT id FROM users WHERE is_active = 1"
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def update_password_hash(self, user_id: int, password_hash: str) -> None:
        self.connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )
        self.connection.commit()

    def save_session(
        self, token_hash: str, user_id: int, created_at: int, expires_at: int
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO sessions (token_hash, user_id, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (token_hash, user_id, created_at, expires_at),
        )
        self.connection.commit()

    def user_for_session(self, token_hash: str, now: int) -> User | None:
        self.connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        row = self.connection.execute(
            """
            SELECT u.id, u.email, u.display_name, u.created_at
            FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ? AND s.expires_at > ? AND u.is_active = 1
            """,
            (token_hash, now),
        ).fetchone()
        self.connection.commit()
        return User(**dict(row)) if row else None

    def delete_session(self, token_hash: str) -> None:
        self.connection.execute(
            "DELETE FROM sessions WHERE token_hash = ?", (token_hash,)
        )
        self.connection.commit()

    def initialize_account(self, user_id: int, starting_cash: float) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO paper_account (user_id, cash) VALUES (?, ?)",
            (user_id, starting_cash),
        )
        self.connection.commit()

    def create_bot(
        self,
        user_id: int,
        name: str,
        symbol: str,
        strategy: str,
        risk_level: str,
        entry_score: float,
        max_allocation: float,
        lower_price: float | None = None,
        upper_price: float | None = None,
        grid_slices: int | None = None,
        grid_stop_price: float | None = None,
    ) -> PaperBot:
        created_at = int(time.time() * 1000)
        try:
            cursor = self.connection.execute(
                """
                INSERT INTO paper_bots
                    (user_id, name, symbol, strategy, risk_level, status,
                     entry_score, max_allocation, lower_price, upper_price,
                     grid_slices, grid_stop_price, created_at)
                VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    name.strip(),
                    symbol.upper(),
                    strategy,
                    risk_level,
                    entry_score,
                    max_allocation,
                    lower_price,
                    upper_price,
                    grid_slices,
                    grid_stop_price,
                    created_at,
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "A bot with that name or market already exists"
            ) from exc
        return PaperBot(
            id=self._inserted_id(cursor),
            name=name.strip(),
            symbol=symbol.upper(),
            strategy=strategy,
            risk_level=risk_level,
            status="running",
            entry_score=entry_score,
            max_allocation=max_allocation,
            created_at=created_at,
            lower_price=lower_price,
            upper_price=upper_price,
            grid_slices=grid_slices,
            grid_stop_price=grid_stop_price,
        )

    def list_bots(self, user_id: int, running_only: bool = False) -> list[PaperBot]:
        status_filter = " AND status = 'running'" if running_only else ""
        rows = self.connection.execute(
            f"""
            SELECT id, name, symbol, strategy, risk_level, status,
                   entry_score, max_allocation, created_at, lower_price,
                   upper_price, grid_slices, grid_stop_price, last_price
            FROM paper_bots WHERE user_id = ?{status_filter}
            ORDER BY created_at
            """,
            (user_id,),
        ).fetchall()
        return [PaperBot(**dict(row)) for row in rows]

    def update_bot_status(
        self, user_id: int, bot_id: int, status: str
    ) -> PaperBot | None:
        self.connection.execute(
            """
            UPDATE paper_bots
            SET status = ?,
                last_price = CASE
                    WHEN ? = 'running' AND strategy IN ('Grid', 'Adaptive Grid')
                    THEN NULL
                    ELSE last_price
                END
            WHERE id = ? AND user_id = ?
            """,
            (status, status, bot_id, user_id),
        )
        self.connection.commit()
        row = self.connection.execute(
            """
            SELECT id, name, symbol, strategy, risk_level, status,
                   entry_score, max_allocation, created_at, lower_price,
                   upper_price, grid_slices, grid_stop_price, last_price
            FROM paper_bots WHERE id = ? AND user_id = ?
            """,
            (bot_id, user_id),
        ).fetchone()
        return PaperBot(**dict(row)) if row else None

    def update_bot_last_price(self, user_id: int, bot_id: int, price: float) -> None:
        self.connection.execute(
            "UPDATE paper_bots SET last_price = ? WHERE id = ? AND user_id = ?",
            (price, bot_id, user_id),
        )
        self.connection.commit()

    def update_bot_grid_range(
        self,
        user_id: int,
        bot_id: int,
        lower_price: float,
        upper_price: float,
        grid_stop_price: float,
        last_price: float,
    ) -> None:
        self.connection.execute(
            """
            UPDATE paper_bots
            SET lower_price = ?, upper_price = ?, grid_stop_price = ?,
                last_price = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                lower_price,
                upper_price,
                grid_stop_price,
                last_price,
                bot_id,
                user_id,
            ),
        )
        self.connection.commit()

    def delete_bot(self, user_id: int, bot_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM paper_bots WHERE id = ? AND user_id = ?",
            (bot_id, user_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def update_bot_learned_settings(
        self, user_id: int, bot_id: int, settings: dict[str, object]
    ) -> None:
        """Apply only the paper bot fields admitted by the learner's safe policy."""
        self.connection.execute(
            """
            UPDATE paper_bots SET entry_score = ?, max_allocation = ?,
                grid_slices = COALESCE(?, grid_slices),
                grid_stop_price = COALESCE(?, grid_stop_price)
            WHERE id = ? AND user_id = ?
            """,
            (
                settings["entry_score"],
                settings["max_allocation"],
                settings.get("grid_slices"),
                settings.get("grid_stop_price"),
                bot_id,
                user_id,
            ),
        )
        self.connection.commit()

    def learner_versions(
        self, user_id: int, strategy: str, market: str
    ) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT * FROM learner_versions
            WHERE user_id = ? AND strategy = ? AND market = ?
            ORDER BY created_at, id
            """,
            (user_id, strategy, market),
        ).fetchall()

    def create_learner_version(
        self,
        user_id: int,
        strategy: str,
        market: str,
        role: str,
        settings: dict[str, object],
        parent_id: int | None = None,
        created_at: int | None = None,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO learner_versions
                (user_id, strategy, market, role, settings_json, parent_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                strategy,
                market,
                role,
                json.dumps(settings, sort_keys=True, separators=(",", ":")),
                parent_id,
                created_at or int(time.time() * 1000),
            ),
        )
        self.connection.commit()
        return self._inserted_id(cursor)

    def record_learner_evaluation(
        self, user_id: int, version_id: int, market: str, result: dict[str, object]
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO learner_evaluations
                (user_id, version_id, market, started_at, ended_at,
                 completed_trades, net_return, max_drawdown, objective,
                 eligible, reason, evaluated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, version_id, market, result.get("started_at"),
                result.get("ended_at"), result["completed_trades"],
                result["net_return"], result["max_drawdown"], result["objective"],
                int(bool(result["eligible"])), result["reason"],
                int(time.time() * 1000),
            ),
        )
        self.connection.commit()

    def promote_learner_version(
        self,
        user_id: int,
        strategy: str,
        market: str,
        prior_id: int,
        promoted_id: int,
        reason: str,
    ) -> None:
        now = int(time.time() * 1000)
        with self.connection:
            self.connection.execute(
                "UPDATE learner_versions SET role='retired', retired_at=? "
                "WHERE id=? AND user_id=? AND role='champion'",
                (now, prior_id, user_id),
            )
            self.connection.execute(
                "UPDATE learner_versions SET role='champion' "
                "WHERE id=? AND user_id=? AND role='challenger'",
                (promoted_id, user_id),
            )
            self.connection.execute(
                """
                INSERT INTO learner_promotions
                    (user_id, strategy, market, prior_version_id,
                     promoted_version_id, reason, promoted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, strategy, market, prior_id, promoted_id, reason, now),
            )

    def learner_snapshot(self, user_id: int) -> dict[str, object]:
        versions = self.connection.execute(
            """
            SELECT v.*, e.completed_trades, e.net_return, e.max_drawdown,
                   e.objective, e.eligible, e.reason, e.started_at, e.ended_at,
                   e.evaluated_at
            FROM learner_versions v
            LEFT JOIN learner_evaluations e ON e.id = (
                SELECT id FROM learner_evaluations
                WHERE version_id=v.id AND user_id=?
                ORDER BY evaluated_at DESC, id DESC LIMIT 1
            )
            WHERE v.user_id=? AND v.role IN ('champion','challenger')
            ORDER BY v.strategy, v.market, v.role, v.id
            """,
            (user_id, user_id),
        ).fetchall()
        promotions = self.connection.execute(
            """
            SELECT * FROM learner_promotions WHERE user_id=?
            ORDER BY promoted_at DESC, id DESC LIMIT 50
            """,
            (user_id,),
        ).fetchall()
        def converted(row: sqlite3.Row) -> dict[str, object]:
            item = dict(row)
            item["settings"] = json.loads(str(item.pop("settings_json")))
            return item
        return {
            "paper_only": True,
            "versions": [converted(row) for row in versions],
            "promotions": [dict(row) for row in promotions],
        }

    def save_candles(self, candles: Iterable[Candle]) -> None:
        self.connection.executemany(
            """
            INSERT INTO candles
                (symbol, period, timestamp, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, period, timestamp) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume=excluded.volume
            """,
            (
                (
                    candle.symbol,
                    candle.period,
                    candle.timestamp,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                )
                for candle in candles
            ),
        )
        self.connection.commit()

    def get_candles(self, symbol: str, period: int, limit: int = 1000) -> list[Candle]:
        rows = self.connection.execute(
            """
            SELECT symbol, period, timestamp, open, high, low, close, volume
            FROM candles WHERE symbol = ? AND period = ?
            ORDER BY timestamp DESC LIMIT ?
            """,
            (symbol, period, limit),
        ).fetchall()
        return [Candle(**dict(row)) for row in reversed(rows)]

    def save_signal(self, signal: Signal) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO signals
                (symbol, generated_at, score, price, action, reasons, history_quality)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.symbol,
                signal.generated_at,
                signal.score,
                signal.price,
                signal.action,
                json.dumps(signal.reasons),
                signal.history_quality,
            ),
        )
        self.connection.commit()

    def cash(self, user_id: int) -> float:
        row = self.connection.execute(
            "SELECT cash FROM paper_account WHERE user_id = ?", (user_id,)
        ).fetchone()
        return float(row["cash"]) if row else 0.0

    def set_cash(self, user_id: int, cash: float) -> None:
        self.connection.execute(
            "UPDATE paper_account SET cash = ? WHERE user_id = ?", (cash, user_id)
        )
        self.connection.commit()

    def save_exchange_credentials(
        self,
        user_id: int,
        access_key: str,
        encrypted_secret: str,
        exchange: str = "nonkyc",
    ) -> None:
        now = int(time.time() * 1000)
        self.connection.execute(
            """
            INSERT INTO exchange_accounts
                (user_id, exchange, access_key, encrypted_secret, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, exchange) DO UPDATE SET
                access_key = excluded.access_key,
                encrypted_secret = excluded.encrypted_secret,
                updated_at = excluded.updated_at
            """,
            (user_id, exchange, access_key, encrypted_secret, now, now),
        )
        self.connection.commit()

    def get_exchange_credentials(
        self, user_id: int, exchange: str = "nonkyc"
    ) -> tuple[str, str] | None:
        row = self.connection.execute(
            """
            SELECT access_key, encrypted_secret FROM exchange_accounts
            WHERE user_id = ? AND exchange = ?
            """,
            (user_id, exchange),
        ).fetchone()
        if row is None:
            return None
        return str(row["access_key"]), str(row["encrypted_secret"])

    def delete_exchange_credentials(
        self, user_id: int, exchange: str = "nonkyc"
    ) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM exchange_accounts WHERE user_id = ? AND exchange = ?",
            (user_id, exchange),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def record_grid_bot_pnl(
        self, user_id: int, points: Iterable[dict], min_interval_ms: int = 240_000
    ) -> None:
        """Appends one P&L data point per real bot (falling back to symbol
        for entries with no bot_id, e.g. "other activity"), skipping keys
        that already have a point within ``min_interval_ms`` so frequent
        polling (e.g. every 20s) doesn't flood the history table."""
        now = int(time.time() * 1000)
        for point in points:
            key = str(point.get("bot_id") or point["symbol"])
            label = str(point.get("symbol") or key)
            last = self.connection.execute(
                """
                SELECT recorded_at FROM grid_bot_pnl_history
                WHERE user_id = ? AND symbol = ?
                ORDER BY recorded_at DESC LIMIT 1
                """,
                (user_id, key),
            ).fetchone()
            if last is not None and now - int(last["recorded_at"]) < min_interval_ms:
                continue
            self.connection.execute(
                """
                INSERT OR REPLACE INTO grid_bot_pnl_history
                    (user_id, symbol, recorded_at, total_pnl, grid_profit, unrealized, label)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    key,
                    now,
                    float(point["total_pnl"]),
                    float(point["grid_profit"]),
                    float(point["unrealized"]),
                    label,
                ),
            )
        self.connection.commit()

    def get_grid_bot_pnl_history(
        self, user_id: int, since_ms: int | None = None, limit_per_symbol: int = 500
    ) -> list[dict]:
        since_clause = "AND recorded_at >= ?" if since_ms is not None else ""
        params: tuple = (user_id, since_ms) if since_ms is not None else (user_id,)
        rows = self.connection.execute(
            f"""
            SELECT symbol, label, recorded_at, total_pnl, grid_profit, unrealized
            FROM grid_bot_pnl_history
            WHERE user_id = ? {since_clause}
            ORDER BY symbol, recorded_at ASC
            """,
            params,
        ).fetchall()
        history: list[dict] = [
            {
                "bot_id": str(row["symbol"]),
                "symbol": str(row["label"] or row["symbol"]),
                "recorded_at": int(row["recorded_at"]),
                "total_pnl": float(row["total_pnl"]),
                "grid_profit": float(row["grid_profit"]),
                "unrealized": float(row["unrealized"]),
            }
            for row in rows
        ]
        if limit_per_symbol:
            by_symbol: dict[str, list[dict]] = {}
            for point in history:
                by_symbol.setdefault(point["bot_id"], []).append(point)
            history = [
                point
                for points in by_symbol.values()
                for point in points[-limit_per_symbol:]
            ]
        return history

    def open_positions(self, user_id: int) -> list[PaperPosition]:
        rows = self.connection.execute(
            """
            SELECT id, symbol, quantity, entry_price, stop_price, target_price,
                   highest_price, opened_at, status, closed_at, exit_price,
                   exit_reason, realized_pnl, entry_reasons, bot_id, strategy,
                   grid_level
            FROM paper_positions
            WHERE user_id = ? AND status = 'open' ORDER BY opened_at
            """,
            (user_id,),
        ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def all_positions(self, user_id: int, limit: int = 100) -> list[PaperPosition]:
        rows = self.connection.execute(
            """
            SELECT id, symbol, quantity, entry_price, stop_price, target_price,
                   highest_price, opened_at, status, closed_at, exit_price,
                   exit_reason, realized_pnl, entry_reasons, bot_id, strategy,
                   grid_level
            FROM paper_positions
            WHERE user_id = ? ORDER BY opened_at DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [self._row_to_position(row) for row in rows]

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> PaperPosition:
        data = dict(row)
        data["entry_reasons"] = json.loads(data["entry_reasons"])
        return PaperPosition(**data)

    def insert_position(self, user_id: int, position: PaperPosition) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO paper_positions
                (user_id, symbol, quantity, entry_price, stop_price, target_price,
                 highest_price, opened_at, status, entry_reasons, bot_id,
                 strategy, grid_level)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                position.symbol,
                position.quantity,
                position.entry_price,
                position.stop_price,
                position.target_price,
                position.highest_price,
                position.opened_at,
                position.status,
                json.dumps(position.entry_reasons),
                position.bot_id,
                position.strategy,
                position.grid_level,
            ),
        )
        self.connection.commit()
        return self._inserted_id(cursor)

    def update_position(self, user_id: int, position: PaperPosition) -> None:
        self.connection.execute(
            """
            UPDATE paper_positions SET
                stop_price = ?, highest_price = ?, status = ?, closed_at = ?,
                exit_price = ?, exit_reason = ?, realized_pnl = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                position.stop_price,
                position.highest_price,
                position.status,
                position.closed_at,
                position.exit_price,
                position.exit_reason,
                position.realized_pnl,
                position.id,
                user_id,
            ),
        )
        self.connection.commit()
