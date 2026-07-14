# Public Release And Sync Process

This document describes how to publish LiF from the private working repository
to a clean public repository without exposing the private commit history.

The public v1.0 release should be created only after the final checks in
`GO_PUBLIC.md` are complete.

## One-Time Decisions

Before running the export:

1. Create a new empty GitHub repository.
2. Do not initialize it with a README, license, or `.gitignore`.
3. Decide the public repository URL, for example:

   ```text
   git@github.com:lif-planner/lif.git
   ```

4. Use the public initial commit identity:

   ```text
   LiF Maintainers <yogitea@users.noreply.github.com>
   ```

5. Enable the tracked Git hooks in every checkout that creates or promotes
   public commits:

   ```bash
   git config core.hooksPath .githooks
   ```

   The hooks reject commits on `public-release` when the committer is not the
   maintainer identity and reject pushes to the public repository if any pushed
   commit has a non-maintainer author or committer.

## Final Prep Commit

On private `main`, make one normal private commit that:

- sets `lif/version.py` default version to `1.0.0`
- moves the `CHANGELOG.md` `1.0.0` section from planned to released
- updates placeholder repository URLs if the final public repo name is known
- adds screenshots or GIFs to `README.md`, if available
- passes the release checks below

Run:

```bash
python3 scripts/scan_secrets.py
python3 scripts/scan_public_readiness.py
./scripts/simulate_public_checkout.sh
pipenv run python manage.py test
pipenv run python manage.py check
pipenv run python manage.py check_production
```

`check_production` should be run with production-like environment variables if
you want a completely clean report. Local development warnings for debug mode,
the dev secret key, or disabled login are expected in a dev checkout.

The public checkout simulation exports only tracked files into a temporary
directory, verifies ignored/private paths are absent, installs locked
dependencies, migrates an isolated SQLite database, seeds demo data, and runs the
smoke test. Set `KEEP_PUBLIC_SIMULATION_DIR=1` if you need to inspect the temp
checkout after a failure.

## Export A Fresh Public History

The helper script creates an orphan branch with exactly one commit:

```bash
CONFIRM_PUBLIC_RELEASE=1 ./scripts/create_public_release_branch.sh
```

By default it creates:

- branch: `public-release`
- commit message: `Initial public release (v1.0.0)`
- tag: `v1.0.0`
- author: `LiF Maintainers <yogitea@users.noreply.github.com>`

It refuses to run unless:

- `CONFIRM_PUBLIC_RELEASE=1` is set
- the working tree is clean
- `lif/version.py` contains `1.0.0`
- `CHANGELOG.md` has a `1.0.0` section
- `docs/internal/FIXME.md`, when present locally, is ignored by Git

Example:

```bash
CONFIRM_PUBLIC_RELEASE=1 ./scripts/create_public_release_branch.sh
```

The script does not push anything.

## Push To The New Public Repo

After inspecting the generated branch:

```bash
git remote add public git@github.com:lif-planner/lif.git
git push public public-release:main
git push public v1.0.0
```

Then create a GitHub Release from `v1.0.0`.

## Return To Private Main

After exporting:

```bash
git switch main
```

Keep the private repository private. Future development can move to the new
public repository once you are comfortable, or this private repository can stay
as a workbench/archive.

## Ongoing Private-To-Public Updates

After `v1.0.0`, the recommended workflow is:

1. Keep normal development on private `main`.
2. Commit and push private work to `origin/main`.
3. Move only public-safe commits onto the clean `public-release` branch.
4. Push `public-release` to the public repository as `main`.

The private repository remains the workbench. The public repository receives
only reviewed code, docs, examples, and synthetic demo data.

### Daily Private Work

On private `main`:

```bash
git switch main
git pull
# make code/docs changes
python3 scripts/scan_secrets.py
python3 scripts/scan_public_readiness.py
pipenv run python manage.py test
pipenv run python manage.py check
git add <changed-files>
git commit -m "Describe the private change"
git push origin main
```

Do not commit real data, local SQLite databases, private seed files, Ansible
vault files, `.env` files, backups, logs, collected static files, or anything
from `local_private/`.

### Promote A Public-Safe Commit

If a private commit is safe for public history, cherry-pick it onto
`public-release`:

```bash
git switch public-release
git pull public main
GIT_COMMITTER_NAME='LiF Maintainers' \
GIT_COMMITTER_EMAIL='yogitea@users.noreply.github.com' \
  git cherry-pick <private-commit-sha>
GIT_AUTHOR_NAME='LiF Maintainers' \
GIT_AUTHOR_EMAIL='yogitea@users.noreply.github.com' \
GIT_COMMITTER_NAME='LiF Maintainers' \
GIT_COMMITTER_EMAIL='yogitea@users.noreply.github.com' \
  git commit --amend --no-edit --reset-author
python3 scripts/scan_secrets.py
python3 scripts/scan_public_readiness.py
pipenv run python manage.py test
pipenv run python manage.py check
git push public public-release:main
git switch main
```

Use this path for normal code, migrations, tests, documentation, examples, and
synthetic seed data.

For small public-only documentation fixes, it is also fine to commit directly on
`public-release` and push to `public`. If the same change should exist in the
private workbench too, cherry-pick it back to private `main`.

### Promote Several Commits

Prefer small, reviewable commits. If several adjacent private commits are all
public-safe, cherry-pick them in order:

```bash
git switch public-release
git pull public main
GIT_COMMITTER_NAME='LiF Maintainers' \
GIT_COMMITTER_EMAIL='yogitea@users.noreply.github.com' \
  git cherry-pick <oldest-safe-sha>
GIT_AUTHOR_NAME='LiF Maintainers' \
GIT_AUTHOR_EMAIL='yogitea@users.noreply.github.com' \
GIT_COMMITTER_NAME='LiF Maintainers' \
GIT_COMMITTER_EMAIL='yogitea@users.noreply.github.com' \
  git commit --amend --no-edit --reset-author
GIT_COMMITTER_NAME='LiF Maintainers' \
GIT_COMMITTER_EMAIL='yogitea@users.noreply.github.com' \
  git cherry-pick <next-safe-sha>
GIT_AUTHOR_NAME='LiF Maintainers' \
GIT_AUTHOR_EMAIL='yogitea@users.noreply.github.com' \
GIT_COMMITTER_NAME='LiF Maintainers' \
GIT_COMMITTER_EMAIL='yogitea@users.noreply.github.com' \
  git commit --amend --no-edit --reset-author
GIT_COMMITTER_NAME='LiF Maintainers' \
GIT_COMMITTER_EMAIL='yogitea@users.noreply.github.com' \
  git cherry-pick <newest-safe-sha>
GIT_AUTHOR_NAME='LiF Maintainers' \
GIT_AUTHOR_EMAIL='yogitea@users.noreply.github.com' \
GIT_COMMITTER_NAME='LiF Maintainers' \
GIT_COMMITTER_EMAIL='yogitea@users.noreply.github.com' \
  git commit --amend --no-edit --reset-author
python3 scripts/scan_secrets.py
python3 scripts/scan_public_readiness.py
pipenv run python manage.py test
pipenv run python manage.py check
git push public public-release:main
git switch main
```

If a private commit mixes public code with private notes or local configuration,
do not cherry-pick it directly. Create a clean public commit manually on
`public-release` with only the safe files.

### Tags And Releases After 1.0

A release is not done until every step below has happened. Skipping the
add-on repo sync (step 6) or the tags (step 5) has bitten before: the code
and image were live while Home Assistant still offered the old version and
the release was unfindable on GitHub.

1. Update `CHANGELOG.md` and `homeassistant-addon/lif/CHANGELOG.md`.
2. Bump `lif/version.py` **and** `homeassistant-addon/lif/config.yaml` to the
   same version. The container-image workflow tags the GHCR image with the
   `lif/version.py` value, and Home Assistant pulls `image:version` from
   `config.yaml` -- if they diverge, the add-on points at a tag that does not
   exist.
3. Promote the release commit to `public-release`.
4. Push the branch to the public repo and wait for the "Container Image"
   workflow to succeed (it publishes `ghcr.io/lif-planner/lif:<version>`).
5. Tag **both** repos with the maintainer identity. Annotated tags carry
   their own tagger name/email, which is public -- never tag with a personal
   identity (`scripts/check_git_identity.py` rejects it on push).
6. Sync the add-on repository. Home Assistant reads add-on versions from
   `lif-planner/home-assistant-addon`, not from this repo -- without this
   step, no HA instance ever sees the update.
7. Verify.

Example:

```bash
# steps 3-4
git switch public-release
git cherry-pick <release-sha>
git push public public-release:main
gh run watch --repo lif-planner/lif   # Container Image workflow

# step 5 (maintainer tagger identity)
GIT_COMMITTER_NAME='LiF Maintainers' \
GIT_COMMITTER_EMAIL='yogitea@users.noreply.github.com' \
git tag -a v1.1.0 -m "LiF 1.1.0"
git push public v1.1.0

# step 6 (from the home-assistant-addon checkout next to this repo)
cd ../home-assistant-addon
sh scripts/sync_from_lif.sh ../LiF
sh scripts/validate.sh
git add -A && git commit -m "Release LiF Planner add-on v1.1.0"
GIT_COMMITTER_NAME='LiF Maintainers' \
GIT_COMMITTER_EMAIL='yogitea@users.noreply.github.com' \
git tag -a v1.1.0 -m "LiF Planner add-on 1.1.0"
git push origin main v1.1.0

# step 7
gh api repos/lif-planner/lif/tags --jq '.[0].name'
curl -s "https://raw.githubusercontent.com/lif-planner/home-assistant-addon/main/lif/config.yaml" | grep version
```

Then create the GitHub Release from the tag when the release warrants notes.

## Do Not Do This

- Do not make the current private repository public.
- Do not push private `main` to the public repository.
- Do not use `git push --mirror`.
- Do not run public pull-request workflows on a self-hosted runner.
- Do not cherry-pick commits that include real household data, local deployment
  secrets, private hostnames, private IPs, or internal-only notes.
