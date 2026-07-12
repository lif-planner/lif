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
git cherry-pick <private-commit-sha>
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
git cherry-pick <oldest-safe-sha>
git cherry-pick <next-safe-sha>
git cherry-pick <newest-safe-sha>
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

For future public releases:

1. Update `CHANGELOG.md`.
2. Bump `lif/version.py`.
3. Promote the release commit to `public-release`.
4. Create a public tag on `public-release`.
5. Push the branch and tag to the public repo.

Example:

```bash
git switch public-release
git tag v1.1.0
git push public public-release:main
git push public v1.1.0
```

Then create the GitHub Release from that tag.

## Do Not Do This

- Do not make the current private repository public.
- Do not push private `main` to the public repository.
- Do not use `git push --mirror`.
- Do not run public pull-request workflows on a self-hosted runner.
- Do not cherry-pick commits that include real household data, local deployment
  secrets, private hostnames, private IPs, or internal-only notes.
