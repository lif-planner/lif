# LiF Planner Roadmap

## Future Enhancements

### Product and UX Redesign

Redesign the app around clear workflows instead of the current accumulated feature surface. The planning model has grown useful, but the UI now exposes too many concepts at once and makes the app feel cluttered, inconsistent, and hard to navigate.

Design reference:
- Use Actual Budget as the main structural reference: local-first, privacy-focused, data-dense, fast, and organized around accounts, budget/plan work, reports, imports, and settings.
- Use YNAB as the mental-model reference: fewer concepts at once, clear jobs/goals, visible true expenses, flexible planning, and obvious next actions.
- Do not copy either product literally; adapt the pattern for future planning, retirement forecasting, German household assumptions, and local/private deployment.

Why this matters:
- Real household finance data is stressful and sensitive; the app should feel calm, legible, and trustworthy.
- New users need a clear path from demo exploration to real-data setup without being confronted by every tool immediately.
- Returning users need fast access to dashboard, analytics, imports, quality checks, and annual review without hunting through dense navigation.
- Each page should answer one primary question and offer a small number of obvious next actions.

Possible scope:
- Define primary user journeys: demo exploration, real-data onboarding, monthly review, import/reconciliation, retirement planning, annual snapshot review, and scenario comparison.
- Replace the crowded top navigation with a clearer information architecture, likely grouping setup/admin/system tools away from planning views.
- Redesign the dashboard as a focused planning cockpit with fewer cards, better hierarchy, and more useful graph-first summaries.
- Defer a full dashboard revision until the surrounding workflows are in place and stable. The dashboard should be redesigned last, once Plan, Accounts, Forecast, Review, Imports, and System pages have clear responsibilities.
- Create a consistent page layout system for list/detail/edit/review pages.
- Reduce duplicated call-to-action buttons and repeated checklist panels.
- Make data quality and readiness warnings contextual instead of showing broad warning blocks everywhere.
- Improve mobile and narrow-width layouts before the read-only iPhone companion work.
- Establish a small design system for spacing, typography, buttons, status pills, tables, forms, and charts.
- Add screenshot or browser-based UI regression checks for the most important pages.
- Review every recently added workflow and decide whether it belongs in primary navigation, secondary navigation, admin/system, or only as a contextual action.

### Projection Snapshots

Allow a user to freeze a point-in-time projection and compare it against reality later.

Why this matters:
- A projection is only useful if the user can later ask whether planning and reality matched.
- Real household finance changes slowly, and comparing against old assumptions helps reveal which inputs were too optimistic, too pessimistic, or missing.
- This is especially important for retirement planning, debt payoff plans, depot assumptions, pensions, and large future expenses.

Initial shape:
- Create a named snapshot from the current household state and projection.
- Store the source date, projection horizon, display mode, assumptions, account balances, debts, rules, people, income streams, pensions, depot values, and generated projection rows.
- Show snapshots in a timeline.
- Let the user open a snapshot read-only.
- Compare a snapshot against current actual data after 3, 6, 12, or more months.

Useful comparison views:
- Planned vs actual liquid balance.
- Planned vs actual net worth.
- Planned vs actual account balances.
- Planned vs actual income and expenses.
- Debt principal planned vs actual.
- Depot value planned vs actual.
- Pension and retirement assumption drift.

Implementation notes:
- Snapshot data should be immutable once created.
- Store enough denormalized data to make old snapshots stable even if calculation logic changes later.
- Record the app/calculation version used to create the snapshot.
- Consider a `ProjectionSnapshot` model plus child rows for monthly/yearly projected values and serialized assumptions.
- Later, add snapshot diff warnings such as "cash is 8% below plan" or "mortgage principal is ahead of plan."

## Low Priority Backlog

These are useful ideas, but they should wait until the core planning model, auditability, and local data workflow feel stable.

### Full Multi-Language Support

Make the application fully translatable, with German as the first language to implement.

Possible scope:
- Add Django internationalization for all UI text, form labels, help text, validation messages, and admin-visible strings.
- Provide German translations first, then keep English as a fallback or second supported language.
- Localize currency, dates, month/year labels, decimal formatting, and export formats.
- Make German financial terms first-class where appropriate, for example Kindergeld, Rentenpunkte, Direktversicherung, Betriebsrente, and Zinsbindung.
- Avoid hard-coded English in seed data, audit pages, roadmap-visible reports, and future exports.
- Add a language switcher once more than one language is complete.
- Add tests or checks that new UI strings are marked for translation.

### YNAB Import and Reconciliation

Import accounts, balances, categories, and recurring cost signals from a local YNAB export or explicitly configured local sync flow.

Possible scope:
- Map YNAB accounts to LiF account types.
- Detect duplicate or missing accounts.
- Reconcile imported balances against current LiF balances.
- Use YNAB category averages to suggest baseline expenses.
- Keep the import local and make any external API use opt-in.

### Depot Price Updates

Track depot holding values from manually entered holdings or imported account balances.

Possible scope:
- Manual price snapshots per holding.
- Optional CSV import from broker exports.
- MoneyMoney import for depot positions and prices, kept local-only.
- Optional price provider integration later.
- Show depot value drift separately from savings-plan contributions.
- Holding-level dividend/distribution planning with cadence, estimated yield or amount, tax rate, and reinvest-vs-cash handling.

### Inflation and Real-Term Views

Add nominal vs inflation-adjusted projections.

Possible scope:
- Household-level inflation assumption.
- Category-specific inflation assumptions for housing, food, childcare, health, and education.
- Toggle charts and audit pages between nominal and real values.

### Tax, Social Insurance, and Health Insurance Assumptions

Model German tax and social-insurance effects more explicitly instead of relying only on net amounts.

Possible scope:
- Track gross vs net income assumptions.
- Health insurance mode: statutory, private, family-insured.
- Retirement phase health and care insurance deductions.
- Keep manual net-entry as the default until this is trustworthy.

### Pension Variants

Extend retirement planning beyond statutory pension and simple private monthly pensions.

Possible scope:
- Direktversicherung.
- Betriebsrente.
- Riester/Rurup-style contracts.
- Lump-sum vs monthly payout assumptions.
- Survivor pension notes for household-level planning.

### Scenario Comparison

Compare multiple scenarios side by side over the same horizon.

Possible scope:
- Base, pessimistic, optimistic, and custom scenarios.
- Compare liquidity, net worth, debt-free date, retirement gap, and stress months.
- Show differences by year.

### Interactive Dashboard Graphs

Add richer dashboard charts with adjustable time frames and interactive exploration.

Possible scope:
- Evaluate chart libraries before implementation, with Plotly.js, Apache ECharts, and Chart.js as initial candidates.
- Prefer a library with built-in or straightforward support for range selectors, range sliders, tooltips, legends, series toggles, and responsive rendering.
- Add time-frame controls such as 12 months, 36 months, 10 years, full horizon, custom from/to, and retirement-only view.
- Add charts for liquid balance, net worth, debt principal, depot value, income vs expenses, pension income, and scenario comparison.
- Keep monthly calculation data available while letting long horizons default to yearly chart aggregation.
- Make chart data available through a structured endpoint or serialized JSON block instead of scraping table values.
- Ensure charts are usable on mobile and on the future read-only iPhone companion.
- Include accessible table fallbacks and export-friendly data.

### Milestones and Life Events

Add explicit planning markers for important family and retirement events.

Possible scope:
- Children finishing school or moving out.
- Elternzeit or temporary part-time work.
- Car replacement.
- Renovation projects.
- Retirement start per adult.
- Debt-free date.

### Export and Reporting

Let users export projections and audits for offline review.

Possible scope:
- CSV export for monthly/yearly projections.
- PDF-style report for a selected scenario.
- Snapshot comparison report once snapshots exist.

### MCP Server for LLM Analytics

Expose LiF data through an MCP server so an LLM can help with forecasting, analytics, and explanation. This should not be designed as a local-only server; it should be deployable as a separate, secured service with explicit access control.

Possible scope:
- Provide read-only tools for household structure, accounts, debts, rules, cash goals, scenarios, projections, snapshots, and audit rows.
- Add explicit tools for common questions such as retirement gap analysis, draw-risk explanation, cashflow stress periods, debt payoff outlook, and scenario comparison.
- Keep the server disabled by default.
- Require explicit configuration before any LLM client can connect.
- Support deployment as a separate service, for example on a private server or VPN-reachable host.
- Require authentication, authorization scopes, request logging, and a clear data exposure model.
- Prefer read-only scoped access for the first version.
- Avoid exposing secrets, Django settings, raw backup files, or unrelated local files.
- Consider a scoped write mode later for creating draft scenarios or reports, but keep the first version read-only.
- Document supported clients, network exposure assumptions, privacy boundaries, and recommended secure deployment patterns.
- Add tests around permission boundaries and payload redaction before considering this safe for real data.

### Validation and Warnings

Add proactive checks for suspicious inputs.

Possible scope:
- Mortgage payment lower than interest due.
- Planning horizon ends before retirement starts.
- Depot holdings total differs from depot account balance.
- Negative cash despite positive net worth.
- Missing end dates for temporary income or expenses.

### Privacy and Backup Workflow

Support local-first data safety without pushing private finance data to third parties.

Possible scope:
- Encrypted local backup export.
- Restore from backup.
- Clear separation between demo data and real household data.
- Local-only documentation for safe updates and migrations.

### Dockerized Deployment

Package the application for repeatable local or self-hosted deployment.

Possible scope:
- Add a production-ready `Dockerfile`.
- Add `docker-compose.yml` for the Django app, persistent database volume, static files, and local environment configuration.
- Keep SQLite as a simple local option, but document when PostgreSQL would be safer.
- Use environment variables for `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`, database path, and backup paths.
- Include migration and static-file steps in the deployment workflow.
- Document backup and restore for Docker volumes.
- Add CI checks that build the image and run Django checks/tests inside the container.
- Avoid sending financial data to any external service by default.

### Read-Only iPhone Companion App

Long-term companion app for viewing planning results on iPhone without editing household data.

Possible scope:
- Read-only dashboard for liquid balance, net worth, debt, depot value, and retirement outlook.
- Mobile-friendly charts for yearly projections, liquidity stress, and scenario comparisons.
- Snapshot viewing and planned-vs-actual comparison once snapshots exist.
- No account editing, imports, rule changes, or sensitive configuration from the phone.
- Local-network sync or encrypted export/import, keeping the local-first privacy model intact.
- Optional biometric app lock for viewing real household finance data.
