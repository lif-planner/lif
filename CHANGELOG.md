# Changelog

All notable public changes to LiF will be documented here.

This project follows semantic versioning after the first public release.

## [Unreleased]

No unreleased changes yet.

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
