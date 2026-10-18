#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -Eeuo pipefail
[[ $EUID == 0 && $# == 2 ]] || { echo 'Usage: sudo deploy/upgrade.sh BACKUP_FILE PASSPHRASE_FILE' >&2; exit 1; }
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# Use this checkout's maintenance code, including during upgrades from older CLIs.
export PYTHONPATH="$SOURCE/src"
/opt/homeline/venv/bin/homeline backup "$1" --passphrase-file "$2" --check
trap 'echo "Upgrade failed; service remains stopped. Restore the encrypted pre-upgrade backup before rollback." >&2' ERR
systemctl stop homeline
"$SOURCE/deploy/backup.sh" "$1" "$2"
/opt/homeline/venv/bin/pip install -r "$SOURCE/requirements.lock"
/opt/homeline/venv/bin/pip install --no-deps "$SOURCE"
runuser -u homeline -- /opt/homeline/venv/bin/homeline migrate
install -m 0644 "$SOURCE/deploy/systemd/homeline.service" /etc/systemd/system/
systemctl daemon-reload
systemctl start homeline
for attempt in {1..30}; do
    if curl --fail --silent http://127.0.0.1:8000/health/ready >/dev/null; then
        runuser -u homeline -- /opt/homeline/venv/bin/homeline doctor
        exit 0
    fi
    sleep 1
done
echo 'Upgrade readiness failed' >&2
exit 1
