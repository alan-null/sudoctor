# How auth and connections work

These diagrams show who connects to whom, which account each connection uses, and where reports end up. They cover three flows:

1. [How reports reach a dashboard](#1-how-reports-reach-a-dashboard). Two independent setups: [local PC](#1a-local-pc-manual) or [Docker controller](#1b-docker-controller-scheduled).
2. [Standard check](#2-standard-check-ssh-key--restricted-sudo). SSH key plus restricted sudo, using the inventory's `ansible_user`.
3. [Dedicated check account](#3-dedicated-check-account-ssh-key--password). SSH key plus password, for hosts whose `sshd` requires both.

## Legend

Every diagram uses the same color code:

```mermaid
flowchart LR
    L1["🐳 Docker container"]:::container
    L2[("💾 Data / files")]:::data
    L3["🖥️ Target server"]:::host
    L4["⌨️ Local PC process"]:::local
    L5(["👤 Person / browser"]):::person
    L1 ~~~ L2 ~~~ L3 ~~~ L4 ~~~ L5
    classDef container fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a8a
    classDef data fill:#fef3c7,stroke:#d97706,stroke-width:2px,color:#78350f
    classDef host fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#14532d
    classDef local fill:#ede9fe,stroke:#7c3aed,stroke-width:2px,color:#4c1d95
    classDef person fill:#fce7f3,stroke:#db2777,stroke-width:2px,color:#831843
```

Arrows in the flowcharts: **green** = SSH to a server, **amber** = reading or writing report files, **pink** = HTTP from a browser.

## 1. How reports reach a dashboard

There are two independent ways to run sudoctor. Pick the one that fits. You don't need both, and neither depends on the other. If you do run both, each keeps its own reports and its own dashboard.

In both cases the servers never connect back. The controller (your PC or the scheduler container) opens the SSH connection and pulls the JSON report.

### 1a. Local PC (manual)

You run checks yourself from your PC. No Docker is needed.

```mermaid
flowchart LR
    subgraph PC["💻 Your PC"]
        INV[("💾 ~/.config/sudoctor/inventory")]:::data
        KEYS[("🔐 ~/.ssh<br/>config + keys")]:::data
        CLI["⌨️ ./scripts/sudoctor check"]:::local
        LR[("💾 ~/.local/state/sudoctor/reports<br/>or SUDOCTOR_REPORT_DIR")]:::data
        LD["📊 ./scripts/sudoctor dashboard<br/>--local, binds 127.0.0.1"]:::local
        INV -- "reads" --> CLI
        KEYS -- "reads" --> CLI
        CLI -- "writes latest + archive" --> LR
        LR -- "reads" --> LD
    end

    subgraph TARGETS["🖥️ Target servers"]
        H1["🖥️ host A<br/>sudoctor-health.py"]:::host
        H2["🖥️ host B<br/>sudoctor-health.py"]:::host
    end

    CLI -- "🔑 SSH + sudo -n collector" --> H1
    CLI -- "🔑 SSH + sudo -n collector" --> H2
    Browser(["👤 🌐 Browser"]):::person -- "http://127.0.0.1:8000" --> LD

    classDef container fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a8a
    classDef data fill:#fef3c7,stroke:#d97706,stroke-width:2px,color:#78350f
    classDef host fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#14532d
    classDef local fill:#ede9fe,stroke:#7c3aed,stroke-width:2px,color:#4c1d95
    classDef person fill:#fce7f3,stroke:#db2777,stroke-width:2px,color:#831843
    style TARGETS fill:#16a34a14,stroke:#16a34a,stroke-width:2px,stroke-dasharray:6 4,color:#16a34a
    style PC fill:#7c3aed14,stroke:#7c3aed,stroke-width:2px,stroke-dasharray:6 4,color:#7c3aed
    linkStyle 0,1,2,3 stroke:#d97706,stroke-width:2px
    linkStyle 4,5 stroke:#16a34a,stroke-width:2.5px
    linkStyle 6 stroke:#db2777,stroke-width:2px
```

The dashboard only reads files, so it works without an inventory or SSH access. Run a check, then press **Refresh**.

### 1b. Docker controller (scheduled)

An always-on machine runs checks on a cron schedule. Your PC is only needed to open the dashboard.

```mermaid
flowchart LR
    subgraph DOCKER["🐳 Docker controller"]
        INV[("💾 inventory dir<br/>mounted read-only")]:::data
        KEYS[("🔐 SUDOCTOR_SSH_DIR → /root/.ssh<br/>mounted read-only")]:::data
        CRON["⏰ scheduler container<br/>cron → sudoctor-scheduled.py sweep"]:::container
        VOL[("💾 state volume<br/>state.json + reports/")]:::data
        DD["📊 dashboard container<br/>read-only, no SSH credentials"]:::container
        INV -- "reads" --> CRON
        KEYS -- "reads" --> CRON
        CRON -- "writes" --> VOL
        VOL -- "reads (ro)" --> DD
    end

    subgraph TARGETS["🖥️ Target servers"]
        H1["🖥️ host A<br/>sudoctor-health.py"]:::host
        H2["🖥️ host B<br/>sudoctor-health.py"]:::host
    end

    CRON -- "🔑 SSH + sudo -n collector" --> H1
    CRON -- "🔑 SSH + sudo -n collector" --> H2
    Browser(["👤 🌐 Browser"]):::person -- "http://127.0.0.1:8000<br/>(SUDOCTOR_BIND_ADDRESS)" --> DD

    classDef container fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a8a
    classDef data fill:#fef3c7,stroke:#d97706,stroke-width:2px,color:#78350f
    classDef host fill:#dcfce7,stroke:#16a34a,stroke-width:2px,color:#14532d
    classDef local fill:#ede9fe,stroke:#7c3aed,stroke-width:2px,color:#4c1d95
    classDef person fill:#fce7f3,stroke:#db2777,stroke-width:2px,color:#831843
    style TARGETS fill:#16a34a14,stroke:#16a34a,stroke-width:2px,stroke-dasharray:6 4,color:#16a34a
    style DOCKER fill:#2563eb14,stroke:#2563eb,stroke-width:2px,stroke-dasharray:6 4,color:#2563eb
    linkStyle 0,1,2,3 stroke:#d97706,stroke-width:2px
    linkStyle 4,5 stroke:#16a34a,stroke-width:2.5px
    linkStyle 6 stroke:#db2777,stroke-width:2px
```

Only the scheduler container holds SSH credentials. The dashboard container gets the state volume read-only.

Neither dashboard has built-in authentication. Both bind to loopback by default.

## 2. Standard check: SSH key + restricted sudo

This is the default for every host. One account (`ansible_user` from `hosts.ini`) does everything. `setup` uses its full sudo rights once. After that, checks can run only the fixed collector command without a password.

```mermaid
sequenceDiagram
    autonumber
    box rgba(219,39,119,0.10) 👤 You
        actor Admin as 👤 Admin
    end
    box rgba(124,58,237,0.10) 💻 / 🐳 Controller
        participant C as ⌨️ Controller<br/>(PC or scheduler)
    end
    box rgba(22,163,74,0.12) 🖥️ Target server
        participant S as 🚪 sshd
        participant U as 👤 ansible_user
        participant R as 🛡️ root (sudo)
    end

    rect rgba(217,119,6,0.12)
    Note over Admin,R: 🛠️ One-time: ./scripts/sudoctor setup HOST
    Admin->>C: setup (optionally SUDOCTOR_BECOME_ARGS='--ask-become-pass')
    C->>S: 🔑 SSH as ansible_user (key from ~/.ssh, host key verified)
    S-->>U: session
    U->>R: sudo (full admin, password if required)
    R->>R: install /usr/local/libexec/sudoctor/sudoctor-health.py<br/>and /etc/sudoctor/health.json (root-owned)
    R->>R: write /etc/sudoers.d/sudoctor-health<br/>ansible_user NOPASSWD: only the collector command
    end

    rect rgba(22,163,74,0.12)
    Note over Admin,R: 🔍 Every check (manual or scheduled)
    C->>S: 🔑 SSH as ansible_user, key auth<br/>scheduler adds BatchMode=yes, StrictHostKeyChecking=yes
    S-->>U: session
    U->>R: sudo -n python3 sudoctor-health.py health.json
    R-->>U: 📄 health JSON
    U-->>C: stdout
    C->>C: 💾 store report (local reports dir or /state)
    end
```

What the restricted sudo rule allows:

- `check` runs `sudo -n` with only the exact collector path and config path. Any other command still needs a sudo password, and `-n` fails instead of prompting.
- `update`, `reboot` and `recover` use normal sudo authentication.

## 3. Dedicated check account: SSH key + password

Use this for hosts where `sshd` has `AuthenticationMethods publickey,password`. The scheduler can't type a password, so it logs in as a separate low-privilege `sudoctor_check_user`, and `sshpass` supplies the stored password. Admin actions keep using `ansible_user`.

### Provisioning (once per host)

```mermaid
sequenceDiagram
    autonumber
    box rgba(219,39,119,0.10) 👤 You
        actor Admin as 👤 Admin
    end
    box rgba(37,99,235,0.12) 🐳 Scheduler container
        participant H as 🧰 sudoctor-check-user
        participant C as ⏰ scheduler
    end
    box rgba(217,119,6,0.12) 💾 Mounted files
        participant F as 🔐 /root/.ssh<br/>/run/secrets
        participant HV as 📝 host_vars/ALIAS.yml
    end
    box rgba(22,163,74,0.12) 🖥️ Target server
        participant T as 🛡️ ansible_user + sudo
    end

    rect rgba(37,99,235,0.08)
    Note over Admin,F: 1️⃣ Generate credentials
    Admin->>H: docker compose run ... sudoctor-check-user ALIAS
    H->>F: 🔑 sudoctor-check@ALIAS (ed25519 key, no passphrase)
    H->>F: 🔒 sudoctor-check@ALIAS.pass (random, mode 600)
    H-->>Admin: prints sudoctor_check_* settings
    end

    rect rgba(217,119,6,0.08)
    Note over Admin,HV: 2️⃣ Configure the host
    Admin->>HV: sudoctor_check_user / _key_file / _password_file<br/>(optional sudoctor_check_from CIDR)
    end

    rect rgba(22,163,74,0.10)
    Note over Admin,T: 3️⃣ Run setup once as admin
    Admin->>C: compose exec ... sudoctor setup ALIAS<br/>--ask-pass --ask-become-pass
    C->>F: read .pub key and .pass on the controller
    C->>T: 🔑+🔒 SSH as ansible_user (admin key + admin password)
    T->>T: useradd sudoctor_check_user (no extra groups)
    T->>T: chpasswd from .pass
    T->>T: authorized_keys: restrict[,from="CIDR"] PUBKEY
    T->>T: sudoers: ansible_user, check_user NOPASSWD: collector only
    end
```

### Scheduled check

```mermaid
sequenceDiagram
    autonumber
    box rgba(37,99,235,0.12) 🐳 Scheduler container
        participant SW as ⏰ sudoctor-scheduled.py sweep
        participant A as ⚙️ ansible-playbook check
    end
    box rgba(22,163,74,0.12) 🖥️ Target server
        participant S as 🚪 sshd
        participant K as 👤 sudoctor_check_user
        participant R as 🛡️ root (sudo)
    end
    box rgba(217,119,6,0.12) 💾 Data
        participant V as 💾 state volume
    end

    SW->>SW: read hostvars via ansible-inventory
    alt sudoctor_check_user is set
        SW->>SW: write temp check-account.yml (extra vars):<br/>ansible_user, private key file, ansible_password
        SW->>SW: drop BatchMode=yes so sshpass can answer
    else not set
        SW->>SW: standard flow (ansible_user, BatchMode=yes)
    end
    SW->>A: sudoctor check ALIAS -e @check-account.yml

    rect rgba(22,163,74,0.12)
    Note over A,S: 🔑 + 🔒 Two-factor SSH login
    A->>S: 🔑 1st factor: publickey (as check user)
    S->>S: authorized_keys: restrict (no PTY or forwarding),<br/>optional from= check
    A->>S: 🔒 2nd factor: password (via sshpass)
    S-->>K: session
    end

    K->>R: 🛡️ sudo -n python3 sudoctor-health.py health.json
    R-->>K: 📄 health JSON
    K-->>A: stdout
    A-->>SW: report in temp dir
    SW->>V: update state.json, archive in reports/ALIAS/
```

Why extra vars: Ansible extra vars take priority over inventory values. The scheduler can override the login account for each run without changing the inventory, so `setup`, `update`, `reboot`, `recover` and manual `check` keep using `ansible_user`.

## Accounts at a glance

| Action | Runs from | SSH account | Auth to sshd | Privilege on target |
| --- | --- | --- | --- | --- |
| `setup`, `update`, `reboot`, `recover` | PC or scheduler (`exec`) | `ansible_user` | your SSH key (+ password if sshd requires it) | full sudo, normal authentication |
| `check`, manual | PC | `ansible_user` | your SSH key | `sudo -n` for the collector only |
| `check`, scheduled, standard host | scheduler | `ansible_user` | key, `BatchMode=yes` | `sudo -n` for the collector only |
| `check`, scheduled, key + password host | scheduler | `sudoctor_check_user` | restricted key + password via `sshpass` | `sudo -n` for the collector only |
| dashboard (local or Docker) | PC or dashboard container | none | none | reads report files only |
