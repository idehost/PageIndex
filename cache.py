"""
Redis caching layer.
Provides transparent caching for tree structures, document metadata,
and search results with configurable TTLs.
"""

import hashlib
import json
import logging
from typing import Optional

import redis.asyncio as aioredis

from config import RedisConfig

logger = logging.getLogger("pageindex.cache")


class Cache:
    """Async Redis cache manager."""

    def __init__(self, config: RedisConfig):
        self.config = config
        self._redis: Optional[aioredis.Redis] = None
        self._available = False

    async def connect(self):
        """Initialize Redis connection pool."""
        try:
            self._redis = aioredis.from_url(
                self.config.url,
                max_connections=self.config.max_connections,
                socket_timeout=self.config.socket_timeout,
                socket_connect_timeout=self.config.socket_connect_timeout,
                retry_on_timeout=self.config.retry_on_timeout,
                decode_responses=True,
            )
            # Verify connection
            await self._redis.ping()
            self._available = True
            logger.info(f"Redis connected: {self._mask_url(self.config.url)}")
        except Exception as e:
            self._available = False
            logger.warning(f"Redis unavailable (cache disabled): {e}")

    async def disconnect(self):
        """Close Redis connections."""
        if self._redis:
            await self._redis.aclose()
            logger.info("Redis disconnected")

    async def health_check(self) -> dict:
        """Check Redis connectivity."""
        if not self._available:
            return {"status": "disabled", "message": "Redis not configured or unavailable"}
        try:
            info = await self._redis.info("memory")
            return {
                "status": "healthy",
                "used_memory_human": info.get("used_memory_human", "unknown"),
                "connected_clients": (await self._redis.info("clients")).get(
                    "connected_clients", 0
                ),
            }
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

    # ── Key builders ──────────────────────────────────────────────

    def _key(self, namespace: str, identifier: str) -> str:
        return f"{self.config.key_prefix}{namespace}:{identifier}"

    def _search_key(self, doc_id: str, query: str, model: str) -> str:
        query_hash = hashlib.sha256(f"{query}:{model}".encode()).hexdigest()[:16]
        return self._key("search", f"{doc_id}:{query_hash}")

    # ── Generic get/set with graceful degradation ──────────────────

    async def _get(self, key: str) -> Optional[str]:
        if not self._available:
            return None
        try:
            return await self._redis.get(key)
        except Exception as e:
            logger.warning(f"Redis GET failed for {key}: {e}")
            return None

    async def _set(self, key: str, value: str, ttl: int) -> bool:
        if not self._available:
            return False
        try:
            await self._redis.setex(key, ttl, value)
            return True
        except Exception as e:
            logger.warning(f"Redis SET failed for {key}: {e}")
            return False

    async def _delete(self, key: str) -> bool:
        if not self._available:
            return False
        try:
            await self._redis.delete(key)
            return True
        except Exception as e:
            logger.warning(f"Redis DELETE failed for {key}: {e}")
            return False

    async def _delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching a pattern. Returns count deleted."""
        if not self._available:
            return 0
        try:
            count = 0
            async for key in self._redis.scan_iter(match=pattern, count=100):
                await self._redis.delete(key)
                count += 1
            return count
        except Exception as e:
            logger.warning(f"Redis DELETE pattern failed for {pattern}: {e}")
            return 0

    # ── Document metadata cache ───────────────────────────────────

    async def get_document(self, doc_id: str) -> Optional[dict]:
        raw = await self._get(self._key("doc", doc_id))
        if raw:
            logger.debug(f"Cache HIT: doc:{doc_id}")
            return json.loads(raw)
        return None

    async def set_document(self, doc_id: str, data: dict):
        serialized = json.dumps(data, default=str)
        await self._set(self._key("doc", doc_id), serialized, self.config.ttl_status)

    async def invalidate_document(self, doc_id: str):
        """Invalidate document metadata + tree + search caches."""
        await self._delete(self._key("doc", doc_id))
        await self._delete(self._key("tree", doc_id))
        await self._delete_pattern(f"{self.config.key_prefix}search:{doc_id}:*")

    # ── Tree structure cache ──────────────────────────────────────

    async def get_tree(self, doc_id: str) -> Optional[dict | list]:
        raw = await self._get(self._key("tree", doc_id))
        if raw:
            logger.debug(f"Cache HIT: tree:{doc_id}")
            return json.loads(raw)
        return None

    async def set_tree(self, doc_id: str, tree: dict | list):
        serialized = json.dumps(tree)
        # Trees can be large — only cache if under 5MB serialized
        if len(serialized) < 5 * 1024 * 1024:
            await self._set(self._key("tree", doc_id), serialized, self.config.ttl_tree)
        else:
            logger.info(f"Tree too large to cache: {doc_id} ({len(serialized)} bytes)")

    # ── Search result cache ───────────────────────────────────────

    async def get_search_result(
        self, doc_id: str, query: str, model: str
    ) -> Optional[dict]:
        key = self._search_key(doc_id, query, model)
        raw = await self._get(key)
        if raw:
            logger.debug(f"Cache HIT: search for '{query[:50]}' on {doc_id}")
            return json.loads(raw)
        return None

    async def set_search_result(
        self, doc_id: str, query: str, model: str, result: dict
    ):
        key = self._search_key(doc_id, query, model)
        serialized = json.dumps(result)
        await self._set(key, serialized, self.config.ttl_search)

    # ── Cache stats ───────────────────────────────────────────────

    async def get_stats(self) -> dict:
        """Get cache statistics."""
        if not self._available:
            return {"available": False}
        try:
            info = await self._redis.info("stats")
            keyspace = await self._redis.info("keyspace")
            return {
                "available": True,
                "hits": info.get("keyspace_hits", 0),
                "misses": info.get("keyspace_misses", 0),
                "hit_rate": round(
                    info.get("keyspace_hits", 0)
                    / max(
                        info.get("keyspace_hits", 0) + info.get("keyspace_misses", 0), 1
                    )
                    * 100,
                    1,
                ),
                "keys": keyspace,
            }
        except Exception:
            return {"available": True, "error": "Could not fetch stats"}

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _mask_url(url: str) -> str:
        """Mask password in Redis URL for logging."""
        if "@" in url:
            parts = url.split("@")
            return f"redis://***@{parts[-1]}"
        return url
