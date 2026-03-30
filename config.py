"""
Centralized configuration management.
All settings loaded from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class DatabaseConfig:
    host: str = ""
    port: int = 5432
    name: str = "pageindex"
    user: str = "postgres"
    password: str = ""
    pool_min: int = 2
    pool_max: int = 10
    statement_timeout: int = 60_000  # ms

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    @property
    def async_dsn(self) -> str:
        return (
            f"postgresql+asyncpg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}"
        )


@dataclass(frozen=True)
class RedisConfig:
    url: str = "redis://localhost:6379/0"
    max_connections: int = 20
    socket_timeout: float = 5.0
    socket_connect_timeout: float = 5.0
    retry_on_timeout: bool = True
    ttl_tree: int = 3600        # 1 hour cache for tree structures
    ttl_status: int = 30        # 30s cache for document status
    ttl_search: int = 1800      # 30 min cache for search results
    key_prefix: str = "pi:"


@dataclass(frozen=True)
class StorageConfig:
    volume_path: str = "/data/pdfs"
    max_file_size_mb: int = 100
    allowed_extensions: tuple = (".pdf", ".md", ".markdown")

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024


@dataclass(frozen=True)
class LiteLLMConfig:
    proxy_url: str = ""
    proxy_key: str = ""
    default_model: str = "gpt-4o-2024-11-20"
    request_timeout: int = 120
    max_retries: int = 3


@dataclass(frozen=True)
class AppConfig:
    port: int = 8000
    workers: int = 1
    log_level: str = "info"
    api_key: str = ""              # optional auth key for this service
    max_concurrent_indexing: int = 5
    db: DatabaseConfig = field(default_factory=DatabaseConfig)
    redis: RedisConfig = field(default_factory=RedisConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    llm: LiteLLMConfig = field(default_factory=LiteLLMConfig)


def load_config() -> AppConfig:
    """Load configuration from environment variables."""

    db = DatabaseConfig(
        host=os.getenv("PGHOST", os.getenv("DATABASE_HOST", "localhost")),
        port=int(os.getenv("PGPORT", os.getenv("DATABASE_PORT", "5432"))),
        name=os.getenv("PGDATABASE", os.getenv("DATABASE_NAME", "pageindex")),
        user=os.getenv("PGUSER", os.getenv("DATABASE_USER", "postgres")),
        password=os.getenv("PGPASSWORD", os.getenv("DATABASE_PASSWORD", "")),
        pool_min=int(os.getenv("DB_POOL_MIN", "2")),
        pool_max=int(os.getenv("DB_POOL_MAX", "10")),
    )

    redis = RedisConfig(
        url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        max_connections=int(os.getenv("REDIS_MAX_CONNECTIONS", "20")),
        ttl_tree=int(os.getenv("REDIS_TTL_TREE", "3600")),
        ttl_status=int(os.getenv("REDIS_TTL_STATUS", "30")),
        ttl_search=int(os.getenv("REDIS_TTL_SEARCH", "1800")),
    )

    storage = StorageConfig(
        volume_path=os.getenv("STORAGE_VOLUME_PATH", "/data/pdfs"),
        max_file_size_mb=int(os.getenv("STORAGE_MAX_FILE_SIZE_MB", "100")),
    )

    llm = LiteLLMConfig(
        proxy_url=os.getenv("LITELLM_PROXY_URL", "https://llm.up.railway.app"),
        proxy_key=os.getenv("LITELLM_PROXY_KEY", ""),
        default_model=os.getenv("PAGEINDEX_DEFAULT_MODEL", "gpt-4o-2024-11-20"),
        request_timeout=int(os.getenv("LLM_REQUEST_TIMEOUT", "120")),
        max_retries=int(os.getenv("LLM_MAX_RETRIES", "3")),
    )

    return AppConfig(
        port=int(os.getenv("PORT", "8000")),
        workers=int(os.getenv("WORKERS", "1")),
        log_level=os.getenv("LOG_LEVEL", "info"),
        api_key=os.getenv("PAGEINDEX_API_KEY", ""),
        max_concurrent_indexing=int(os.getenv("MAX_CONCURRENT_INDEXING", "5")),
        db=db,
        redis=redis,
        storage=storage,
        llm=llm,
    )
