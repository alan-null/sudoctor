<h1><img src="dashboard/assets/sudoctor-mascot.png" alt="" width="64" align="absmiddle"> sudoctor</h1>

Minimal, local-PC-driven maintenance for Debian/Ubuntu Linux servers over SSH.

**No LLM, target agent daemon, or external collection service required.**
The optional Docker deployment adds scheduled checks and a dashboard

## Requirements

- On your local PC: Ansible (`sudo apt install ansible`), OpenSSH, Python 3.
- On each target: Python 3, systemd, APT, SSH, sudo access for maintenance.
- Set up SSH aliases/keys and verify you can log in using `ssh cloud-dev`.
- If sudo requires a password, prefix commands with `SUDOCTOR_BECOME_ARGS='--ask-become-pass'`.
- The scripts do not bypass host-key verification.

## Scheduled checks with Docker

For scheduled checks and the dashboard using Docker Compose, see the [Docker deployment guide](docker/README.md). Docker configuration and deployment files are contained in [docker/](docker/).

## 1. Configure hosts

Keep personal configuration outside the checkout. The default inventory is `${XDG_CONFIG_HOME:-$HOME/.config}/sudoctor/inventory/hosts.ini`. Create it and copy the shared defaults:

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/sudoctor/inventory/host_vars" "${XDG_CONFIG_HOME:-$HOME/.config}/sudoctor/inventory/group_vars"
cp inventory/group_vars/servers.yml "${XDG_CONFIG_HOME:-$HOME/.config}/sudoctor/inventory/group_vars/servers.yml"
```

Create `hosts.ini` in that directory with your own SSH aliases:

```ini
[servers]
cloud-dev
```

Add per-host settings in the adjacent `host_vars/<hostname>.yml`. Host variable filenames match the **Ansible inventory alias**. Keep hosts under `servers`, directly or through child groups. The checkout's inventory contains examples and is never selected automatically.

```bash
./scripts/sudoctor setup cloud-dev
./scripts/sudoctor check cloud-dev
./scripts/sudoctor check cloud-dev --inventory ~/private-infrastructure/inventory/hosts.ini
```

Inventory precedence is `--inventory PATH` → `SUDOCTOR_INVENTORY` → the default home inventory. An inventory directory is also accepted by the CLI. Relative override paths are resolved from your current directory. Missing inventory produces an error with setup guidance. Host/group selection works independently of inventory selection. The launcher always uses sudoctor's bundled Ansible configuration.

Reports default to `${XDG_STATE_HOME:-$HOME/.local/state}/sudoctor/reports`; set `SUDOCTOR_REPORT_DIR` to override this for any CLI action. Compose retains its separate persistent state volume. When upgrading an existing checkout, copy your inventory (including `host_vars` and `group_vars`) to the new location or explicitly select its existing path.

### Configuration schemas and autocomplete

[JSON Schemas](schemas/) describe the inventory YAML and the generated runtime JSON:

- [inventory.schema.json](schemas/inventory.schema.json) validates host and group variables, including services, containers, health probes, and tmux sessions/windows/panes.
- [tmux.schema.json](schemas/tmux.schema.json) validates a generated `/etc/sudoctor/tmux/<name>.json` session configuration.
- [health.schema.json](schemas/health.schema.json) validates generated `/etc/sudoctor/health.json` expectations. This is not the schema for health reports.

In VS Code, install the recommended **YAML extension by Red Hat** for autocomplete, descriptions, allowed values, and validation errors. Workspace settings associate all inventory host/group YAML files with the inventory schema. Existing files also have a `yaml-language-server` comment, which other compatible editors can use. For personal inventory outside the checkout, use an absolute path to `schemas/inventory.schema.json` in the comment or configure your editor to associate those files with that schema; the example relative schema paths only work inside the checkout.

All inventory settings are optional because Ansible inherits group defaults; schema defaults describe values but do not insert them. Unknown `sudoctor_` properties and unknown nested fields are rejected, while other Ansible/custom variables are allowed. Values must be literal YAML values for editor validation; Ansible/Jinja expressions may be flagged when a boolean or number is expected. Use either `command` or `panes` per tmux window. JSON Schema does not enforce uniqueness of names within object lists or verify users, paths, installed programs, or connectivity; tmux startup validates session/window/pane names and local runtime requirements separately. These schemas provide editor/tool validation; they are not automatically enforced by every playbook.

To generate a configuration, give the generator the inventory schema together with its referenced definitions, ask for YAML, and specify the target user, paths, and programs. Validate the result before setup. Keep the schema files together because runtime schemas reference definitions in the inventory schema.

Configuration keys:

```yaml
sudoctor_update_enabled: true  # explicit opt-in to APT changes
sudoctor_auto_reboot: false     # if true, update reboots on /var/run/reboot-required
sudoctor_services: [ssh.service, docker.service]
sudoctor_docker:
  - name: my-container
    require_healthy: true        # requires Docker HEALTHCHECK in image/Compose
sudoctor_tcp:
  - name: vpn-backend
    host: 10.10.0.2
    port: 5432
sudoctor_http:
  - name: api
    url: http://127.0.0.1:8080/health
    status: 200
sudoctor_tmux:
  - name: worker
    user: alan
    vpn_unit: netbird.service     # optional; adjust to your VPN unit
    ready_tcp:                  # optional; mandatory for applications needing VPN
      host: 10.10.0.2
      port: 5432
      timeout: 180
    windows:
      - name: worker
        command: 'exec /home/alan/apps/worker/start.sh'
```

`command` is an intentionally **trusted shell command**. Never insert secrets or untrusted user input in inventory YAML, as the generated configuration is world-readable to allow unprivileged service startup. Keep secrets in restricted application environment files, not in command lines. Add actual application health probes; an existing tmux window is *not* proof of health.

### Docker restart behavior

Docker container definitions are **monitored**, not automatically rebuilt or updated. For boot recovery enable `docker.service` and ensure Docker/Compose containers have an appropriate restart policy (e.g. `restart: unless-stopped`). `docker compose up -d` should first be used manually; don't expect this project to recreate containers from scratch. It deliberately does not auto-update images, volumes or Compose projects.

### Configure tmux sessions, windows, and panes

Each entry under `sudoctor_tmux` defines a **session** owned by an existing Linux `user`. A session contains named **windows** (tabs); each window contains one or more **panes** (terminal areas), each running its own `command`. Start with the commented example in [cloud-dev host variables](inventory/host_vars/cloud-dev.yml):

```yaml
sudoctor_tmux:
  - name: apps
    user: alan
    cwd: /home/alan/apps
    windows:
      - name: worker
        layout: even-horizontal
        panes:
          - name: app
            cwd: /home/alan/apps/worker
            command: 'exec ./start.sh'
          - name: logs
            command: 'exec tail -F /home/alan/apps/worker/worker.log'
      - name: metrics
        command: 'exec /home/alan/apps/metrics/run.sh'
```

This creates session `apps`, window `worker` with side-by-side `app` and `logs` panes, and window `metrics` with one program. For a single pane, use the existing window-level `command` shorthand; for multiple panes, use `panes`. Do not put both on the same window.

- `name`: session names must match `[a-z][a-z0-9-]{0,49}` and be unique per host. Window and pane names accept letters, digits, underscores, and hyphens; window names must be unique within a session and pane names within a window.
- `cwd`: optional absolute working directory, inherited from session → window → pane. Without any `cwd`, the user's home directory is used. Directories must already exist and be accessible to `user`; `~` and environment variables are not expanded in paths.
- `command`: required for every pane; runs through `/bin/bash -c`. Use `exec` for a long-running foreground program so the pane directly tracks it. Commands can include arguments, pipes, or `&&`. Set required environment variables explicitly or source your application's environment file; login profiles are not loaded. Avoid backgrounding the program with `&` or using a daemonizing command. A short command such as `ls` exits immediately and is reported as a failed pane; use `ls; exec /bin/bash` if you want to list files and then keep an interactive shell open.
- `layout`: optional window layout, defaults to `tiled` when adding panes. Choose `even-horizontal` (side by side), `even-vertical` (stacked), `main-horizontal` (large top pane), `main-vertical` (large left pane), or `tiled` (grid). List order determines initial pane creation order. A specified layout is reapplied during recovery; otherwise existing arrangements are preserved until panes are added.
- `vpn_unit`: optional systemd VPN dependency. It orders startup after the unit and asks systemd to activate it; it does not prove the tunnel is ready.
- `ready_tcp`: optional session-level dependency probe with `host`, `port`, and `timeout` (seconds, default 180). Use it when commands need a VPN/backend to be reachable. The boot unit has a total startup timeout of 300 seconds; keep the probe timeout below that.

Apply the configuration and inspect the result:

```bash
./scripts/sudoctor setup cloud-dev
ssh -t cloud-dev 'tmux attach -t apps'
./scripts/sudoctor check cloud-dev
./scripts/sudoctor recover cloud-dev
```

Attach as the configured `user` (the example assumes SSH logs in as `alan`). In tmux, press `Ctrl-b` then `w` to choose a window, `Ctrl-b` then an arrow key to move between panes, and `Ctrl-b` then `d` to detach while programs continue running. Pane borders show the configured name and current program; health reports include pane names, IDs, current programs, and dead state. The program field is tmux's current foreground command, not an application health check or the full configured command line. Use TCP/HTTP probes to verify actual application health.

### tmux recovery

`setup` installs a per-session **systemd one-shot boot unit** as the specified user. It starts configured sessions, windows, and panes during `setup` and restores them when the host boots, even when the local controller is offline. If readiness fails, systemd retries the boot task every 30 seconds. The unit **does not continuously supervise** the programs; run `check` to detect missing/exited panes and `recover` to restore them. For critical services prefer a normal systemd service with `Restart=on-failure`. Avoid interactive prompts in boot-restored commands.

Managed windows retain exited panes so failures remain visible. Recovery restarts dead panes, creates missing ones, and leaves running commands alone. Pane identity is stored separately from the terminal title, so programs changing their titles do not cause duplicate panes. Window auto-renaming is disabled to preserve configured names. Existing single-command windows are adopted by their window name; existing panes without managed identities in a multi-pane window are left alone and the configured panes are added.

After editing inventory, run `setup` again to install the updated configuration. Changing `command` or `cwd` does **not** restart a live pane: stop that pane's program, then run `recover` to launch the new command. Removed/renamed sessions, windows, panes, and boot units are not automatically deleted; retire them manually. Changing a pane name creates a new pane. No configuration change is pushed to the host until you run `setup`.

## 2. Run

```bash
./scripts/sudoctor setup cloud-dev          # installs on-host health and boot files
./scripts/sudoctor check cloud-dev          # inspect just one server
./scripts/sudoctor check                    # inspect every listed server
./scripts/sudoctor recover cloud-dev        # safe, idempotent repair of allowlisted services/tmux
./scripts/sudoctor update cloud-dev --yes   # safe apt upgrade, optional reboot, verify
./scripts/sudoctor reboot cloud-dev --yes   # explicit reboot, waits for SSH, verifies
```

The latest JSON report for each host is stored in `${XDG_STATE_HOME:-$HOME/.local/state}/sudoctor/reports/<inventory-alias>.json` on your PC. Every completed collection is also archived in `<report-directory>/<inventory-alias>/<collection-time>.json`, with a UTC timestamp from the report in the filename. These per-host files provide raw history for future trends; a check that fails before collecting JSON has no report to archive. Treat the reports as private because health URLs, hostnames, and diagnostic metadata may reveal internal services. Use them for later weekly report aggregation or AI analysis. Do not blindly feed logs or secrets to an LLM.

To view local reports, run `./scripts/sudoctor dashboard` and open `http://127.0.0.1:8000`. The server reads the same `SUDOCTOR_REPORT_DIR` or XDG state directory as CLI checks, including archived snapshots. Run a check and press **Refresh** to load new results. No inventory is required to view existing reports. Set `SUDOCTOR_HTTP_PORT` to change the port; the local server binds to loopback. You can also open `dashboard/index.html` directly and use **Open reports**. The Docker dashboard continues to use its shared scheduler state volume.

The dashboard footer's **View demo** button loads three fictional hosts with four snapshots each. It works without an inventory or collected reports. Every demo report is labeled; **Refresh** returns to real reports. Click the logo to leave the demo and open the real reports overview.

The [GitHub Pages workflow](.github/workflows/pages.yml) publishes the dashboard as a standalone demo when dashboard files change on `master` or `main`, or when run manually. In the repository's **Settings → Pages**, select **GitHub Actions** as the build and deployment source before running it. The published site opens with fictional demo content by default; **Refresh** and the logo return to the demo. Only dashboard files are published, without inventories or collected reports. Local and Docker dashboards continue to load real reports by default.

OS and update checks appear in a separate dashboard module. Reports keep their existing fields and add a `checks` array with `id`, `category`, `label`, `value`, `message`, and `status`. These observations use the report's `timestamp_utc`. Checks inspect local APT metadata, package state, reboot requirements, OS/kernel information, lifecycle dates, and current APT history. Collection does not refresh package indexes. Security-suite counts depend on cached repository labels, and missing index timestamps or lifecycle data appear as unknown. Standard support dates do not include extended support entitlements. APT history completion and unattended-upgrades service results do not prove successful patching. Pending service restarts are marked skipped. Run `setup` on a host to deploy the updated collector before collecting these fields; older reports still display.

`update` is **serial** (one server at a time), respects your per-host `sudoctor_update_enabled` and `sudoctor_auto_reboot` settings, and fails on critical post-maintenance health. It does *not* roll back packages. If automatic reboot is false, it reports `reboot_required: true` and leaves it for manual maintenance. Ansible waits for SSH to return, then waits up to 180 seconds for expected application health to recover.

### Safe rollout

1. Start with **one disposable/noncritical host** and only `ssh.service` as an expected systemd service.
2. Run `setup`, `check`, then `update ... --yes` with `sudoctor_auto_reboot: false`.
3. Declare actual Docker containers, tmux apps, VPN dependency probes, and HTTP health endpoints.
4. Test reboot recovery by rebooting the host manually or using `reboot ... --yes` in a maintenance window.
5. Only then consider enabling `sudoctor_auto_reboot: true` on that host.
6. Keep tested backups/snapshots and out-of-band console access before rebooting distant machines.

## Important limitations

- **No scheduled automatic patching yet:** local-host commands are manual by design. Add a local systemd timer later if desired; it only runs when the PC is available.
- **No zero-downtime promise:** OS upgrades can restart services even without a reboot.
- **Not an incident controller:** failed checks cause a nonzero command exit; safe recovery doesn't auto-restart Docker containers or change VPN/firewall config.
- **No automatic rollback:** capture backups and use snapshots for production systems.
- **No sudo privilege restriction included:** the examples assume maintenance-level sudo. Use dedicated credentials and restrict permissions where feasible.
- **Network-online is not a tunnel readiness test:** use the `ready_tcp` probe for VPN-dependent tmux apps.
- **Don't configure identical simultaneous APT schedules:** coordinate with `unattended-upgrades` and host timers to prevent update overlaps.
- The default health check may report warnings for a pending reboot or high disk usage; only critical checks return a failing playbook status.
- `setup` can install packages and start configured services. Review inventory before running it.

## Validate before rollout

```bash
ansible-inventory --inventory "${SUDOCTOR_INVENTORY:-${XDG_CONFIG_HOME:-$HOME/.config}/sudoctor/inventory/hosts.ini}" --list
ansible-playbook --syntax-check playbooks/setup.yml
ansible-playbook --syntax-check playbooks/check.yml
ansible-playbook --syntax-check playbooks/update.yml
ansible-playbook --syntax-check playbooks/reboot.yml
ansible-playbook --syntax-check playbooks/recover.yml
python3 -m py_compile scripts/*.py
python3 -m unittest discover -s tests -v
```

The tmux integration tests require local `tmux` and use a private test server; they do not modify your normal tmux sessions. They are skipped when tmux is unavailable.

Validate schemas, inventory files, generated configuration shapes, and invalid examples without installing packages on the host:

```bash
docker run --rm -v "$PWD:/work:ro" -w /work python:3.12-slim sh -c 'pip install --quiet "jsonschema>=4.18,<5" "PyYAML>=6,<7" && python -B -m unittest discover -s tests -p test_schemas.py -v'
```

Schema tests are skipped by the regular local test command if these optional validation packages are unavailable. Running the Docker command above performs all schema tests.
