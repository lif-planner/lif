import json

from django.core.management.base import BaseCommand, CommandError

from planner import mcp_data
from planner.feature_flags import feature_enabled


class Command(BaseCommand):
    help = (
        "Run the local read-only LiF MCP server over stdio. Lets an external LLM "
        "inspect inputs, assumptions, and the computed projection. Requires the "
        "'mcp_server' feature flag and the optional 'mcp' package."
    )

    def handle(self, *args, **options):
        if not feature_enabled("mcp_server"):
            raise CommandError(
                "The 'mcp_server' feature flag is disabled. Enable it in Django admin "
                "or with LIF_FEATURE_MCP_SERVER=1 before starting the server."
            )

        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError as exc:
            raise CommandError(
                "The 'mcp' package is not installed. Install it (e.g. `pipenv install mcp`) "
                "to run the MCP server."
            ) from exc

        server = FastMCP("LiF")

        def _result(name, arguments=None):
            return json.dumps(mcp_data.call_tool(name, arguments), indent=2, ensure_ascii=False)

        # Thin wrappers: the flag is re-checked inside call_tool on every request,
        # so toggling the flag off disables the tools without restarting.
        @server.tool()
        def overview() -> str:
            """Household, assumptions, current totals, counts, projection endpoints, and a quality summary. Start here."""
            return _result("overview")

        @server.tool()
        def assumptions() -> str:
            """All planning assumptions (currency, horizon, inflation, tax and health-insurance rates)."""
            return _result("assumptions")

        @server.tool()
        def inputs() -> str:
            """Every modeled input: people, accounts, holdings, debts, rules, retirement plans, equity grants, income investments, true expenses, cash goals, scenarios, child milestones, salary changes."""
            return _result("inputs")

        @server.tool()
        def projection(granularity: str = "yearly") -> str:
            """The computed projection rows. granularity='yearly' (default) or 'monthly'."""
            return _result("projection", {"granularity": granularity})

        @server.tool()
        def audit_lines() -> str:
            """Aggregated audit lines explaining every cash/asset/liability effect across the projection."""
            return _result("audit_lines")

        @server.tool()
        def quality_report() -> str:
            """LiF's own conformance/health findings (the things it already flags as off)."""
            return _result("quality_report")

        @server.tool()
        def debt_schedules() -> str:
            """Per-debt amortization summary: payoff month, months to payoff, lifetime interest, ending principal."""
            return _result("debt_schedules")

        @server.tool()
        def retirement_analysis() -> str:
            """Per retirement-year tax-aware summary: net income, cash gap, and the gross depot draw needed."""
            return _result("retirement_analysis")

        @server.tool()
        def income_timeline() -> str:
            """Year-by-year income broken out by source; each year's sources reconcile to total income."""
            return _result("income_timeline")

        self.stderr.write("Starting LiF MCP server (read-only) over stdio...")
        server.run()
