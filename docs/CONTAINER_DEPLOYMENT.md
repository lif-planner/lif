# Container Deployment

This path runs LiF as a Docker Compose service inside a Debian or Ubuntu Proxmox
LXC. Keep the LXC reachable only through VPN or LAN. Do not expose LiF directly
to the public internet.

## Proxmox LXC

Create a Debian or Ubuntu LXC with:

- nesting enabled
- at least 1 CPU core
- 1-2 GB RAM
- enough disk for backups and SQLite history

Inside the LXC, install Docker and Compose:

```bash
apt update
apt install -y ca-certificates curl git docker.io docker-compose-plugin
systemctl enable --now docker
```

## Checkout

```bash
mkdir -p /opt
cd /opt
git clone git@github.com:lif-planner/lif.git lif
cd /opt/lif
```

Create persistent data directories:

```bash
mkdir -p data/backups data/staticfiles
```

## Environment

Create the private environment file:

```bash
cp docker/lif.env.example docker/lif.env
nano docker/lif.env
```

Required edits:

- `DJANGO_SECRET_KEY`: set a long random value.
- `DJANGO_ALLOWED_HOSTS`: include the LXC IP, DNS name, or VPN hostname you use
  from your phone and desktop.
- `DJANGO_CSRF_TRUSTED_ORIGINS` / `DJANGO_TRUST_PROXY_SSL_HEADER`: only needed
  if you put a reverse proxy or tunnel (e.g. Cloudflare Tunnel) in front of
  the container on its own hostname — see `docker/lif.env.example`.
- `LIF_REQUIRE_LOGIN=1`: recommended for any real data.

Generate a secret key:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(64))'
```

## Build And Start

```bash
docker compose up -d --build
```

Create the first admin user:

```bash
docker compose exec lif python manage.py createsuperuser
```

Open:

```text
http://<lxc-ip>:8001/
```

For Ansible-managed deployments, the playbook writes `.env` so the same Compose
file publishes LiF on port `80` instead:

```text
http://<lxc-ip>/
```

## Update

```bash
cd /opt/lif
git pull --ff-only
docker compose up -d --build
```

The container entrypoint runs migrations and `collectstatic` on startup.

To automate host setup and repeated deploys from your Mac, use the Ansible
workflow in [ANSIBLE_DEPLOYMENT.md](ANSIBLE_DEPLOYMENT.md).

## Data Layout

The Compose file bind-mounts `./data` to `/data` in the container:

```text
/opt/lif/data/db.sqlite3
/opt/lif/data/backups/
/opt/lif/data/staticfiles/
```

Back up `/opt/lif/data`. That directory is the part that contains real planning
data.

## Backup

Manual app-level backup:

```bash
docker compose exec lif python manage.py backup_data --label manual
```

Host-level copy:

```bash
tar -czf lif-data-$(date +%Y%m%d-%H%M%S).tar.gz data
```

## Restore

Stop the app, restore the SQLite file, then start again:

```bash
docker compose down
cp data/backups/<backup-file>.sqlite3 data/db.sqlite3
docker compose up -d
```

## Notes

- SQLite is fine for one household/family instance with one running container.
- Do not run multiple LiF containers writing to the same SQLite database.
- Use Proxmox firewall rules so port `8001` is reachable only from VPN/LAN.
- Add a reverse proxy with HTTPS later if you want a friendlier hostname.
