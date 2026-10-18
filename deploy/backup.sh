#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -Eeuo pipefail
[[ $EUID == 0 && $# == 2 ]] || { echo 'Usage: sudo deploy/backup.sh ARCHIVE PASSPHRASE_FILE' >&2; exit 1; }
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# Use this checkout's maintenance code, including during upgrades from older CLIs.
export PYTHONPATH="$SOURCE/src"
exec /opt/homeline/venv/bin/homeline backup "$1" --passphrase-file "$2"
