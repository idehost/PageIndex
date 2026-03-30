"""
PageIndex Self-Hosted API Server — Production Grade
====================================================
Full-featured FastAPI server with:
  - PostgreSQL for persistent document & tree storage
  - Redis for caching trees, metadata, and search results
  - Railway Volume for persistent PDF file storage
  - LiteLLM proxy integration for all LLM calls
  - Connection pooling, health checks, graceful shutdown
  - API key authentication (optional)
  - Concurrency-limited background indexing
"""

import asyncio
import copy
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import (
    Depends, FastAPI, File, HTTPException, Query, Security, UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader

from config import AppConfig, load_config
from db import Database
from cache import Cache
from storage import FileStorage
from models import (
    DocumentListResponse, DocumentResponse, HealthResponse,
    IndexResponse, RAGRequest, RAGResponse, SearchRequest,
    SearchResponse, TreeResponse,
)

# ── Logging ───────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("pageindex")

# ── Global state ──────────────────────────────────────────────────

config: AppConfig = None
db: Database = None
cache: Cache = None
storage: FileStorage = None
indexing_semaphore: asyncio.Semaphore = None


# ── LiteLLM configuration ────────────────────────────────────────

def configure_litellm(cfg: AppConfig):
    """
    Configure LiteLLM to route all LLM calls through the Railway LiteLLM proxy.
    PageIndex uses litellm internally, so setting OPENAI_API_BASE routes
    all completion calls through your proxy.
    """
    proxy_url = cfg.llm.proxy_url
    if proxy_url:
        base_url = f"{proxy_url.rstrip('/')}"
        # If the URL doesn't end with /v1, add it
        if not base_url.endswith("/v1"):
            base_url = f"{base_url}/v1"
        os.environ["OPENAI_API_BASE"] = base_url
        if cfg.llm.proxy_key:
            os.environ["OPENAI_API_KEY"] = cfg.llm.proxy_key
        logger.info(f"LiteLLM proxy configured: {proxy_url}")
    else:
        logger.warning("No LITELLM_PROXY_URL — using direct LLM provider keys")


# ── App lifecycle ─────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global config, db, cache, storage, indexing_semaphore

    logger.info("=" * 60)
    logger.info("PageIndex API Server starting...")
    logger.info("=" * 60)

    # Load config
    config = load_config()

    # Configure LLM routing
    configure_litellm(config)

    # Initialize database
    db = Database(config.db)
    await db.connect()
    await db.initialize_schema()

    # Initialize cache
    cache = Cache(config.redis)
    await cache.connect()

    # Initialize file storage
    storage = FileStorage(config.storage)
    await storage.initialize()

    # Semaphore for concurrent indexing
    indexing_semaphore = asyncio.Semaphore(config.max_concurrent_indexing)

    logger.info("All services initialized. Server ready.")
    logger.info("=" * 60)

    yield

    # Shutdown
    logger.info("Shutting down...")
    await cache.disconnect()
    await db.disconnect()
    logger.info("Shutdown complete.")


# ── App init ──────────────────────────────────────────────────────

app = FastAPI(
    title="PageIndex Self-Hosted API",
    description=(
        "Vectorless, reasoning-based RAG — self-hosted on Railway "
        "with PostgreSQL, Redis, and LiteLLM integration."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Authentication ────────────────────────────────────────────────

api_key_header = APIKeyHeader(name="Authorization", auto_error=False)


async def verify_api_key(
    key: Optional[str] = Security(api_key_header),
) -> Optional[str]:
    """Verify API key if PAGEINDEX_API_KEY is set."""
    cfg = load_config()
    if not cfg.api_key:
        return None  # Auth disabled
    if not key:
        raise HTTPException(401, "Authorization header required")
    # Accept "Bearer <key>" or raw key
    token = key.replace("Bearer ", "").strip()
    if token != cfg.api_key:
        raise HTTPException(403, "Invalid API key")
    return token


# ── Health ────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Comprehensive health check across all services."""
    db_health = await db.health_check()
    redis_health = await cache.health_check()
    storage_health = storage.health_check()
    doc_counts = await db.get_document_count()

    overall = "healthy"
    if db_health.get("status") != "healthy":
        overall = "degraded"
    if storage_health.get("status") != "healthy":
        overall = "degraded"

    return HealthResponse(
        status=overall,
        litellm_proxy=config.llm.proxy_url or "not configured",
        database=db_health,
        redis=redis_health,
        storage=storage_health,
        documents=doc_counts,
    )


# ── Index a document ─────────────────────────────────────────────

@app.post("/v1/index", response_model=IndexResponse)
async def index_document(
    file: UploadFile = File(...),
    model: str = Query(
        default="gpt-4o-2024-11-20",
        description="LLM model for tree generation (must be available via LiteLLM proxy)",
    ),
    add_summary: bool = Query(default=True),
    add_node_id: bool = Query(default=True),
    add_description: bool = Query(default=True),
    max_pages_per_node: Optional[int] = Query(default=None),
    max_tokens_per_node: Optional[int] = Query(default=None),
    _auth: str = Depends(verify_api_key),
):
    """Upload a PDF/Markdown file and generate PageIndex tree structure."""

    # Validate extension
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in config.storage.allowed_extensions:
        raise HTTPException(
            400,
            f"File type '{ext}' not supported. "
            f"Accepted: {', '.join(config.storage.allowed_extensions)}",
        )

    # Check concurrent indexing limit
    processing_count = await db.get_processing_count()
    if processing_count >= config.max_concurrent_indexing:
        raise HTTPException(
            429,
            f"Too many documents processing ({processing_count}). "
            f"Max concurrent: {config.max_concurrent_indexing}. Try again later.",
        )

    # Read file content
    content = await file.read()
    if len(content) > config.storage.max_file_size_bytes:
        raise HTTPException(
            413,
            f"File too large ({len(content) / 1024 / 1024:.1f}MB). "
            f"Max: {config.storage.max_file_size_mb}MB",
        )

    # Store file persistently on volume
    doc_id = None
    try:
        # Create DB record first to get doc_id
        doc_id = await db.create_document(
            name=file.filename,
            file_path="",  # will update after storing
            file_size_bytes=len(content),
            model=model,
        )

        # Store file on volume
        file_path = await storage.store_file(doc_id, content, file.filename)

    except ValueError as e:
        if doc_id:
            await db.update_document_failed(doc_id, str(e))
        raise HTTPException(400, str(e))
    except Exception as e:
        if doc_id:
            await db.update_document_failed(doc_id, str(e))
        raise HTTPException(500, f"Failed to store file: {e}")

    # Launch background indexing
    asyncio.create_task(
        _background_index(
            doc_id=doc_id,
            file_path=file_path,
            ext=ext,
            model=model,
            opts={
                "if_add_node_summary": "yes" if add_summary else "no",
                "if_add_node_id": "yes" if add_node_id else "no",
                "if_add_doc_description": "yes" if add_description else "no",
                "max_page_num_each_node": max_pages_per_node,
                "max_token_num_each_node": max_tokens_per_node,
            },
        )
    )

    return IndexResponse(
        doc_id=doc_id,
        status="processing",
        name=file.filename,
        message=(
            f"Document submitted. Poll GET /v1/documents/{doc_id} for status. "
            f"Model: {model} via LiteLLM proxy."
        ),
    )


async def _background_index(
    doc_id: str,
    file_path: str,
    ext: str,
    model: str,
    opts: dict,
):
    """Background task: generate PageIndex tree with concurrency control."""
    async with indexing_semaphore:
        start_time = time.monotonic()
        try:
            logger.info(f"Indexing started: {doc_id} (model: {model})")

            if ext == ".pdf":
                from pageindex import page_index_main
                from pageindex.utils import ConfigLoader

                user_opt = {"model": model}
                user_opt.update({k: v for k, v in opts.items() if v is not None})
                opt = ConfigLoader().load(user_opt)

                # page_index_main is synchronous — run in executor
                loop = asyncio.get_event_loop()
                tree = await loop.run_in_executor(None, page_index_main, file_path, opt)
            else:
                from pageindex.page_index_md import md_to_tree

                tree = await md_to_tree(
                    md_path=file_path,
                    model=model,
                    if_add_node_summary=opts.get("if_add_node_summary", "yes") == "yes",
                    if_add_doc_description=opts.get("if_add_doc_description", "yes") == "yes",
                )

            elapsed_ms = int((time.monotonic() - start_time) * 1000)

            # Extract metadata from tree
            page_count = _extract_page_count(tree)
            description = _extract_description(tree)

            # Store in database
            await db.update_document_completed(
                doc_id=doc_id,
                tree=tree,
                page_count=page_count,
                description=description,
                processing_time_ms=elapsed_ms,
            )

            # Pre-cache the tree
            await cache.set_tree(doc_id, tree)
            await cache.invalidate_document(doc_id)

            logger.info(
                f"Indexing completed: {doc_id} "
                f"({page_count} pages, {elapsed_ms}ms)"
            )

        except Exception as e:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            error_msg = f"{type(e).__name__}: {str(e)}"
            await db.update_document_failed(doc_id, error_msg)
            await cache.invalidate_document(doc_id)
            logger.error(f"Indexing failed: {doc_id} — {error_msg}")


# ── Document endpoints ────────────────────────────────────────────

@app.get("/v1/documents/{doc_id}", response_model=DocumentResponse)
async def get_document(doc_id: str, _auth: str = Depends(verify_api_key)):
    """Get document metadata and status."""
    # Check cache first
    cached = await cache.get_document(doc_id)
    if cached:
        return DocumentResponse(**cached)

    doc = await db.get_document(doc_id)
    if not doc:
        raise HTTPException(404, f"Document {doc_id} not found")

    response = DocumentResponse(
        doc_id=doc["id"],
        name=doc["name"],
        status=doc["status"],
        page_count=doc.get("page_count"),
        description=doc.get("description"),
        file_size_bytes=doc.get("file_size_bytes"),
        model_used=doc.get("model_used"),
        error_message=doc.get("error_message"),
        processing_time_ms=doc.get("processing_time_ms"),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )

    # Cache completed/failed docs longer, processing docs briefly
    await cache.set_document(doc_id, response.model_dump(mode="json"))
    return response


@app.get("/v1/documents", response_model=DocumentListResponse)
async def list_documents(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: Optional[str] = Query(default=None),
    _auth: str = Depends(verify_api_key),
):
    """List documents with pagination."""
    docs = await db.list_documents(limit=limit, offset=offset, status=status)
    counts = await db.get_document_count()

    return DocumentListResponse(
        documents=[
            DocumentResponse(
                doc_id=d["id"],
                name=d["name"],
                status=d["status"],
                page_count=d.get("page_count"),
                file_size_bytes=d.get("file_size_bytes"),
                model_used=d.get("model_used"),
                created_at=d.get("created_at"),
                updated_at=d.get("updated_at"),
            )
            for d in docs
        ],
        total=counts.get("total", 0),
        limit=limit,
        offset=offset,
    )


@app.delete("/v1/documents/{doc_id}")
async def delete_document(doc_id: str, _auth: str = Depends(verify_api_key)):
    """Delete a document, its tree, cached data, and stored files."""
    doc = await db.get_document(doc_id)
    if not doc:
        raise HTTPException(404, f"Document {doc_id} not found")

    # Delete from all stores
    await db.delete_document(doc_id)
    await cache.invalidate_document(doc_id)
    await storage.delete_file(doc_id)

    return {"deleted": True, "doc_id": doc_id}


# ── Tree endpoint ─────────────────────────────────────────────────

@app.get("/v1/tree/{doc_id}", response_model=TreeResponse)
async def get_tree(doc_id: str, _auth: str = Depends(verify_api_key)):
    """Get the PageIndex tree structure for a document."""
    # Check cache
    cached_tree = await cache.get_tree(doc_id)
    if cached_tree:
        return TreeResponse(doc_id=doc_id, tree=cached_tree)

    # Load from database
    tree = await db.get_document_tree(doc_id)
    if tree is None:
        doc = await db.get_document(doc_id)
        if doc is None:
            raise HTTPException(404, f"Document {doc_id} not found")
        raise HTTPException(
            400,
            f"Document status is '{doc['status']}'. Tree available when 'completed'.",
        )

    # Cache for next time
    await cache.set_tree(doc_id, tree)
    return TreeResponse(doc_id=doc_id, tree=tree)


# ── Search endpoint ───────────────────────────────────────────────

@app.post("/v1/search", response_model=SearchResponse)
async def tree_search(req: SearchRequest, _auth: str = Depends(verify_api_key)):
    """
    Reasoning-based tree search over an indexed document.
    LLM calls route through LiteLLM proxy → your configured models.
    """
    model = req.model or config.llm.default_model

    # Check search cache
    cached_result = await cache.get_search_result(req.doc_id, req.query, model)
    if cached_result:
        return SearchResponse(**cached_result, cached=True)

    # Get tree (from cache or DB)
    tree = await _get_tree_or_404(req.doc_id)

    # Build search prompt
    tree_for_search = _remove_fields(copy.deepcopy(tree), ["text"])
    search_prompt = _build_search_prompt(req.query, tree_for_search)

    # Call LLM via LiteLLM (routes through proxy)
    import litellm
    try:
        response = await litellm.acompletion(
            model=model,
            messages=[{"role": "user", "content": search_prompt}],
            temperature=0,
            timeout=config.llm.request_timeout,
            num_retries=config.llm.max_retries,
        )
    except Exception as e:
        raise HTTPException(502, f"LLM call failed via {config.llm.proxy_url}: {e}")

    result_text = response.choices[0].message.content.strip()
    search_result = _parse_llm_json(result_text)

    # Build response with context extraction
    node_map = _build_node_map(tree)
    retrieved_nodes = []
    context_parts = []

    for node_id in search_result.get("node_list", []):
        if node_id in node_map:
            node = node_map[node_id]
            retrieved_nodes.append({
                "node_id": node_id,
                "title": node.get("title", ""),
                "start_index": node.get("start_index"),
                "end_index": node.get("end_index"),
            })
            if "text" in node and node["text"]:
                context_parts.append(node["text"])

    result_data = {
        "doc_id": req.doc_id,
        "query": req.query,
        "thinking": search_result.get("thinking", ""),
        "retrieved_nodes": retrieved_nodes,
        "context": "\n\n".join(context_parts),
    }

    # Cache the search result
    await cache.set_search_result(req.doc_id, req.query, model, result_data)

    return SearchResponse(**result_data)


# ── RAG endpoint ──────────────────────────────────────────────────

@app.post("/v1/rag", response_model=RAGResponse)
async def rag_query(req: RAGRequest, _auth: str = Depends(verify_api_key)):
    """
    Full RAG pipeline: tree search → context extraction → answer generation.
    All LLM calls route through your LiteLLM proxy.
    """
    model = req.model or config.llm.default_model

    # Step 1: Tree search (may use cache)
    search_req = SearchRequest(doc_id=req.doc_id, query=req.query, model=model)
    search_result = await tree_search(search_req)

    if not search_result.context:
        return RAGResponse(
            doc_id=req.doc_id,
            query=req.query,
            answer="No relevant context found in the document for this query.",
            thinking=search_result.thinking,
            retrieved_nodes=search_result.retrieved_nodes,
            context="",
        )

    # Step 2: Generate answer
    system = req.system_prompt or (
        "You are a precise document analyst. Answer based only on the provided context. "
        "Cite page numbers or section titles when available."
    )
    answer_prompt = (
        f"Answer this question based on the context below.\n\n"
        f"Question: {req.query}\n\n"
        f"Context:\n{search_result.context}\n\n"
        f"Provide a clear, concise answer grounded in the context."
    )

    import litellm
    try:
        response = await litellm.acompletion(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": answer_prompt},
            ],
            temperature=0,
            timeout=config.llm.request_timeout,
            num_retries=config.llm.max_retries,
        )
    except Exception as e:
        raise HTTPException(502, f"LLM answer generation failed: {e}")

    answer = response.choices[0].message.content.strip()

    return RAGResponse(
        doc_id=req.doc_id,
        query=req.query,
        answer=answer,
        thinking=search_result.thinking,
        retrieved_nodes=search_result.retrieved_nodes,
        context=search_result.context,
        cached=search_result.cached,
    )


# ── Cache management ─────────────────────────────────────────────

@app.get("/v1/cache/stats")
async def cache_stats(_auth: str = Depends(verify_api_key)):
    """Get Redis cache statistics."""
    return await cache.get_stats()


@app.post("/v1/cache/invalidate/{doc_id}")
async def invalidate_cache(doc_id: str, _auth: str = Depends(verify_api_key)):
    """Invalidate all cached data for a document."""
    await cache.invalidate_document(doc_id)
    return {"invalidated": True, "doc_id": doc_id}


# ── Helpers ───────────────────────────────────────────────────────

async def _get_tree_or_404(doc_id: str) -> dict | list:
    """Get tree from cache or DB, raise 404/400 if not available."""
    # Try cache
    tree = await cache.get_tree(doc_id)
    if tree:
        return tree

    # Try database
    tree = await db.get_document_tree(doc_id)
    if tree is None:
        doc = await db.get_document(doc_id)
        if doc is None:
            raise HTTPException(404, f"Document {doc_id} not found")
        raise HTTPException(400, f"Document status: '{doc['status']}'. Must be 'completed'.")

    # Cache for next time
    await cache.set_tree(doc_id, tree)
    return tree


def _build_search_prompt(query: str, tree_structure: dict | list) -> str:
    return f"""You are given a question and a tree structure of a document.
Each node contains a node id, node title, and a corresponding summary.
Your task is to find all nodes that are likely to contain the answer to the question.

Question: {query}

Document tree structure:
{json.dumps(tree_structure, indent=2)}

Please reply in the following JSON format:
{{
    "thinking": "<Your thinking process on which nodes are relevant to the question>",
    "node_list": ["node_id_1", "node_id_2", ..., "node_id_n"]
}}
Directly return the final JSON structure. Do not output anything else."""


def _parse_llm_json(text: str) -> dict:
    """Parse JSON from LLM response, handling code blocks."""
    text = text.strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try extracting from markdown code block
    if "```json" in text:
        json_str = text.split("```json")[1].split("```")[0].strip()
        return json.loads(json_str)
    if "```" in text:
        json_str = text.split("```")[1].split("```")[0].strip()
        return json.loads(json_str)
    raise ValueError(f"Could not parse LLM JSON response: {text[:300]}")


def _remove_fields(obj, fields: list):
    """Recursively remove fields from tree structure."""
    if isinstance(obj, dict):
        return {k: _remove_fields(v, fields) for k, v in obj.items() if k not in fields}
    elif isinstance(obj, list):
        return [_remove_fields(item, fields) for item in obj]
    return obj


def _build_node_map(tree, node_map=None) -> dict:
    """Flatten tree into {node_id: node} mapping."""
    if node_map is None:
        node_map = {}
    if isinstance(tree, list):
        for item in tree:
            _build_node_map(item, node_map)
    elif isinstance(tree, dict):
        if "node_id" in tree:
            node_map[tree["node_id"]] = tree
        for child in tree.get("nodes", []):
            _build_node_map(child, node_map)
    return node_map


def _extract_page_count(tree) -> int:
    """Extract total page count from tree structure."""
    max_page = 0
    def _walk(node):
        nonlocal max_page
        if isinstance(node, dict):
            for key in ("end_index", "start_index", "page_index"):
                if key in node and isinstance(node[key], int):
                    max_page = max(max_page, node[key])
            for child in node.get("nodes", []):
                _walk(child)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
    _walk(tree)
    return max_page


def _extract_description(tree) -> str:
    """Extract document description from tree structure."""
    if isinstance(tree, list) and tree:
        root = tree[0]
    elif isinstance(tree, dict):
        root = tree
    else:
        return ""
    return (
        root.get("description", "")
        or root.get("summary", "")
        or root.get("prefix_summary", "")
        or ""
    )[:500]


# ── Entry point ───────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    cfg = load_config()
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=cfg.port,
        workers=cfg.workers,
        log_level=cfg.log_level,
    )
