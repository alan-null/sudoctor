#!/bin/sh
set -eu
cd /app
if [ "${1:-}" = scheduler ]; then
  python3 - <<'PY'
import os
import re
import shlex
import sys
schedule = os.environ.get('SUDOCTOR_CRON', '*/15 * * * *')
if not re.fullmatch(r'[0-9*/,\-]+(?: +[0-9*/,\-]+){4}', schedule):
    sys.exit('SUDOCTOR_CRON must contain five numeric cron fields')
with open('/tmp/sudoctor-env', 'w') as env:
    for key in ('SUDOCTOR_INVENTORY', 'XDG_CONFIG_HOME', 'SUDOCTOR_STATE_DIR', 'SUDOCTOR_HOST_TIMEOUT', 'SUDOCTOR_STALE_SECONDS', 'ANSIBLE_CONFIG'):
        if key in os.environ:
            env.write('export ' + key + '=' + shlex.quote(os.environ[key]) + '\n')
with open('/tmp/sudoctor-crontab', 'w') as cron:
    cron.write('PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n')
    cron.write(schedule + ' . /tmp/sudoctor-env; /app/scripts/sudoctor-scheduled.py sweep >> /proc/1/fd/1 2>&1\n')
PY
  crontab /tmp/sudoctor-crontab
  mkdir -p "${SUDOCTOR_STATE_DIR:-/state}"
  exec flock -n "${SUDOCTOR_STATE_DIR:-/state}/scheduler.lock" sh -c '/app/scripts/sudoctor-scheduled.py sweep & exec cron -f'
fi
exec /app/scripts/sudoctor-scheduled.py "$@"
