#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -Eeuo pipefail
[[ $EUID == 0 && $# == 2 ]] || { echo 'Usage: sudo deploy/restore.sh ARCHIVE PASSPHRASE_FILE' >&2; exit 1; }
systemctl stop homeline homeline-asterisk
trap 'echo "Restore failed; service remains stopped for diagnosis" >&2' ERR
/opt/homeline/venv/bin/homeline restore "$1" --passphrase-file "$2"
chown homeline:asterisk /var/lib/homeline/homeline.sqlite3
chown -R homeline:asterisk /etc/homeline
systemctl start homeline-asterisk homeline
for attempt in {1..30}; do
    if curl --fail --silent http://127.0.0.1:8000/health/ready >/dev/null; then exit 0; fi
    sleep 1
done
exit 1
