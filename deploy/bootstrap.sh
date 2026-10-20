#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Download to a file before running; this script may read setup values from /dev/tty.
set -Eeuo pipefail
umask 077

usage() {
    cat <<'EOF'
Usage: sudo bash bootstrap.sh [options]
  --host DNS             Name covered by the trusted TLS certificate
  --cert /absolute/FILE  PEM certificate chain
  --key /absolute/FILE   Unencrypted PEM private key
  --gateway IPv4         Optional FXO gateway on a trusted LAN/VPN
  --media-address IPv4   Optional advertised media address
  --ref COMMIT           Fetch an exact 40-character Git commit (default: main)
  --non-interactive      Fail instead of prompting for missing setup values
  --help                 Show this help without changing the system

Requires Raspberry Pi OS Bookworm/Trixie 64-bit and a running systemd.
No credentials are embedded. An existing installation is checked, never upgraded.
EOF
}

die() { echo "HomeLine setup: $*" >&2; exit 1; }

check_platform() {
    [[ $EUID == 0 ]] || die 'Run with sudo bash bootstrap.sh'
    source /etc/os-release
    [[ ${ID:-} == debian || ${ID:-} == raspbian ]] &&
        [[ ${VERSION_CODENAME:-} == bookworm || ${VERSION_CODENAME:-} == trixie ]] &&
        [[ $(uname -m) == aarch64 ]] || die 'Requires Raspberry Pi OS Bookworm/Trixie 64-bit (aarch64)'
    [[ -f /proc/device-tree/model ]] && grep -q 'Raspberry Pi' /proc/device-tree/model || die 'Raspberry Pi hardware required'
    [[ -d /run/systemd/system ]] || die 'Boot Raspberry Pi OS normally with systemd before installing'
}

existing_installation() { [[ -f /etc/homeline/server.env ]]; }

check_existing() {
    [[ -x /opt/homeline/venv/bin/homeline ]] || die 'Incomplete installation; keep its database/key and repair using deploy/install.sh from the original checkout'
    runuser -u homeline -- /opt/homeline/venv/bin/homeline doctor || die 'Existing installation is unhealthy; inspect journalctl -u homeline -u homeline-asterisk'
    echo 'Existing installation verified; no packages, configuration or accounts changed.'
    echo 'To upgrade, use deploy/upgrade.sh BACKUP_FILE PASSPHRASE_FILE from the reviewed new checkout.'
}

prompt_value() {
    local label=$1 target=$2 answer
    [[ $NON_INTERACTIVE == false && -r /dev/tty && -w /dev/tty ]] || die "Missing $target; supply --host, --cert and --key (see --help)"
    printf '%s: ' "$label" >/dev/tty
    IFS= read -r answer </dev/tty || die "Could not read $target"
    printf -v "$target" '%s' "$answer"
}

validate_values() {
    [[ $HOST =~ ^[A-Za-z0-9.-]{1,200}$ ]] || die 'Invalid DNS hostname'
    local path value octet
    local -a octets
    for path in "$CERT" "$KEY"; do
        [[ $path =~ ^/[A-Za-z0-9_./-]+$ && -s $path ]] || die 'Certificate/key must be nonempty files with absolute paths containing only letters, digits, _, ., / and -'
    done
    for value in "$GATEWAY" "$MEDIA_ADDRESS"; do
        [[ -n $value ]] || continue
        [[ $value =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'Invalid gateway/media IPv4 address'
        IFS=. read -r -a octets <<<"$value"
        for octet in "${octets[@]}"; do
            [[ $octet == 0 || $octet =~ ^[1-9][0-9]{0,2}$ ]] && (( 10#$octet <= 255 )) || die 'Invalid gateway/media IPv4 address'
        done
    done
}

ensure_tools() {
    apt-get update
    apt-get install -y --no-install-recommends git ca-certificates openssl
}

validate_certificate() {
    openssl x509 -in "$CERT" -noout -checkend 0 >/dev/null || die 'Invalid or expired certificate'
    openssl verify -purpose sslserver -verify_hostname "$HOST" -untrusted "$CERT" "$CERT" >/dev/null || die 'Certificate chain/hostname is not trusted; install your private CA on this Pi and on each phone first'
    local cert_public key_public
    cert_public=$(openssl x509 -in "$CERT" -pubkey -noout | openssl pkey -pubin -outform DER | sha256sum) || die 'Invalid certificate public key'
    key_public=$(openssl pkey -in "$KEY" -passin pass: -pubout -outform DER | sha256sum) || die 'Private key must be valid and unencrypted'
    [[ $cert_public == "$key_public" ]] || die 'Certificate and private key do not match'
}

fetch_source() {
    local source=$1 ref=$2
    git -c init.defaultBranch=main init -q "$source"
    git -C "$source" remote add origin https://github.com/hekuo5310/HomeLine-Server.git
    git -C "$source" fetch --depth=1 origin "$ref"
    git -C "$source" -c advice.detachedHead=false checkout --detach FETCH_HEAD
    echo "Installing source commit: $(git -C "$source" rev-parse HEAD)"
}

run_installer() { bash "$1/deploy/install.sh" "${@:2}"; }

initialize_admin() {
    local answer
    if [[ $NON_INTERACTIVE == false && -t 0 ]]; then
        read -r -p 'Create initial administrator admin / extension 1001 now? [Y/n] ' answer || answer=n
        if [[ -z $answer || $answer == y || $answer == Y ]]; then
            runuser -u homeline -- /opt/homeline/venv/bin/homeline init-admin --username admin --extension 1001
            return
        fi
    fi
    echo 'Create your administrator (password is entered privately):'
    echo '  sudo -u homeline /opt/homeline/venv/bin/homeline init-admin --username admin --extension 1001'
}

acquire_install_lock() {
    exec 9>/run/lock/homeline-install.lock
    flock -n 9 || die 'Another HomeLine installation is running'
}

main() {
    HOST='' CERT='' KEY='' GATEWAY='' MEDIA_ADDRESS='' REF=main NON_INTERACTIVE=false
    while (( $# )); do
        case $1 in
            --help|-h) usage; return 0 ;;
            --non-interactive) NON_INTERACTIVE=true; shift ;;
            --host|--cert|--key|--gateway|--media-address|--ref)
                [[ $# -ge 2 && -n $2 && $2 != --* ]] || die "Missing value for $1"
                case $1 in
                    --host) HOST=$2 ;; --cert) CERT=$2 ;; --key) KEY=$2 ;;
                    --gateway) GATEWAY=$2 ;; --media-address) MEDIA_ADDRESS=$2 ;; --ref) REF=$2 ;;
                esac
                shift 2 ;;
            *) die "Unknown option: $1 (see --help)" ;;
        esac
    done
    [[ $REF == main || $REF =~ ^[0-9a-fA-F]{40}$ ]] || die '--ref must be main or a full 40-character commit SHA'
    check_platform
    # Serialize bootstrap attempts before apt, downloads or account creation.
    acquire_install_lock
    if existing_installation; then check_existing; return 0; fi
    [[ -n $HOST ]] || prompt_value 'HomeLine DNS hostname' HOST
    [[ -n $CERT ]] || prompt_value 'Fullchain PEM absolute path' CERT
    [[ -n $KEY ]] || prompt_value 'Private key absolute path' KEY
    validate_values
    ensure_tools
    validate_certificate
    WORK=$(mktemp -d /var/tmp/homeline-setup.XXXXXXXX)
    trap 'rm -rf -- "$WORK"' EXIT
    fetch_source "$WORK/source" "$REF"
    local args=(--host "$HOST" --cert "$CERT" --key "$KEY")
    [[ -z $GATEWAY ]] || args+=(--gateway "$GATEWAY")
    [[ -z $MEDIA_ADDRESS ]] || args+=(--media-address "$MEDIA_ADDRESS")
    run_installer "$WORK/source" "${args[@]}"
    echo 'Installation and readiness checks completed.'
    initialize_admin
    echo 'Firebase and FXO setup: https://github.com/hekuo5310/HomeLine-Server/blob/main/docs/deployment.md'
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    trap 'echo "HomeLine setup failed at line $LINENO; retain any existing database and master key." >&2' ERR
    main "$@"
fi
