
<div align="center">

# TripMate AI

### A Multi-Agent Travel Planner powered by LangGraph, FastAPI, Groq & Model Context Protocol

**From a natural-language travel request to a structured, budget-aware itinerary with research evidence, automated validation, and human approval.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Backend-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-Multi--Agent-1C3C3C)](https://github.com/langchain-ai/langgraph)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Checkpointing-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![MCP](https://img.shields.io/badge/MCP-Tool%20Integration-7950F2)](https://modelcontextprotocol.io/)

[Overview](#overview) · [Architecture](#architecture) · [Quick Start](#quick-start) · [API](#api-reference) · [Testing](#testing) · [Roadmap](#roadmap)

</div>

> [!IMPORTANT]
> **TripMate AI is a travel-planning prototype, not a booking platform.** Hotel rates, flight schedules, room inventory, transport connections, weather forecasts, and prices can be incomplete or outdated. The system distinguishes source research from bookable offers and asks for human review before finalizing a planning draft. **Approval does not purchase or reserve travel services.**

## Overview

TripMate AI coordinates specialized agents to transform a request such as *“Plan a 12-day New Zealand trip from Hyderabad for two adults, covering Auckland, Rotorua, Queenstown and Milford Sound, including hotels, transport and a total budget under INR 5 lakhs”* into a structured itinerary.

The backend uses **LangGraph** for orchestration, **Groq-hosted language models** for structured planning and cost estimation, **MCP** for external travel/weather research, and **PostgreSQL-backed checkpoints** for pausing and resuming human decisions. A **FastAPI** application serves the web interface and JSON endpoints.

### Current capabilities

| Capability | Implementation | Important qualification |
| --- | --- | --- |
| Natural-language trip intake | Extracts origin, destination, duration, travelers, travel period, requested stops, and budget | Missing or ambiguous details can require clarification |
| Supervised multi-agent workflow | Supervisor, flight, weather, itinerary, transport, hotel, budget, optimizer, and validation stages | Agents run as a LangGraph workflow, not independent booking services |
| Structured day-by-day planning | Chronological locations, activities, transport legs, overnights, and rental-car events | Transport times and feasibility are not globally verified |
| Hotel discovery | Searches overnight cities for individual, source-linked properties | Directory pages are filtered; rates are not live room quotes |
| Transport research | Retrieves public evidence related to significant travel legs | Search evidence does not confirm nonstop service or a seat |
| Weather integration | Local OpenWeather MCP server | Date-specific forecasts only within the supported short-term window |
| Group budget and scenarios | Category-level estimate, computed totals, budget-ceiling checks, indicative alternatives | Estimates are model-generated, not supplier quotations |
| Quality gates | Route continuity, travel-day counts, rental ordering, transport/budget consistency, and other checks | Passing internal checks does not establish real-world bookability |
| Human-in-the-loop (HITL) | Review, approve, or request revisions through LangGraph `interrupt` | Checkpoints require PostgreSQL |
| Frontend | Responsive HTML, CSS and JavaScript; review panels and print/save-PDF UI | PDF action uses the browser's print/save capabilities |

## Architecture

```mermaid
flowchart TD
    U[Traveler / Web UI] --> API[FastAPI API]
    API --> S[Supervisor & constraint extraction]
    S --> F[Flight research agent]
    F --> W[Weather agent]
    W --> I[Itinerary agent]
    I --> T[Transport evidence agent]
    T --> H[Hotel discovery agent]
    H --> B[Budget agent]
    B --> O[Budget optimizer]
    O --> V{Validation passed?}
    V -->|Fixable route issue| R[Route revision]
    R --> T
    V -->|Unresolved / unsupported| N[Needs review]
    V -->|Yes| HITL{Human approval}
    HITL -->|Request changes| R
    HITL -->|Approve| FINAL[Approved planning draft]
    HITL -->|Decline / revision limit| N
    FINAL --> API
    N --> API
    API --> U

    F -.-> MCP[MCP: Tavily research]
    T -.-> MCP
    H -.-> MCP
    W -.-> WX[Local Weather MCP / OpenWeather]
    S -.-> LLM[Groq LLM]
    I -.-> LLM
    B -.-> LLM
    HITL -.-> PG[(PostgreSQL checkpoints)]
```

### Agent responsibilities

1. **Supervisor:** validates travel intent, extracts constraints, selects the planning pipeline, and rejects unsupported requests.
2. **Flight Research Agent:** looks up travel-routing information; it does not confirm live fares or availability.
3. **Weather Agent:** consults the local OpenWeather MCP server when a supported date-specific forecast is available.
4. **Itinerary Agent:** generates compact, structured daily routes; the backend expands these into chronological events.
5. **Transport Agent:** gathers sources for selected flight, train, bus, and ferry legs and attaches verification-status notes.
6. **Hotel Agent:** identifies overnight locations and searches for specific properties, filtering directories and unrelated pages.
7. **Budget Agent & Optimizer:** estimates whole-group costs, calculates totals in Python, and evaluates permitted alternatives.
8. **Validation & HITL:** checks route and cost consistency; only eligible drafts proceed to a resumable human review.

### MCP integrations

| Integration | Role | Status in the current workflow |
| --- | --- | --- |
| [Tavily MCP](https://docs.tavily.com/) | Web research for flights, transport routes, and hotels | Called by active research agents |
| Local OpenWeather MCP | Current weather and short-term forecasts | Called by the weather agent when applicable |
| AviationStack MCP | AviationStack tool client in `mcp_client.py` | Integration available in the client; **not a live-flight-booking guarantee** and not required by the active graph |

External research is presented with provenance and uncertainty where possible. **A source link is not proof of a reservable offer.**

## Technology stack

- **Backend:** Python, FastAPI, LangGraph, LangChain, LangChain Groq
- **LLM:** Groq API (configurable through `GROQ_MODEL`)
- **Tool interoperability:** Model Context Protocol (MCP), `langchain-mcp-adapters`
- **External services:** Tavily; OpenWeather; optional AviationStack MCP client
- **State persistence:** PostgreSQL, `psycopg`, LangGraph `PostgresSaver`
- **Frontend:** HTML5, CSS3, vanilla JavaScript
- **Development:** VS Code, Python virtual environment/Conda, Git, Dockerfile

## Repository layout

```text
TripMate-AI/
├── app_new.py                       # Active FastAPI entry point
├── app.py                           # Existing/legacy application entry point
├── backend.py                       # LangGraph agents, orchestration, validation, HITL
├── mcp_integration/
│   ├── __init__.py
│   ├── mcp_client.py                 # MCP client and external tool adapters
│   └── custom_weather_mcp_server.py # Local OpenWeather MCP server
├── tools/
│   ├── __init__.py
│   ├── flight_tool.py
│   └── tavily_tool.py
├── static/
│   ├── script.js
│   └── style.css
├── templates/
│   └── index.html
├── tests/
│   ├── test_validation_offline.py
│   ├── mcp_client_test.py
│   └── test.py
├── docs/
│   ├── README_SETUP.md
│   └── diagrams/
│       ├── diagram.excalidraw
│       └── mcp_demo.excalidraw
├── Dockerfile
├── .dockerignore
├── .env.example                     # Placeholder values only
├── .gitignore
├── requirements.txt
├── LICENSE
└── README.md
```

> The layout reflects the reorganized local project. If your checkout still has the MCP modules at the repository root, follow the [MCP relocation note](#mcp-relocation-note) before starting the application.

## Quick start

### Prerequisites

- **Python 3.10+** (use a version supported by the packages in `requirements.txt`)
- **PostgreSQL** running locally or an accessible PostgreSQL instance
- Groq API key
- Tavily API key for web research
- OpenWeather API key for weather queries
- Git; optional `uvx` if using the AviationStack MCP integration

### 1. Clone the repository

```bash
git clone https://github.com/Rajesh241199/TripMate-AI---Multi-Agent-Travel-Planner-.git
cd TripMate-AI---Multi-Agent-Travel-Planner-
```

### 2. Create an environment and install dependencies

**Windows PowerShell:**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

**macOS / Linux:**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

You can also use your existing Conda environment (for example, `conda activate travel`).

### 3. Configure environment variables

Copy `.env.example` to `.env` and replace placeholders with your own credentials:

```powershell
# Windows PowerShell
Copy-Item .env.example .env
```

```bash
# macOS / Linux
cp .env.example .env
```

Example configuration:

```dotenv
GROQ_API_KEY=replace_with_your_groq_api_key
GROQ_MODEL=openai/gpt-oss-120b
TAVILY_API_KEY=replace_with_your_tavily_api_key
OPENWEATHER_API_KEY=replace_with_your_openweather_api_key
DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/tripmate

# Optional: for the AviationStack MCP client
AVIATION_STACK_API_KEY=replace_with_your_aviationstack_api_key
```

**Never commit `.env` or real credentials.** The `.env.example` file must contain placeholders only. Some keys are optional for partial functionality, but missing research credentials will limit source-backed results.

### 4. Prepare PostgreSQL

Create a PostgreSQL database named `tripmate` with an account authorized to connect and create checkpoint tables. For example, from the `psql` console, using an account with database-creation privileges:

```sql
CREATE DATABASE tripmate;
```

Set `DATABASE_URL` to the resulting connection string. The backend initializes LangGraph's PostgreSQL checkpoint tables on first graph setup (`PostgresSaver.setup()`). This is suitable for local development; in production, manage schema setup and migrations separately.

### 5. Start TripMate AI

```powershell
python -m py_compile backend.py app_new.py
python app_new.py
```

Open **http://127.0.0.1:8000** in your browser. The UI should display an API health/reachability indicator when the server is accessible.

### MCP relocation note

The reorganized project stores the MCP code in `mcp_integration/`. Ensure `backend.py` imports from the new Python package:

```python
from mcp_integration.mcp_client import tavily_mcp_search, forecast_mcp_search
```

Also check the MCP client resolves the weather server relative to its own file:

```python
from pathlib import Path
WEATHER_SERVER_PATH = Path(__file__).resolve().parent / "custom_weather_mcp_server.py"
```

The MCP modules previously loaded `.env` relative to their own location. **After moving them**, either load environment variables from the project root or ensure `backend.py` loads the root `.env` before calling MCP functions. For standalone MCP-client tests, explicitly load the root `.env`. Search your code for any old `mcp_client` import or weather-server path before running.

## Using the application

Enter a natural-language request with your departure city, destination, trip duration, number of travelers, travel month or exact dates, desired stops, transport preferences, and total budget.

**Example:**

> Plan a 12-day New Zealand trip from Hyderabad in February 2027 for two adults, covering Auckland, Rotorua, Queenstown and Milford Sound. Include flights, rental cars, sightseeing and named hotel recommendations with source links and price status. Keep the total budget under INR 5 lakhs and require human approval.

The UI displays the supervisor's extracted constraints, the structured itinerary, estimated budget, validation messages, hotel/transport research, and review actions. Select **Approve draft** to approve the planning result, or **Request changes** to submit revision feedback. An approved draft is **not** a purchase, reservation, or confirmed booking.

## API reference

The frontend communicates with the FastAPI endpoints below. Exact request validation and HTTP error formats are defined in `app_new.py`.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/` | Render the web interface |
| `GET` | `/health` | Application health/reachability check |
| `POST` | `/api/travel` | Start a new planning workflow |
| `POST` | `/api/travel/approve` | Resume a paused approval/revision workflow |

### Create a planning draft

```bash
curl -X POST http://127.0.0.1:8000/api/travel \
  -H "Content-Type: application/json" \
  -d '{"message":"Plan a 7-day Japan trip from Chennai in March 2027 for two adults under INR 3 lakhs.","thread_id":null}'
```

### Resume a human approval

Use the `thread_id` returned by the first call **only when** `requires_approval` is `true`:

```bash
curl -X POST http://127.0.0.1:8000/api/travel/approve \
  -H "Content-Type: application/json" \
  -d '{"thread_id":"trip_REPLACE_WITH_RETURNED_ID","approved":true,"feedback":""}'
```

To request changes, send `"approved": false` with a nonempty `feedback` value.

### Selected response fields

The backend exposes these fields for the existing frontend:

| Field | Meaning |
| --- | --- |
| `thread_id` | Checkpoint identifier for the workflow |
| `planning_status` | Current outcome, e.g., draft awaiting approval, completed, or needs review |
| `requires_approval` | Whether the graph is paused for human input |
| `trip_constraints` | Parsed user requirements |
| `route_days`, `itinerary` | Structured daily route and rendered Markdown |
| `hotel_recommendations`, `hotel_results` | Source-linked hotel discovery and price-status details |
| `transport_checks`, `transport_results` | Travel-leg evidence and uncertainty labels |
| `budget_items`, `budget_total`, `budget_gap` | Whole-group cost model and computed totals |
| `validation_passed`, `validation_errors`, `validation_warnings` | Internal quality gates and limitations |
| `answer` | Primary Markdown response used by the frontend |

New planning calls generate a new `thread_id`; approval/revision calls resume an existing paused checkpoint.

## Reliability, validation and limits

TripMate includes several **application-level** safeguards:

- **Structured planning:** compact JSON, bounded route-generation retries, and sequential travel-day checks.
- **Route continuity:** verifies day-to-day start/end locations, rental pickup/drop-off order, and early returns home.
- **Transport checks:** flags implausible connections and sources that do not establish the specific route; includes some New Zealand-specific checks.
- **Hotel source filtering:** attempts to exclude directories, forum discussions, PDFs, and generic hotel lists; published prices are tagged as undated unless supported by booking-specific evidence.
- **Budget reconciliation:** totals category estimates in Python, checks the requested currency and ceiling, and flags inconsistent transport or lodging assumptions.
- **Rate-limit resilience:** bounded retries for Groq HTTP 429 responses; provider limits still apply.
- **Fail-closed approval:** incomplete plans or critical internal validation errors do not advance to normal approval.

**These controls are not a real-time travel inventory, universal geospatial routing engine, fraud detector, or financial guarantee.** They can miss factual errors. Confirm schedules, passport/visa rules, travel advisories, accommodation policies, supplier availability, fees, taxes, and actual checkout prices independently.

## Testing

Run the syntax/import checks first:

```powershell
python -m compileall -q backend.py app_new.py mcp_integration tools
python -c "from backend import run_travel_agent, resume_travel_agent; print('Backend import OK')"
```

The reorganized repository includes test scripts under `tests/`. Run the applicable test entry points, for example:

```powershell
python -m pytest -q tests
```

If you moved tests from the root, review test imports, fixture file paths, and whether `pytest` is included in `requirements.txt`. Tests for Groq/MCP/PostgreSQL require their external services, while pure route/budget validation can be exercised offline.

### Suggested manual smoke test

1. Open `/health` and verify the application starts.
2. Submit a short trip; confirm constraints, day count, route and budget panels appear.
3. Inspect hotel names, source URLs, pricing statuses and transport warnings.
4. Request changes and ensure the same thread resumes.
5. Approve a valid draft and confirm its final status and browser PDF export.
6. Trigger an invalid itinerary or missing cost category; confirm the system refuses approval.

## Security and deployment considerations

This repository demonstrates a working local planning workflow; **it is not claimed to be production-deployed or security-audited**. Before opening it to public traffic:

- Put authentication, authorization and per-user checkpoint isolation in front of planning/approval endpoints.
- Use a managed secrets store; never log or commit API keys or connection strings.
- Add request limits, quotas, timeouts, observability, audit logs and cost monitoring.
- Use a PostgreSQL connection pool and a deployment-appropriate concurrency model; the current single-connection lock is development-oriented.
- Add CI checks, integration tests, dependency scanning, pinned dependencies, and deployment health/readiness probes.
- Separate model research output from trusted instructions; sanitize user-facing external content and avoid treating search snippets as authoritative.
- Implement explicit data retention and deletion policies for stored travel requests and checkpoints.
- Verify Docker image behavior, runtime environment and secrets configuration before using the included `Dockerfile` in deployment.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `GROQ_API_KEY is missing` | Root `.env`, key spelling and environment loading |
| Groq `429` or model/token errors | Provider rate limits, configured model, request frequency and quota |
| `DATABASE_URL` missing / PostgreSQL connection error | Database availability, credentials, network, checkpoint permissions |
| `ModuleNotFoundError: mcp_client` | Update imports to `mcp_integration.mcp_client` after moving files |
| Weather MCP server not found | Resolve `custom_weather_mcp_server.py` relative to `mcp_client.py` |
| Hotel names or nightly rates unavailable | Source search results may not identify individual properties or dated offers |
| `Plan requires review` | Inspect `validation_errors`, `route_generation_error`, and `budget_generation_error` |
| Browser shows old behavior | Restart FastAPI, create a new thread, hard-refresh the browser |

For extended setup notes, see [`docs/README_SETUP.md`](docs/README_SETUP.md). Architecture sketches are under [`docs/diagrams/`](docs/diagrams/).

## Roadmap

- [ ] Dated hotel-offer API integration with occupancy, inventory and total-cost checks
- [ ] Flight timetable/offer API integration and explicit multi-leg connection modeling
- [ ] Geocoding, routing distances, realistic travel durations and time-zone handling
- [ ] Consistent currency conversion, taxes and booking-fee reconciliation
- [ ] Structured evaluation fixtures and CI for itinerary feasibility and hallucination handling
- [ ] Authentication, per-user persistence, secrets management and production observability
- [ ] Deployable container configuration with environment-specific documentation

## Contributing

Issues and pull requests are welcome. For changes to routes, hotel source filtering or budget logic, include an offline regression test and describe any external API assumptions. Avoid committing credentials, live customer information, or unlicensed third-party assets.

## License

See [`LICENSE`](LICENSE) for the applicable terms. Confirm the final license text after resolving any Git merge conflicts.

---

<div align="center">

**TripMate AI — intelligent travel-planning assistance with transparent uncertainty and a human decision in the loop.**

</div>
