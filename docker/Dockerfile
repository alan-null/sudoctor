FROM debian:bookworm-slim AS scheduler
RUN apt-get update && apt-get install -y --no-install-recommends ansible openssh-client sshpass cron python3 ca-certificates tzdata util-linux && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY ansible.cfg ./
COPY scripts/sudoctor scripts/sudoctor-check-user scripts/sudoctor-health.py scripts/sudoctor-scheduled.py scripts/sudoctor-tmux.py scripts/sudoctor_notify.py scripts/sudoctor_notify_preview.py ./scripts/
COPY scripts/notify-plugins ./scripts/notify-plugins
COPY playbooks ./playbooks
COPY templates ./templates
COPY docker/docker-entrypoint.sh /usr/local/bin/sudoctor-entrypoint
ENTRYPOINT ["/usr/local/bin/sudoctor-entrypoint"]
CMD ["scheduler"]

FROM python:3.11-alpine AS dashboard
WORKDIR /app
COPY scripts/sudoctor-dashboard.py ./scripts/sudoctor-dashboard.py
COPY dashboard ./dashboard
ENTRYPOINT ["python3", "/app/scripts/sudoctor-dashboard.py"]
