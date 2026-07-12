# Engineering Guardrails

These notes keep future changes aligned with the current direction of LiF Planner.

## Financial data

- Keep financial calculations in `Decimal` on the Python side.
- Do not serialize money as JSON floats. Use decimal strings or integer cents.
- User-visible calculation changes should have regression tests and, where practical, audit output.

## Frontend safety

- Do not use `innerHTML` in app-owned templates or static JavaScript.
- Build dynamic DOM with `textContent`, `createElement`, and `replaceChildren`.
- Keep chart/table data sourced from structured JSON, not scraped rendered text.

## Projection design

- Keep projection behavior explainable from opening balances plus applied line items.
- When adding a new financial domain, add direct tests for timing, cash effect, net-worth effect, and audit lines.
- If the projection loop grows further, prefer extracting domain-specific helpers over adding more logic directly to the main loop.

## Analytics design

- Interactive analytics should remain on its own page while the dashboard stays scannable.
- Prefer reusable static JavaScript modules once chart behavior grows beyond simple page-local code.
- Long horizons should default to yearly views while preserving monthly calculation data.

## Feature flags

- In-progress user-facing features should be hidden behind a named feature flag until they are ready.
- Hide feature links in templates and protect direct view access with `feature_required`.
- Prefer removing flags once a feature is stable instead of letting stale conditionals accumulate.

## Internationalization

- New user-facing UI text should be easy to translate.
- German is the first target language, so avoid hard-coding locale-sensitive date, number, and currency behavior when adding new UI.
