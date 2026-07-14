# Changelog

All notable public changes to LiF will be documented here.

This project follows semantic versioning after the first public release.

## [Unreleased]

No unreleased changes yet.

## [1.1.9] - 2026-07-14

### Added

- Home Assistant add-on option `seed_demo_on_start` to insert demo households on
  startup when no demo household exists.

### Fixed

- Privacy mode now persists under Home Assistant Ingress using the same
  iframe-local query state as language selection.

## [1.1.8] - 2026-07-14

### Fixed

- Home Assistant Ingress path handling now also works when Supervisor does not
  forward `X-Ingress-Path`, fixing language and privacy controls behind
  Cloudflare/Safari request shapes.

## [1.1.7] - 2026-07-14

### Fixed

- Home Assistant Ingress language selection now stays on the selected language
  even when add-on cookies are not preserved by the ingress proxy.
- Privacy mode can now be toggled from Home Assistant Ingress without CSRF
  failures.

## [1.1.6] - 2026-07-14

### Fixed

- Language selection now persists after switching under Home Assistant Ingress
  instead of falling back to the browser/Home Assistant language on the next
  page load.

## [1.1.5] - 2026-07-14

### Fixed

- Language selection under Home Assistant Ingress now redirects back to the LiF
  ingress path instead of loading Home Assistant UI content inside the LiF
  panel.

## [1.1.4] - 2026-07-14

### Fixed

- Home Assistant demo-mode startup now checks whether demo households actually
  exist before skipping seeding, so a stale `/data/.demo_seeded` marker no
  longer leaves the add-on with an empty database.
- Language selection no longer fails with CSRF errors when posted through Home
  Assistant Ingress.

## [1.1.3] - 2026-07-14

### Fixed

- Home Assistant Ingress pages now generate static asset URLs with the ingress
  prefix, fixing missing CSS and JavaScript when the add-on is opened through
  Home Assistant.

## [1.1.2] - 2026-07-14

### Fixed

- Home Assistant Ingress requests with dynamic `X-Ingress-Path` prefixes now
  route correctly and generated LiF links keep the ingress prefix.

## [1.1.1] - 2026-07-14

### Fixed

- Home Assistant add-on deployments now default Django `ALLOWED_HOSTS` to `*`
  when running under Home Assistant/Supervisor, avoiding 400 Bad Request errors
  caused by dynamic ingress or local-network hostnames.

## [1.1.0] - 2026-07-14

### Added

- Experimental Home Assistant add-on packaging staged in the main repository
  and published as a dedicated add-on repository.
- GHCR container image workflow with smoke testing and multi-architecture
  `amd64`/`arm64` publishing.
- App-version GHCR tags so Home Assistant add-on versions can resolve to
  matching LiF container image tags.
- Home Assistant add-on validation, release notes, ingress checklist, and
  icon/logo assets.

### Fixed

- Container smoke-test cleanup now tolerates root-owned files created inside
  temporary Docker `/data` mounts.

## [1.0.0] - 2026-07-12

### Added

- Public-release checklist for moving the private project toward an open-source
  v1.0.
- Public project files for security reporting, contributing, and community
  conduct.
- GitHub CI workflow for Django checks and tests.
- Public release process documentation and a guarded helper script for creating
  the clean orphan `v1.0.0` release branch.
- Third-party `NOTICE.md` and a public-readiness scan for private-looking
  repository references.
- Clean public checkout simulation script for validating tracked-file exports.
- Public release target set to `github.com/lif-planner/lif` with the
  `LiF Maintainers <yogitea@users.noreply.github.com>` initial commit identity.

### Changed

- Deployment examples now use neutral placeholders instead of private machine
  paths and LAN addresses.
- License switched to MIT to keep public use, forks, and contributions simple.

Initial public release scope:

- local-first Django household planner
- German-style family, retirement, pension, depot, debt, and cash-flow planning
- demo seed data
- Docker Compose and Ansible deployment paths
- local import/reconciliation workflow
- projection audit and data-confidence views
