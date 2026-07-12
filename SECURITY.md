# Security Policy

LiF is a local-first household finance planner. It is designed for private
self-hosting and does not include telemetry, hosted sync, or third-party
finance API calls by default.

## Supported Versions

Security fixes are provided for the current `main` branch until formal releases
begin. After the first public release, supported versions will be documented in
the changelog.

## Reporting A Vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub
private vulnerability reporting if it is enabled on the repository, or contact
the maintainer through the private channel listed on the public project page.

Include:

- the affected LiF version or commit
- whether the app was running with `DJANGO_DEBUG=0`
- whether `LIF_REQUIRE_LOGIN=1` was enabled
- a short reproduction path
- any relevant deployment details, without sharing real financial data

## Threat Model

LiF assumes your real-data instance runs on a trusted local machine, VPN, or LAN.
Do not expose it directly to the public internet.

For any instance containing real data:

- set `DJANGO_DEBUG=0`
- set a unique `DJANGO_SECRET_KEY`
- set `LIF_REQUIRE_LOGIN=1`
- restrict `DJANGO_ALLOWED_HOSTS`
- keep the SQLite database, backups, env files, and Ansible vault files out of Git
- run `pipenv run python manage.py check_production`

The demo mode is intentionally easy to open and reset. Do not enter private
household data into a no-login demo deployment.
