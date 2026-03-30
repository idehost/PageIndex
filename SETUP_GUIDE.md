# PageIndex Production Deployment on Railway

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        Railway Project                               │
│                                                                      │
│  ┌────────────────────┐     ┌──────────────────┐                    │
│  │  PageIndex API     │────▶│  LiteLLM Proxy   │──▶ OpenAI / etc   │
│  │  FastAPI :8000     │     │  :4000           │                    │
│  │                    │     │  llm.up.railway  │                    │
│  └──┬──────────┬──────┘     └──────────────────┘                    │
│     │          │                                                     │
│     ▼          ▼                                                     │
│  ┌──────┐  ┌──────┐   ┌────────────┐                               │
│  │ PG   │  │Redis │   │  Volume    │                                │
│  │      │  │      │   │  /data/pdfs│                                │
│  │trees │  │cache │   │  PDF files │                                │
│  │meta  │  │TTLs  │   │  persistent│                                │
│  └──────┘  └──────┘   └────────────┘                                │
└──────────────────────────────────────────────────────────────────────┘
```

## What lives where

| Data              | Storage       | Survives redeploy? | Survives service delete? |
|-------------------|---------------|--------------------|--------------------------|
| Tree JSON indexes | PostgreSQL    | Yes                | Yes (separate service)   |
| Document metadata | PostgreSQL    | Yes                | Yes                      |
| Uploaded PDFs     | Railway Volume| Yes                | No (bound to service)    |
| Hot tree cache    | Redis (1h TTL)| Yes (until TTL)    | No                       |
| Search results    | Redis (30m)   | Yes (until TTL)    | No                       |
| Doc status        | Redis (30s)   | Yes (until TTL)    | No                       |

---

## Step-by-step setup

### 1. Fork the PageIndex repo

```bash
git clone https://github.com/YOUR_USERNAME/PageIndex.git
cd PageIndex
```

### 2. Add production files to the repo root

Copy all these files into the root of your forked repo:

```
PageIndex/
├── pageindex/           # existing PageIndex source
├── cookbook/             # existing
├── run_pageindex.py     # existing
├── requirements.txt     # REPLACE with production version
├── server.py            # NEW — FastAPI server
├── config.py            # NEW — configuration management
├── db.py                # NEW — PostgreSQL layer
├── cache.py             # NEW — Redis caching
├── storage.py           # NEW — Volume file storage
├── models.py            # NEW — API schemas
├── Dockerfile           # NEW — container build
├── railway.toml         # NEW — Railway config
└── .env.example         # NEW — env var template
```

### 3. Push to GitHub

```bash
git add -A
git commit -m "Add production Railway deployment with PG/Redis/Volume"
git push origin main
```

### 4. Add PostgreSQL to your Railway project

1. Open your Railway project (where LiteLLM already runs)
2. Click **"+ New"** → **"Database"** → **"PostgreSQL"**
3. Railway creates the service with auto-generated credentials
4. Note: Railway auto-exposes `PGHOST`, `PGPORT`, etc. as service variables

### 5. Add Redis to your Railway project

1. Click **"+ New"** → **"Database"** → **"Redis"**
2. Railway creates the service with `REDIS_URL` variable

### 6. Deploy PageIndex service

1. Click **"+ New"** → **"GitHub Repo"**
2. Select your forked PageIndex repo
3. Railway detects the Dockerfile and starts building

### 7. Attach a Volume

1. Click the PageIndex service
2. Go to **Settings** → **Volumes**
3. Click **"+ New Volume"**
4. Mount path: `/data/pdfs`
5. Size: start with 1GB, scale as needed

### 8. Configure environment variables

Click PageIndex service → **Variables** tab → Add these:

```
# LiteLLM connection
LITELLM_PROXY_URL = https://llm.up.railway.app
LITELLM_PROXY_KEY = sk-your-litellm-key

# PostgreSQL (use Railway variable references)
PGHOST = ${{Postgres.PGHOST}}
PGPORT = ${{Postgres.PGPORT}}
PGDATABASE = ${{Postgres.PGDATABASE}}
PGUSER = ${{Postgres.PGUSER}}
PGPASSWORD = ${{Postgres.PGPASSWORD}}

# Redis (use Railway variable reference)
REDIS_URL = ${{Redis.REDIS_URL}}

# Model default
PAGEINDEX_DEFAULT_MODEL = gpt-4o-2024-11-20

# Storage
STORAGE_VOLUME_PATH = /data/pdfs
```

> **Important:** The `${{ServiceName.VAR}}` syntax is Railway's variable
> referencing. It injects the actual value at deploy time, keeping credentials
> out of your code.

### 9. Enable public networking

PageIndex service → **Settings** → **Networking**:
- Click **"Generate Domain"** for a public URL
- Or add a custom domain

### 10. Verify deployment

```bash
curl https://your-pageindex.up.railway.app/health
```

Expected response:
```json
{
  "status": "healthy",
  "service": "pageindex",
  "version": "1.0.0",
  "litellm_proxy": "https://llm.up.railway.app",
  "database": {
    "status": "healthy",
    "pool_size": 2,
    "pool_free": 2,
    "pool_used": 0
  },
  "redis": {
    "status": "healthy",
    "used_memory_human": "1.02M",
    "connected_clients": 1
  },
  "storage": {
    "status": "healthy",
    "path": "/data/pdfs",
    "total_gb": 1.0,
    "free_gb": 0.98
  },
  "documents": {
    "total": 0
  }
}
```

---

## API reference

### POST /v1/index
Upload and index a document.

```bash
curl -X POST https://pageindex.up.railway.app/v1/index \
  -H "Authorization: Bearer your-api-key" \
  -F "file=@./annual-report.pdf" \
  -F "model=gpt-4o"
```

### GET /v1/documents/{doc_id}
Check processing status.

### GET /v1/documents
List all documents (paginated).

### GET /v1/tree/{doc_id}
Get tree JSON. Served from Redis cache when available.

### POST /v1/search
Reasoning-based tree search (retrieval only). Results cached in Redis.

```bash
curl -X POST https://pageindex.up.railway.app/v1/search \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{"doc_id": "pi-abc123", "query": "What is the revenue?", "model": "gpt-4o"}'
```

### POST /v1/rag
Full pipeline: search → context → answer.

```bash
curl -X POST https://pageindex.up.railway.app/v1/rag \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key" \
  -d '{"doc_id": "pi-abc123", "query": "Summarize financial results"}'
```

### DELETE /v1/documents/{doc_id}
Delete document from PG, Redis, and Volume.

### GET /v1/cache/stats
Redis cache hit/miss statistics.

### POST /v1/cache/invalidate/{doc_id}
Force-clear cached data for a document.

---

## How LiteLLM integration works

The `server.py` sets two environment variables at startup:

```python
os.environ["OPENAI_API_BASE"] = "https://llm.up.railway.app/v1"
os.environ["OPENAI_API_KEY"]  = "<your proxy key>"
```

Since PageIndex uses `litellm` internally for all LLM calls, and LiteLLM
checks `OPENAI_API_BASE` first, every call routes through your proxy.

This means you can use any model your LiteLLM proxy supports:

```bash
# OpenAI via proxy
curl -X POST .../v1/rag -d '{"doc_id":"...", "query":"...", "model":"gpt-4o"}'

# Claude via proxy
curl -X POST .../v1/rag -d '{"doc_id":"...", "query":"...", "model":"anthropic/claude-sonnet-4-20250514"}'

# Local model via proxy
curl -X POST .../v1/rag -d '{"doc_id":"...", "query":"...", "model":"ollama/llama3"}'
```

---

## Data flow walkthrough

### Indexing (POST /v1/index)

```
Client uploads PDF
  → FastAPI validates (size, type)
  → File stored on Railway Volume (/data/pdfs/{shard}/{doc_id}/)
  → Metadata row created in PostgreSQL (status: processing)
  → Background task starts (concurrency-limited)
    → PageIndex reads PDF from Volume
    → LLM calls go through LiteLLM proxy
    → Tree JSON written to PostgreSQL (jsonb column)
    → Tree pre-cached in Redis (1h TTL)
    → Status updated to "completed"
```

### Query (POST /v1/rag)

```
Client sends query + doc_id
  → Check Redis for cached search result (30m TTL)
  → If cache miss:
    → Load tree from Redis cache (1h TTL)
    → If Redis miss: load from PostgreSQL, re-cache
    → Build search prompt with tree structure
    → LLM reasons over tree (via LiteLLM proxy)
    → Extract context from matched nodes
    → Cache search result in Redis
  → Generate answer with context (via LiteLLM proxy)
  → Return answer + sources + reasoning trace
```

---

## Production checklist

- [ ] PostgreSQL added to Railway project
- [ ] Redis added to Railway project
- [ ] Volume attached at `/data/pdfs`
- [ ] All env vars configured with Railway variable references
- [ ] Public domain generated
- [ ] Health check returns "healthy" for all services
- [ ] Test indexing: upload a PDF and verify tree generation
- [ ] Test RAG: query the indexed document
- [ ] Set PAGEINDEX_API_KEY for auth (optional but recommended)
- [ ] Monitor: check Railway metrics for memory/CPU after first indexing

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Health check shows DB "unhealthy" | PG credentials wrong | Verify `${{Postgres.PGHOST}}` reference |
| Redis "disabled" | REDIS_URL not set | Add `${{Redis.REDIS_URL}}` to variables |
| Volume "not writable" | Volume not attached | Settings → Volumes → attach at `/data/pdfs` |
| "Too many documents processing" (429) | Hit concurrent limit | Wait or increase `MAX_CONCURRENT_INDEXING` |
| LLM call failed (502) | Proxy URL wrong | Verify `LITELLM_PROXY_URL` matches your service |
| Tree generation timeout | Large PDF + slow model | Use `max_pages_per_node=5` or faster model |
| OOM during indexing | Large PDF on small plan | Upgrade Railway plan or limit PDF size |
