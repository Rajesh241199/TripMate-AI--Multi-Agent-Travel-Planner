
"""TripMate OpenWeather MCP server."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

mcp = FastMCP("TripMate Weather MCP")

REQUEST_TIMEOUT_SECONDS = 20


# =====================================================
# API CONFIGURATION
# =====================================================

def _api_key() -> str:
    key = os.getenv(
        "OPENWEATHER_API_KEY", ""
    ).strip()

    if not key:
        raise RuntimeError(
            "OPENWEATHER_API_KEY is missing from .env"
        )

    return key


# =====================================================
# OPENWEATHER HTTP CLIENT
# =====================================================

def _request_openweather(
    endpoint: str,
    city: str,
) -> dict[str, Any]:

    city = city.strip()

    if not city:
        raise ValueError(
            "City cannot be empty. Example: Tokyo,JP"
        )

    url = (
        "https://api.openweathermap.org/data/2.5/"
        f"{endpoint}"
    )

    try:
        response = requests.get(
            url,
            params={
                "q": city,
                "appid": _api_key(),
                "units": "metric",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    except requests.RequestException as exc:
        raise RuntimeError(
            f"OpenWeather network error: {exc}"
        ) from exc

    if response.status_code == 401:
        raise RuntimeError(
            "OpenWeather HTTP 401: "
            "invalid or inactive API key"
        )

    if response.status_code == 404:
        raise RuntimeError(
            f"OpenWeather HTTP 404: "
            f"city '{city}' not found"
        )

    if response.status_code == 429:
        raise RuntimeError(
            "OpenWeather HTTP 429: "
            "quota/rate limit exceeded"
        )

    if not response.ok:
        raise RuntimeError(
            f"OpenWeather HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )

    try:
        payload = response.json()

    except ValueError as exc:
        raise RuntimeError(
            "OpenWeather returned invalid JSON"
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError(
            "OpenWeather returned an unexpected response"
        )

    return payload


# =====================================================
# MCP TOOL 1: CURRENT WEATHER
# =====================================================

@mcp.tool()
def get_current_weather(
    city: str,
) -> dict[str, Any]:

    """Fetch current weather for a destination city."""

    data = _request_openweather(
        "weather",
        city,
    )

    return {
        "source": "OpenWeather current-weather API",
        "data_type": "current_observation",
        "city": data["name"],
        "country": data.get(
            "sys", {}
        ).get("country"),
        "observed_at_unix_utc": data.get("dt"),
        "temperature_c": data["main"]["temp"],
        "feels_like_c": data["main"]["feels_like"],
        "humidity_percent": data[
            "main"
        ]["humidity"],
        "condition": data[
            "weather"
        ][0]["description"],
        "wind_speed_m_s": data.get(
            "wind", {}
        ).get("speed"),
    }


# =====================================================
# MCP TOOL 2: WEATHER FORECAST
# =====================================================

@mcp.tool()
def get_forecast(
    city: str,
) -> dict[str, Any]:

    """
    Return the available OpenWeather 5-day,
    3-hour forecast grouped by local date.

    Temperature ranges are calculated from
    available three-hour samples.
    """

    data = _request_openweather(
        "forecast",
        city,
    )

    offset_seconds = int(
        data.get(
            "city", {}
        ).get("timezone", 0)
    )

    grouped: dict[
        str,
        list[dict[str, Any]],
    ] = {}

    for entry in data.get("list", []):

        local_date = datetime.fromtimestamp(
            int(entry["dt"]) + offset_seconds,
            tz=timezone.utc,
        ).date().isoformat()

        grouped.setdefault(
            local_date, []
        ).append(entry)

    summaries = []

    for date in sorted(grouped):

        entries = grouped[date]

        temperatures = [
            float(entry["main"]["temp"])
            for entry in entries
        ]

        descriptions = [
            entry["weather"][0]["description"]
            for entry in entries
        ]

        summaries.append({
            "local_date": date,
            "min_sample_temperature_c": round(
                min(temperatures), 1
            ),
            "max_sample_temperature_c": round(
                max(temperatures), 1
            ),
            "typical_condition": (
                Counter(descriptions)
                .most_common(1)[0][0]
            ),
            "three_hour_samples": len(entries),
        })

    return {
        "source": (
            "OpenWeather 5-day / 3-hour forecast API"
        ),
        "data_type": "near_term_forecast",
        "city": data.get(
            "city", {}
        ).get("name", city),
        "country": data.get(
            "city", {}
        ).get("country"),
        "forecast": summaries,
        "note": (
            "Forecast available only for the "
            "returned dates. Temperature ranges "
            "reflect forecast samples rather than "
            "guaranteed daily extremes."
        ),
    }


# =====================================================
# START MCP SERVER
# =====================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
