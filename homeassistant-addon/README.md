# LiF Planner Home Assistant Add-On Repository

This is the Home Assistant add-on repository for LiF Planner.

## Add-On Contents

- `lif/` - LiF Planner add-on metadata, docs, translations, and runtime script.
- `repository.yaml` - Home Assistant add-on repository descriptor.
- `docs/` - release and ingress test notes.
- `scripts/` - validation and sync helpers.

## Install

1. In Home Assistant, go to **Settings -> Add-ons -> Add-on Store**.
2. Open the three-dot menu and choose **Repositories**.
3. Add:

   ```text
   https://github.com/lif-planner/home-assistant-addon
   ```

4. Install **LiF Planner**.
5. Start with `demo_mode: true` and open it through the Home Assistant sidebar.

## Validate

Run:

```bash
scripts/validate.sh
```

## Current Status

The add-on is experimental. It uses the public image:

```text
ghcr.io/lif-planner/lif
```

Home Assistant uses `lif/config.yaml` `version` as the image tag.

Before entering real household data, verify that Home Assistant ingress works
for static files, charts, redirects, backups, and optional login.
