# Mac Mini Deployment

This setup runs LiF on a Mac mini and updates it automatically whenever `main` is pushed.

It assumes remote access is handled by your existing VPN. Do not expose the LiF port directly to the public internet.

## Layout

Recommended directories:

```text
/Users/YOUR_USERNAME/Services/LiF
/Users/YOUR_USERNAME/Services/github-runner-lif
/Users/YOUR_USERNAME/.config/lif/lif.env
```

Use a dedicated checkout for the running app. Do not use that same checkout for daily development work.

## Initial Checkout

```bash
mkdir -p ~/Services
cd ~/Services
git clone git@github.com:lif-planner/lif.git LiF
cd LiF
```

Install dependencies:

```bash
brew install python pipenv
pipenv install
```

## Environment File

Create a private environment file outside Git:

```bash
mkdir -p ~/.config/lif
nano ~/.config/lif/lif.env
```

Example:

```bash
DJANGO_DEBUG=0
DJANGO_SECRET_KEY=replace-with-a-long-random-secret
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,lif.local,192.0.2.10
LIF_REQUIRE_LOGIN=1
LIF_BACKUP_DIR=/Users/YOUR_USERNAME/Services/LiF/backups
```

Include the hostname or VPN/LAN IP that your phone will use in
`DJANGO_ALLOWED_HOSTS`. The service and convenience update script bind to
`0.0.0.0`, but Django still rejects requests whose host is not allowed. The
convenience script adds the Mac's current local interface IPs and hostnames when
`DJANGO_ALLOWED_HOSTS` is not already broad enough. It also expands
`LIF_ALLOWED_HOST_PREFIXES`, into concrete allowed hosts such as your VPN or
LAN address.

Generate a secret key:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(64))'
```

Load the environment for manual commands:

```bash
set -a
source ~/.config/lif/lif.env
set +a
```

## Initialize LiF

```bash
cd ~/Services/LiF
pipenv run python manage.py migrate
pipenv run python manage.py createsuperuser
pipenv run python manage.py check_production
pipenv run python manage.py smoke_test
```

Expected production warnings on a correctly configured local deployment:

- Backup directory may not exist before the first backup.

Warnings that should be fixed:

- `DJANGO_DEBUG is enabled`
- `DJANGO_SECRET_KEY is using the local development default`
- missing collected static files when `DJANGO_DEBUG=0`
- `LIF_REQUIRE_LOGIN is disabled`
- pending migrations
- missing feature flag rows

## LaunchAgent Service

Copy the example plist:

```bash
mkdir -p ~/Library/LaunchAgents
cp deploy/macos/com.example.lif.plist.example ~/Library/LaunchAgents/com.example.lif.plist
```

Edit paths, hostnames, and secrets:

```bash
nano ~/Library/LaunchAgents/com.example.lif.plist
```

Create the log directory:

```bash
mkdir -p ~/Services/LiF/logs
```

Start the service:

```bash
launchctl load ~/Library/LaunchAgents/com.example.lif.plist
launchctl start com.example.lif
```

Restart it after configuration changes:

```bash
launchctl kickstart -k gui/$(id -u)/com.example.lif
```

Open LiF through VPN or LAN:

```text
http://lif.local:8000/
http://192.0.2.10:8000/
```

## Auto-Deploy On Push

Install a GitHub Actions self-hosted runner for this repository on the Mac mini:

1. Open GitHub.
2. Go to your LiF repository -> `Settings` -> `Actions` -> `Runners`.
3. Choose `New self-hosted runner`.
4. Choose `macOS`.
5. Follow GitHub's commands in `~/Services/github-runner-lif`.

Install the runner as a service if GitHub's setup offers that option.

Then copy the workflow example:

```bash
mkdir -p .github/workflows
cp .github/workflows/deploy-macmini.yml.example .github/workflows/deploy-macmini.yml
```

Review the paths in the copied workflow before committing it.

The workflow does:

```text
fetch main
reset deployment checkout to origin/main
load /Users/YOUR_USERNAME/.config/lif/lif.env
install dependencies
run tests
run deploy_local
collect static files through deploy_local
restart LaunchAgent
run smoke_test
```

Keep this workflow limited to trusted pushes to `main`. Do not run deployment from pull requests that can contain untrusted code.

## Firewall And VPN

Use macOS firewall and your router/VPN setup so port `8000` is reachable only from trusted VPN/LAN devices.

Do not add public port forwarding for LiF.

## Updating Manually

If auto-deploy is paused, update manually from the deployment checkout:

```bash
cd ~/Services/LiF
set -a
source ~/.config/lif/lif.env
set +a
./scripts/update_and_run_8001.sh
```

The convenience script pulls the latest code, syncs dependencies, migrates,
collects static files, and starts Django on `0.0.0.0:8001` by default. Override
with `LIF_RUN_HOST` or `LIF_RUN_PORT` if needed. If your phone still sees a
400 Bad Request, check the script output and add the phone-facing host manually:

```bash
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost,lif.local,192.0.2.10 ./scripts/update_and_run_8001.sh
```

To allow another VPN/LAN subnet, pass a comma-separated prefix list:

```bash
LIF_ALLOWED_HOST_PREFIXES=192.0.2.,10.8.0. ./scripts/update_and_run_8001.sh
```

`collectstatic` is required so CSS and JavaScript are served by WhiteNoise when `DJANGO_DEBUG=0`.
Set `DJANGO_STATIC_ROOT` in `~/.config/lif/lif.env` only if you want collected static files outside the checkout.

## Rollback

If a pushed version breaks the running app:

```bash
cd ~/Services/LiF
git log --oneline -5
git checkout <known-good-commit>
cp backups/<backup-file>.sqlite3 db.sqlite3
pipenv install
pipenv run python manage.py smoke_test
launchctl kickstart -k gui/$(id -u)/com.example.lif
```

Use `read_only_mode` in Django admin before risky imports or maintenance windows.
