# Going Public - LiF v1.0 Checklist

This document tracks what must be true before LiF moves from a private personal
repository to a public open-source project. It is intentionally operational:
every item should either be done, explicitly deferred, or called out as a
release blocker.

Status: **release export ready after final checks**.

## Release Principles

- Keep LiF local-first: no telemetry, no hosted sync, no third-party finance
  API by default, and no real household data in Git.
- Treat this like a finance app, not a toy demo: security notes, test coverage,
  reproducible setup, and clear data-safety messaging matter.
- Start the public repository from a fresh history. The current private history
  contains personal author metadata and internal iteration chatter and must not
  be made public.
- Prefer a small, credible v1.0 over a broad launch. A hosted public demo and a
  polished marketing site can follow after the code repository is public.

## Decisions

| Decision | Status | Notes |
| --- | --- | --- |
| License | Decided | MIT. `LICENSE` is already present. |
| Git history | Decided | Public `main` starts from one fresh orphan commit. |
| Repo location/name | Decided | Publish to `github.com/lif-planner/lif`. |
| Public commit author | Decided | `LiF Maintainers <yogitea@users.noreply.github.com>`. |
| Hosted demo | Deferred | Recommended for v1.1, not a v1.0 blocker. |
| Public landing page | Partial | Static `gh-pages` content exists; enable Pages only after the repo is public. |

## Current Public-Readiness Findings

### Already In Good Shape

- `LICENSE` exists and is MIT.
- `.gitignore` excludes local SQLite databases, env files, Ansible vault files,
  local private seeds, deployment data, logs, static build output, and media.
- Demo and example data are synthetic.
- `scripts/scan_secrets.py` exists and commit hooks run it before commits.
- User/admin docs exist: `docs/USER_GUIDE.md` and `docs/ADMIN_GUIDE.md`.
- Local-first deployment paths are documented for Docker Compose, Ansible/LXC,
  and Mac.
- Vendored ECharts assets include upstream license and notice files.
- `NOTICE.md` summarizes bundled third-party notices and the optional
  `py-money` connector license posture.

### Must Fix Before Public

1. **Fresh public history**
   - Do not make the current private repository visible as-is.
   - Finish all cleanup on private `main`.
   - Create an orphan release branch/commit as the first public commit.
   - Tag that commit `v1.0.0`.

2. **Final scans**
   - Run secret scan across tracked files.
   - Run a personal-reference scan for local paths, hostnames, names, and
     private repo assumptions.
   - Run full tests and production checks.

### Landed During Public Cleanup

- Generic deployment examples now use `YOUR_USERNAME`, the public
  `lif-planner/lif` repo URL, and documentation-only IP addresses.
- Public project files exist: `SECURITY.md`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `CHANGELOG.md`, issue templates, and pull-request
  template.
- GitHub-hosted CI exists at `.github/workflows/ci.yml`.
- Runtime dependencies in `Pipfile` use explicit compatible ranges instead of
  wildcards, and `Pipfile.lock` is current.
- `README.md` now opens with the public-facing project pitch, local-first data
  posture, German household focus, and a quick demo path.
- The raw private engineering log was removed from the tracked root and moved
  to ignored local path `docs/internal/FIXME.md`.
- `py-money` is public and listed by GitHub as BSD-2-Clause licensed. It has no
  published GitHub releases, so LiF keeps using a pinned git ref.
- `docs/RELEASE.md` documents the clean public-repo export process.
- `scripts/create_public_release_branch.sh` can create the orphan
  `public-release` branch and `v1.0.0` tag locally after explicit confirmation.
- `scripts/scan_public_readiness.py` scans tracked files for private-looking
  paths, hostnames, LAN IPs, personal domains, and private repo URLs.
- `scripts/simulate_public_checkout.sh` exports tracked files into a clean temp
  checkout and verifies the demo boot path works without ignored/private files.
- `lif/version.py` now defaults to `1.0.0`.
- `CHANGELOG.md` has a dated `1.0.0` release entry.

### Still To Verify Before Public

- Final checks pass immediately before exporting the orphan public branch.

## Recommended Execution Order

1. Update this checklist as cleanup work lands.
2. Follow `docs/RELEASE.md` for the final public export.
3. Run release checks:

   ```bash
   python3 scripts/scan_secrets.py
   python3 scripts/scan_public_readiness.py
   ./scripts/simulate_public_checkout.sh
   pipenv run python manage.py test
   pipenv run python manage.py check
   pipenv run python manage.py check_production
   ```

4. Create the fresh public history with the guarded helper:

   ```bash
   CONFIRM_PUBLIC_RELEASE=1 ./scripts/create_public_release_branch.sh
   ```

5. Push that fresh history to the chosen public destination.
6. Enable GitHub Pages from the existing `gh-pages` branch after the repo is
    public.

## Non-Blockers For v1.0

- Hosted read-only demo.
- Custom domain.
- README screenshots or a short GIF.
- Fully complete German translation.
- Full UI redesign.
- PDF/report export polish.

## Notes For Future Public Contributors

- Financial calculations should stay in `Decimal`.
- Calculation behavior changes need regression tests and audit output where
  practical.
- User-facing work should respect the local-first/no-telemetry posture.
- New UI text should be translatable; German is the first non-English target.
