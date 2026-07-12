# Real Data Review Cadence

LiF can only be as trustworthy as the data and assumptions behind the projection. Use a small recurring review loop before relying on the app for real household decisions.

## Monthly

Reconcile account balances after the latest bank, depot, and MoneyMoney data is available.

- Run a backup before imports or manual bulk changes.
- Update cash, savings, depot, debt, and private-loan balances.
- Open `/quality/` and resolve high-severity issues first.
- Open `/ledger/` and scan the latest cash movements for unexpected transfers.
- Check whether the emergency fund still matches the current monthly expense baseline.

## Quarterly

Review data sources and investment assumptions.

- Confirm imported accounts still map to the right local accounts.
- Check depot holdings, payout dates, bond maturity amounts, and distribution assumptions.
- Review savings interest rates and tax assumptions for interest/dividend income.
- Review RSU grants, bonuses, salary changes, and planned investment purchases.
- Create a snapshot after the quarter-end data is clean.

## Annually

Review long-range planning assumptions.

- Open `/assumptions/` and mark reviewed assumptions with notes.
- Refresh inflation, tax, health-insurance, pension, and depot growth assumptions.
- Review mortgage refinance assumptions and private-loan repayment dates.
- Update cash goals for the next years.
- Create an annual snapshot review and record what changed versus last year's plan.

## Before Major Decisions

Run an extra review before choices such as a property transfer, debt payoff, major gift, career change, or large investment purchase.

- Create or clone a household/scenario before changing real-data assumptions.
- Capture a snapshot before and after the decision model.
- Use projection audit pages to verify the relevant months or years.
- Document the decision assumptions in notes, not just in numbers.

## Warning Signs

Do not rely on a projection until these are understood:

- Reconciliation errors or projection integrity failures.
- Accounts with low data confidence.
- Expired assumption reviews.
- Large unexplained jumps in liquid balance, invested balance, debt, or net worth.
- Seed/import changes that were not followed by a backup and smoke test.
