# User Guide

This is a walkthrough for using LiF once it's running -- what the concepts
mean and how the pages fit together. For installing/self-hosting LiF, see
[ADMIN_GUIDE.md](ADMIN_GUIDE.md) instead.

## What LiF is

LiF is a local-first planning tool for household finances and long-horizon
retirement/FIRE projections, built around a German household: Kindergeld,
statutory pension assumptions, depot (brokerage) holdings with
Vorabpauschale/Teilfreistellung-aware tax handling, and mortgage refinance
modeling. Everything you enter stays in your own database; there's no
account, no cloud sync, and no telemetry.

## Core concepts

- **Household** -- the top-level container for one family's planning data.
  You can have more than one (see "Multiple households" below), but you're
  always working inside exactly one at a time.
- **People** -- the adults and children in the household. Adults can have
  income rules, retirement plans, and equity grants attached to them;
  children can have milestones (e.g. "starts daycare") and Kindergeld-style
  income.
- **Accounts** -- cash, savings, depot (brokerage), and loan accounts. Depot
  accounts can either carry a single flat balance, or be valued as the sum
  of individually tracked **depot holdings** (specific ETFs/stocks/bonds,
  each with its own price, quantity, and optional distribution rate).
- **Rules** -- recurring income or expense line items (salary, rent,
  subscriptions, etc.), each with a start/end month.
- **Transfer rules** -- recurring moves of money between your own accounts
  (e.g. "€500/month from Giro to Depot"), as opposed to income/expense rules
  which model money entering or leaving the household entirely.
- **The projection** -- LiF calculates a month-by-month forecast from today
  out to your configured planning horizon, applying every active rule,
  transfer, debt payment, distribution, etc. in sequence. Every number the
  projection produces can be traced back to its inputs on an **audit page**
  (Month view / Year view) -- nothing is a black box.
- **Scenarios** -- a scenario is a "what if" variant of your household (e.g.
  "what if I retire two years earlier") that you can compare side by side
  with your main plan, without touching your real data.

## Getting started

1. **First run**: on a fresh checkout with no data yet, you're redirected to
   **Household setup** (`/setup/`). This is where you set the household's
   starting balance, start month, planning horizon, and core assumptions
   (tax rates, inflation) before anything else.
2. **Add people**: add each adult and child so income, expenses, and
   milestones can be attributed to someone.
3. **Add accounts**: use the guided **Account setup** wizard
   (`/setup/accounts/`) to add your cash, savings, depot, and loan accounts.
   It can also create a first depot holding or debt repayment plan for you
   inline, so you don't have to hop between pages.
4. **Add income and expenses**: go to **Plan -> Income & expenses** and add
   your recurring salary, rent, subscriptions, etc.
5. **Check the dashboard**: once the basics are in, the dashboard gives you
   an at-a-glance summary and links into the deeper planning areas.
6. **Demo data**: a fresh checkout can be seeded with realistic (fake)
   sample data instead, so you can explore every feature before entering
   anything real. If a household is marked as demo data, the app shows a
   banner reminding you it's safe to explore/reset and not to enter private
   financial data there.
7. **Going from demo to real**: when you're ready to enter your own numbers,
   use **Settings -> Real data start** (`/setup/real-data/start/`). It walks
   through a short readiness checklist (switch out of demo mode, name the
   household, take a backup) before you start entering private data, and
   **Settings -> Onboarding** shows the same kind of checklist for general
   setup completeness.

## Everyday use

The sidebar groups everything into five areas:

### Money

- **Accounts** -- your cash/savings/depot/loan accounts and their balances.
- **Depot holdings** -- individual ETFs/stocks/bonds inside your depots,
  each with its own price, quantity, and (optionally) its own distribution
  rate and cadence.
- **Debts** -- mortgages and loans, with interest/principal split,
  fixed-interest periods, and refinance assumptions.
- **Properties** -- real estate (residence or investment), with
  appreciation, carrying costs, and planned transfers/sales.

### Plan

- **Income & expenses** -- all your recurring rules, transfer rules, family
  gifts, and planned investment purchases in one place (this is also where
  you'll find the "Future changes" list of anything scheduled to start
  later).
- **Transfers** -- a focused view of just the transfer rules moving money
  between your own accounts.
- **Cash goals** -- a yearly spending target used to calculate how much your
  portfolio would need to cover if you're drawing from it (FIRE-style).
- **Retirement plans** -- German statutory pension and private pension
  income modeling per person.

### Forecast

- **Analytics** -- the main chart page: liquid balance, net worth, and
  depot value over time, plus cash-flow and goal-coverage charts, with
  labeled milestone markers for every planned change (retirement start,
  debt payoff, equity vesting, investment purchases, etc.).
- **Goal planner** -- works backward from a target (e.g. a retirement date
  or net-worth goal) to see what has to be true to get there.
- **Assumptions** -- a single registry of every rate/assumption in play
  (tax rates, depot return/distribution rates, inflation, etc.) with a
  confidence indicator for whether it's a real, reviewed number or still a
  default.
- **Integrity** -- a technical check that projection math reconciles
  (every account's ending balance matches its opening balance plus applied
  line items).
- **Income** -- the income timeline, with a `source` filter so you can
  isolate one income stream (e.g. just depot distributions) across the
  whole horizon.
- **Month view / Year view** -- the audit pages: every line item that
  changed a balance in a given month or year, in full detail.
- **Year report** -- a yearly summary, also available as a slide-style view
  for presenting the plan.
- **Scenarios** -- compare your main plan against one or more cloned
  what-if variants.

### Data health

- **Health checks** -- a data-quality report: missing information, likely
  double-counted income, stale account data, and similar planning gaps.
- **Reconciliation** -- compares your actual recorded balances against what
  the projection expected, to catch drift early.
- **Change history** -- an audit trail of edits to planning data.
- **Snapshots** -- freeze the current plan and compare it against reality
  later (useful for an annual review).
- **Imports** -- preview-first CSV imports for accounts and depot holdings
  (upload, review a dry-run table, then apply). MoneyMoney import exists as
  an experimental feature flag for Mac-based households.

### Settings

Household setup, household settings, onboarding, backups, system status,
feature flags, and the Django admin, plus the demo-to-real-data workflow
described above.

## Feature deep-dives

### Depot holdings and distributions

A depot account can be valued two ways:

- **Flat balance**: one number for the whole depot, with one blended
  distribution rate/cadence on the account itself.
- **Sum of holdings**: each holding (ETF, stock, bond) is tracked
  individually with its own price, quantity, and its own distribution rate
  and cadence -- useful when a depot mixes accumulating and distributing
  funds, since not every holding pays out. Distributions scale with the
  depot's own configured return rate over time, rather than staying flat
  forever.

### Planned investment purchases

If you know you'll move cash into a specific ETF at a future date, add a
**planned investment purchase** (Plan -> Income & expenses -> "Add
investment purchase") rather than a generic transfer rule. It debits your
chosen funding account, credits the depot, and -- if you set a distribution
rate -- pays out on that specific ETF's own rate and cadence from that month
onward, instead of your generic cash movement being invisible to (or
worse, smeared across) your other holdings' distribution modeling.

### Family gifts

Model a planned gift to a child (e.g. seeding a Kinderdepot) with its own
German gift-tax allowance window tracking, separate from regular transfer
rules, so the allowance-window math doesn't get mixed up with day-to-day
account transfers.

### Real estate

Track your residence or an investment property with appreciation, carrying
costs, mortgage linkage, and a planned transfer/sale event, all reflected
in net worth and liquidity projections.

### Cash goals and portfolio draws

Set a yearly cash need; if "fund cash goal from depot" is enabled on the
household, the projection automatically models drawing from your portfolio
to cover it, with capital-gains tax and the Teilfreistellung partial
exemption applied.

### Scenarios

Clone your household into a scenario, tweak it (retire earlier, change a
return assumption, disable something), and compare the two side by side on
the Scenarios page without touching your real plan.

### Privacy mode

Toggle **Privacy** in the sidebar to mask all money amounts on screen --
useful when sharing your screen or showing the app to someone else without
exposing real numbers.

### Multiple households

You can create more than one household (e.g. a real one and a scratch/demo
one) and switch between them from the household switcher at the top of the
sidebar. Each household's data is completely separate.

## Tips

- **Keyboard navigation**: on the Month view / Year view audit pages, use
  the arrow keys to step forward/back a period at a time.
- **Language**: use the language selector in the sidebar to switch to
  German; the shared navigation and an increasing number of pages are fully
  translated.
- **Read-only mode**: if an administrator has turned on read-only mode
  (e.g. during maintenance), planner pages stay viewable but editing is
  disabled everywhere except Django admin.

## Where to go next

- [ADMIN_GUIDE.md](ADMIN_GUIDE.md) -- installing and self-hosting LiF.
- [REAL_DATA_SEEDING.md](REAL_DATA_SEEDING.md) -- keeping real household data
  organized and out of Git.
- [REAL_DATA_REVIEW_CADENCE.md](REAL_DATA_REVIEW_CADENCE.md) -- a suggested
  monthly/quarterly/yearly review rhythm to keep the plan trustworthy.
- [MONEYMONEY_IMPORT.md](MONEYMONEY_IMPORT.md) -- details on the
  MoneyMoney import path, if you're on a Mac.
- The repo root `ROADMAP.md` for what's planned next.
