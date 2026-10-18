#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -Eeuo pipefail
[[ $EUID == 0 && $# == 2 ]] || { echo 'Usage: sudo deploy/backup.sh ARCHIVE PASSPHRASE_FILE' >&2; exit 1; }
exec /opt/homeline/venv/bin/homeline backup "$1" --passphrase-file "$2"
