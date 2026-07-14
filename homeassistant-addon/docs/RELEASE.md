# Release Process

The Home Assistant add-on uses the public LiF container image:

```text
ghcr.io/lif-planner/lif
```

When `lif/config.yaml` contains:

```yaml
image: "ghcr.io/lif-planner/lif"
version: "1.0.0"
```

Home Assistant pulls:

```text
ghcr.io/lif-planner/lif:1.0.0
```

Keep the add-on `version` aligned with a published GHCR tag.

## Release Checklist

1. Confirm the LiF public repo has a green **Container Image** workflow.
2. Confirm the matching GHCR tag exists and is public.
3. Update `lif/config.yaml` `version`.
4. Update `lif/CHANGELOG.md`.
5. Run:

   ```bash
   scripts/validate.sh
   ```

6. Commit with the public maintainer identity:

   ```bash
   GIT_AUTHOR_NAME='LiF Maintainers' \
   GIT_AUTHOR_EMAIL='yogitea@users.noreply.github.com' \
   GIT_COMMITTER_NAME='LiF Maintainers' \
   GIT_COMMITTER_EMAIL='yogitea@users.noreply.github.com' \
     git commit -m "Release LiF Planner add-on vX.Y.Z"
   ```

7. Push `main`.
8. Tag the add-on repository:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

## Sync From Main LiF Repository

From a checkout next to the main LiF repository:

```bash
scripts/sync_from_lif.sh ../LiF
scripts/validate.sh
```

This copies the staged `homeassistant-addon/` folder from the main LiF repo into
this standalone add-on repository.
