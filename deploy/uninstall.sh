#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -Eeuo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root' >&2; exit 1; }
[[ $# == 0 ]] || { echo 'No arguments accepted: data/configuration are always retained' >&2; exit 1; }
for service in homeline-acme.timer homeline-cert-activate.timer homeline-acme.service homeline-cert-activate.service homeline homeline-asterisk; do
    if systemctl cat "$service" >/dev/null 2>&1; then systemctl disable --now "$service"; fi
done
rm -f /etc/systemd/system/homeline.service /etc/systemd/system/homeline-asterisk.service
rm -f /etc/systemd/system/homeline-acme.{service,timer} /etc/systemd/system/homeline-cert-activate.{service,timer}
if [[ -L /etc/nginx/sites-enabled/homeline ]]; then rm /etc/nginx/sites-enabled/homeline; fi
systemctl daemon-reload
nginx -t && systemctl reload nginx
echo 'Services removed. Database, encrypted secrets, configuration, independent Asterisk and Python environment retained. Back up before removing those directories manually.'
