# Ansible Deployment

This path lets you deploy or update the LiF Docker Compose instance from your
Mac with one Ansible command.

It targets a Debian or Ubuntu Proxmox LXC and keeps the app reachable only
through your VPN or LAN. Do not expose LiF directly to the public internet.

## What Ansible Does

The playbook in `deploy/ansible/lif.yml`:

- installs Docker, Compose, Git, curl, and certificates
- creates `/opt/lif`
- clones or updates the configured LiF repository
- renders the private `docker/lif.env`
- renders `.env` with the host port for Docker Compose
- creates persistent `data/` directories
- optionally creates a pre-deploy SQLite backup
- runs `docker compose up -d --build`
- checks `http://127.0.0.1/health/`
- checks the app with the LXC IP as the `Host` header so Django
  `ALLOWED_HOSTS` problems fail during deployment instead of in the browser

Debian and Ubuntu expose Docker Compose under different package names. The
playbook tries `docker-compose-plugin`, `docker-compose-v2`, and
`docker-compose`, then uses whichever command works on the host.

The real planning data stays on the target host in:

```text
/opt/lif/data/
```

Back up that directory carefully.

## Install Ansible On The Mac

```bash
brew install ansible
```

The Mac must be able to SSH into the LXC:

```bash
ssh lifadmin@192.0.2.20
```

The LXC must also be able to clone this repository from GitHub. Use a deploy key
or an SSH key on the LXC that has read access to your LiF repository.

## Create Local Ansible Files

Copy the examples:

```bash
cp deploy/ansible/inventory.example.ini deploy/ansible/inventory.ini
cp deploy/ansible/group_vars/lif/vars.example.yml deploy/ansible/group_vars/lif/vars.yml
cp deploy/ansible/group_vars/lif/vault.example.yml deploy/ansible/group_vars/lif/vault.yml
```

Edit the inventory:

```bash
nano deploy/ansible/inventory.ini
```

Example:

```ini
[lif]
lif-lxc ansible_host=192.0.2.20 ansible_user=lifadmin
```

Edit non-secret settings:

```bash
nano deploy/ansible/group_vars/lif/vars.yml
```

The playbooks apply built-in defaults only when a variable is not set, so values
in `group_vars/lif/vars.yml` control the deployment.

At minimum, set:

```yaml
lif_django_allowed_hosts:
  - 127.0.0.1
  - localhost
  - 192.0.2.20

lif_container_port: 80
```

For a frictionless demo deployment with sample data and no login screen, set:

```yaml
lif_demo_deployment: true
lif_demo_require_login: false
```

For demo-with-login, set `lif_demo_require_login: true` and optionally set
`lif_demo_username` and `lif_demo_email`.

## Store Secrets With Ansible Vault

Generate a Django secret key:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(64))'
```

Put it in:

```bash
nano deploy/ansible/group_vars/lif/vault.yml
```

For demo-with-login deployments, also add a demo login password of at least 12
characters:

```yaml
lif_demo_password: replace-with-a-demo-password
```

Then encrypt it:

```bash
ansible-vault encrypt deploy/ansible/group_vars/lif/vault.yml
```

The real inventory, local vars, and vault files are ignored by Git.

## Deploy

Run from the repo root:

```bash
ansible-playbook -i deploy/ansible/inventory.ini deploy/ansible/lif.yml --ask-vault-pass
```

Or use the convenience wrapper:

```bash
./scripts/deploy_ansible.sh --ask-vault-pass
```

After the first run, open:

```text
http://192.0.2.20/
```

If `lif_demo_deployment: true` and `lif_demo_require_login: false`, no login is
required. If both are true, log in with the configured demo username and the
`lif_demo_password` from your encrypted vault. The playbook fails if a
no-login demo still redirects to `/login/`.

Create the first admin user on the LXC:

```bash
ssh lifadmin@192.0.2.20
cd /opt/lif
docker compose exec lif python manage.py createsuperuser
```

## Update Workflow

Normal flow from this machine:

```bash
git push
./scripts/deploy_ansible.sh --ask-vault-pass
```

The playbook is safe to run repeatedly. It updates the checkout, rebuilds the
container, runs migrations through the container entrypoint, collects static
files, and verifies the health endpoint.

When `lif_demo_deployment: true`, each run also refreshes the demo household
data. If `lif_demo_require_login: true`, the playbook creates or updates the
demo login user and verifies that the configured demo credentials authenticate
in Django. Keep demo deployment off for real-data deployments.

To refresh only the in-app demo household without wiping the container checkout:

```bash
./scripts/reset_demo_seed_ansible.sh --ask-vault-pass
```

This runs `python manage.py reset_demo_data` inside the existing LiF container.
It keeps `/opt/lif`, the checkout, Compose files, and container setup intact.
If you are already SSH'd into the LXC, the equivalent manual command is
`cd /opt/lif && docker compose exec lif python manage.py reset_demo_data`.

## Reset Demo Deployment

For a broken or stale demo LXC, you can wipe the LiF deployment directory and
let the normal deploy recreate a fresh checkout, container, SQLite database, and
seed data.

This removes `/opt/lif`, including:

- the deployment checkout
- the demo SQLite database
- demo backups
- collected static files
- rendered environment files

Run:

```bash
./scripts/reset_demo_ansible.sh --ask-vault-pass
./scripts/deploy_ansible.sh --ask-vault-pass
```

The reset playbook refuses to run unless `lif_reset_confirm=true` is supplied.
The wrapper supplies that flag for the default `/opt/lif` demo deployment.

## Rollback

SSH to the LXC:

```bash
cd /opt/lif
git log --oneline -5
git checkout <known-good-commit>
docker compose up -d --build
```

If data must be restored:

```bash
docker compose down
cp data/backups/<backup-file>.sqlite3 data/db.sqlite3
docker compose up -d
```

## Notes

- The Ansible deployment publishes LiF on host port `80` by default.
- Plain `docker compose up` without Ansible still defaults to host port `8001`.
- If the Compose package name differs on your LXC image, adjust
  `lif_compose_package_candidates` in `group_vars/lif/vars.yml`.
- If you see Django `400 Bad Request`, add the exact IP or hostname you are
  opening in the browser to `lif_django_allowed_hosts`, then rerun the playbook.
  The playbook automatically includes `ansible_host` and the inventory host
  name, but it cannot guess extra aliases such as custom DNS names.
- Use `lif_demo_deployment: true` only for demo instances. It refreshes demo
  household data. It manages a demo login user only when
  `lif_demo_require_login` is also true.
- Keep `LIF_REQUIRE_LOGIN=1` for real data.
- Keep the LXC reachable only through VPN/LAN or Proxmox firewall rules.
- Do not commit `deploy/ansible/inventory.ini`, `group_vars/lif/vars.yml`, or
  `group_vars/lif/vault.yml`.
