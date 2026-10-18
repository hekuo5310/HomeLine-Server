#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -Eeuo pipefail
trap 'echo "Install failed at line $LINENO; inspect journalctl -u homeline -u homeline-asterisk" >&2' ERR
[[ $EUID == 0 ]] || { echo 'Run as root' >&2; exit 1; }
source /etc/os-release
[[ ${ID:-} == debian || ${ID:-} == raspbian ]] && [[ ${VERSION_CODENAME:-} == trixie || ${VERSION_CODENAME:-} == bookworm ]] && [[ $(uname -m) == aarch64 ]] || { echo 'Requires 64-bit Raspberry Pi OS Bookworm/Trixie (aarch64)' >&2; exit 1; }
[[ -f /proc/device-tree/model ]] && grep -q 'Raspberry Pi' /proc/device-tree/model || { echo 'Raspberry Pi hardware required' >&2; exit 1; }
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
# Required first-install flags: --host phone.example --cert /path/fullchain.pem --key /path/key.pem
[[ $# -ge 6 || -f /etc/homeline/server.env ]] || { echo 'Supply --host DNS --cert FILE --key FILE (optional --gateway IPv4 --media-address IPv4)' >&2; exit 1; }
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv python3-dev build-essential pkg-config curl ca-certificates libjansson-dev libedit-dev libsrtp2-dev libxml2-dev uuid-dev libsqlite3-dev libncurses-dev libssl-dev libcurl4-openssl-dev nginx rsync bzip2
getent group asterisk >/dev/null || groupadd --system asterisk
id asterisk >/dev/null 2>&1 || useradd --system --gid asterisk --home-dir /var/lib/homeline-asterisk --shell /usr/sbin/nologin asterisk
id homeline >/dev/null 2>&1 || useradd --system --gid asterisk --home-dir /var/lib/homeline --shell /usr/sbin/nologin homeline
install -d -m 0750 -o homeline -g asterisk /etc/homeline /var/lib/homeline /etc/homeline/asterisk/generated
install -d -m 0750 -o asterisk -g asterisk /var/lib/homeline-asterisk /var/log/homeline-asterisk /var/spool/homeline-asterisk
install -d -m 0755 /opt/homeline
[[ -x /opt/homeline/venv/bin/python ]] || python3 -m venv /opt/homeline/venv
/opt/homeline/venv/bin/pip install -r "$SOURCE/requirements.lock"
/opt/homeline/venv/bin/pip install --no-deps "$SOURCE"
if [[ ! -x /opt/homeline-asterisk/sbin/asterisk ]]; then
    WORK=$(mktemp -d /var/tmp/homeline-build.XXXXXXXX)
    curl --fail --location --proto '=https' --tlsv1.2 --retry 3 https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-22.11.0.tar.gz -o "$WORK/asterisk.tar.gz"
    echo '3bd5ee040509a3d3cd9b1ba9520c18e6ec0a7e7981ca68c457dcd36ba3c54d94  '"$WORK/asterisk.tar.gz" | sha256sum --check -
    tar -xzf "$WORK/asterisk.tar.gz" --directory "$WORK"
    pushd "$WORK/asterisk-22.11.0" >/dev/null
    ./configure --prefix=/opt/homeline-asterisk --sysconfdir=/etc/homeline/asterisk --with-pjproject-bundled
    make menuselect.makeopts
    menuselect/menuselect --disable BUILD_NATIVE menuselect.makeopts
    make -j2
    make install
    popd >/dev/null
    echo "Independent Asterisk build retained at $WORK; remove after verification."
fi
[[ $(/opt/homeline-asterisk/sbin/asterisk -V) == 'Asterisk 22.11.0' ]] || { echo 'Unexpected Asterisk version; review before upgrading' >&2; exit 1; }
if [[ $# -gt 0 ]]; then
    /opt/homeline/venv/bin/python "$SOURCE/deploy/configure.py" "$@"
else
    [[ -f /etc/homeline/asterisk/asterisk.conf ]] || { echo 'Configuration incomplete; repeat with first-install flags' >&2; exit 1; }
fi
chown -R homeline:asterisk /etc/homeline
chmod 0750 /etc/homeline /etc/homeline/asterisk /etc/homeline/asterisk/generated
chmod 0600 /etc/homeline/master.key /etc/homeline/server.env
# Asterisk owns only its mutable state; API owns only generated device configuration.
runuser -u homeline -- /opt/homeline/venv/bin/homeline migrate
install -m 0644 "$SOURCE/deploy/systemd/homeline.service" /etc/systemd/system/
install -m 0644 "$SOURCE/deploy/systemd/homeline-asterisk.service" /etc/systemd/system/
ln -sfn /etc/nginx/sites-available/homeline /etc/nginx/sites-enabled/homeline
nginx -t
systemctl daemon-reload
systemctl enable --now homeline-asterisk homeline nginx
systemctl restart homeline nginx
for attempt in {1..30}; do
    if curl --fail --silent http://127.0.0.1:8000/health/ready >/dev/null; then
        echo 'HomeLine ready. Initialize administrator: sudo -u homeline /opt/homeline/venv/bin/homeline init-admin'
        echo 'Config: /etc/homeline/server.env; diagnostics: journalctl -u homeline -u homeline-asterisk'
        exit 0
    fi
    sleep 1
done
echo 'Readiness check failed; services/configuration retained for diagnosis' >&2
exit 1
