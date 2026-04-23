# Qdrant setup for Mem0 (memory_management)

This guide is for **self-hosted Qdrant** used as Mem0’s vector store. The database runs **on infrastructure you control** (Docker on your laptop, a VM, or Qdrant Cloud). Mem0 OSS only **connects** to it; it is **not** Mem0’s hosted vector tier unless you separately use their cloud product.

---

## Library vs host app (independence)

**Unchanged:** `memory_management` does **not** import your application (`src.*`, `api.*`, etc.). Memory and Qdrant settings are whatever you pass in.

| Entry point | Role |
|-------------|------|
| **`initialize(settings, get_connection, …)`** | Call **once** at API startup. Internally runs **`set_memory_config(settings)`** (so Mem0 reads `qdrant_host`, `mem0_qdrant_collection`, … from your object or dict) and wires the session backend. |
| **`process_turn(..., settings=settings, …)`** | Call **per request**; uses the same config holder for memory + session. |

You may also call **`set_memory_config(...)`** alone if you integrate memory without the full orchestrator (see **[GETTING_STARTED.md](GETTING_STARTED.md)**). Qdrant only needs the **same keys** documented in **[ENV_VARS.md](ENV_VARS.md)** on that object (or in `os.environ` if your host builds settings from env).

---

## 1. Prerequisites

- **Docker Desktop** (Windows/macOS) or Docker Engine (Linux), **running**, before `docker run`.
- Python deps: **`qdrant-client`** and **`mem0ai`** (see `memory_management/requirements.txt`).
- Your app already calls **`load_dotenv()`** before **`initialize()`** if you rely on `.env` (see GETTING_STARTED).

---

## 2. Run Qdrant locally (Docker)

### Minimal (ephemeral storage inside container)

```bash
docker run -p 6333:6333 -p 6334:6334 qdrant/qdrant:latest
```

- **HTTP / REST:** `http://localhost:6333`
- **gRPC:** `6334` (optional for clients that use gRPC)
- **Web UI:** `http://localhost:6333/dashboard`

Data is **lost** if you remove the container without a volume.

### With a named volume (survives container recreation)

```bash
docker volume create qdrant_storage
docker run -p 6333:6333 -p 6334:6334 -v qdrant_storage:/qdrant/storage qdrant/qdrant:latest
```

### Windows: “pipe / dockerDesktopLinuxEngine” error

That means **Docker Desktop is not running** or the engine failed to start. Open Docker Desktop, wait until it is healthy, then rerun `docker run`.

---

## 3. Environment variables (host `.env` or dict passed to `set_memory_config`)

| Variable | Typical local value | Purpose |
|----------|---------------------|---------|
| `MEMORY_VECTOR_STORE` | `qdrant` | Use Qdrant instead of FAISS. |
| `MEMORY_QDRANT_PATH` | *(optional)* | Use **local path mode** (`QdrantClient(path=...)`) instead of server. |
| `QDRANT_HOST` | `localhost` | Qdrant server hostname (ignored when `MEMORY_QDRANT_PATH` is set). |
| `QDRANT_PORT` | `6333` | HTTP port (ignored when `MEMORY_QDRANT_PATH` is set). |
| `MEM0_QDRANT_COLLECTION` | *(empty)* | If empty, collection name falls back to `MEM0_DEFAULT_COLLECTION` (`mem0_memories`). |
| `MEM0_DEFAULT_COLLECTION` | `mem0_memories` | Fallback collection name. |
| `QDRANT_URL` + `QDRANT_API_KEY` | *(optional)* | **Qdrant Cloud** or secured instances: Mem0’s config validator expects **both** when using URL mode. |
| `QDRANT_ON_DISK` | `false` | Passed through to Mem0’s Qdrant config (optional). |

Reranker and CrossEncoder patch keys are in **[ENV_VARS.md](ENV_VARS.md)** (`MEM0_RERANK_*`, `MEM0_PATCH_CROSS_ENCODER_RERANKER`).

---

## 4. Verify Qdrant is up

**Browser:** open `http://localhost:6333/dashboard` — you should see the Qdrant UI.

**HTTP:**

```bash
curl -s http://localhost:6333/collections
```

Expect JSON (possibly `{"result":{"collections":[]}, ...}` on a fresh instance).

After the chatbot stores memories, refresh **Collections** in the dashboard; you should see your collection (default **`mem0_memories`**) and point counts increasing.

---

## 5. How filtering works (short)

For Qdrant, Mem0 passes **`user_id`** (and other filters) as **`query_filter`** into Qdrant’s **`query_points`**, so payload constraints are applied **inside the vector search**, not only as a Python post-filter. Your adapter may still enforce `user_id` again when formatting results (defense in depth).

---

## 6. Production / shared Qdrant

- Point **`QDRANT_HOST` / `QDRANT_PORT`** at your cluster, or use **`QDRANT_URL`** + **`QDRANT_API_KEY`** for Qdrant Cloud.
- Use a **dedicated collection per environment** (`MEM0_QDRANT_COLLECTION` or `MEM0_DEFAULT_COLLECTION`) so dev and prod never mix vectors.
- Restrict network access (firewall / private VPC); enable TLS where your Qdrant deployment requires it.

---

## 7. Troubleshooting

| Symptom | Check |
|---------|--------|
| Connection refused | Qdrant container running? Correct host/port? Firewall? |
| Empty search, dimension errors | Embedder must match **`embedding_model_dims`** (this repo uses **1024** for Bedrock Titan v2 in Mem0 config). |
| Wrong user’s memories | Confirm the API returns and reuses the same **`user_id`**; inspect `[Memory][search]` logs in the host log file. |
| No collection | First **`add`** / Mem0 write creates the collection when configured correctly. |

---

## 8. Related docs

- **[GETTING_STARTED.md](GETTING_STARTED.md)** — install, DB, Mem0 paths, CrossEncoder patch note.  
- **[ENV_VARS.md](ENV_VARS.md)** — full memory and Qdrant key list.  
- **[LIBRARY_INDEPENDENCE.md](LIBRARY_INDEPENDENCE.md)** — reuse checklist (no `src.*`).
