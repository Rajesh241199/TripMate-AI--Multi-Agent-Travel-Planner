
/* TripMate AI v6 — Complete frontend for the FastAPI backend.
   Endpoints: GET /health, POST /api/travel, POST /api/travel/approve
   Requires the element IDs defined in templates/index.html.
*/
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const app = {
    busy: false,
    threadId: null,
    pending: false,
    approvalAllowed: false,
    exportAllowed: false,
    answer: "",
    started: 0,
    clock: null,
  };

  const EXAMPLES = {
    "New Zealand": "Plan a 12-day New Zealand trip from Hyderabad in February 2027 for two adults, covering Auckland, Rotorua, Queenstown and Milford Sound, including flights, rental car, hotels and sightseeing under INR 5 lakhs for the couple.",
    "Europe": "Plan a 14-day Europe trip from Chennai in May 2027 for two adults, covering France, Switzerland, Germany and Italy, including flights, trains, hotels and sightseeing under INR 6 lakhs for the whole group.",
    "Kenya": "Plan a 7-day Kenya trip from Bengaluru in January 2027 for four adults, including Nairobi, Masai Mara safari, flights, hotels and transfers under INR 8 lakhs for the whole group.",
    "Japan": "Plan a 7-day Japan trip from Chennai in March 2027 for two adults, including Tokyo, Kyoto, flights, hotels, trains and sightseeing under INR 3 lakhs total.",
  };
  const AGENTS = [
    ["flight_agent", "✈️ Flight research", "flight_results"],
    ["hotel_agent", "🏨 Hotel research", "hotel_results"],
    ["weather_agent", "🌤️ Weather research", "weather_results"],
    ["budget_agent", "💰 Budget research", "budget_results"],
  ];

  function el(tag, className = "", value = "") {
    const n = document.createElement(tag);
    if (className) n.className = className;
    n.textContent = String(value ?? "");
    return n;
  }
  function text(id, value) {
    const n = $(id);
    if (n) n.textContent = String(value ?? "");
  }
  function visible(id, yes) {
    const n = typeof id === "string" ? $(id) : id;
    if (n) n.hidden = !yes;
  }
  function money(value, currency = "") {
    return Number.isFinite(value)
      ? `${currency ? currency + " " : ""}${value.toLocaleString("en-IN")}`
      : "Not available";
  }
  function sectionTitle(value) { return el("h3", "panel-heading", value); }
  function paragraph(value, className = "subtle") { return el("p", className, value); }
  function note(value, kind = "info") { return el("div", `notice notice-${kind}`, value); }
  function addList(parent, entries, kind) {
    if (!entries.length) return;
    const box = el("div", `notice notice-${kind}`);
    const ul = el("ul", "issue-list");
    entries.forEach((entry) => ul.append(el("li", "", entry)));
    box.append(ul);
    parent.append(box);
  }

  function setError(message) {
    text("errorMessage", message);
    visible("errorMessage", !!message);
  }
  function setStatus(label, detail, working = false) {
    visible("planningStatus", true);
    text("statusLabel", label);
    text("statusDescription", detail);
    visible("statusProgress", working);
  }
  function startClock() {
    stopClock();
    app.started = Date.now();
    const update = () => {
      const seconds = Math.floor((Date.now() - app.started) / 1000);
      text("elapsed", `Elapsed: ${seconds}s · Agent results appear after the API responds.`);
    };
    update();
    app.clock = window.setInterval(update, 1000);
  }
  function stopClock() {
    if (app.clock !== null) window.clearInterval(app.clock);
    app.clock = null;
  }
  function setBusy(flag) {
    app.busy = flag;
    $("generateBtn").disabled = flag;
    $("clearBtn").disabled = flag;
    $("approveBtn").disabled = flag || !app.approvalAllowed;
    $("reviseBtn").disabled = flag || !app.approvalAllowed;
    $("copyBtn").disabled = flag || !app.answer.trim();
    $("printBtn").disabled = flag || !app.exportAllowed;
    visible("generateSpinner", flag);
    text("generateBtnText", flag ? "Planning…" : "Generate Plan");
  }

  async function postJSON(path, body) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 240000);
    try {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      const raw = await response.text();
      let data;
      try { data = JSON.parse(raw); }
      catch { throw new Error(`Server returned an invalid response (HTTP ${response.status}).`); }
      if (!response.ok || data.success === false) {
        const reason = data.detail ?? data.error ?? `HTTP ${response.status}`;
        throw new Error(typeof reason === "string" ? reason : JSON.stringify(reason));
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") {
        throw new Error("The browser stopped waiting after four minutes. The server may still be processing; check its terminal before retrying.");
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  // Only display sanitized Markdown. If the optional CDN libraries are
  // unavailable, show the raw text safely rather than interpreting HTML.
  function renderMarkdown(markdown) {
    const target = $("resultBox");
    target.replaceChildren();
    const content = String(markdown || "");
    if (window.marked?.parse && window.DOMPurify?.sanitize) {
      try {
        const raw = window.marked.parse(content, { gfm: true, breaks: true });
        const sanitized = window.DOMPurify.sanitize(raw, {
          ALLOWED_TAGS: ["p", "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr",
            "ul", "ol", "li", "strong", "b", "em", "i", "s", "del", "blockquote",
            "pre", "code", "table", "thead", "tbody", "tr", "th", "td", "a"],
          ALLOWED_ATTR: ["href", "title"],
        });
        const fragment = document.createElement("template");
        fragment.innerHTML = sanitized;
        fragment.content.querySelectorAll("a").forEach((link) => {
          try {
            const url = new URL(link.getAttribute("href"), window.location.href);
            if (!["http:", "https:"].includes(url.protocol)) link.removeAttribute("href");
            else { link.target = "_blank"; link.rel = "noopener noreferrer"; }
          } catch { link.removeAttribute("href"); }
        });
        target.append(fragment.content);
        return;
      } catch (error) { console.warn("Markdown renderer unavailable", error); }
    }
    target.append(el("pre", "plain-text", content));
  }

  function renderSupervisor(data) {
    const status = data.guardrail_status || "unknown";
    const badge = $("guardrailBadge");
    badge.className = `status-badge ${status === "passed" ? "positive" : status === "blocked" ? "negative" : "caution"}`;
    badge.textContent = status === "passed" ? "✓ Guardrail passed"
      : status === "blocked" ? "Guardrail blocked" : "Guardrail not confirmed";
    text("supervisorReasoning", data.supervisor_reasoning || data.guardrail_reason || "No execution plan was returned.");

    const chips = $("agentChips");
    chips.replaceChildren();
    const selected = Array.isArray(data.selected_agents) ? data.selected_agents : [];
    selected.forEach((name) => {
      const known = AGENTS.find((a) => a[0] === name);
      const label = known?.[1] || (name === "itinerary_agent" ? "🗺️ Itinerary" : name);
      const btn = el("button", "chip", label);
      btn.type = "button";
      btn.addEventListener("click", () => {
        const target = name === "itinerary_agent" ? $("routePanel")
          : name === "budget_agent" ? $("budgetPanel")
          : $(`agent-${name}`) || $("researchPanel");
        if (target?.tagName === "DETAILS") target.open = true;
        target?.scrollIntoView({ behavior: "smooth", block: "center" });
      });
      chips.append(btn);
    });

    const c = data.trip_constraints && typeof data.trip_constraints === "object"
      ? data.trip_constraints : {};
    const facts = [
      ["Origin", c.origin], ["Destination", c.destination],
      ["Duration", c.duration || (c.duration_days ? `${c.duration_days} days` : null)],
      ["Travelers", c.travelers], ["Budget", c.budget],
      ["Travel dates", c.travel_start_date || c.travel_month],
    ];
    $("tripFacts").replaceChildren();
    facts.filter(([, v]) => v !== null && v !== undefined && String(v).trim())
      .forEach(([label, value]) => {
        const card = el("div", "fact");
        card.append(el("span", "fact-label", label), el("strong", "", value));
        $("tripFacts").append(card);
      });
    visible("workflowSection", true);
  }

  function renderBudget(data) {
    const host = $("budgetPanel");
    host.replaceChildren(sectionTitle("Budget & cost scenarios"));
    const c = data.trip_constraints || {};
    const currency = data.budget_currency || c.currency || "";
    const total = Number.isFinite(data.budget_total) ? data.budget_total : null;
    const limit = Number.isFinite(c.budget_limit) ? c.budget_limit : null;
    const original = Number.isFinite(data.budget_original_total) ? data.budget_original_total : null;
    const summary = el("div", "stat-grid");
    const rows = [
      ["Estimated total", money(total, currency)],
      ["Budget ceiling", money(limit, c.currency || currency)],
      ["Remaining / shortfall", total !== null && limit !== null
        ? `${money(Math.abs(limit - total), currency)} ${total > limit ? "over" : "remaining"}` : "Unverified"],
    ];
    if (original !== null && total !== null && original > total) {
      rows.push(["Scenario savings", money(original - total, currency)]);
    }
    rows.forEach(([label, value]) => {
      const card = el("div", "stat");
      card.append(el("span", "stat-label", label), el("strong", "", value));
      summary.append(card);
    });
    host.append(summary);

    if (data.budget_optimized) host.append(note(
      "Cheaper options are estimated scenarios, not verified discounts or booking prices.", "warning"));
    if (total !== null && limit !== null && total > limit) host.append(note(
      "Estimated costs exceed the requested ceiling. This draft must not be approved as budget-compliant.", "error"));
    else if (total !== null && limit !== null && limit > 0 && (limit - total) / limit < 0.05) host.append(note(
      "Less than 5% remains for price changes or missing fees.", "warning"));

    const items = Array.isArray(data.budget_items) ? data.budget_items : [];
    if (items.length) {
      const wrap = el("div", "table-scroll");
      const table = el("table", "budget-table");
      const thead = el("thead"); const header = el("tr");
      ["Category", "Estimated cost", "Selected alternative"].forEach((t) => header.append(el("th", "", t)));
      thead.append(header); table.append(thead);
      const tbody = el("tbody");
      items.forEach((item) => {
        const row = el("tr");
        row.append(
          el("td", "", String(item.category || "Expense").replaceAll("_", " ")),
          el("td", "numeric", money(item.amount, currency)),
          el("td", "", item.selected ? (item.tradeoff || "Alternative selected") : "—"),
        );
        tbody.append(row);
      });
      table.append(tbody); wrap.append(table); host.append(wrap);
    } else host.append(note("No comparable whole-group budget breakdown was returned.", "warning"));
    host.append(paragraph("Estimates are not live prices. Check inclusions, exchange rates, and provider availability before paying."));
  }

  function renderValidation(data) {
    const host = $("validationPanel");
    host.replaceChildren(sectionTitle("Quality & validation"));
    const errors = Array.isArray(data.validation_errors) ? data.validation_errors : [];
    const warnings = Array.isArray(data.validation_warnings) ? data.validation_warnings : [];
    if (errors.length) host.append(note("Internal validation failed. Approval is blocked.", "error"));
    else if (data.validation_passed === true) host.append(note(
      "✓ Internal checks passed. Real-world prices, travel times, schedules and availability may still be unverified.", "success"));
    else host.append(note("Validation has not been confirmed for this response.", "warning"));
    addList(host, errors, "error");
    addList(host, warnings, "warning");
  }

  function renderRoute(data) {
    const host = $("routePanel");
    host.replaceChildren(sectionTitle("Journey continuity & transport"));
    const days = Array.isArray(data.route_days) ? data.route_days : [];
    const issues = Array.isArray(data.route_issues) ? data.route_issues : [];
    const warnings = Array.isArray(data.route_warnings) ? data.route_warnings : [];
    if (issues.length) host.append(note(`${issues.length} internal route problem(s) detected.`, "error"));
    else if (days.length && data.validation_passed === true) host.append(note(
      "Chronological checks passed; geographic routing and transport schedules are not independently verified.", "success"));
    else host.append(note(days.length ? "Route provided, but full validation has not passed." : "Structured route not available.", "warning"));
    addList(host, issues, "error");
    if (warnings.length) {
      const d = el("details", "disclosure");
      d.append(el("summary", "", `${warnings.length} travel verification note(s)`));
      addList(d, warnings, "warning");
      host.append(d);
    }
    if (!days.length) return;
    const details = el("details", "disclosure");
    details.append(el("summary", "", `View ${days.length}-day chronological timeline`));
    const timeline = el("div", "timeline");
    days.forEach((day) => {
      const card = el("div", "timeline-day");
      card.append(el("strong", "", `Day ${day.day}: ${day.start || "?"} → ${day.end || "?"}`));
      (Array.isArray(day.events) ? day.events : []).forEach((ev) => {
        const label = ev.type === "transport"
          ? `${ev.mode || "Transport"}: ${ev.from || "?"} → ${ev.to || "?"}`
          : `${String(ev.type || "Event").replaceAll("_", " ")}: ${ev.location || "?"}`;
        const line = el("p", "timeline-event", label);
        if (ev.note) line.append(el("span", "muted", ` — ${ev.note}`));
        card.append(line);
      });
      card.append(el("p", "muted", `Overnight: ${day.overnight || "Not specified"} · ${day.lodging_tier || "Unknown tier"}`));
      timeline.append(card);
    });
    details.append(timeline); host.append(details);
  }

  function renderResearch(data) {
    const host = $("researchPanel");
    host.replaceChildren(sectionTitle("Specialist agent research"));
    let count = 0;
    AGENTS.forEach(([name, label, key]) => {
      const result = data[key];
      if (typeof result !== "string" || !result.trim()) return;
      const details = el("details", "disclosure");
      details.id = `agent-${name}`;
      details.append(el("summary", "", label), el("pre", "research-text", result.slice(0, 12000)));
      host.append(details);
      count++;
    });
    if (!count) host.append(paragraph("No specialist research was returned for this request."));
  }

  function render(data) {
    app.threadId = typeof data.thread_id === "string" ? data.thread_id : app.threadId;
    app.pending = data.requires_approval === true;
    app.approvalAllowed = app.pending && data.validation_passed === true && data.guardrail_status === "passed";
    app.exportAllowed = !app.pending && data.planning_status === "completed"
      && data.approved === true && data.validation_passed === true;
    app.answer = String((app.pending ? data.itinerary : null) || data.answer || data.itinerary || "No itinerary returned.");

    renderSupervisor(data);
    renderMarkdown(app.answer);
    text("resultTitle", app.pending ? "Travel plan — awaiting your approval"
      : app.exportAllowed ? "Your approved travel planning draft" : "Travel planning response");
    text("threadInfo", `Thread ID: ${app.threadId || "not provided"}`);
    renderBudget(data);
    renderValidation(data);
    renderRoute(data);
    renderResearch(data);
    visible("resultSection", true);
    visible("approvalSection", app.pending);
    text("approvalMessage", app.approvalAllowed
      ? (data.approval_request || "Review the itinerary and remaining uncertainties before approving.")
      : "Approval cannot proceed because the returned validation status is incomplete or failed.");
    setStatus(app.pending ? "Human review required" : app.exportAllowed ? "Draft approved" : "Response received",
      app.pending ? "Review and approve or request a correction. This is not a booking."
        : app.exportAllowed ? "The approved planning draft can be printed or saved as a PDF."
          : "Review the validation findings and any missing information.");
    setBusy(false);
    $("resultSection").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function generatePlan() {
    if (app.busy) return;
    setError("");
    if (app.pending) {
      setError("Review the current plan or select New plan before generating another one.");
      return;
    }
    const message = $("userInput").value.trim();
    if (!message) { setError("Enter your trip requirements first."); return; }
    if (message.length > 3500) { setError("Please keep the request under 3,500 characters."); return; }
    app.threadId = null;
    app.exportAllowed = false;
    app.approvalAllowed = false;
    setBusy(true);
    setStatus("Planning your trip", "The supervisor and specialist agents are processing the request.", true);
    startClock();
    try {
      const data = await postJSON("/api/travel", { message, thread_id: null });
      render(data);
    } catch (error) {
      console.error("Planning request failed", error);
      setError(error.message);
      setStatus("Planning unsuccessful", "No validated travel plan was returned.");
    } finally { stopClock(); setBusy(false); }
  }

  async function submitApproval(approved) {
    if (app.busy) return;
    setError("");
    if (!app.approvalAllowed || !app.threadId) {
      setError("There is no validated draft awaiting approval."); return;
    }
    const feedback = $("approvalFeedback").value.trim();
    if (!approved && !feedback) { setError("Describe what should be changed before requesting a revision."); return; }
    setBusy(true);
    setStatus(approved ? "Submitting approval" : "Requesting revision",
      "The saved LangGraph thread is resuming.", true);
    startClock();
    try {
      const data = await postJSON("/api/travel/approve", {
        thread_id: app.threadId, approved, feedback,
      });
      render(data);
      $("approvalFeedback").value = "";
    } catch (error) {
      console.error("Review action failed", error);
      setError(`${error.message} Approval status is not confirmed; do not assume the action succeeded.`);
      setStatus("Review could not be confirmed", "Check the backend terminal before retrying.");
    } finally { stopClock(); setBusy(false); }
  }

  function resetPlan() {
    if (app.busy) return;
    if (app.pending && !window.confirm("Leave the pending draft and start a new plan?")) return;
    app.pending = false;
    app.approvalAllowed = false;
    app.exportAllowed = false;
    app.threadId = null;
    app.answer = "";
    $("userInput").value = "";
    $("approvalFeedback").value = "";
    setError("");
    ["planningStatus", "workflowSection", "resultSection", "approvalSection"].forEach((id) => visible(id, false));
    setBusy(false);
    $("userInput").focus();
  }

  async function copyPlan() {
    if (!app.answer.trim()) return;
    try {
      await navigator.clipboard.writeText(app.answer);
      text("copyBtn", "Copied!");
      window.setTimeout(() => text("copyBtn", "Copy"), 1600);
    } catch { setError("Clipboard access was denied. Select the itinerary text to copy it manually."); }
  }

  function printPlan() {
    if (!app.exportAllowed) { setError("Only an internally validated, human-approved draft can be exported."); return; }
    document.body.classList.add("printing-plan");
    window.addEventListener("afterprint", () => document.body.classList.remove("printing-plan"), { once: true });
    window.print(); // Choose "Save as PDF" in the browser's print dialog.
  }

  async function checkHealth() {
    const badge = $("apiStatus");
    try {
      const response = await fetch("/health", { cache: "no-store" });
      if (!response.ok) throw new Error("API unavailable");
      badge.className = "health healthy";
      badge.textContent = "● API reachable";
    } catch {
      badge.className = "health unhealthy";
      badge.textContent = "● API unavailable";
    }
  }

  function init() {
    const chips = $("exampleChips");
    Object.entries(EXAMPLES).forEach(([name, prompt]) => {
      const button = el("button", "chip", name);
      button.type = "button";
      button.addEventListener("click", () => {
        $("userInput").value = prompt;
        $("userInput").focus();
      });
      chips.append(button);
    });
    $("tripForm").addEventListener("submit", (event) => {
      event.preventDefault();
      generatePlan();
    });
    $("clearBtn").addEventListener("click", resetPlan);
    $("approveBtn").addEventListener("click", () => submitApproval(true));
    $("reviseBtn").addEventListener("click", () => submitApproval(false));
    $("copyBtn").addEventListener("click", copyPlan);
    $("printBtn").addEventListener("click", printPlan);
    $("userInput").addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        generatePlan();
      }
    });
    setBusy(false);
    checkHealth();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else init();
})();
