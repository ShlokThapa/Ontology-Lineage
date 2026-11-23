# Lineage Studio

A read-only web app for exploring the data-lineage ontology stored in **Neo4j AuraDB**:

- **Query**: an Aura-style Cypher workbench. It has a CodeMirror editor with autocomplete, parameters and history, plus a stream of result frames with Graph, Table and Raw views.
- **Lineage Explorer**: column- or table-level lineage, upstream, downstream or both, rendered left to right.
- **Impact Analysis**: everything downstream of a table or column, with a summary such as "A change here affects 3 tables, 15 columns and 2 pipelines".
- **PII Tracker**: every PII column, how it is treated on the way to Fabric, and an `OK` / `VIOLATION` / `NOT MOVED` status for each.
- **Pipelines**: pipeline → activity → reads from / writes to, as a graph and a table.
- **Schema**: `db.schema.visualization()` plus the business ontology (BusinessConcept / RELATED_TO / REPRESENTS).

Every lineage page has an **Open in Query** button that loads the Cypher behind it into the editor.

- **Bloom**: a search-driven, Bloom-style explorer for the lineage and policy graph (live mode only). See [Bloom explorer](#bloom-explorer) below.
- **Chat**: ask questions in plain language about lineage, impact, PII, pipelines and what the policy documents say. See [Chatbot](#chatbot) below.

```
backend/    FastAPI + neo4j driver (Python 3.11+)
frontend/   React 18 + Vite + TypeScript + Tailwind, Cytoscape.js, CodeMirror 6, TanStack Table
data/       snapshot files for offline mode (nodes.json, edges.json)
scripts/    export_snapshot.py (live graph -> data/), build_demo_snapshot.py (demo data)
```

## Prerequisites

- Python 3.11 or newer
- Node.js 20 or newer (with npm)
- A Neo4j AuraDB instance (or any Neo4j 5.x) reachable on port 7687, **or** snapshot mode for offline use
- Optional: Docker, or the Azure CLI for deployment

## Configuration

Copy `.env.example` to `.env` in the repo root and fill it in. Settings are read from the environment first, then `./.env`, then `backend/.env`.

| Variable | Default | Meaning |
|---|---|---|
| `MODE` | `live` | `live` connects to Neo4j. `snapshot` serves the lineage pages from `data/*.json` and turns off live Cypher. |
| `NEO4J_URI` | | e.g. `neo4j+s://xxxx.databases.neo4j.io` |
| `NEO4J_USER` | `neo4j` | |
| `NEO4J_PASSWORD` | | Stays on the server. The browser never talks to Neo4j directly. |
| `NEO4J_DATABASE` | `neo4j` | |
| `CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Comma-separated list of allowed browser origins. |
| `MAX_ROWS` | `1000` | Hard row limit for user queries. A result frame shows a warning when it is reached. |
| `QUERY_TIMEOUT_SECONDS` | `30` | Transaction timeout for user queries. |
| `AZURE_OPEN_AI_KEY` | | Chatbot: Azure OpenAI key. Stays on the server. |
| `AZURE_OPEN_AI_ENDPOINT` | | Chatbot: e.g. `https://<resource>.openai.azure.com` |
| `AZURE_OPEN_AI_MODEL_NAME` | | Chatbot: the Azure **deployment** name. |
| `AZURE_OPEN_AI_VERSION` | `2024-12-01-preview` | Chatbot: Azure OpenAI API version. |

## Run locally

**Windows / VM, one click:** double-click `run-app.bat`. It checks for Python 3.11+ and Node 20+, creates `.env` from `.env.example` the first time and opens it in Notepad for your Neo4j details, installs backend and frontend packages (again only when `requirements.txt` or `package-lock.json` change), starts the API (:8000) and the UI (:5173) in their own windows, waits until both respond and then opens the browser. Both servers listen on all interfaces, so other machines can use `http://<vm-ip>:5173`. `stop-app.bat` stops them. You can move or copy `run-app.bat` to any folder (e.g. the Desktop): on first run it asks you to pick the project folder once and remembers it in `run-app.path.txt` next to the .bat; you can also pass the folder as an argument, `run-app.bat "D:\Apps\Ontology-Lineage"`.

**Windows, dev mode with auto-reload:** run `start-dev.bat`. It creates the virtual environment, installs dependencies the first time, and opens two windows: the API on :8000 and Vite on :5173. Then browse to <http://localhost:5173>.

**Manually:**

```bash
# backend (http://localhost:8000, docs at /docs)
cd backend
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000

# frontend (http://localhost:5173; /api is proxied to :8000)
cd frontend
npm install
npm run dev
```

**Offline / demo:** set `MODE=snapshot` (in `.env`, or `set MODE=snapshot` before starting uvicorn). The lineage pages then use `data/nodes.json` and `data/edges.json`, and the Query page shows "Live Cypher is unavailable in snapshot mode".

- The checked-in files are demo data that mirror the real model: PostgreSQL `bankdb` → 5 Fabric pipelines → `Ontology_Lineage` lakehouse.
- To work offline on the real graph, export it while you have connectivity:

  ```bash
  backend\.venv\Scripts\python scripts\export_snapshot.py      # writes data/nodes.json + data/edges.json
  ```

- To regenerate the demo data: `python scripts/build_demo_snapshot.py`.

## Tests and build

```bash
cd backend && .venv\Scripts\python -m pytest       # write guard, serialisation, snapshot lineage, API
cd frontend && npm run build                       # type-check + production build
```

## Docker

A multi-stage image builds the React app, and FastAPI serves it with the API on a single port.

```bash
docker build -t lineage-studio .
docker run --rm -p 8000:8000 --env-file .env lineage-studio
# or
docker compose up --build
```

Then open <http://localhost:8000>. API docs are at <http://localhost:8000/docs>.

## Deploy to Azure Container Apps

```bash
az login
az extension add --name containerapp --upgrade

# Build in Azure (no local Docker needed) and create/update the app
az containerapp up \
  --name lineage-studio \
  --resource-group rg-lineage \
  --location westeurope \
  --source . \
  --ingress external --target-port 8000 \
  --env-vars MODE=live NEO4J_URI="neo4j+s://xxxx.databases.neo4j.io" \
             NEO4J_USER=neo4j NEO4J_DATABASE=neo4j NEO4J_PASSWORD=secretref:neo4j-password

# Store the password as a secret (referenced above)
az containerapp secret set -n lineage-studio -g rg-lineage --secrets neo4j-password="<password>"
az containerapp update -n lineage-studio -g rg-lineage \
  --set-env-vars NEO4J_PASSWORD=secretref:neo4j-password
```

The UI and API share an origin in the container, so `CORS_ORIGINS` only matters if another site calls the API. Aura allows outbound connections from Azure, which avoids corporate firewall problems with port 7687.

**Authentication:** none is built in. The `require_user` dependency in `backend/app/main.py` is the hook where Microsoft Entra ID token validation would go. Every `/api` route already depends on it. Alternatively, turn on Container Apps "Authentication" (Easy Auth) with Entra ID; that needs no code changes.

## Security model

- Credentials live only on the server.
- User Cypher is protected in **two layers**:
  1. A static guard (`backend/app/guard.py`) rejects `CREATE`, `MERGE`, `DELETE`, `DETACH`, `SET`, `REMOVE`, `DROP`, `FOREACH`, `LOAD CSV`, `CALL {…} IN TRANSACTIONS`, admin commands, write/import `apoc.*` procedures and admin `dbms.*` procedures. Keywords inside strings, comments and back-tick identifiers are ignored. Rejected queries get a friendly 400 error.
  2. Every query runs in a READ access-mode transaction (`execute_read`), so the database itself refuses writes.
- Transaction timeout (`QUERY_TIMEOUT_SECONDS`) and row limit (`MAX_ROWS`).
- Pydantic input validation, CORS restricted to `CORS_ORIGINS`, and errors returned as `{"error": {"code", "message"}}` without stack traces.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | mode, connectivity, host, database, user, server version |
| `POST /api/reconnect` | recreate the driver and re-check |
| `GET /api/schema` | labels, relationship types and property keys, with counts |
| `GET /api/schema/graph` | schema meta-graph |
| `POST /api/query` `{query, params}` | read-only Cypher → `{keys, records, graph, summary, truncated}` |
| `GET /api/neighbors?id=&format=graph\|lineage` | 1-hop expansion (`graph`: by elementId, `lineage`: by asset id) |
| `GET /api/search?q=` | tables, views, columns, pipelines, concepts |
| `GET /api/lineage?id=&direction=&depth=&level=` | lineage graph |
| `GET /api/impact?id=&depth=&level=` | downstream graph, impacted rows, summary |
| `GET /api/pii` | PII rows, KPIs, graph |
| `GET /api/pipelines` | activity rows, graph |
| `GET /api/concepts?include_tables=` | business ontology |
| `GET /api/chat/status` | chatbot configured? model name, is policy search ready? |
| `POST /api/chat` `{messages}` | the conversation so far → `{answer, graph, citations, steps, tokens, model}` |

Lineage endpoints return `{nodes, edges, rows?, summary?, cypher}`. Here `cypher` is the query that was used, with the start id inlined, ready for "Open in Query". `id` accepts a full id (`fabric:Ontology_Lineage.fact.loan_portfolio.risk_bucket`) or a suffix (`fact.loan_portfolio.risk_bucket`, `banking.customers`).

## Bloom explorer

Type in the search bar, pick suggestions to build a search out of tokens, then run it. The results are drawn on the canvas, where you can explore further. The menu label is set by `EXPLORER_MENU_LABEL` in `frontend/src/bloom/constants.ts`.

Nothing about the data model is hard-coded. Labels, relationship types (and which labels they connect) and properties are discovered at runtime:
- **Discovery:** it uses `db.labels`, `db.schema.visualization`, `db.schema.nodeTypeProperties` and `db.schema.relTypeProperties`.
- **Cache and refresh:** the result is cached for 5 minutes. **Refresh schema** (top bar or Settings) picks up new labels straight away.
- **Large values:** vector embeddings and other long numeric lists are summarised as `<list of 384 floats>` instead of being sent to the browser.

**Search patterns** (built left to right):

| Pattern | Example |
|---|---|
| `Label` | `PolicyChunk` |
| `Label property value` | `Column name risk_bucket`, `Table platform PostgreSQL` |
| `Label REL Label …` (up to 4 hops; click the arrow on a relationship chip to switch out / in / both) | `PolicyChunk GOVERNS Column`, `Pipeline CONTAINS Activity` |
| Free text | anything without a matching token: searches `name`, `full_name`, `table`, `title`, `text`, `heading`, `id`. It uses a full-text index if one exists, otherwise `CONTAINS`. |
| Search phrase | `Lineage of risk_bucket`, `Policy clauses for risk_bucket`, `Where does PII flow` |

Suggestions follow the schema. After a label, only relationship types that really touch that label are offered. After a relationship, only valid end labels are offered.
- **Property values:** suggestions respect the whole chain. `View ← FEEDS — Table name =` only offers names of tables that actually FEED a View.
- **Empty results:** when a search finds nothing, one message explains why and offers clickable fixes, for example "Table with name = 'loans' exists (1), but none of them has a FEEDS relationship to a View. Tables that do: accounts, branches, customers."

**On open:**
- Bloom draws the whole graph, up to the *Overview limit* (default 1,000). A banner such as "Showing 1,000 of N nodes. Use search to focus" appears when the database is larger.
- The scene of the current browser session is restored on reload.
- **Show whole graph** (globe icon, or right-click the canvas) reloads it.
- Settings has *Load graph on open*, *Overview limit* and *Search replaces scene* (off by default: results are added to the scene and then selected, like Bloom).

**Keys in the search bar:** ↑/↓ move, Enter adds the suggestion, Backspace on empty removes the last token, Ctrl+Enter or ▶ runs.

**Scene keys and mouse actions:**
- **Selecting:** click to select. Shift+click adds to the selection, and Shift+drag draws a selection box. Ctrl+A selects all, Esc clears.
- **Expanding:** double-click a node to expand it.
- **Undo and dismiss:** Ctrl+Z / Ctrl+Y undo and redo. Delete dismisses the selection.
- **Right-click a node** for Expand (all, incoming, outgoing, by type), Inspect, Show lineage, Shortest path (with exactly 2 selected), Select related, Pin, Hide, Dismiss, Dismiss others and Copy id.
- **Right-click the canvas** for Fit, Re-run layout, Show all hidden and Clear scene.

**Search phrases** are stored on the server in `data/search_phrases.json`. Edit them in the app (speech-bubble icon in the top bar) or in the file. Each entry looks like this:

```json
{
  "id": "policy-clauses-for-column",
  "name": "Policy clauses for $column",
  "description": "PolicyChunk / PolicyDocument nodes linked to a column within 1-2 hops.",
  "cypher": "MATCH (c:Column) WHERE toLower(c.name) = toLower($column) RETURN c, [p = (c)-[*1..2]-(x) WHERE x:PolicyChunk OR x:PolicyDocument | p] AS policy",
  "params": [{ "name": "column", "source": "Column.name" }]
}
```

How a phrase entry works:
- `$name` in the phrase name marks a parameter. `source` (`Label.property`) supplies value autocomplete as you type.
- Phrase Cypher must pass the read-only guard.
- The five defaults are created on first use.

**Perspectives** (book icon) control styling:
- **Per label:** colour, size, caption (a property such as `heading`, or a template such as `{table}.{name}`) and show/hide.
- **Per relationship type:** colour, width and caption.
- **Rules:** for example `Column where is_pii is true → red border`, or `FLOWS_TO where transformation = MASKED → red line`. You can also size nodes by degree.
- **Storage:** perspectives are saved in the browser, and you can export or import them as JSON.

**Other controls:**
- **Filters (funnel icon):** grey out or hide by label, relationship type or property.
- **Legend:** click an entry to show or hide it.
- **Scenes:** save and open them in the browser, or export them as JSON.
- **Export:** PNG (dark or transparent), SVG, CSV (zip of `nodes.csv` + `relationships.csv`), or Cypher that reproduces the scene.

**Limits:**
- **Results:** 300 nodes per search and 100 per expand by default. You can change both in Settings, up to a hard maximum of 2,000 nodes in the scene.
- **Edge captions:** turned off automatically above 400 elements.
- **Read-only:** the backend whitelists every label, type and property against the discovered schema (a label like ``Column`) DETACH DELETE n //`` is rejected), passes values only as parameters, and runs everything read-only.

Bloom API endpoints (under `/api/explore`):
- `GET /schema?refresh=`
- `GET /suggest?q=&tokens=`
- `POST /overview`, `POST /search` (adds `diagnosis` when empty), `POST /expand`, `POST /paths`, `POST /lineage`
- `GET /node/{elementId}`
- `GET /values`
- `GET|POST|PUT|DELETE /phrases`
- `POST /phrases/{id}/run`

## Chatbot

The **Chat** page answers questions in plain language. The browser sends the whole conversation to `POST /api/chat`; the server keeps none.

How it answers:
- **Fixed tools, no generated Cypher.** Azure OpenAI is given eleven read-only tools (`backend/app/chat/tools.py`): policy search, policy section, policies for an asset, list policy documents, find assets, describe table, ontology terms (the business concepts, the tables representing them and the data flows between those tables), lineage, impact, PII columns and pipelines. Each is a reviewed query with bound parameters, run in a READ transaction, or a call into the same repository the lineage pages use.
- **Trimmed results.** The model sees a few plain fields per row. Embeddings never leave the server, text is clipped, and a tool returns at most 60 rows.
- **Graphs only when asked.** If the latest message directly asks for a graph, diagram or visual (words like *graph, diagram, visualise, draw, chart, picture*, or asking to *show / see / display* the lineage, impact, data flow, upstream, downstream or dependencies of something), a twelfth tool, `show_graph`, is offered. It fetches the graph from Neo4j with a fixed query (lineage, impact, policies linked to an asset, PII flow, pipelines, neighbours, or the ontology) and the Chat page draws it under the answer with the same graph component as the other pages, plus an **Open in Query** button that loads the Cypher behind it. Otherwise the tool does not exist for that turn, so a plain question never produces a graph; a follow-up is judged on its own wording. The model sees only the graph's size, and the browser gets at most 120 nodes with embeddings and long text removed. Plain scrolling over a graph scrolls the chat; hold Ctrl (or pinch) to zoom.
- **Policy search is semantic.** The question is embedded locally and matched against the `policy_chunk_embedding` vector index. Answers cite the policy sections they relied on, and the UI lists the sections consulted. Open **How this was found** under an answer to see every tool call.
- **Linked columns are a hint, not the truth.** `GOVERNS` links come from the earlier automated mapping and may be incomplete, and the assistant is told to say so. Nothing in the graph is changed by the chatbot.

Setup:
1. Put the four `AZURE_OPEN_AI_*` values in `.env` (see the table above; `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT` and `AZURE_OPENAI_API_KEY` spellings also work), then restart the backend.
2. Download the local embedding model once:

   ```bash
   backend\.venv\Scripts\python scripts\fetch_embedding_model.py
   ```

   It goes to `.model_cache/` (git-ignored). The script trusts the operating system's certificate store, so it works behind a proxy that re-signs TLS certificates without turning verification off. It must run as its own process: the API server never loads that trust store, because it does not coexist with `NEO4J_URI=neo4j+ssc://`. The model has to be `BAAI/bge-small-en-v1.5` (`EMBEDDING_MODEL`), because that is what produced the vectors stored in Neo4j.

`GET /api/chat/status` (and a notice at the top of the Chat page) tells you what is missing. Lineage, impact, PII and pipeline questions work without the embedding model; policy questions need it **and** live mode (snapshot data has no policy text).

Limits to know about:
- The local model is the ONNX build of bge-small run through `fastembed`, while the vectors in Neo4j were made with `sentence-transformers`. They are close but not guaranteed identical, so check the scores on a few real questions.
- Answers are generated by a language model: read the cited sections before acting on one. Azure's content filter can block a prompt; the page then asks the user to rephrase.
- `/api/chat` has no authentication, like the other routes, and every question costs model tokens. Add real auth in `require_user` before exposing it beyond a trusted network.
- Tool-call rounds are capped (`chat_max_tool_rounds`, default 6); after that the model must answer from what it has.

## Troubleshooting

- **Red "Disconnected" in the top bar / "Cannot reach …:7687".** Corporate networks often block outbound port 7687 (Bolt). Test it from PowerShell:

  ```powershell
  Test-NetConnection xxxx.databases.neo4j.io -Port 7687
  ```

  If `TcpTestSucceeded` is `False`, either use `MODE=snapshot` (export the snapshot from a network that can connect), or deploy to Azure Container Apps (above) and use the app from there.
- **Port 7687 is open but you still get "Unable to retrieve routing information".** A corporate proxy is probably inspecting TLS and re-signing certificates. Use `NEO4J_URI=neo4j+ssc://xxxx.databases.neo4j.io`; the connection stays encrypted but the certificate isn't verified. The proper fix is to trust your company's root CA.
- **"Authentication failed".** Check `NEO4J_USER` / `NEO4J_PASSWORD`. Aura passwords are shown only once, when the instance is created.
- **`/api/...` returns 404 in dev.** Make sure the backend is running on :8000. Vite proxies `/api` to it. Set `VITE_API_TARGET` to point elsewhere.
- **A query is rejected as read-only but you think it is a read.** The guard is deliberately conservative. For example, a variable named `set` or `create` is rejected; rename it.
- **Large results are slow to draw.** The graph view handles a few thousand elements. Add `LIMIT`, or use the Table tab. `MAX_ROWS` caps every result.
