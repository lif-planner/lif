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

Additional hostnames accepted by Django. Keep this narrow for real data.

## Access

The preferred access path is Home Assistant ingress from the sidebar panel. The
direct port is disabled by default.

## Known Gaps

- The add-on points at `ghcr.io/lif-planner/lif`; Home Assistant uses the
  add-on version as the image tag.
- Ingress URL handling still needs end-to-end testing with charts, login, and
  static files.
