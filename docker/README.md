# Scheduled checks with Docker

## Published images

The [image publishing workflow](../.github/workflows/docker-publish.yml) tests pull requests and publishes both Docker targets to GitHub Container Registry on pushes to `main` or `master`, version tags such as `v1.2.3`, and manual runs. Both images support `linux/amd64` and `linux/arm64`:

```bash
docker pull ghcr.io/alan-null/sudoctor-scheduler:latest
docker pull ghcr.io/alan-null/sudoctor-dashboard:latest
```

Default-branch builds receive `latest`; version tags receive full and minor version tags (for example, `1.2.3` and `1.2`). Published builds also receive a `sha-<short-commit>` tag. Use a version tag or image digest to pin deployments. Publishing uses the built-in `GITHUB_TOKEN` and requires no additional registry secrets. To allow anonymous pulls, set each GHCR package's visibility to public after its first publication; private packages require `docker login ghcr.io` with a token that has `read:packages` access.

The [Compose file](compose.yaml) uses these published images. After configuring the inventory and SSH mounts as described below, run `docker compose pull` and `docker compose up -d` from this directory.

## Local deployment

Docker Compose runs checks on a schedule and serves the dashboard from a separate, minimal Python image. The dashboard reads results from a shared volume and has no SSH credentials. Install Docker and the Compose plugin on an always-on controller. The controller must be able to reach each host over SSH. Run `setup` on each target before enabling scheduled checks.

1. Create your personal inventory as described in the [main README](../README.md#1-configure-hosts). Compose mounts `${XDG_CONFIG_HOME:-$HOME/.config}/sudoctor/inventory` by default; set `SUDOCTOR_INVENTORY_DIR` to use another directory containing `hosts.ini`, `host_vars/`, and `group_vars/`.
2. Create a dedicated SSH configuration directory containing `config`, a private key, and a verified `known_hosts` file. The container uses these files as `/root/.ssh`; identity files referenced by `config` must use paths that exist inside the container. Verify DNS, VPN routes, proxy commands, and key permissions from the container. The check uses noninteractive SSH and the playbook uses `become: true`; configure noninteractive sudo on each target. Ansible executes temporary modules through sudo, so a rule allowing only the collector executable is insufficient.
3. From the repository root, enter the Docker directory and copy the example configuration, set `SUDOCTOR_SSH_DIR` to the absolute path of that directory, and start the services:

```bash
cd docker
cp .env.example .env
docker compose pull
docker compose up -d
```

Open `http://127.0.0.1:8000` on the controller. The dashboard binds to loopback by default and does not require Caddy. To access it from another machine, set `SUDOCTOR_BIND_ADDRESS` to a reachable address and restrict access with your network controls. The dashboard has no built-in authentication and reports can contain private hostnames and URLs.

Edit the local `.env` file using [the example](.env.example), or set these variables in your shell before starting Compose:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SUDOCTOR_INVENTORY_DIR` | `${XDG_CONFIG_HOME:-$HOME/.config}/sudoctor/inventory` | Personal inventory directory mounted read-only into the scheduler |
| `SUDOCTOR_SSH_DIR` | `~/.ssh` | SSH configuration and credentials mounted read-only into the scheduler |
| `SUDOCTOR_CRON` | `*/15 * * * *` | Five-field cron schedule; for Sunday at 03:00 use `0 3 * * 0` |
| `TZ` | `UTC` | Scheduler time zone, for example `Europe/Warsaw` |
| `SUDOCTOR_HOST_TIMEOUT` | `180` | Maximum seconds per host check |
| `SUDOCTOR_STALE_SECONDS` | `3600` | Age in seconds after which an attempt is stale; set it above your schedule interval and expected sweep duration |
| `SUDOCTOR_PORT` | `8000` | Host port for the dashboard |
| `SUDOCTOR_BIND_ADDRESS` | `127.0.0.1` | Host address for the dashboard port |

For a weekly schedule, set `SUDOCTOR_STALE_SECONDS=691200` (eight days) or another value that accounts for your sweep duration. Cron runs with the container's time zone. The scheduler also starts one sweep when it starts. Missed runs are not replayed after downtime. If a sweep is still running when another starts, the new sweep exits without overlapping it. The state volume retains reports across container restarts. Only one scheduler should use a state volume, and its filesystem must support local file locks.

Run the following Compose commands from this directory. Run an extra sweep with `docker compose exec scheduler /app/scripts/sudoctor-scheduled.py sweep`. Stop scheduled checks with `docker compose stop scheduler`; the dashboard remains available. Restart them with `docker compose start scheduler`. The dashboard loads archived scheduled reports from the state volume for snapshot selection and history charts, and shows a failed or stale attempt separately from the last report. Its `/health` endpoint reports whether the dashboard process responds, not whether scheduled checks are healthy.

If you already use Caddy on the same host, leave the dashboard bound to loopback and add a site to your existing Caddy configuration:

```caddyfile
sudoctor.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

This example does not configure access control. Restrict access through your VPN or configure authentication in Caddy. If Caddy itself runs in Docker, its loopback address refers to its own container. Put it on a shared Docker network with the dashboard and proxy to `dashboard:8000`, or use an appropriate reachable host address.
