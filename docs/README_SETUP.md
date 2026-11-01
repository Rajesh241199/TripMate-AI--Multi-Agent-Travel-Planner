# TripMate: deterministic itinerary validation + improved PDF output

Built from the previously shared token-optimized `backend.py` and the public
repository's HTML/JavaScript contracts. Keep your working `app_new.py`,
`mcp_client.py`, `custom_weather_mcp_server.py`, `.env`, and existing stylesheet.

## Replace these files

1. Save a backup of your existing `backend.py` and `static/script.js`.
2. Copy `backend.py` in this package to your project root.
3. Copy `static/script.js` over your project's `static/script.js`.
4. Leave your existing MCP files and stylesheet in place.
5. Add the DOMPurify script tag shown below to `templates/index.html`,
   immediately after the existing `marked` script and before `static/script.js`:

```html
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.2.6/dist/purify.min.js"></script>
```

Without DOMPurify the frontend safely displays plain text, not rendered
Markdown. Ensure your CSP and internet connection allow the CDN, or serve
an audited local copy; pin and audit third-party front-end dependencies.

## FastAPI: keep the server event loop responsive

In `app_new.py` (the file you run), add:

```python
from starlette.concurrency import run_in_threadpool
```

Change `/api/travel` to invoke the synchronous backend using:

```python
result = await run_in_threadpool(
    run_travel_agent,
    user_input=user_message,
    thread_id=request_data.thread_id,
)
```

Change `/api/travel/approve` similarly:

```python
result = await run_in_threadpool(
    resume_travel_agent,
    thread_id=request_data.thread_id,
    approved=request_data.approved,
    feedback=request_data.feedback,
)
```

These FastAPI changes are advised even though the backend retains a worker
thread fallback for synchronous calls made inside active event loops.

## Test

Run static validation tests without network credentials or PostgreSQL:

```powershell
python test_validation_offline.py
```

Then start the actual app:

```powershell
conda activate travel
python app_new.py
```

Sample prompt:

> Plan a 5 days Dubai trip from Chennai with flights, hotels and sightseeing under 2 lakhs. I am planning to visit on coming December.

Expected extracted constraints:

- Origin: Chennai (`MAA`)
- Destination: Dubai
- Duration: 5 days
- Budget: INR 200,000 maximum
- Travel period: next December (no particular departure day)
- Traveler count: **not provided** (must not assume 2)

The draft is checked before HITL. If an issue such as `CJB` as Chennai's
departure code, a missing day or return, or a provably over-limit whole-party
budget is detected, the graph attempts a bounded revision. It refuses to
present a repeatedly invalid plan for human approval. Budget affordability
is **unverified** when no group total can be established.

After approval, the final node publishes the validated draft **without**
asking the model to rewrite it, reducing token usage and new hallucinations.
If a person rejects a draft with feedback, the graph revises and requests
human approval again (up to the configured revision count).

## PDF export

The new `Print / Save PDF` button uses the browser's print renderer, not
`html2canvas`. In Microsoft Edge, click it and select **Save as PDF** in the
print dialog. This generally produces selectable text and improves table
pagination. It isn't a one-click file download: this is intentional to avoid
the previous large blank regions and sliced table rows. Save/print is enabled
only for a finalized response.

## Limitations

- Tests validate known patterns, not all possible LLM failures; this is not
  production-grade verification of price, schedule, visa or lodging claims.
- The cost parser requires `ESTIMATED_TOTAL_INR: N` and `ESTIMATE_SCOPE: total`
  from the Budget Agent; missing data is a warning, never a fabricated pass.
- Source data from Tavily/AviationStack is not confirmed live bookable inventory.
- OpenWeather near-term forecasts cannot establish weather for a future
  December beyond their forecast horizon.
- Local-only demo: persistent threads are not authenticated. Add user
  authentication and isolate `thread_id` by user before public deployment.
- The HTML assumes the UI IDs from the GitHub repository. If your local
  `app_new.py` uses a substantially different template, adapt selectors.
- The Groq plan must still respect tokens-per-minute limits; automatic
  revision calls incur additional tokens only when required.
