# Changelog

## 1.1.9

- Persist privacy mode inside Home Assistant Ingress without relying on session
  cookies.
- Add `seed_demo_on_start` to insert demo households on startup when none exist.

## 1.1.8

- Fix Home Assistant Ingress language and privacy controls when the ingress
  prefix must be inferred from the request path.

## 1.1.7

- Keep the selected language active inside Home Assistant Ingress even when
  cookies are not preserved by the ingress proxy.
- Fix privacy mode toggling inside Home Assistant Ingress.

## 1.1.6

- Keep the selected LiF language active after switching inside Home Assistant
  Ingress.

## 1.1.5

- Fix language selection under Home Assistant Ingress so it redirects back to
  LiF instead of loading Home Assistant UI content inside the panel.

## 1.1.4

- Fix demo seeding on Home Assistant when a stale `/data/.demo_seeded` marker
  exists but the database does not contain demo households.
- Fix language selection under Home Assistant Ingress when the proxied POST
  does not pass Django's standard CSRF check.

## 1.1.3

- Fix missing CSS and JavaScript under Home Assistant Ingress by generating
  static asset URLs with the ingress prefix.

## 1.1.2

- Fix Home Assistant Ingress path handling so the add-on no longer shows
  `404: Not Found` behind the dynamic ingress URL prefix.

## 1.1.1

- Default Home Assistant add-on allowed hosts to `*` so ingress and dynamic
  local-network hostnames do not trigger Django 400 Bad Request responses.
- Document when to keep the wildcard and when to restrict hosts for direct-port
  access.

## 1.1.0

- Add repository CI, validation scripts, and release workflow documentation.
- Add Home Assistant ingress test checklist.
- Add LiF icon and logo assets for the add-on store.
- Add watchdog metadata for `/health/`.
- Align add-on version with the published LiF GHCR image tag.

## 1.0.0

- Add experimental Home Assistant add-on packaging for LiF.
