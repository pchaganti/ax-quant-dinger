# QuantDinger MCP server

Thin Model Context Protocol server that exposes a curated subset of the
QuantDinger Agent Gateway (`/api/agent/v1`) as MCP tools, so AI clients
that support MCP (Cursor, Claude-style desktop apps, etc.) can drive
QuantDinger without writing custom HTTP code.

This package is an **additive** integration. The Agent Gateway REST API
remains the source of truth.

## What it exposes

Read-class (R) and Backtest-class (B) tools only — no live trading from
MCP. Use the REST `/api/agent/v1/quick-trade/...` endpoints if/when you
explicitly enable trading for an agent.

| Tool | Class | Purpose |
|------|-------|---------|
| `whoami`               | R | Inspect the calling token |
| `list_markets`         | R | Markets the token may query |
| `search_symbols`       | R | Symbols within a market |
| `get_klines`           | R | OHLCV bars |
| `get_price`            | R | Latest price |
| `list_strategies`      | R | Tenant's strategies |
| `get_strategy`         | R | One strategy |
| `submit_backtest`      | B | Queue a backtest |
| `get_job`              | R | Poll a job |
| `regime_detect`        | B | Synchronous regime detection |
| `submit_structured_tune` | B | Queue grid/random tuning |

## Install (editable)

```bash
cd mcp_server
pip install -e .
```

## Run

Configuration is env-only so the same binary works in desktop and cloud.

| Variable | Required | Purpose |
|----------|----------|---------|
| `QUANTDINGER_BASE_URL`     | yes | e.g. `http://localhost:8888` |
| `QUANTDINGER_AGENT_TOKEN`  | yes | a token issued via `/api/agent/v1/admin/tokens` |
| `QUANTDINGER_MCP_TRANSPORT`| no  | `stdio` (default), `sse`, or `streamable-http` |
| `QUANTDINGER_MCP_HOST`     | no  | bind host for HTTP transports (default `127.0.0.1`) |
| `QUANTDINGER_MCP_PORT`     | no  | bind port for HTTP transports (default `8000`) |
| `QUANTDINGER_TIMEOUT_S`    | no  | upstream HTTP timeout (default `60`) |

### stdio (desktop IDEs)

```bash
QUANTDINGER_BASE_URL=http://localhost:8888 \
QUANTDINGER_AGENT_TOKEN=qd_agent_xxxxx \
quantdinger-mcp
```

### SSE / Streamable HTTP (cloud agents, remote IDEs)

```bash
QUANTDINGER_BASE_URL=http://localhost:8888 \
QUANTDINGER_AGENT_TOKEN=qd_agent_xxxxx \
QUANTDINGER_MCP_TRANSPORT=streamable-http \
QUANTDINGER_MCP_HOST=0.0.0.0 \
QUANTDINGER_MCP_PORT=7800 \
quantdinger-mcp
```

The server is then reachable at `http://<host>:7800/`. Use `sse` instead of
`streamable-http` for clients that only support the older SSE transport.

## Wire into a client

### Local stdio client config

```json
{
  "mcpServers": {
    "quantdinger": {
      "command": "quantdinger-mcp",
      "env": {
        "QUANTDINGER_BASE_URL": "http://localhost:8888",
        "QUANTDINGER_AGENT_TOKEN": "qd_agent_xxxxxxxx"
      }
    }
  }
}
```

### Remote HTTP client config

For clients that connect to an MCP server over HTTP/SSE rather than spawning
a subprocess, point them at the URL the server is bound to (e.g.
`http://your-host:7800`) and let the client handle protocol negotiation.

Never put production exchange keys or admin JWTs in the MCP config — only
agent tokens, scoped to the capabilities the client actually needs.
