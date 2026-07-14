# Ingress Test Checklist

Use this checklist before recommending the Home Assistant add-on for real data.

## Install And Start

- Add `https://github.com/lif-planner/home-assistant-addon` as a custom add-on
  repository.
- Install **LiF Planner**.
- Keep `demo_mode: true` for the first run.
- Keep `login_required: false` for the first ingress test.
- Start the add-on and confirm it stays running.

## Ingress UI

- Open LiF from the Home Assistant sidebar.
- Confirm the dashboard loads without a Django `400 Bad Request`.
- Confirm CSS and JavaScript load.
- Open **Forecast -> Analytics** and confirm ECharts charts render.
- Open pages with forms and confirm redirects stay inside the ingress path.
- Confirm `/health/` returns healthy through the add-on logs or direct port
  when enabled.

## Data And Backups

- Confirm demo data is seeded once, not on every restart.
- Restart the add-on and confirm data remains.
- Create a Home Assistant backup including the add-on.
- Restore the backup on a test instance and confirm `/data/lif.sqlite3` content
  survives.

## Optional Direct Port

- Enable the optional direct web port.
- Keep the default `allowed_hosts: ["*"]` for ingress testing. If you expose the
  optional direct web port, enable LiF login and optionally restrict
  `allowed_hosts` to the LAN hostname/IP.
- Enable `login_required: true` before entering real data.
- Confirm direct access requires LiF login.

## Known Items To Watch

- Static asset paths under ingress.
- Login/logout redirects under ingress.
- CSRF behavior behind Home Assistant's reverse proxy.
- Chart sizing inside the Home Assistant panel frame.
