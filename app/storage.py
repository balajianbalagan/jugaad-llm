from __future__ import annotations

import aiosqlite
import hashlib
import secrets
from datetime import UTC, datetime, timedelta


class RequestStore:
    def __init__(self, database_path: str):
        self.database_path = database_path

    async def initialize(self) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("""CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                model TEXT NOT NULL, provider_model TEXT, status_code INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL, prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
                estimated_cost REAL DEFAULT 0, fallback_used INTEGER DEFAULT 0, error TEXT)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS dashboard_sessions (
                token_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL)""")
            await db.execute("""CREATE TABLE IF NOT EXISTS provider_status (
                provider TEXT PRIMARY KEY, status TEXT NOT NULL, last_checked_at TEXT NOT NULL,
                last_success_at TEXT, last_error TEXT)""")
            await db.commit()

    async def log(self, **record: object) -> None:
        columns = ", ".join(record)
        values = ", ".join(f":{key}" for key in record)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(f"INSERT INTO requests ({columns}) VALUES ({values})", record)
            await db.commit()

    async def stats(self) -> dict[str, object]:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            totals = await (await db.execute("""SELECT COUNT(*) requests,
                COALESCE(SUM(total_tokens), 0) tokens, COALESCE(SUM(estimated_cost), 0) cost,
                COALESCE(SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END), 0) errors,
                COALESCE(SUM(fallback_used), 0) fallbacks FROM requests""")).fetchone()
            models = await (await db.execute("""SELECT model, COUNT(*) requests,
                COALESCE(SUM(total_tokens), 0) tokens, COALESCE(SUM(estimated_cost), 0) cost
                FROM requests GROUP BY model ORDER BY requests DESC""")).fetchall()
        return {"requests": totals["requests"], "tokens": totals["tokens"], "estimated_cost_usd": totals["cost"],
                "errors": totals["errors"], "fallbacks": totals["fallbacks"], "by_model": [dict(row) for row in models]}

    async def create_session(self, hours: int) -> str:
        token = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        expires = now + timedelta(hours=hours)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("INSERT INTO dashboard_sessions VALUES (?, ?, ?, ?)",
                (self._hash(token), now.isoformat(), expires.isoformat(), now.isoformat()))
            await db.commit()
        return token

    async def valid_session(self, token: str | None) -> bool:
        if not token:
            return False
        now = datetime.now(UTC)
        async with aiosqlite.connect(self.database_path) as db:
            row = await (await db.execute("SELECT expires_at FROM dashboard_sessions WHERE token_hash = ?", (self._hash(token),))).fetchone()
            if not row or datetime.fromisoformat(row[0]) <= now:
                await db.execute("DELETE FROM dashboard_sessions WHERE token_hash = ?", (self._hash(token),))
                await db.commit()
                return False
            await db.execute("UPDATE dashboard_sessions SET last_seen_at = ? WHERE token_hash = ?", (now.isoformat(), self._hash(token)))
            await db.commit()
            return True

    async def revoke_session(self, token: str | None) -> None:
        if token:
            async with aiosqlite.connect(self.database_path) as db:
                await db.execute("DELETE FROM dashboard_sessions WHERE token_hash = ?", (self._hash(token),))
                await db.commit()

    async def update_provider(self, provider: str, status: str, error: str | None = None) -> None:
        now = datetime.now(UTC).isoformat()
        success = now if status == "healthy" else None
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("""INSERT INTO provider_status (provider, status, last_checked_at, last_success_at, last_error)
                VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider) DO UPDATE SET status=excluded.status,
                last_checked_at=excluded.last_checked_at, last_success_at=COALESCE(excluded.last_success_at, provider_status.last_success_at), last_error=excluded.last_error""",
                (provider, status, now, success, error))
            await db.commit()

    async def provider_states(self) -> dict[str, dict[str, str | None]]:
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("SELECT * FROM provider_status")).fetchall()
        return {row["provider"]: dict(row) for row in rows}

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()
