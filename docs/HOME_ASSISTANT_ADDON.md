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
2. Publish a multi-arch GHCR image for LiF and update the add-on to consume it.
3. Test local add-on installs on Home Assistant OS or a supervised install.
4. Verify ingress behavior for static files, charts, redirects, CSRF, and login.
5. Split the packaging into a dedicated Home Assistant add-on repository.
6. Add screenshots and install docs for Home Assistant users.

## Export To Dedicated Repository

After creating `lif-planner/home-assistant-addon` on GitHub, export the staged
add-on repository:

```bash
./scripts/export_homeassistant_addon_repo.sh ../home-assistant-addon
cd ../home-assistant-addon
git push -u origin main
```

The export script copies `homeassistant-addon/` into a clean standalone Git
repository, creates an initial commit with the public maintainer identity, and
sets `git@github.com:lif-planner/home-assistant-addon.git` as `origin`.

The staged add-on repository includes its own CI, validation scripts, release
notes, ingress test checklist, and Home Assistant icon/logo assets. Keep those
files synchronized when changing the add-on packaging in the main LiF repo.

## Current Limitations

- The add-on metadata points at `ghcr.io/lif-planner/lif`, published by GitHub
  Actions from public `main` and version tags.
- The experimental add-on Dockerfile still exists as a source-build fallback
  while the dedicated add-on repository is not split out.
- The add-on is marked `experimental`.
- Ingress behavior has not yet been verified on a real Home Assistant instance.
- Dedicated add-on repository publishing is not implemented yet.
- The current main LiF repository is not meant to be added directly to Home
  Assistant as an add-on repository; use the staged folder as the source for the
  future dedicated repository.

## Local Development Notes

Home Assistant add-ons are built from the add-on directory as their Docker
context. That means a Dockerfile inside `homeassistant-addon/lif/` cannot simply
`COPY` the parent LiF source tree. The preferred path is the published
`ghcr.io/lif-planner/lif` image; the source-build Dockerfile is only a fallback
for early packaging experiments.

The container image workflow builds the regular LiF Docker image, runs
`scripts/smoke_container.sh` against `/health/`, and publishes multi-arch images
on pushes to public `main` and version tags.
