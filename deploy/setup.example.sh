#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Copy to deploy/setup.local.sh (git-ignored), fill values, then sudo bash that file.
set -Eeuo pipefail
HOST='phone.example.com'
ACME_EMAIL='you@example.com'
DNS_PLUGIN='dns_cf'  # Any plugin available in the pinned acme.sh release.
DNS_CREDENTIALS='/root/homeline-dns.json'  # Root-owned 0600 JSON, NOT a shell script.
TURN_CONFIG='/root/homeline-turn.json'    # Root-owned 0600 JSON; see turn.example.json.
FXO_GATEWAY=''                           # Optional LAN address.
SOURCE_REF='main'                        # Or full reviewed 40-character commit SHA.

args=(--host "$HOST" --acme-email "$ACME_EMAIL" --acme-dns "$DNS_PLUGIN" --dns-env "$DNS_CREDENTIALS" --ref "$SOURCE_REF")
[[ -z $TURN_CONFIG ]] || args+=(--turn-config "$TURN_CONFIG")
[[ -z $FXO_GATEWAY ]] || args+=(--gateway "$FXO_GATEWAY")
bash "$(dirname -- "${BASH_SOURCE[0]}")/bootstrap.sh" "${args[@]}"
