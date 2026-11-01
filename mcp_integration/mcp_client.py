
"""TripMate MCP client: Tavily, AviationStack, Weather."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any

import certifi
from dotenv import load_dotenv
from langchain_mcp_adapters.client import (
    MultiServerMCPClient,
)

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

os.environ.setdefault(
    "SSL_CERT_FILE",
    certifi.where(),
)

os.environ.setdefault(
    "REQUESTS_CA_BUNDLE",
    certifi.where(),
)

TAVILY_API_KEY = os.getenv(
    "TAVILY_API_KEY", ""
).strip()

AVIATION_STACK_API_KEY = (
    os.getenv("AVIATION_STACK_API_KEY")
    or os.getenv("AVIATIONSTACK_API_KEY")
    or ""
).strip()

OPENWEATHER_API_KEY = os.getenv(
    "OPENWEATHER_API_KEY", ""
).strip()

GROQ_API_KEY = os.getenv(
    "GROQ_API_KEY", ""
).strip()

WEATHER_SERVER_PATH = (
    BASE_DIR / "custom_weather_mcp_server.py"
)

UVX_COMMAND = shutil.which("uvx") or "uvx"


# =====================================================
# ENVIRONMENT HELPERS
# =====================================================

def _require(
    name: str,
    value: str,
) -> str:

    if not value:
        raise RuntimeError(
            f"{name} missing from .env"
        )

    return value


def _subprocess_env(
    **extra: str,
) -> dict[str, str]:

    env = os.environ.copy()

    env.update({
        name: value
        for name, value in extra.items()
        if value
    })

    return env


# =====================================================
# MCP SERVER CONFIGURATION
# =====================================================

client = MultiServerMCPClient({

    "tavily": {
        "transport": "streamable_http",
        "url": (
            "https://mcp.tavily.com/mcp/"
            f"?tavilyApiKey={TAVILY_API_KEY}"
        ),
    },

    "aviationstack": {
        "transport": "stdio",
        "command": UVX_COMMAND,
        "args": [
            "aviationstack-mcp",
        ],
        "env": _subprocess_env(
            AVIATION_STACK_API_KEY=(
                AVIATION_STACK_API_KEY
            ),
        ),
    },

    "weather": {
        "transport": "stdio",
        "command": sys.executable,
        "args": [
            str(WEATHER_SERVER_PATH),
        ],
        "env": _subprocess_env(
            OPENWEATHER_API_KEY=(
                OPENWEATHER_API_KEY
            ),
        ),
    },

})


# =====================================================
# MCP TOOL DISCOVERY
# =====================================================

async def _get_server_tool(
    server: str,
    tool_name: str,
):

    if server == "tavily":
        _require(
            "TAVILY_API_KEY",
            TAVILY_API_KEY,
        )

    elif server == "aviationstack":
        _require(
            "AVIATION_STACK_API_KEY",
            AVIATION_STACK_API_KEY,
        )

        if shutil.which("uvx") is None:
            raise RuntimeError(
                "uvx not found. Install uv and "
                "check: uvx --version"
            )

    elif server == "weather":
        _require(
            "OPENWEATHER_API_KEY",
            OPENWEATHER_API_KEY,
        )

        if not WEATHER_SERVER_PATH.is_file():
            raise FileNotFoundError(
                "Weather MCP server missing: "
                f"{WEATHER_SERVER_PATH}"
            )

    else:
        raise ValueError(
            f"Unknown MCP server: {server}"
        )

    tools = await client.get_tools(
        server_name=server
    )

    tool = next(
        (
            item
            for item in tools
            if item.name == tool_name
        ),
        None,
    )

    if tool is None:

        available = ", ".join(
            sorted(t.name for t in tools)
        ) or "none"

        raise RuntimeError(
            f"MCP tool '{tool_name}' not found "
            f"on '{server}'. Tools: {available}"
        )

    return tool


# =====================================================
# TAVILY MCP SEARCH
# =====================================================

async def tavily_mcp_search(
    query: str,
) -> Any:

    tool = await _get_server_tool(
        "tavily",
        "tavily_search",
    )

    return await tool.ainvoke({
        "query": query,
    })


# =====================================================
# AVIATIONSTACK MCP
# =====================================================

async def aviation_mcp_call(
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
) -> Any:

    tool = await _get_server_tool(
        "aviationstack",
        tool_name,
    )

    return await tool.ainvoke(
        tool_args or {}
    )


# =====================================================
# WEATHER MCP
# =====================================================

async def weather_mcp_search(
    city: str,
) -> Any:

    tool = await _get_server_tool(
        "weather",
        "get_current_weather",
    )

    return await tool.ainvoke({
        "city": city,
    })


async def forecast_mcp_search(
    city: str,
) -> Any:

    tool = await _get_server_tool(
        "weather",
        "get_forecast",
    )

    return await tool.ainvoke({
        "city": city,
    })


# =====================================================
# MCP CONNECTION DIAGNOSTICS
# =====================================================

async def get_all_tools() -> None:

    for server in (
        "tavily",
        "aviationstack",
        "weather",
    ):

        try:
            tools = await client.get_tools(
                server_name=server
            )

            print(
                f"{server}: "
                f"{[t.name for t in tools]}"
            )

        except Exception as exc:
            print(
                f"{server} FAILED: "
                f"{type(exc).__name__}: {exc}"
            )


# =====================================================
# DESTINATION EXTRACTION
# =====================================================

def extract_destination(
    query: str,
) -> str:

    from langchain_core.messages import (
        HumanMessage,
        SystemMessage,
    )

    from langchain_groq import ChatGroq

    api_key = _require(
        "GROQ_API_KEY",
        GROQ_API_KEY,
    )

    model = ChatGroq(
        model=os.getenv(
            "GROQ_MODEL",
            "openai/gpt-oss-120b",
        ),
        api_key=api_key,
        temperature=0,
    )

    response = model.invoke([
        SystemMessage(
            content=(
                "Extract only the TRAVEL DESTINATION "
                "city or country, not the origin. "
                "For example: "
                "'7 days Japan from Bengaluru' "
                "=> Japan. "
                "Return only the name; no explanation."
            )
        ),
        HumanMessage(content=query),
    ])

    destination = str(
        response.content
    ).strip().strip("`'\"")

    if not destination:
        raise ValueError(
            "Destination could not be extracted"
        )

    return destination
