#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -Eeuo pipefail
umask 077
require_root() { [[ $EUID == 0 ]]; }
pending_installation() { [[ -f /etc/homeline-certs/pending && -f /etc/homeline/server.env ]]; }
acquire_lock() { exec 9>/run/lock/homeline-certificate.lock; flock -n 9; }
certificate_paths() {
    CERT=/etc/homeline-certs/fullchain.pem
    KEY=/etc/homeline-certs/key.pem
    HOST=$(cat /etc/homeline-acme/domain)
}
idle() { /opt/homeline-asterisk/sbin/asterisk -C /etc/homeline/asterisk/asterisk.conf -rx 'core show channels count' | grep -q '^0 active channels$'; }

activate() {
    require_root
    pending_installation || return 0
    acquire_lock || return 0
    certificate_paths
openssl verify -purpose sslserver -verify_hostname "$HOST" -untrusted "$CERT" "$CERT" >/dev/null
openssl x509 -in "$CERT" -noout -checkend 0 >/dev/null
cert_public=$(openssl x509 -in "$CERT" -pubkey -noout | openssl pkey -pubin -outform DER | sha256sum)
key_public=$(openssl pkey -in "$KEY" -passin pass: -pubout -outform DER | sha256sum)
[[ $cert_public == "$key_public" ]] || { echo 'Certificate/key mismatch; pending activation retained' >&2; return 1; }
# Do not terminate ongoing calls. The timer retries pending changes every five minutes.
if ! idle; then echo 'Certificate activation deferred: PBX busy or unavailable'; return 0; fi
systemctl stop homeline
trap 'systemctl start homeline' EXIT
if ! idle; then echo 'Certificate activation deferred: channel appeared'; return 0; fi
install -o homeline -g asterisk -m 0640 "$CERT" /etc/homeline/asterisk/tls/server.crt
install -o homeline -g asterisk -m 0640 "$KEY" /etc/homeline/asterisk/tls/server.key
nginx -t
systemctl reload nginx
systemctl restart homeline-asterisk
systemctl start homeline
for attempt in {1..30}; do
    if curl --fail --silent http://127.0.0.1:8000/health/ready >/dev/null; then
        rm -- /etc/homeline-certs/pending
        return 0
    fi
    sleep 1
done
echo 'Certificate activation readiness failed; pending activation retained' >&2
return 1
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then activate; fi
