# Agent Quickstart — using QuantDinger from an AI agent

This quickstart shows how to drive the QuantDinger Agent Gateway
(`/api/agent/v1`) from any AI / automation client. It assumes you already
have the stack running (see the root `README.md`) and admin credentials.

For the full design, see [AI_INTEGRATION_DESIGN.md](AI_INTEGRATION_DESIGN.md).
For the machine-readable contract, see [agent-openapi.json](agent-openapi.json).

---

## 1. Issue an agent token (one-time, admin)

Tokens are minted by the human admin, never by an agent. Get a normal admin
JWT first (login UI or `/api/auth/login`), then:

```bash
curl -X POST http://localhost:8888/api/agent/v1/admin/tokens \
  -H "Authorization: Bearer <ADMIN_JWT>" \
  -H "Content-Type: application/json" \
  -d '{
        "name": "my-research-bot",
        "scopes": "R,B",
        "markets": "Crypto,USStock",
        "instruments": "*",
        "rate_limit_per_min": 120,
        "expires_in_days": 30
      }'
```

Response (the full token is shown **once**):

```json
{
  "code": 0,
  "message": "issued",
  "data": {
    "id": 1,
    "name": "my-research-bot",
    "token": "qd_agent_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "token_prefix": "qd_agent_xxxxxxxx",
    "scopes": ["B","R"],
    "markets": ["Crypto","USStock"],
    "paper_only": true
  }
}
```

Store the `token` value somewhere safe (password manager, secrets store).
The server only keeps a hash — there is no way to recover it later.

### Scope cheat sheet

| Scope | Class                      | Default | Notes |
|-------|----------------------------|---------|-------|
| `R`   | Read                       | yes     | Market data, strategies, jobs |
| `W`   | Workspace write            | no      | Create / patch strategies     |
| `B`   | Backtest / simulation      | no      | Async jobs                    |
| `N`   | Notifications & misc side-effects | no | rate-limited                  |
| `C`   | Credentials                | no      | admin only; not exposed to agents |
| `T`   | Trading / capital          | no      | paper-only by default; live requires opt-in |

---

## 2. Smoke-test the token

```bash
TOKEN=qd_agent_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

curl -s http://localhost:8888/api/agent/v1/health
curl -s http://localhost:8888/api/agent/v1/whoami \
  -H "Authorization: Bearer $TOKEN"
```

`/health` is public; `/whoami` should echo your token's scopes and allowlists.

---

## 3. Read market data (class R)

```bash
curl -s "http://localhost:8888/api/agent/v1/markets" \
  -H "Authorization: Bearer $TOKEN"

curl -s "http://localhost:8888/api/agent/v1/markets/Crypto/symbols?keyword=BTC&limit=5" \
  -H "Authorization: Bearer $TOKEN"

curl -s "http://localhost:8888/api/agent/v1/klines?market=Crypto&symbol=BTC/USDT&timeframe=1D&limit=10" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 4. Run a backtest (class B, async)

```bash
curl -s -X POST http://localhost:8888/api/agent/v1/backtests \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: ma-cross-2024-q1-001" \
  -d '{
        "code": "output = {\"signal\": df[\"close\"] > df[\"close\"].rolling(20).mean()}",
        "market": "Crypto",
        "symbol": "BTC/USDT",
        "timeframe": "1D",
        "start_date": "2024-01-01",
        "end_date": "2024-03-31"
      }'
```

You get back `{ job_id, status: "queued" }`. Poll:

```bash
curl -s "http://localhost:8888/api/agent/v1/jobs/<job_id>" \
  -H "Authorization: Bearer $TOKEN"
```

When `status` becomes `succeeded`, the backtest result is in `result`.

The `Idempotency-Key` header makes retries safe: the second call with the
same key returns the original job instead of submitting a duplicate.

### 4.1 Stream partial results (SSE)

For long-running jobs (`ai-optimize`, `structured-tune`, multi-round
pipelines) the Gateway exposes a Server-Sent Events stream so an LLM client
can react to partial results without polling:

```bash
curl -N "http://localhost:8888/api/agent/v1/jobs/<job_id>/stream" \
  -H "Authorization: Bearer $TOKEN"
```

Frame types:

| Event      | When                                             | Payload |
|------------|--------------------------------------------------|---------|
| `snapshot` | first frame; current row from `qd_agent_jobs`    | full job record |
| `progress` | each call the runner makes to `on_progress(...)` | `{seq, ts, data, terminal}` |
| `ping`     | every ~15s while idle                            | `{ts}` (keepalive) |
| `result`   | once, just before close                          | `{job_id, status, result, error}` |

Reconnect with `?since=<seq>` (or the standard `Last-Event-ID` header) to
resume from a known sequence number. If the job already finished, the server
returns the `snapshot` and `result` frames immediately and closes — your
client doesn't need a separate code path.

---

## 5. Strategies (class R / W)

```bash
# list (R)
curl -s "http://localhost:8888/api/agent/v1/strategies" -H "Authorization: Bearer $TOKEN"

# create (W) — never auto-runs; status defaults to 'stopped'
curl -s -X POST http://localhost:8888/api/agent/v1/strategies \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ "strategy_name": "ma-cross-bot",
        "strategy_type": "IndicatorStrategy",
        "market_category": "Crypto",
        "trading_config": { "symbol": "BTC/USDT", "timeframe": "1D",
                            "initial_capital": 10000, "leverage": 1 } }'
```

Switching `status` to `running` requires a `T` scope on the token (see
the design doc for the rationale).

---

## 6. Trading (class T) — paper-only by default

A token with `T` is hard-gated:

1. The token must explicitly set `paper_only=false` (default is `true`).
2. The deployment must set env `AGENT_LIVE_TRADING_ENABLED=true` to allow live.

Until both are set, every `T` call records a **paper** order in
`qd_agent_paper_orders` using the latest market price as the simulated fill:

```bash
curl -s -X POST http://localhost:8888/api/agent/v1/quick-trade/orders \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ "market": "Crypto", "symbol": "BTC/USDT",
        "side": "buy", "qty": 0.001 }'
```

Cancel any open paper orders for this tenant in one call:

```bash
curl -s -X POST http://localhost:8888/api/agent/v1/quick-trade/kill-switch \
  -H "Authorization: Bearer $TOKEN"
```

---

## 7. Audit & revoke (admin)

```bash
# recent agent calls (this tenant)
curl -s "http://localhost:8888/api/agent/v1/admin/audit?limit=50" \
  -H "Authorization: Bearer <ADMIN_JWT>"

# list / revoke tokens
curl -s "http://localhost:8888/api/agent/v1/admin/tokens" \
  -H "Authorization: Bearer <ADMIN_JWT>"

curl -s -X DELETE "http://localhost:8888/api/agent/v1/admin/tokens/1" \
  -H "Authorization: Bearer <ADMIN_JWT>"
```

Revoking a token sets its status to `revoked`; subsequent calls with that
token return `401`.

---

## 8. MCP integration (optional)

For AI clients that speak MCP (Cursor, Claude-style desktops, cloud agents),
see [`mcp_server/README.md`](../../mcp_server/README.md) for a thin Python
server that wraps the read + backtest subset of the Gateway.

Two transports are supported via `QUANTDINGER_MCP_TRANSPORT`:

* `stdio` (default) — desktop IDEs that spawn the server as a subprocess.
* `sse` / `streamable-http` — cloud agents and remote IDEs that connect to a
  long-running HTTP endpoint. Combine with `QUANTDINGER_MCP_HOST` /
  `QUANTDINGER_MCP_PORT`.

---

## 9. Errors

All `/api/agent/v1/...` errors share this envelope:

```json
{
  "code":      400,
  "message":   "human-readable reason",
  "details":   "...",
  "retriable": false
}
```

| HTTP | Meaning                              | Retry? |
|------|--------------------------------------|--------|
| 401  | Missing / invalid / expired token    | no (re-issue) |
| 403  | Token lacks scope or allowlist       | no |
| 404  | Resource not found in this tenant    | no |
| 429  | Rate limit (per token)               | yes (after 60s) |
| 500  | Internal error                        | sometimes |
| 502  | Upstream data source failure          | yes |
| 501  | Live trading requested but not enabled| no |
