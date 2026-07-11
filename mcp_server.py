"""MCP server for conviction — lets agents read portfolio signals and post
investment theses. Wraps the existing HTTP API (no direct DB/file access),
so it works against any running instance (local, VPS, tailnet).

Deliberately does NOT expose position add/remove/bulk-import: those stay a
human-in-the-UI action. Agents read signals and post a thesis; the human
still manages what's actually in the portfolio.

Run: python mcp_server.py (stdio transport, for `hermes mcp add`)
Env: CONVICTION_URL (default http://127.0.0.1:8080)
     CONVICTION_AUTH_FILE (optional private KEY=VALUE file containing
     CONVICTION_AUTH_USERNAME and CONVICTION_AUTH_PASSWORD)
"""

import os
from typing import Literal

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("CONVICTION_URL", "http://127.0.0.1:8080").rstrip("/")

mcp = FastMCP(
    "conviction",
    instructions=(
        "conviction is a portfolio decision cockpit. For ANY question about "
        "portfolio positions, prices, RSI/SMA signals, analyst targets, or "
        "'how does X look' -- call get_portfolio_summary or get_analyst_detail "
        "FIRST, before considering browser navigation to the tracker's web UI. "
        "These tools return the same data the UI shows, in a fraction of the "
        "size and with zero rendering noise. Only fall back to the browser if "
        "these tools are unavailable or return an error."
    ),
)


def _auth_credentials() -> tuple[str, str] | None:
    """Load optional HTTP Basic Auth credentials from the configured file.

    The file is intentionally a small shell-style ``KEY=VALUE`` file: blank
    lines and comments are ignored, and values are split only on their first
    equals sign. It must be kept outside the repository and Hermes config.
    """
    auth_file = os.environ.get("CONVICTION_AUTH_FILE")
    if not auth_file:
        return None

    values = {}
    try:
        with open(auth_file, encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    except OSError:
        return None

    username = values.get("CONVICTION_AUTH_USERNAME")
    password = values.get("CONVICTION_AUTH_PASSWORD")
    return (username, password) if username and password else None


def _client() -> httpx.Client:
    """Create the API client, adding Basic Auth only when configured."""
    credentials = _auth_credentials()
    if credentials:
        return httpx.Client(timeout=30, auth=credentials)
    return httpx.Client(timeout=30)


def _get(path: str, **params):
    with _client() as client:
        r = client.get(f"{BASE_URL}{path}", params=params)
        r.raise_for_status()
        return r.text if path.endswith("format=md") or params.get("format") == "md" else r.json()


@mcp.tool()
def get_portfolio_summary(format: Literal["md", "json"] = "md") -> str:
    """PREFERRED over browser/navigate for any portfolio question ("how does
    it look", "how's NVIDIA", "any overheated positions"). Compact snapshot of
    all portfolio + watchlist positions: prices, RSI, SMA signals, analyst
    targets, distance to all-time high, and any existing agent thesis. Small
    (~4-10 KB), structured, and always current — no need to open the web UI
    and take a DOM snapshot to answer a portfolio question."""
    with _client() as client:
        r = client.get(f"{BASE_URL}/api/summary", params={"format": format})
        r.raise_for_status()
        return r.text


@mcp.tool()
def get_config() -> dict:
    """Base currency, currency symbol, UI language, and the configurable
    buy-rule thresholds (RSI, P/E, P/B, SMA-50 band, ATH bands) currently
    in effect. Use this to interpret get_portfolio_summary's signals correctly
    -- thresholds are per-instance configurable, not fixed."""
    return _get("/api/config")


@mcp.tool()
def get_analyst_detail(ticker: str) -> dict:
    """Deep-dive analyst data for one ticker: individual analyst target
    range, recommendation history, market cap, margins, growth rates.
    Use after get_portfolio_summary flags a position worth a closer look."""
    return _get(f"/api/analysts/{ticker}")


@mcp.tool()
def post_thesis(
    ticker: str,
    verdict: Literal["buy-watch", "wait", "too-hot", "conditional", "sell-watch"],
    rationale: str,
    author: str,
) -> dict:
    """Post your investment thesis for a ticker back to the tracker. Shown in
    the UI next to the quantitative signals, with your name and a staleness
    marker after 30 days. Write the rationale for a human reader: state the
    structural/fundamental judgment the quant signals can't make (e.g. "AI
    displacement risk despite a technical bounce"), not just a restatement
    of the numbers. `author` should be your agent name (e.g. "wu", "real",
    "larry") so multiple agents' theses on the same ticker don't overwrite
    each other."""
    with _client() as client:
        r = client.post(f"{BASE_URL}/api/thesis", json={
            "ticker": ticker, "verdict": verdict,
            "rationale": rationale, "author": author,
        })
        r.raise_for_status()
        return r.json()


if __name__ == "__main__":
    mcp.run(transport="stdio")
