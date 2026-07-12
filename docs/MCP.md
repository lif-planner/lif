# LiF MCP server (read-only)

A local [Model Context Protocol](https://modelcontextprotocol.io) server that
lets an external LLM (Claude Desktop, Claude Code, etc.) inspect LiF's modeled
inputs, planning assumptions, and the computed projection — so you can ask it to
check whether anything is inconsistent with what LiF computed.

It is **read-only** and **off by default**, gated behind the `mcp_server`
feature flag.

## Enable it

1. Install the optional dependency:
   ```
   pipenv install mcp
   ```
2. Turn on the feature flag, either:
   - in Django admin → Feature flags → `mcp_server` → enabled, or
   - per-process: `LIF_FEATURE_MCP_SERVER=1`
3. Start the server (stdio transport):
   ```
   pipenv run python manage.py run_mcp_server
   ```

The flag is re-checked on every tool call, so turning it off disables access
without restarting.

## Connect a client

Point an MCP-capable client at the command. For Claude Desktop, add to its MCP
config (adjust the path):

```json
{
  "mcpServers": {
    "lif": {
      "command": "pipenv",
      "args": ["run", "python", "manage.py", "run_mcp_server"],
      "env": { "LIF_FEATURE_MCP_SERVER": "1" }
    }
  }
}
```

## Tools (all read-only)

| Tool | Returns |
|------|---------|
| `overview` | Household, assumptions, current totals, counts, projection endpoints, quality summary. Start here. |
| `assumptions` | All planning knobs (inflation, tax/health rates, horizon). Differences from another model usually start here. |
| `inputs` | Every modeled input: people, accounts, holdings, debts, money/transfer rules, private loans, retirement plans, equity grants, income investments, true expenses, cash goals, scenarios, child milestones, salary changes. |
| `projection` | Computed rows; `granularity` = `yearly` (default) or `monthly`. |
| `audit_lines` | Aggregated per-line cash/asset/liability effects — the trail to verify computed totals. |
| `quality_report` | LiF's own conformance/health findings. |
| `debt_schedules` | Per-debt payoff month, months to payoff, lifetime interest, ending principal. |
| `retirement_analysis` | Per retirement-year tax-aware summary (net income, cash gap, gross draw needed). |

## Privacy

The tools expose real financial data. A cloud LLM client sends that data to its
provider. Keep this in mind, or point a local model at the same server.
