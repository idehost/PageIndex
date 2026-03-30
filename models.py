"""
API request and response models.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── Document models ───────────────────────────────────────────────

class DocumentResponse(BaseModel):
    doc_id: str
    name: str
    status: str
    page_count: Optional[int] = None
    description: Optional[str] = None
    file_size_bytes: Optional[int] = None
    model_used: Optional[str] = None
    error_message: Optional[str] = None
    processing_time_ms: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class IndexResponse(BaseModel):
    doc_id: str
    status: str
    name: str
    message: str


class DocumentListResponse(BaseModel):
    documents: list[DocumentResponse]
    total: int
    limit: int
    offset: int


# ── Tree models ───────────────────────────────────────────────────

class TreeResponse(BaseModel):
    doc_id: str
    tree: dict | list


# ── Search models ─────────────────────────────────────────────────

class SearchRequest(BaseModel):
    doc_id: str
    query: str = Field(..., min_length=1, max_length=5000)
    model: Optional[str] = None


class RetrievedNode(BaseModel):
    node_id: str
    title: str
    start_index: Optional[int] = None
    end_index: Optional[int] = None


class SearchResponse(BaseModel):
    doc_id: str
    query: str
    thinking: str
    retrieved_nodes: list[RetrievedNode]
    context: str
    cached: bool = False


# ── RAG models ────────────────────────────────────────────────────

class RAGRequest(BaseModel):
    doc_id: str
    query: str = Field(..., min_length=1, max_length=5000)
    model: Optional[str] = None
    system_prompt: Optional[str] = None


class RAGResponse(BaseModel):
    doc_id: str
    query: str
    answer: str
    thinking: str
    retrieved_nodes: list[RetrievedNode]
    context: str
    cached: bool = False


# ── Health models ─────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    service: str = "pageindex"
    version: str = "1.0.0"
    litellm_proxy: str
    database: dict
    redis: dict
    storage: dict
    documents: dict
