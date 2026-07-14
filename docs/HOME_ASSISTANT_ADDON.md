# Home Assistant Add-On Plan

LiF can run as a Home Assistant add-on because the production container already
stores mutable runtime data under `/data`, which maps well to Supervisor-managed
add-on storage.

The add-on packaging starts in this repository under
`homeassistant-addon/lif/`. Once the packaging is stable, move or mirror that
folder into a dedicated `lif-planner/home-assistant-addon` repository so Home
Assistant users can add it as a custom add-on repository.

`homeassistant-addon/repository.yaml` is intentionally placed inside that
staging folder. When we split the add-on packaging into its own repository,
that file becomes the root-level Home Assistant repository descriptor.

## Target Shape

- Home Assistant ingress is the preferred UI access path.
- The direct port remains disabled by default.
- SQLite data, static files, backups, and generated secrets live under `/data`.
- Home Assistant backups capture the add-on data.
- Demo mode can seed synthetic data on first start.
- Login is disabled by default for ingress use and can be enabled when exposing
  a direct LAN port.

## Iteration Plan

1. Add the add-on skeleton in `homeassistant-addon/lif/`.
2. Test local add-on builds on Home Assistant OS or a supervised install.
3. Verify ingress behavior for static files, charts, redirects, CSRF, and login.
4. Publish a multi-arch GHCR image for LiF and update the add-on to consume it.
5. Split the packaging into a dedicated Home Assistant add-on repository.
6. Add screenshots and install docs for Home Assistant users.

## Current Limitations

- The experimental add-on Dockerfile builds LiF from the public Git repository.
- The add-on is marked `experimental`.
- Ingress behavior has not yet been verified on a real Home Assistant instance.
- Dedicated add-on repository publishing is not implemented yet.
- The current main LiF repository is not meant to be added directly to Home
  Assistant as an add-on repository; use the staged folder as the source for the
  future dedicated repository.

## Local Development Notes

Home Assistant add-ons are built from the add-on directory as their Docker
context. That means a Dockerfile inside `homeassistant-addon/lif/` cannot simply
`COPY` the parent LiF source tree. For now, the Dockerfile clones the public
repository during image build. Long term, publishing `ghcr.io/lif-planner/lif`
and pointing the add-on at that image will be cleaner.
