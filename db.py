"""
PostgreSQL database layer.
Handles connection pooling, schema migrations, and all document CRUD operations.
Uses asyncpg for high-performance async Postgres access.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from config import DatabaseConfig

logger = logging.getLogger("pageindex.db")

# ── Schema ────────────────────────────────────────────────────────
SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- Schema versioning
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Documents table
CREATE TABLE IF NOT EXISTS documents (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    file_path       TEXT,
    file_size_bytes BIGINT,
    page_count      INTEGER,
    description     TEXT,
    tree            JSONB,
    model_used      TEXT,
    error_message   TEXT,
    processing_time_ms  BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_created_at ON documents(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_name ON documents(name);

-- GIN index on tree JSONB for tree queries
CREATE INDEX IF NOT EXISTS idx_documents_tree ON documents USING GIN(tree jsonb_path_ops);

-- Updated_at trigger
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

DROP TRIGGER IF EXISTS update_documents_updated_at ON documents;
CREATE TRIGGER update_documents_updated_at
    BEFORE UPDATE ON documents
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Insert schema version
INSERT INTO schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;
"""


class Database:
    """Async PostgreSQL database manager with connection pooling."""

    def __init__(self, config: DatabaseConfig):
        self.config = config
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        """Initialize connection pool."""
        try:
            self._pool = await asyncpg.create_pool(
                host=self.config.host,
                port=self.config.port,
                database=self.config.name,
                user=self.config.user,
                password=self.config.password,
                min_size=self.config.pool_min,
                max_size=self.config.pool_max,
                command_timeout=self.config.statement_timeout / 1000,
                server_settings={
                    "statement_timeout": str(self.config.statement_timeout),
                },
            )
            logger.info(
                f"Database pool created: {self.config.host}:{self.config.port}"
                f"/{self.config.name} (pool: {self.config.pool_min}-{self.config.pool_max})"
            )
        except Exception as e:
            logger.error(f"Failed to create database pool: {e}")
            raise

    async def disconnect(self):
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
            logger.info("Database pool closed")

    async def initialize_schema(self):
        """Run schema migrations."""
        async with self._pool.acquire() as conn:
            await conn.execute(SCHEMA_SQL)
            logger.info(f"Database schema initialized (version {SCHEMA_VERSION})")

    async def health_check(self) -> dict:
        """Check database connectivity and pool stats."""
        try:
            async with self._pool.acquire() as conn:
                result = await conn.fetchval("SELECT 1")
                pool_size = self._pool.get_size()
                pool_free = self._pool.get_idle_size()
                return {
                    "status": "healthy",
                    "pool_size": pool_size,
                    "pool_free": pool_free,
                    "pool_used": pool_size - pool_free,
                }
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

    # ── Document CRUD ─────────────────────────────────────────────

    def _generate_doc_id(self) -> str:
        """Generate a unique document ID."""
        return f"pi-{uuid.uuid4().hex[:20]}"

    async def create_document(
        self,
        name: str,
        file_path: str,
        file_size_bytes: int,
        model: str,
    ) -> str:
        """Create a new document record. Returns doc_id."""
        doc_id = self._generate_doc_id()
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO documents (id, name, file_path, file_size_bytes, model_used, status)
                VALUES ($1, $2, $3, $4, $5, 'processing')
                """,
                doc_id, name, file_path, file_size_bytes, model,
            )
        logger.info(f"Document created: {doc_id} ({name})")
        return doc_id

    async def update_document_completed(
        self,
        doc_id: str,
        tree: dict | list,
        page_count: int,
        description: str,
        processing_time_ms: int,
    ):
        """Mark document as completed with tree data."""
        tree_json = json.dumps(tree)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE documents
                SET status = 'completed',
                    tree = $2::jsonb,
                    page_count = $3,
                    description = $4,
                    processing_time_ms = $5
                WHERE id = $1
                """,
                doc_id, tree_json, page_count, description, processing_time_ms,
            )
        logger.info(f"Document completed: {doc_id} ({page_count} pages, {processing_time_ms}ms)")

    async def update_document_failed(self, doc_id: str, error: str):
        """Mark document as failed."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE documents SET status = 'failed', error_message = $2
                WHERE id = $1
                """,
                doc_id, error[:2000],  # truncate long errors
            )
        logger.warning(f"Document failed: {doc_id} — {error[:200]}")

    async def get_document(self, doc_id: str) -> Optional[dict]:
        """Get document metadata (without tree)."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, name, status, file_path, file_size_bytes,
                       page_count, description, model_used, error_message,
                       processing_time_ms, created_at, updated_at
                FROM documents WHERE id = $1
                """,
                doc_id,
            )
        if row is None:
            return None
        return dict(row)

    async def get_document_tree(self, doc_id: str) -> Optional[dict]:
        """Get the full tree structure for a document."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT tree, status FROM documents WHERE id = $1",
                doc_id,
            )
        if row is None:
            return None
        if row["status"] != "completed":
            return None
        tree_data = row["tree"]
        if isinstance(tree_data, str):
            return json.loads(tree_data)
        return tree_data

    async def list_documents(
        self,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
    ) -> list[dict]:
        """List documents with pagination and optional status filter."""
        async with self._pool.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    """
                    SELECT id, name, status, page_count, file_size_bytes,
                           model_used, created_at, updated_at
                    FROM documents
                    WHERE status = $1
                    ORDER BY created_at DESC
                    LIMIT $2 OFFSET $3
                    """,
                    status, limit, offset,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, name, status, page_count, file_size_bytes,
                           model_used, created_at, updated_at
                    FROM documents
                    ORDER BY created_at DESC
                    LIMIT $1 OFFSET $2
                    """,
                    limit, offset,
                )
        return [dict(r) for r in rows]

    async def delete_document(self, doc_id: str) -> bool:
        """Delete a document. Returns True if found and deleted."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM documents WHERE id = $1 RETURNING file_path",
                doc_id,
            )
        return row is not None

    async def get_document_count(self) -> dict:
        """Get document counts by status."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT status, COUNT(*) as count FROM documents GROUP BY status"
            )
        counts = {r["status"]: r["count"] for r in rows}
        counts["total"] = sum(counts.values())
        return counts

    async def get_processing_count(self) -> int:
        """Get number of currently processing documents."""
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COUNT(*) FROM documents WHERE status = 'processing'"
            )
