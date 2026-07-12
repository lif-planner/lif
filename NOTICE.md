# Third-Party Notices

LiF is released under the MIT license. This file summarizes notable bundled or
directly integrated third-party components.

## Apache ECharts

LiF vendors Apache ECharts for interactive charts:

- `planner/static/vendor/echarts.min.js`
- `planner/static/vendor/echarts.LICENSE.txt`
- `planner/static/vendor/echarts.NOTICE.txt`

Apache ECharts is licensed under the Apache License 2.0. The upstream license
and notice files are included next to the vendored JavaScript file.

## py-money

LiF can optionally use `py-money` as a local MoneyMoney connector:

- upstream: `https://github.com/MirkoDziadzka/py-money`
- license: BSD-2-Clause, as reported by GitHub
- dependency pin: see `Pipfile` / `Pipfile.lock`

The MoneyMoney integration is local-only and feature-flagged. No hosted sync or
third-party finance API is enabled by default.

## Runtime Dependencies

Python runtime dependencies are listed in `Pipfile` and pinned in
`Pipfile.lock`. The lockfile is the source of truth for reproducible installs.
