# LiF Planner Home Assistant Add-On

This add-on runs LiF as a Home Assistant Supervisor-managed container.

## Current Status

The add-on packaging is experimental. Start with demo data and verify ingress
behavior before entering real household data.

## Data Location

LiF stores mutable data below `/data`, which is persistent add-on storage:

- SQLite database: `/data/lif.sqlite3`
- collected static files: `/data/staticfiles`
- LiF backups: `/data/backups`
- generated Django secret: `/data/lif.env`

Home Assistant backups include this add-on data when the add-on is selected.

## Options

`demo_mode`

When enabled, the add-on seeds synthetic demo data on first start. Disable this
before entering real data in a new install.

`login_required`

When disabled, LiF relies on Home Assistant ingress access control. Enable it if
you expose the optional direct web port.

`allowed_hosts`

Hostnames accepted by Django. The default is `*` because Home Assistant
Ingress and local-network access can present dynamic hostnames, add-on slugs,
IP addresses, or Supervisor proxy hosts. In the add-on, Home Assistant is the
primary access boundary.

If you expose the optional direct web port outside Home Assistant, enable
`login_required` and replace `*` with the exact hostnames or IP addresses you
use.

## Access

The preferred access path is Home Assistant ingress from the sidebar panel. The
direct port is disabled by default. If you enable the direct port for LAN
access, also enable LiF login or keep the app behind another trusted access
control layer.

## Known Gaps

- The add-on points at `ghcr.io/lif-planner/lif`; Home Assistant uses the
  add-on version as the image tag.
- Ingress chart sizing and form flows still need end-to-end testing on more
  Home Assistant installations.
