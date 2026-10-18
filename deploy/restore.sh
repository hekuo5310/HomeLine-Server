#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -Eeuo pipefail
[[ $EUID == 0 && $# == 2 ]] || { echo 'Usage: sudo deploy/restore.sh ARCHIVE PASSPHRASE_FILE' >&2; exit 1; }
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# Use this checkout's maintenance code, including during upgrades from older CLIs.
export PYTHONPATH="$SOURCE/src"
/opt/homeline/venv/bin/homeline restore "$1" --passphrase-file "$2" --check
trap 'echo "Restore failed; service remains stopped for diagnosis" >&2' ERR
systemctl stop homeline homeline-asterisk
/opt/homeline/venv/bin/homeline restore "$1" --passphrase-file "$2"
chown homeline:asterisk /var/lib/homeline/homeline.sqlite3
chown -R homeline:asterisk /etc/homeline
systemctl start homeline-asterisk homeline
for attempt in {1..30}; do
    if curl --fail --silent http://127.0.0.1:8000/health/ready >/dev/null; then
        runuser -u homeline -- /opt/homeline/venv/bin/homeline doctor
        exit 0
    fi
    sleep 1
done
exit 1
