"""TripMate AI — consolidated backend for the v6 HTML/JavaScript frontend.

Public functions used by app_new.py:
    run_travel_agent(user_input: str, thread_id: str | None = None) -> dict
    resume_travel_agent(thread_id: str, approved: bool, feedback: str = "") -> dict

This system creates planning DRAFTS. It cannot verify bookability, schedules,
travel regulations, or accurate provider prices without authoritative APIs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import random
import threading
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, TypedDict
from urllib.parse import urlsplit

import psycopg
from dotenv import load_dotenv
from groq import RateLimitError
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from psycopg.rows import dict_row

from mcp_client import tavily_mcp_search, forecast_mcp_search

load_dotenv()
logger = logging.getLogger("tripmate")

MAX_DAYS = 30
MAX_AUTO_REVISIONS = 2
MAX_HUMAN_REVISIONS = 2
CATEGORIES = {
    "international_flights", "domestic_flights", "accommodation",
    "car_rental", "fuel", "ground_transport", "ferry", "meals",
    "activities", "insurance", "contingency", "other",
}
MODES = {"flight", "car", "train", "bus", "ferry", "taxi", "transfer", "walk", "other"}
MONTHS = {name.lower(): index for index, name in enumerate(
    "January February March April May June July August September October November December".split(), 1
)}
AGENTS = ["flight_agent", "hotel_agent", "weather_agent", "itinerary_agent", "budget_agent"]


class TravelState(TypedDict, total=False):
    query: str
    constraints: dict[str, Any]
    guardrail_allowed: bool
    guardrail_status: str
    guardrail_reason: str
    supervisor_reasoning: str
    selected_agents: list[str]
    flight_results: str
    hotel_results: str
    hotel_recommendations: list[dict[str, Any]]
    hotel_search_cities: list[str]
    weather_results: str
    weather_forecast_applicable: bool
    route_days: list[dict[str, Any]]
    route_generation_error: str
    budget_generation_error: str
    route_issues: list[str]
    route_warnings: list[str]
    itinerary: str
    budget_items: list[dict[str, Any]]
    budget_original_total: int | None
    budget_total: int | None
    budget_currency: str
    budget_optimized: bool
    budget_feasible: bool | None
    budget_gap: int | None
    budget_results: str
    validation_passed: bool
    validation_errors: list[str]
    validation_warnings: list[str]
    auto_revisions: int
    human_revisions: int
    human_feedback: str
    approved: bool
    final_response: str
    planning_status: str


def trim(value: Any, limit: int = 1000) -> str:
    value = str(value or "")
    return value[:limit] + (" [truncated]" if len(value) > limit else "")


def llm_json(system: str, user: str, max_tokens: int = 700) -> dict[str, Any]:
    """Compact JSON completion with bounded Groq 429 recovery.

    We disable hidden SDK retries so our retry budget is explicit.
    Never turn a failed model call into invented structured data.
    """
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY is missing in .env")

    name = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
    params: dict[str, Any] = {
        "model": name,
        "api_key": key,
        "temperature": 0,
        "max_retries": 0,
        "max_tokens": max_tokens,
    }
    if name in {"openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.8-27b"}:
        params["reasoning_effort"] = "low"
    model = ChatGroq(**params).bind(response_format={"type": "json_object"})
    messages = [
        SystemMessage(content=system + " Output a compact JSON object only."),
        HumanMessage(content=user),
    ]
    for attempt in range(3):
        try:
            response = model.invoke(messages)
            content = response.content if isinstance(response.content, str) else str(response.content)
            start, end = content.find("{"), content.rfind("}")
            if start < 0 or end < start:
                raise ValueError("Groq did not return a JSON object")
            value = json.loads(content[start:end + 1])
            if not isinstance(value, dict):
                raise ValueError("Groq JSON must be an object")
            return value
        except RateLimitError as exc:
            if attempt == 2:
                raise
            response = getattr(exc, "response", None)
            headers = getattr(response, "headers", {}) or {}
            try:
                delay = float(headers.get("retry-after"))
                if not (0 <= delay <= 90):
                    raise ValueError("retry-after outside bounded request budget")
            except (TypeError, ValueError):
                delay = 16.0 * (attempt + 1)
            delay = min(90.0, max(1.0, delay)) + random.uniform(0.25, 1.0)
            logger.warning("Groq rate limited the request; next attempt after %.1fs", delay)
            time.sleep(delay)

    raise RuntimeError("Groq did not produce a valid response")


def parse_constraints(query: str) -> dict[str, Any]:
    """Extract explicit travel details before calling any model."""
    out: dict[str, Any] = {}
    duration = re.search(r"\b(\d{1,3})\s*[- ]?\s*days?\b", query, re.I)
    if duration:
        out["duration_days"] = int(duration.group(1))
        out["duration"] = f"{out['duration_days']} days"

    origin = re.search(
        r"\bfrom\s+(.+?)(?=\s+(?:for|with|in|under|during|including|covering|on|to)\b|[,.;!?]|$)",
        query, re.I,
    )
    if origin:
        out["origin"] = origin.group(1).strip()

    stop = r"(?=\s+(?:from|for|with|including|under|within|in|on|during|covering)\b|[,.;!?]|$)"
    patterns = [
        r"\b\d{1,3}\s*[- ]?\s*days?\s+(?P<dest>.+?)\s+(?:road[ -]+)?(?:trip|holiday|vacation|tour|itinerary)\b",
        r"\b(?:trip|holiday|vacation|travel|tour|itinerary)\s+(?:from\s+.+?\s+)?to\s+(?P<dest>.+?)" + stop,
        r"\b(?:\d{1,3}\s*[- ]?\s*days?|trip|holiday|vacation)\s+in\s+(?P<dest>.+?)" + stop,
        r"\bfrom\s+.+?\s+to\s+(?P<dest>.+?)" + stop,
    ]
    for pattern in patterns:
        m = re.search(pattern, query, re.I)
        if m:
            destination = re.sub(r"\s+", " ", m.group("dest")).strip(" ,.-")
            destination = re.sub(r"^(?:a|an|the)\s+", "", destination, flags=re.I)
            if 2 <= len(destination) <= 90 and destination.casefold() not in {"complete", "family", "budget", "luxury", "road"}:
                out["destination"] = destination
                break

    if re.search(r"\b(?:a couple|couples?|two adults|two people)\b", query, re.I):
        out["travelers"] = 2
    else:
        people = re.search(
            r"\b(?:for|of)\s+(\d+|one|two|three|four|five|six|seven|eight)\s+(?:adults|people|travelers|travellers)\b",
            query, re.I,
        )
        if people:
            words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}
            token = people.group(1).lower()
            out["travelers"] = int(token) if token.isdigit() else words[token]

    budget = re.search(r"(?:\bINR\b|₹|\bRs\.?\b)\s*([\d,.]+)\s*(lakhs?|lacs?)?", query, re.I)
    if budget:
        out["currency"] = "INR"
        out["budget_limit"] = int(Decimal(budget.group(1).replace(",", "")) * (100000 if budget.group(2) else 1))
    else:
        foreign = re.search(r"\b(USD|EUR|GBP|AUD|NZD|CAD|SGD|AED)\s*([\d,.]+)\b", query, re.I)
        if foreign:
            out["currency"] = foreign.group(1).upper()
            out["budget_limit"] = int(Decimal(foreign.group(2).replace(",", "")))
    if "budget_limit" in out:
        out["budget"] = f"{out['currency']} {out['budget_limit']:,}"

    month = re.search(r"\b(" + "|".join(MONTHS) + r")\s*(20\d{2})?\b", query, re.I)
    if month:
        month_number = MONTHS[month.group(1).lower()]
        now = date.today()
        year = int(month.group(2)) if month.group(2) else now.year + int(month_number < now.month)
        out["travel_month"] = f"{month.group(1).title()} {year}"

    exact = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", query)
    if exact:
        try:
            out["travel_start_date"] = date.fromisoformat(exact.group(1)).isoformat()
        except ValueError:
            pass

    out["one_way"] = bool(re.search(r"\bone[- ]way\b|\bwithout (?:a )?return\b", query, re.I))
    out["rental_requested"] = bool(re.search(r"\b(?:rental car|car rental|rent a car|car hire|hire car|road[ -]+trip)\b", query, re.I))

    stops = re.search(
        r"\b(?:covering|visiting)\s+(.+?)(?=\s+(?:including|with|under|within|on a budget|and a budget)\b|[.;]|$)",
        query, re.I,
    )
    if stops:
        phrase = re.sub(r"\s+and\s+", ",", stops.group(1), flags=re.I)
        out["requested_places"] = [p.strip() for p in phrase.split(",") if p.strip() and len(p.strip()) <= 60][:12]
    # A city-level list of hotels and price indications is an explicit output
    # requirement, not a generic accommodation research suggestion.
    out["hotel_names_requested"] = bool(re.search(
        r"(?i)\b(?:best|recommend|suggest|list|which|named|name(?:s)?)\b.{0,70}\b(?:hotels?|lodg(?:e|ing)|accommodation)\b"
        r"|\b(?:hotels?|lodg(?:e|ing)|accommodation)\b.{0,70}\b(?:best|recommend|suggest|list|names?)\b",
        query,
    ))
    out["hotel_price_requested"] = bool(re.search(r"(?i)\b(?:hotels?|lodg(?:e|ing)|accommodation)\b.{0,80}\b(?:prices?|rates?|costs?)\b|\b(?:prices?|rates?|costs?)\b.{0,80}\b(?:hotels?|lodg(?:e|ing)|accommodation)\b", query))
    return out


def normalize_place(value: Any) -> str:
    # Text matching only; not geocoding. Airport/city aliases may need manual review.
    value = str(value or "").split(",", 1)[0].lower().strip()
    return " ".join(re.sub(r"[^\w\s]", " ", value).split())


def same_place(a: Any, b: Any) -> bool:
    x, y = normalize_place(a), normalize_place(b)
    return bool(x and x == y)


def unwrap_mcp(value: Any) -> Any:
    if getattr(value, "isError", False) or (isinstance(value, dict) and value.get("isError")):
        raise RuntimeError("MCP tool failed")
    if hasattr(value, "content"):
        return unwrap_mcp(value.content)
    if isinstance(value, list):
        results = [unwrap_mcp(x) for x in value]
        return results[0] if len(results) == 1 else results
    if isinstance(value, dict):
        if value.get("type") == "text":
            return unwrap_mcp(value.get("text"))
        return value
    if isinstance(value, str):
        if value.lower().startswith(("error:", "error calling tool", "error executing tool")):
            raise RuntimeError(trim(value, 140))
        try:
            return unwrap_mcp(json.loads(value))
        except json.JSONDecodeError:
            return value
    return value


def mcp_fetch(function: Any, *args: Any) -> Any:
    # All graph runs are synchronous in the FastAPI worker thread.
    async def call():
        return await asyncio.wait_for(function(*args), timeout=25)
    return unwrap_mcp(asyncio.run(call()))


def research_summary(result: Any) -> str:
    data = unwrap_mcp(result)
    if isinstance(data, list):
        return trim("\n".join(research_summary(v) for v in data), 2300)
    if isinstance(data, dict):
        hits = data.get("results") or data.get("sources")
        if isinstance(hits, list):
            return "\n".join(
                f"- {trim(x.get('title'), 75)}: {trim(x.get('content') or x.get('snippet'), 170)} "
                f"[Source: {trim(x.get('url'), 150)}]"
                for x in hits[:4] if isinstance(x, dict)
            ) or "No relevant search results."
        return trim(data.get("answer") or data.get("content") or data, 2000)
    return trim(data, 1800)


def supervisor(state: TravelState) -> dict[str, Any]:
    query = state["query"]
    if not re.search(r"\b(?:trip|travel|holiday|vacation|itinerary|flight|hotel|tour|journey|visit|sightseeing|road trip)\b", query, re.I):
        return {"guardrail_allowed": False, "guardrail_status": "blocked",
                "guardrail_reason": "Enter a travel-related request.", "selected_agents": []}

    c = parse_constraints(query)
    if c.get("duration_days", 1) > MAX_DAYS:
        return {"guardrail_allowed": False, "guardrail_status": "blocked",
                "guardrail_reason": f"Maximum supported duration is {MAX_DAYS} days.", "selected_agents": []}
    explanation = "Supervisor selected flight, hotel, weather, route, and budget research."
    # Fallback extraction helps when natural language is outside the deterministic patterns.
    if not c.get("destination") or not c.get("origin"):
        try:
            extracted = llm_json(
                "Extract travel locations without inventing missing information.",
                f"Request: {trim(query, 1100)}\n"
                'Return {"origin":"", "destination":"", "reasoning":""}. '
                "Destination may be a country or multi-country region.", 220,
            )
            for key in ("origin", "destination"):
                value = extracted.get(key)
                if not c.get(key) and isinstance(value, str) and 2 <= len(value.strip()) <= 90:
                    c[key] = value.strip()
            explanation = trim(extracted.get("reasoning") or explanation, 350)
        except Exception as exc:
            logger.warning("Supervisor LLM extraction unavailable: %s", type(exc).__name__)
            explanation = "Deterministic travel parsing used; confirm missing constraints."

    return {"guardrail_allowed": True, "guardrail_status": "passed",
            "guardrail_reason": "Travel request accepted for planning (not booking verification).",
            "constraints": c, "selected_agents": AGENTS.copy(),
            "supervisor_reasoning": explanation}


def flight_agent(state: TravelState) -> dict[str, Any]:
    c = state["constraints"]
    try:
        data = mcp_fetch(tavily_mcp_search, f"international airport and flight routing from {c.get('origin')} to {c.get('destination')} official airlines airports")
        message = "Flight research (not live booking inventory):\n" + research_summary(data)
    except Exception as exc:
        logger.warning("Flight research unavailable: %s", type(exc).__name__)
        message = "Flight research unavailable. No schedules, fares, airport codes, or seats have been verified."
    return {"flight_results": message}


def hotel_search_records(value: Any) -> list[dict[str, Any]]:
    """Read the structured Tavily result items; never invent a property."""
    data = unwrap_mcp(value)
    if isinstance(data, dict):
        results = data.get("results") or data.get("sources") or []
    elif isinstance(data, list):
        results = data
    else:
        return []
    return [r for r in results if isinstance(r, dict)]


def credible_hotel_hit(hit: dict[str, Any], city: str) -> dict[str, Any] | None:
    """Accept only property-specific search hits with an HTTP(S) source.

    This deliberately omits general 'top 10 hotels' pages instead of
    hallucinating hotel names from travel blogs or a country search.
    """
    title = re.sub(r"\s+", " ", str(hit.get("title") or "")).strip()
    url = str(hit.get("url") or "").strip()
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            return None
    except ValueError:
        return None
    if not title or len(title) > 240:
        return None
    if re.search(r"(?i)\b(?:top\s*\d+|best\s*\d+|best hotels|hotels in|hotel deals|"
                 r"where to stay|compare hotels|find hotels|cheap hotels|hotel guide|"
                 r"holiday packages|hotel reviews and prices|\d+ best hotels)\b", title):
        return None
    # Property title normally precedes a site/provider or a date suffix.
    name = re.split(r"\s+\|\s+|\s+[–—]\s+|\s+-\s+(?=Book|Official|Prices|Reviews|Hotels\.)", title)[0].strip()
    name = re.sub(r"(?i)^book\s+", "", name)
    name = re.sub(r"(?i)\s*\(?updated\s*20\d\d.*$", "", name).strip()
    if not 5 <= len(name) <= 110:
        return None
    property_url = bool(re.search(
        r"(?i)/(?:hotel|hotels)/[a-z0-9-]{2,}/|/ho[0-9]{5,}|/en-us/hotels/[a-z0-9-]+/",
        parsed.path,
    ))
    property_named = bool(re.search(
        r"(?i)\b(?:hotel|motel|hostel|resort|lodge|inn|suites?|apartments?|"
        r"guesthouse|guest house|holiday park|backpackers|accommodation|"
        r"bed & breakfast|b\s*&\s*b)\b",
        name,
    ))
    if not (property_named or property_url):
        return None
    # An undated published price can be displayed as a source mention, never a quote.
    excerpt = str(hit.get("content") or hit.get("snippet") or "")[:800]
    price = None
    currency = None
    # Currency must be explicit. Bare '$' is ambiguous internationally.
    price_match = re.search(
        r"(?i)\b(NZD|AUD|USD|EUR|GBP|INR|CAD)\s*\$?\s*([1-9][0-9]{1,5}(?:,[0-9]{3})*)\s*(?:/\s*night|per\s*night|nightly)\b",
        excerpt,
    )
    if price_match:
        currency = price_match.group(1).upper()
        price = int(price_match.group(2).replace(",", ""))
    return {
        "city": city,
        "name": name,
        "source_url": url,
        "source_title": title[:180],
        "published_nightly_amount": price,
        "published_currency": currency,
        "price_status": "undated_source_mention" if price is not None else "not_available",
        "price_note": "Not a February 2027 booking quote; check exact dates and room type.",
    }


def hotel_agent(state: TravelState) -> dict[str, Any]:
    """Find named hotels for the cities where the trip ACTUALLY stays.

    Must run after itinerary_agent; no additional Groq calls are required.
    """
    c = state.get("constraints") or {}
    days = state.get("route_days") or []
    origin = c.get("origin")
    cities = list(dict.fromkeys(
        d["overnight"].strip()
        for d in days if isinstance(d.get("overnight"), str)
        and d["overnight"].strip() and not same_place(d["overnight"], origin)
    ))
    if not cities:
        return {"hotel_results": "No overnight cities available for a targeted hotel search.",
                "hotel_recommendations": [], "hotel_search_cities": []}

    suggestions: list[dict[str, Any]] = []
    lines = ["Named property research by overnight location (not live availability):"]
    # Bound Tavily usage for larger multi-city trips. Missing cities are clearly flagged.
    for city in cities[:7]:
        country = str(c.get("destination") or "")
        query = (f'Individual hotel property official sites and budget-midrange lodgings '
                 f'in {city}, {country}; named hotels, nightly price if published')
        try:
            results = hotel_search_records(mcp_fetch(tavily_mcp_search, query))
            options = []
            seen = set()
            for hit in results:
                hotel = credible_hotel_hit(hit, city)
                if hotel and hotel["name"].casefold() not in seen:
                    options.append(hotel)
                    seen.add(hotel["name"].casefold())
                if len(options) == 3:
                    break
            suggestions.extend(options)
            if options:
                lines.append(f'\n{city}:')
                for h in options:
                    price = (f"{h['published_currency']} {h['published_nightly_amount']:,}/night "
                             "(undated public mention, NOT a trip-date quote)"
                             if h['published_nightly_amount'] is not None
                             else 'Trip-date room rate unavailable')
                    lines.append(f"- {h['name']} — {price}; source: {h['source_url']}")
            else:
                lines.append(f'\n{city}: No individual hotel property was confirmed from this search.')
        except Exception as exc:
            logger.warning('Hotel research failed in %s: %s', city, type(exc).__name__)
            lines.append(f'\n{city}: Hotel provider research unavailable.')
    if len(cities) > 7:
        lines.append(f'Additional overnight cities not searched: {", ".join(cities[7:])}.')
    lines.append('Room availability, taxes, occupancy and dated rates require a hotel-offer provider and exact stay dates.')
    return {"hotel_results": "\n".join(lines),
            "hotel_recommendations": suggestions, "hotel_search_cities": cities[:7]}


def weather_agent(state: TravelState) -> dict[str, Any]:
    c = state["constraints"]
    start = c.get("travel_start_date")
    if not start:
        return {"weather_results": "No exact travel date: seasonal guidance only. Date-specific forecasts are unavailable.",
                "weather_forecast_applicable": False}
    try:
        travel_date = date.fromisoformat(start)
    except ValueError:
        travel_date = None
    if not travel_date or not date.today() <= travel_date <= date.today() + timedelta(days=5):
        return {"weather_results": "Trip is beyond the reliable near-term forecast window. No trip-date forecast is claimed.",
                "weather_forecast_applicable": False}
    city = c.get("destination", "")
    # For country/region requests, ask for a representative city only for near-term dates.
    # This is a candidate lookup, not an externally validated geocoding result.
    if not isinstance(city, str) or not re.fullmatch(r"[\w .'-]{2,65},[A-Za-z]{2}", city):
        try:
            suggested = llm_json(
                "Geography helper. Supply a real city and two-letter ISO country code or return empty.",
                f"Travel destination: {trim(c.get('destination'), 90)}. "
                f"Requested stops: {trim(c.get('requested_places'), 260)}. "
                'Return {"city":"City,CC"}. Choose a representative named city.', 140,
            )
            city = suggested.get("city", "")
        except Exception:
            city = ""
    if not isinstance(city, str) or not re.fullmatch(r"[\w .'-]{2,65},[A-Za-z]{2}", city):
        return {"weather_results": "A valid City,CC location could not be resolved for the near-term forecast.",
                "weather_forecast_applicable": False}
    try:
        data = mcp_fetch(forecast_mcp_search, city)
        forecast = data.get("forecast", []) if isinstance(data, dict) else []
        matched = [d for d in forecast if isinstance(d, dict) and d.get("local_date") == start]
        if matched:
            return {"weather_results": f"Dated forecast for {city} on {start}: {trim(matched[0], 650)}",
                    "weather_forecast_applicable": True}
    except Exception as exc:
        logger.warning("Forecast lookup failed: %s", type(exc).__name__)
    return {"weather_results": "Matching date-specific forecast unavailable; no forecast is claimed.",
            "weather_forecast_applicable": False}


def normalize_days(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_DAYS:
        raise ValueError("Expected a non-empty list of day records")
    output = []
    for d in raw:
        if not isinstance(d, dict) or type(d.get("day")) is not int:
            raise ValueError("Each day must have an integer day number")
        number = d["day"]
        start, end = str(d.get("start") or "").strip(), str(d.get("end") or "").strip()
        if not start or not end or len(start) > 90 or len(end) > 90:
            raise ValueError(f"Day {number}: invalid origin/destination")
        raw_events = d.get("events", [])
        if not isinstance(raw_events, list) or len(raw_events) > 12:
            raise ValueError(f"Day {number}: invalid event list")
        events = []
        for e in raw_events:
            if not isinstance(e, dict) or e.get("type") not in {"transport", "car_pickup", "car_dropoff", "activity"}:
                raise ValueError(f"Day {number}: unknown event type")
            item = {"type": e["type"], "note": trim(e.get("note"), 150)}
            if e["type"] == "transport":
                if e.get("mode") not in MODES or not e.get("from") or not e.get("to"):
                    raise ValueError(f"Day {number}: incomplete travel leg")
                item.update({"mode": e["mode"], "from": str(e["from"])[:90], "to": str(e["to"])[:90],
                             "flight_scope": e.get("flight_scope") if e.get("flight_scope") in {"international", "domestic"} else "unknown",
                             "vehicle_onboard": e.get("vehicle_onboard") is True})
            else:
                if not e.get("location"):
                    raise ValueError(f"Day {number}: activity/pickup/dropoff needs a location")
                item["location"] = str(e["location"])[:90]
            events.append(item)
        lodging_tier = str(d.get("lodging_tier") or "unknown").lower()
        output.append({"day": number, "start": start, "end": end,
                       "country": trim(d.get("country"), 70), "overnight": trim(d.get("overnight"), 90),
                       "lodging_tier": lodging_tier, "events": events})
    return output


def build_route_chunk(state: TravelState, start_day: int, last_day: int,
                      start_place: str, built: list[dict[str, Any]],
                      issues: list[str]) -> list[dict[str, Any]]:
    """One compact day-level route request, then deterministic event expansion.

    Older versions requested verbose rental and flight events from the model and
    could exhaust Groq TPM or return truncated JSON. This reduced schema usually
    needs far fewer output tokens; rental events are inserted deterministically.
    """
    c = state["constraints"]
    n = c["duration_days"]
    previous = built[-1] if built else None
    places = c.get("requested_places", [])
    schema = (
        '{"days":[{"day":1,"start":"City","end":"City",'
        '"mode":"stay|flight|car|train|bus|ferry|taxi|transfer|walk|other",'
        '"flight_scope":"international|domestic|unknown",'
        '"country":"Country of end location",'
        '"activities":["brief activity"]}]}'
    )
    prompt = (
        f'Plan day numbers {start_day} to {last_day} (inclusive) of {n} days. '
        f'Start day {start_day} in {start_place}. '
        f'Origin: {c.get("origin")}; destination: {c.get("destination")}; '
        f'visits: {places}; month: {c.get("travel_month")}; '
        f'one-way: {c.get("one_way")}; rental requested: {c.get("rental_requested")}. '
        f'Last generated day: {trim(previous, 190)}. '
        f'Feedback/errors: {trim(issues, 190)} / {trim(state.get("human_feedback"), 120)}. '
        f'Original request: {trim(state["query"], 450)}. '
        f'JSON shape: {schema}. '
        'Every start equals the previous end. Use mode stay only when start=end; '
        'each other mode moves from start to end. If flight specify domestic or international. '
        'Car means driving an actual rental, not bus or taxi. '
        'For round trips arrive at origin ONLY on final day. No days at home after return. '
        'Include all requested stops and allow enough days at destinations. '
        'Use plausible connections, without inventing schedules or prices. '
        'Keep each activity under five words and use 0–2 per day. '
        'Output exactly the specified numbered days and nothing else.'
    )
    out = llm_json(
        'Travel route planner. Return minimal, valid JSON. No invented prices.',
        prompt,
        max_tokens=min(2250, 330 + 145 * (last_day - start_day + 1)),
    )
    raw = out.get("days")
    if not isinstance(raw, list) or len(raw) != last_day-start_day+1:
        raise ValueError(f'Route JSON contains the wrong number of days ({start_day}-{last_day})')
    expected = list(range(start_day, last_day+1))
    if [d.get('day') for d in raw if isinstance(d, dict)] != expected:
        raise ValueError(f'Route JSON has wrong day numbers; expected {expected}')
    if not same_place(raw[0].get('start'), start_place):
        raise ValueError('Route JSON begins at the wrong location')
    allowed = MODES | {'stay'}
    days = []
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError('Route JSON contains a non-object day')
        mode = str(row.get('mode') or '').lower()
        start, end = str(row.get('start') or '').strip(), str(row.get('end') or '').strip()
        if mode not in allowed or not start or not end:
            raise ValueError('Route JSON has invalid mode or location')
        if mode == 'stay' and not same_place(start, end):
            raise ValueError(f'Day {row["day"]}: stay mode changes cities')
        scope = str(row.get('flight_scope') or 'unknown').lower()
        events = []
        if mode == 'car':
            # A car can be collected on the same day it is first needed.
            # Any preceding flight occurred before this new pickup.
            if not (built and _has_active_rental(built)) and not _has_active_rental(days):
                events.append({'type': 'car_pickup', 'location': start})
        elif mode != 'stay' and (built or days) and _has_active_rental(built+days):
            # The car is returned BEFORE the next non-driving movement.
            events.append({'type': 'car_dropoff', 'location': start})
        if mode != 'stay':
            events.append({'type': 'transport', 'from': start, 'to': end,
                           'mode': mode, 'flight_scope': scope,
                           'vehicle_onboard': False,
                           'note': 'Indicative route; check provider times'})
        activities = row.get('activities')
        if not isinstance(activities, list):
            activities = []
        for name in activities[:2]:
            if isinstance(name, str) and name.strip():
                # Hotel names are accommodation candidates, not sightseeing.
                # Do not present model-invented hotel names as itinerary facts;
                # the Hotel Agent provides independently sourced options below.
                if re.search(r'(?i)\b(?:hotel|hostel|motel|resort|lodge|guesthouse|inn)\b', name):
                    continue
                events.append({'type': 'activity', 'location': end, 'note': trim(name, 95)})
        # At the end of the entire tour, a rental must be returned.
        if row['day'] == n and _has_active_rental(built+days + [
            {'events': events}]):
            events.append({'type': 'car_dropoff', 'location': end})
        # For a last driving leg in the batch, the next batch's first
        # non-driving day returns the car before transport, if applicable.
        country = trim(row.get('country'), 70)
        overnight = '' if row['day'] == n and same_place(end, c.get('origin')) else end
        tier = ('budget' if state.get('budget_optimized') or
                any(x.get('category') == 'accommodation' and x.get('selected')
                    for x in state.get('budget_items', [])) else 'midrange')
        days.append({'day': row['day'], 'start': start, 'end': end,
                     'country': country, 'overnight': overnight,
                     'lodging_tier': 'none' if not overnight else tier,
                     'events': events})
    return normalize_days(days)


def _has_active_rental(days: list[dict[str, Any]]) -> bool:
    active = False
    for day in days:
        for e in day.get('events', []):
            if e.get('type') == 'car_pickup':
                active = True
            elif e.get('type') == 'car_dropoff':
                active = False
    return active


def friendly_failure(exc: Exception, agent: str) -> str:
    """Expose safe actionable diagnostics, not raw API keys or organization IDs."""
    name = type(exc).__name__
    if isinstance(exc, RateLimitError) or 'rate limit' in str(exc).lower():
        return f'{agent}: Groq rate limit (HTTP 429) reached. Check console limits, then retry a NEW plan after reset.'
    if name in {'JSONDecodeError', 'ValueError'}:
        return f'{agent}: model returned invalid or inconsistent route JSON ({trim(str(exc), 160)}).'
    if 'tokens' in str(exc).lower() and 'limit' in str(exc).lower():
        return f'{agent}: provider token allowance exceeded. Try after the quota resets.'
    return f'{agent}: {name}; see the backend terminal for the full traceback.'


def itinerary_agent(state: TravelState) -> dict[str, Any]:
    c = state["constraints"]
    count = c.get('duration_days')
    if not isinstance(count, int) or not 1 <= count <= MAX_DAYS or not c.get('origin') or not c.get('destination'):
        return {'route_days': [], 'itinerary': '',
                'route_generation_error': 'Missing origin, destination or valid duration.',
                'validation_passed': False, 'planning_status': 'needs_clarification'}

    # A compact 12-day JSON response is much smaller than 4 full event-JSON
    # generations. If its schema is invalid, retry once using smaller chunks.
    last_error: Exception | None = None
    best_partial: list[dict[str, Any]] = []
    sizes = (12, 6) if count > 6 else (count,)
    for size in sizes:
        days: list[dict[str, Any]] = []
        place = c['origin']
        try:
            for begin in range(1, count + 1, size):
                chunk = build_route_chunk(state, begin, min(begin + size - 1, count),
                                          place, days, state.get('validation_errors') or [])
                days.extend(chunk)
                place = days[-1]['end']
            return {'route_days': days, 'itinerary': '', 'route_generation_error': '',
                    'validation_passed': False, 'planning_status': 'route_generated'}
        except Exception as exc:
            logger.exception('Itinerary generation failed at chunk starting after %d completed days', len(days))
            if len(days) > len(best_partial):
                best_partial = days
            last_error = exc
            # A 429 or provider error is not a reason to burn even more requests.
            if not isinstance(exc, (ValueError, json.JSONDecodeError)):
                break

    return {'route_days': best_partial, 'itinerary': '',
            'route_generation_error': friendly_failure(last_error or ValueError('Route generation failed'), 'Itinerary Agent'),
            'validation_passed': False, 'planning_status': 'route_unavailable'}


def int_amount(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        x = Decimal(str(value).strip().replace(",", ""))
        return int(x) if x.is_finite() and 0 <= x <= 10**11 and x == int(x) else None
    except (ValueError, InvalidOperation):
        return None


def normalize_budget(data: dict[str, Any], currency: str) -> list[dict[str, Any]]:
    if str(data.get("currency") or "").upper() != currency or data.get("scope") != "whole_group":
        raise ValueError("Budget currency or whole-group scope invalid")
    values = data.get("items")
    if not isinstance(values, list) or not values:
        raise ValueError("Empty cost model")
    used, result = set(), []
    for x in values:
        if not isinstance(x, dict):
            raise ValueError("Budget items must be objects")
        category = str(x.get("category") or "")
        amount, alternate = int_amount(x.get("amount")), int_amount(x.get("alternative_amount"))
        if category not in CATEGORIES or category in used or amount is None:
            raise ValueError(f"Invalid cost category or amount: {category}")
        used.add(category)
        result.append({"category": category, "amount": amount,
                       "alternative_amount": alternate if alternate is not None and alternate < amount else None,
                       "tradeoff": trim(x.get("tradeoff"), 180), "selected": False})
    return result


def budget_agent(state: TravelState) -> dict[str, Any]:
    c = state["constraints"]
    currency = c.get("currency")
    if state.get('route_generation_error'):
        return {'budget_items': [], 'budget_total': None, 'budget_original_total': None,
                'budget_currency': currency or 'UNKNOWN', 'budget_generation_error': 'Skipped: itinerary is incomplete.',
                'budget_optimized': False, 'budget_feasible': None,
                'budget_results': 'Budget estimation skipped until a complete route is available.'}
    if not state.get("route_days") or not currency or not c.get("travelers"):
        return {"budget_items": [], "budget_total": None, "budget_original_total": None,
                "budget_currency": currency or "UNKNOWN", "budget_optimized": False,
                "budget_feasible": None, "budget_generation_error": "Missing route, currency, or traveler count.", "budget_results": "Whole-group estimate unavailable: missing route, currency, or traveler count."}
    compact_days = [
        {"day": d["day"], "start": d["start"], "end": d["end"], "overnight": d["overnight"],
         "events": [{"type": e["type"], "mode": e.get("mode"), "from": e.get("from"),
                     "to": e.get("to")} for e in d["events"]]}
        for d in state["route_days"]
    ]
    try:
        result = llm_json(
            "Conservative whole-group travel cost estimator. No live price claims.",
            f"Request: {trim(state['query'], 650)}\nCurrency: {currency}; travelers: {c['travelers']}.\n"
            f"Travel segments: {trim(json.dumps(compact_days, ensure_ascii=False), 4000)}\n"
            "Cost every planned flight type, ferry, rental car, fuel, accommodation nights away from home, "
            "ground transfers, meals, activities, insurance, contingency. Do not double-count package inclusions. "
            "Each category at most once, with a cost for whole party. Never invent a confirmed booking quote. "
            "Offer optional realistic lower-cost scenario and clearly explain tradeoff; never invent discounts. "
            f'Return {{"currency":"{currency}","scope":"whole_group","items":['
            '{"category":"international_flights","amount":120000,"alternative_amount":null,"tradeoff":""}]}. '
            "Allowed categories: " + ", ".join(sorted(CATEGORIES)) + ". Numbers integer, no total field.",
            max_tokens=1400,
        )
        items = normalize_budget(result, currency)
        total = sum(x["amount"] for x in items)
        return {"budget_items": items, "budget_total": total, "budget_original_total": total,
                "budget_currency": currency, "budget_optimized": False, "budget_generation_error": "",
                "budget_feasible": None, "budget_results": ""}
    except Exception as exc:
        logger.exception("Budget estimate failed")
        return {"budget_items": [], "budget_total": None, "budget_original_total": None,
                "budget_currency": currency, "budget_optimized": False, "budget_feasible": None,
                "budget_generation_error": friendly_failure(exc, "Budget Agent"),
                "budget_results": "Budget estimates unavailable and cannot be validated."}


def lead_time_months(month: str, today: date | None = None) -> float | None:
    m = re.fullmatch(r"([A-Za-z]+)\s+(20\d{2})", str(month or ""))
    if not m or m.group(1).lower() not in MONTHS:
        return None
    target = date(int(m.group(2)), MONTHS[m.group(1).lower()], 1)
    return (target - (today or date.today())).days / 30.44


def alternative_is_feasible(x: dict[str, Any], month: str, today: date | None = None) -> bool:
    tradeoff = x.get("tradeoff", "")
    if not tradeoff or re.search(r"\b(?:promo code|coupon|guaranteed discount|flash sale)\b", tradeoff, re.I):
        return False
    lead = lead_time_months(month, today)
    m = re.search(r"\b(?:book(?:ing)?|reserv(?:e|ing))\s+(\d+)\s*(?:to\s*\d+\s*)?months?\s+(?:ahead|early|in advance)\b", tradeoff, re.I)
    if m and lead is not None and float(m.group(1)) > lead:
        return False
    return True


def budget_optimizer(state: TravelState) -> dict[str, Any]:
    c = state["constraints"]
    ceiling, total = c.get("budget_limit"), state.get("budget_total")
    items = [x.copy() for x in state.get("budget_items") or []]
    if not isinstance(total, int) or not items:
        return {"budget_feasible": None, "budget_gap": None,
                "budget_results": "Comparable whole-group budget was not calculated."}
    original = state.get("budget_original_total") if isinstance(state.get("budget_original_total"), int) else total
    if isinstance(ceiling, int) and total > ceiling:
        candidates = sorted(
            (x for x in items if isinstance(x.get("alternative_amount"), int) and x["alternative_amount"] < x["amount"]
             and alternative_is_feasible(x, c.get("travel_month", ""))),
            key=lambda x: x["amount"] - x["alternative_amount"], reverse=True,
        )
        for x in candidates:
            if total <= ceiling:
                break
            total -= x["amount"] - x["alternative_amount"]
            x["amount"] = x["alternative_amount"]
            x["selected"] = True
    optimized = any(x.get("selected") for x in items)
    feasible = total <= ceiling if isinstance(ceiling, int) else None
    lines = ["## Whole-group indicative budget (unverified)"]
    for x in items:
        suffix = f" — estimated substitution: {x['tradeoff']}" if x.get("selected") else ""
        lines.append(f"- {x['category'].replace('_', ' ').title()}: {state['budget_currency']} {x['amount']:,}{suffix}")
    lines.append(f"\n**Python-calculated estimate: {state['budget_currency']} {total:,}**")
    if optimized:
        lines.append(f"Original indicative estimate: {state['budget_currency']} {original:,}")
    if isinstance(ceiling, int):
        lines.append(f"User's ceiling: {state['budget_currency']} {ceiling:,}")
        lines.append("Under estimated ceiling, not verified by providers." if feasible else "Still over the requested estimated ceiling.")
    lines.append("*Not a live quote. Check all actual dates, taxes, FX, bookings and inclusions.*")
    return {"budget_items": items, "budget_original_total": original, "budget_total": total,
            "budget_optimized": optimized, "budget_feasible": feasible,
            "budget_gap": max(0, total - ceiling) if isinstance(ceiling, int) else None,
            "budget_results": "\n".join(lines)}


def validate_route(days: list[dict[str, Any]], c: dict[str, Any], items: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    if not days:
        return ["No structured travel route was produced."], []
    n = c.get("duration_days")
    if [d["day"] for d in days] != list(range(1, len(days) + 1)):
        errors.append("Day numbers are not unique and sequential.")
    if isinstance(n, int) and n != len(days):
        errors.append(f"Requested {n} days but generated {len(days)}.")
    origin = c.get("origin")
    if origin and not same_place(days[0]["start"], origin):
        errors.append("Day 1 does not start at the original departure city (check location aliases).")
    round_trip = bool(origin and not c.get("one_way"))
    if round_trip and not same_place(days[-1]["end"], origin):
        errors.append("The requested round trip does not end at the origin.")
    rental_active = False
    used_modes, flight_scopes = set(), set()
    encountered = []
    country_names = set()
    overnight_count = 0
    for idx, day in enumerate(days):
        number, start, end = day["day"], day["start"], day["end"]
        encountered.extend([start, end])
        country_names.add(day.get("country", "").casefold())
        if idx and not same_place(start, days[idx - 1]["end"]):
            errors.append(f"Day {number}: starts in {start}, but previous day ended in {days[idx-1]['end']}.")
        if round_trip and idx < len(days) - 1 and same_place(end, origin):
            # This deliberately fails on home-day padding, rather than validating only numbered days.
            errors.append(f"Day {number}: return home occurs before the final travel day.")
        if day["overnight"] and not same_place(day["overnight"], end):
            errors.append(f"Day {number}: overnight city differs from destination city.")
        if day["lodging_tier"] not in {"budget", "midrange", "premium", "none"}:
            errors.append(f"Day {number}: invalid accommodation tier.")
        if idx < len(days) - 1 and not day["overnight"]:
            warnings.append(f"Day {number}: hotel/overnight location is unspecified.")
        if day["overnight"] and not (round_trip and same_place(end, origin)):
            overnight_count += 1
        position = start
        for event in day["events"]:
            kind = event["type"]
            if kind == "transport":
                mode = event["mode"]
                if not same_place(position, event["from"]):
                    errors.append(f"Day {number}: {mode} departs from {event['from']} but traveler is at {position}.")
                if mode == "car" and c.get("rental_requested") and not rental_active:
                    errors.append(f"Day {number}: rental-car travel without previous pickup.")
                if mode == "flight" and rental_active:
                    errors.append(f"Day {number}: flight while rental car still active; return car first.")
                if mode == "ferry" and rental_active and not event.get("vehicle_onboard"):
                    errors.append(f"Day {number}: active rental crosses ferry without vehicle transfer confirmation.")
                used_modes.add(mode)
                if mode == "flight":
                    flight_scopes.add(event.get("flight_scope", "unknown"))
                position = event["to"]
                encountered.append(position)
            else:
                if not same_place(position, event["location"]):
                    errors.append(f"Day {number}: {kind} at {event['location']} but traveler is at {position}.")
                if kind == "car_pickup":
                    if rental_active:
                        errors.append(f"Day {number}: new vehicle collected before previous car was returned.")
                    rental_active = True
                elif kind == "car_dropoff":
                    if not rental_active:
                        errors.append(f"Day {number}: car returned without previous pickup.")
                    rental_active = False
        if not same_place(position, end):
            errors.append(f"Day {number}: events end in {position}, but day destination is {end}.")
    if rental_active:
        errors.append("A rental car was picked up but never returned.")
    for name in c.get("requested_places", []):
        term = str(name).casefold().strip()
        if term and not any(term in place.casefold() for place in encountered) and term not in country_names:
            errors.append(f"Requested location missing from journey: {name}.")
    priced = {x.get("category"): x.get("amount") for x in items if isinstance(x, dict)}
    needs = set()
    if "flight" in used_modes:
        if "international" in flight_scopes:
            needs.add("international_flights")
        if "domestic" in flight_scopes:
            needs.add("domestic_flights")
        if "unknown" in flight_scopes:
            errors.append("Unknown flight segment classification prevents cost reconciliation.")
    if "ferry" in used_modes:
        needs.add("ferry")
    if used_modes.intersection({"taxi", "train", "bus", "transfer"}):
        needs.add("ground_transport")
    if "car" in used_modes:
        needs.update({"car_rental", "fuel"})
    if overnight_count:
        needs.add("accommodation")
    if isinstance(c.get("budget_limit"), int):
        for cat in sorted(needs):
            if not isinstance(priced.get(cat), int) or priced[cat] <= 0:
                errors.append(f"Missing positive cost for {cat}, used by the itinerary.")
    elif needs:
        warnings.append("No comparable full budget: some journey segments may remain unpriced.")
    if any(x.get("category") == "accommodation" and x.get("selected") for x in items):
        for day in days:
            if day["overnight"] and not (round_trip and same_place(day["end"], origin)) and day["lodging_tier"] != "budget":
                errors.append(f"Day {day['day']}: cheaper accommodation assumption conflicts with {day['lodging_tier']} lodging.")
    warnings.append("Geographic distances, travel schedules, weather, hotel inventory, and visa rules are not externally verified.")
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


def render_itinerary(days: list[dict[str, Any]], c: dict[str, Any], budget: str,
                     hotels: list[dict[str, Any]] | None = None,
                     budget_items: list[dict[str, Any]] | None = None) -> str:
    result = [f"# {len(days)}-day trip — {c.get('destination') or 'destination to confirm'}",
              f"**Period:** {c.get('travel_start_date') or c.get('travel_month') or 'Dates not specified'}",
              "*Planning draft; not a booking or proof of available services.*"]
    stays: dict[str, int] = {}
    for day in days:
        result.append(f"\n## Day {day['day']} — {day['start']} → {day['end']}")
        if not day['events']:
            result.append('- Local day, no transport booked.')
        for e in day['events']:
            if e['type'] == 'transport':
                result.append(f"- **{e['mode'].title()}:** {e['from']} → {e['to']}" +
                              (f" — {e['note']}" if e['note'] else ''))
            elif e['type'] in ('car_pickup', 'car_dropoff'):
                result.append(f"- **Rental {e['type'].split('_')[1]}:** {e['location']}")
            else:
                result.append(f"- **Activity:** {e['note'] or 'Local sightseeing'} ({e['location']})")
        if day['overnight']:
            result.append(f"- **Overnight:** {day['overnight']} ({day['lodging_tier']}; not booked) — see named hotel options below")
            if not same_place(day['overnight'], c.get('origin')):
                stays[day['overnight']] = stays.get(day['overnight'], 0) + 1

    if c.get('hotel_names_requested') or c.get('hotel_price_requested') or hotels:
        result.append('\n## Hotels by overnight city — sourced options and price status')
        nights = sum(stays.values())
        accommodation = next((x['amount'] for x in (budget_items or [])
                              if x.get('category') == 'accommodation'), None)
        if isinstance(accommodation, int) and nights:
            nightly = accommodation / nights
            result.append(
                f"**Accommodation allocation:** approximately {c.get('currency', '')} {nightly:,.0f} "
                f"per night for the **entire group** ({nights} overnight stays). "
                '**This is a planning allowance, NOT the price of a named hotel.**'
            )
        for city, count in stays.items():
            result.append(f"\n### {city} — {count} night{'s' if count != 1 else ''}")
            choices = [h for h in (hotels or []) if same_place(h.get('city'), city)]
            if not choices:
                result.append('- Named property not confirmed by source search; check a hotel provider.')
            for h in choices:
                title = re.sub(r'[\[\]\n\r]', ' ', h['name'])
                url = str(h.get('source_url') or '')
                link = f'[{title}](<{url}>)' if url.startswith(('https://', 'http://')) else title
                if h.get('published_nightly_amount') is not None:
                    cost = (f"{h['published_currency']} {h['published_nightly_amount']:,}/night "
                            '(undated source mention; NOT a dated quote)')
                else:
                    cost = 'Room price for travel dates not verified'
                result.append(f'- **{link}** — {cost}')
        result.append('*Exact check-in/check-out dates, number of rooms, taxes and booking inventory must be confirmed with a property or hotel-offer API.*')

    return '\n'.join(result) + ('\n\n' + budget if budget else '')


def validate(state: TravelState) -> dict[str, Any]:
    c = state["constraints"]
    route_errors, route_warnings = validate_route(state.get("route_days") or [], c, state.get("budget_items") or [])
    errors, warnings = route_errors[:], route_warnings[:]
    if state.get('route_generation_error'):
        errors.insert(0, state['route_generation_error'])
    if state.get('budget_generation_error') and isinstance(c.get('budget_limit'), int):
        errors.append(state['budget_generation_error'])
    ceiling, total = c.get("budget_limit"), state.get("budget_total")
    if isinstance(ceiling, int):
        if not isinstance(total, int) or state.get("budget_currency") != c.get("currency"):
            errors.append("Whole-group budget or currency could not be verified.")
        elif total > ceiling:
            errors.append(f"Estimated whole-group cost {total:,} exceeds requested budget {ceiling:,} {c['currency']}.")
        elif ceiling and (ceiling - total) / ceiling < 0.05:
            warnings.append("Budget buffer is below 5% and real provider costs may exceed the ceiling.")
    if not c.get("travel_start_date"):
        warnings.append("Exact departure date was not supplied; schedules and bookings remain unverified.")
    if not state.get("weather_forecast_applicable"):
        warnings.append("No travel-date-specific weather forecast was verified.")
    if "unavailable" in str(state.get("flight_results", "")).lower():
        warnings.append("Flight research was unavailable; routing and fares remain unverified.")
    if "unavailable" in str(state.get("hotel_results", "")).lower():
        warnings.append("Hotel research was unavailable; property information is unverified.")
    if c.get("hotel_names_requested") and state.get("route_days"):
        overnights = list(dict.fromkeys(
            d["overnight"] for d in state["route_days"]
            if d.get("overnight") and not same_place(d["overnight"], c.get("origin"))
        ))
        sourced = {normalize_place(h.get("city")) for h in state.get("hotel_recommendations") or []}
        missing = [city for city in overnights if normalize_place(city) not in sourced]
        if missing:
            errors.append("Named hotel options explicitly requested but not source-confirmed for: " + ", ".join(missing) + ".")
    if c.get("hotel_price_requested"):
        warnings.append("No date-specific property prices are verified without exact check-in/out dates and a hotel-offer provider; accommodation allocation is not a hotel quote.")
    itinerary = render_itinerary(state.get("route_days") or [], c,
                                 state.get("budget_results") or "",
                                 state.get("hotel_recommendations") or [],
                                 state.get("budget_items") or []) if state.get("route_days") else ""
    errors, warnings = list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))
    return {"itinerary": itinerary, "route_issues": route_errors, "route_warnings": route_warnings,
            "validation_passed": not errors, "validation_errors": errors, "validation_warnings": warnings}


def revise_route(state: TravelState) -> dict[str, Any]:
    # Keep the authoritative original query. Supply issues only as revision context;
    # never append generated content to the user's requirements.
    next_state = {**state, "auto_revisions": state.get("auto_revisions", 0) + 1}
    result = itinerary_agent(next_state)
    return {**result, "auto_revisions": next_state["auto_revisions"], "approved": False}


def human_approval(state: TravelState) -> dict[str, Any]:
    # Don't do network calls or side effects before interrupt: the node re-runs on resume.
    decision = interrupt({
        "question": "Approve the internally validated planning draft (no bookings made)?",
        "warnings": state.get("validation_warnings", []),
    })
    if not isinstance(decision, dict) or type(decision.get("approved")) is not bool:
        return {"approved": False, "human_feedback": "Invalid approval message", "human_revisions": MAX_HUMAN_REVISIONS}
    ok = decision["approved"]
    return {"approved": ok, "human_feedback": trim(decision.get("feedback"), 1000),
            "human_revisions": state.get("human_revisions", 0) + int(not ok),
            "auto_revisions": 0 if not ok else state.get("auto_revisions", 0)}


def final_node(state: TravelState) -> dict[str, Any]:
    if not (state.get("approved") and state.get("validation_passed")):
        return {"planning_status": "needs_review", "final_response": "Draft not approved and validated."}
    caution = "\n".join("- " + v for v in state.get("validation_warnings", []))
    return {"planning_status": "completed", "final_response":
            "# Human-approved planning draft (NOT booked)\n\n" + state.get("itinerary", "") +
            "\n\n## Verify before paying\n" + caution}


def needs_review(state: TravelState) -> dict[str, Any]:
    if state.get("guardrail_allowed") is False:
        return {"planning_status": "blocked", "final_response": state.get("guardrail_reason", "Travel request not accepted.")}
    c = state.get("constraints", {})
    missing = [key for key in ("origin", "destination", "duration_days") if not c.get(key)]
    if missing:
        return {"planning_status": "needs_clarification", "final_response":
                "Please specify: " + ", ".join(missing) + ". State a destination, origin and 1–30-day duration."}
    issues = list(state.get("validation_errors") or [])
    # Reaching this node after a human decline/revision limit is NOT a
    # failed route or budget. Preserve the distinction in the user message.
    if not issues:
        if state.get("human_revisions", 0) > 0 and not state.get("approved"):
            issues = [
                "Human review did not approve this draft. "
                "The available revision attempts have ended; "
                "start a new trip request with the requested changes."
            ]
        elif state.get("validation_passed") and not state.get("approved"):
            issues = [
                "The draft passed internal checks but was not approved. "
                "No travel products were booked."
            ]
        else:
            issues = [
                state.get("route_generation_error")
                or state.get("budget_generation_error")
                or "Travel plan is incomplete; check route and budget details."
            ]
    return {"planning_status": "needs_review", "final_response":
            "# Plan requires review\n\n" + "\n".join("- " + x for x in issues) +
            ("\n\n## Unverified draft\n\n" + state["itinerary"] if state.get("itinerary") else "")}


def from_supervisor(state: TravelState) -> str:
    c = state.get("constraints", {})
    return "flight_agent" if (state.get("guardrail_allowed") and c.get("origin") and c.get("destination")
                               and isinstance(c.get("duration_days"), int) and 1 <= c["duration_days"] <= MAX_DAYS) else "needs_review"


def from_validation(state: TravelState) -> str:
    if state.get("validation_passed"):
        return "human_approval"
    # No additional model calls for missing provider evidence or an incomplete route.
    # Rebuilding the itinerary cannot manufacture reliable hotel sources/prices.
    errors = state.get('validation_errors') or []
    if (state.get('route_generation_error') or state.get('budget_generation_error')
            or (not state.get('route_issues') and any(
                'Named hotel options explicitly requested' in e for e in errors))):
        return 'needs_review'
    c = state.get('constraints', {})
    if (isinstance(c.get('budget_limit'), int) and isinstance(state.get('budget_total'), int)
            and state['budget_total'] > c['budget_limit'] and not state.get('route_issues')):
        return 'needs_review'  # Cannot create real money by repeating a cost-estimate call.
    if state.get("auto_revisions", 0) >= MAX_AUTO_REVISIONS or not state.get("route_days") or state.get("budget_total") is None:
        return "needs_review"
    # A route revision cannot repair a cost *estimate* over the limit without changing the route;
    # this implementation does rerun the budget estimator for every revised route.
    return "revise_route"


def from_human(state: TravelState) -> str:
    if state.get("approved"):
        return "final_node"
    if state.get("human_revisions", 0) < MAX_HUMAN_REVISIONS and state.get("human_feedback", "").strip():
        return "revise_route"
    return "needs_review"


def compile_builder() -> StateGraph:
    g = StateGraph(TravelState)
    for name, func in {
        "supervisor": supervisor, "flight_agent": flight_agent, "hotel_agent": hotel_agent,
        "weather_agent": weather_agent, "itinerary_agent": itinerary_agent,
        "budget_agent": budget_agent, "budget_optimizer": budget_optimizer,
        "validate": validate, "revise_route": revise_route, "human_approval": human_approval,
        "final_node": final_node, "needs_review": needs_review,
    }.items():
        g.add_node(name, func)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", from_supervisor)
    for before, after in (
        ("flight_agent", "weather_agent"), ("weather_agent", "itinerary_agent"),
        ("itinerary_agent", "hotel_agent"), ("hotel_agent", "budget_agent"),
        ("budget_agent", "budget_optimizer"), ("budget_optimizer", "validate"),
        ("revise_route", "hotel_agent"),
    ):
        g.add_edge(before, after)
    g.add_conditional_edges("validate", from_validation)
    g.add_conditional_edges("human_approval", from_human)
    g.add_edge("final_node", END)
    g.add_edge("needs_review", END)
    return g


_LOCK = threading.RLock()
_GRAPH: Any = None
_DB: Any = None


def get_graph() -> Any:
    global _GRAPH, _DB
    with _LOCK:
        if _GRAPH is None:
            url = os.getenv("DATABASE_URL")
            if not url:
                raise RuntimeError("DATABASE_URL is required for HITL persistence")
            # A single connection guarded by a lock is fine for a local development instance.
            # For multi-worker production use a managed Postgres connection pool.
            _DB = psycopg.connect(url, autocommit=True, row_factory=dict_row)
            checkpointer = PostgresSaver(_DB)
            checkpointer.setup()
            _GRAPH = compile_builder().compile(checkpointer=checkpointer)
        return _GRAPH


def serialize(config: dict[str, Any], thread_id: str) -> dict[str, Any]:
    snapshot = get_graph().get_state(config)
    data: TravelState = dict(snapshot.values or {})
    pending = "human_approval" in (snapshot.next or ())
    status = "draft_waiting_approval" if pending else data.get("planning_status") or "needs_review"
    keys = (
        "guardrail_allowed", "guardrail_status", "guardrail_reason", "supervisor_reasoning",
        "selected_agents", "flight_results", "hotel_results", "hotel_recommendations", "hotel_search_cities", "weather_results",
        "weather_forecast_applicable", "route_days", "route_issues", "route_warnings", "route_generation_error", "budget_generation_error",
        "budget_items", "budget_total", "budget_original_total", "budget_currency",
        "budget_results", "budget_optimized", "budget_feasible", "budget_gap",
        "validation_passed", "validation_errors", "validation_warnings", "approved",
    )
    return {
        "thread_id": thread_id,
        "requires_approval": pending,
        "planning_status": status,
        "answer": data.get("itinerary", "") if pending else data.get("final_response") or data.get("itinerary", ""),
        "itinerary": data.get("itinerary", ""),
        "trip_constraints": data.get("constraints", {}),
        "approval_request": "Review the draft, constraints, and warnings before approval.",
        **{name: data.get(name) for name in keys},
    }


def run_travel_agent(user_input: str, thread_id: str | None = None) -> dict[str, Any]:
    """Start a NEW checkpoint thread; thread_id is retained for API compatibility."""
    if not isinstance(user_input, str) or not user_input.strip():
        raise ValueError("Travel request is empty")
    if len(user_input) > 3500:
        raise ValueError("Travel request cannot exceed 3500 characters")
    # Never let callers overwrite an existing thread by supplying an arbitrary ID.
    new_id = "trip_" + uuid.uuid4().hex
    cfg = {"configurable": {"thread_id": new_id}}
    with _LOCK:
        get_graph().invoke({"query": user_input.strip(), "auto_revisions": 0,
                            "human_revisions": 0, "human_feedback": "", "approved": False}, cfg)
        return serialize(cfg, new_id)


def resume_travel_agent(thread_id: str, approved: bool, feedback: str = "") -> dict[str, Any]:
    if not isinstance(thread_id, str) or not re.fullmatch(r"trip_[0-9a-f]{32}", thread_id):
        raise ValueError("Invalid thread ID")
    if type(approved) is not bool:
        raise ValueError("approved must be a boolean")
    if not approved and not str(feedback).strip():
        raise ValueError("Please describe changes to request a revision")
    cfg = {"configurable": {"thread_id": thread_id}}
    with _LOCK:
        graph = get_graph()
        snapshot = graph.get_state(cfg)
        if "human_approval" not in (snapshot.next or ()):
            raise ValueError("This thread is not paused for human review")
        graph.invoke(Command(resume={"approved": approved, "feedback": str(feedback)[:1000]}), cfg)
        return serialize(cfg, thread_id)
