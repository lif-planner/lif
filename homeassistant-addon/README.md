# LiF Planner Home Assistant Add-On Repository

This repository packaging is staged from the main LiF source repository. It is
intended to become the root of a dedicated Home Assistant add-on repository at:

```text
https://github.com/lif-planner/home-assistant-addon
```

## Add-On

- `lif/` - LiF Planner add-on metadata, docs, translations, and runtime script.
- `repository.yaml` - Home Assistant add-on repository descriptor.

## Install

Once this folder is published as its own GitHub repository:

1. In Home Assistant, go to **Settings -> Add-ons -> Add-on Store**.
2. Open the three-dot menu and choose **Repositories**.
3. Add:

   ```text
   https://github.com/lif-planner/home-assistant-addon
   ```

4. Install **LiF Planner**.
5. Start with `demo_mode: true` and open it through the Home Assistant sidebar.

## Current Status

The add-on is experimental. It uses the public image:

```text
ghcr.io/lif-planner/lif:main
```

Before entering real household data, verify that Home Assistant ingress works
for static files, charts, redirects, backups, and optional login.
