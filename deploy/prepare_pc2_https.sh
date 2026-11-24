#!/usr/bin/env bash
# Prepare project-local HTTPS files only; never starts a server or robot client.
set -euo pipefail
project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

if [[ ${1:-} == --help ]]; then
    echo "Usage: bash deploy/prepare_pc2_https.sh [PC2_WIFI_IPV4]"
    echo "Defaults to the current wlan0 IPv4 address. Reuses valid matching certificates."
    echo "Otherwise validates a new pair before backing up and replacing the old files."
    echo "Stop the program using port 8012 before replacing certificates. No sudo needed."
    exit 0
fi
if [[ $# -gt 1 ]]; then
    echo "Expected at most one PC2 Wi-Fi IPv4 address; use --help." >&2
    exit 2
fi
wifi_ip=${1:-}
if [[ $# -eq 0 ]]; then
    wifi_ip=$(ip -4 -o addr show dev wlan0 | awk '{split($4, a, "/"); print a[1]; exit}')
fi
if [[ ! $wifi_ip =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    echo "HTTPS ERROR: wlan0 needs an IPv4 address, or pass its current address explicitly." >&2
    exit 2
fi
IFS=. read -r -a octets <<< "$wifi_ip"
for octet in "${octets[@]}"; do
    if ((10#$octet > 255)); then
        echo "HTTPS ERROR: invalid IPv4 address: $wifi_ip" >&2
        exit 2
    fi
done
for command in openssl flock ss; do
    if ! command -v "$command" >/dev/null; then
        echo "HTTPS ERROR: missing command: $command" >&2
        exit 2
    fi
done

umask 077
cert_dir="$project_root/.local/https"
mkdir -p "$cert_dir"
chmod 700 "$cert_dir"
# Serialize certificate preparation, including standalone runs and launchers.
exec 9>"$cert_dir/.prepare.lock"
if ! flock -w 10 9; then
    echo "HTTPS ERROR: another certificate preparation is still running." >&2
    exit 2
fi

valid_pair() {
    local directory=$1 cert_public key_public
    [[ -r "$directory/cert.pem" && -r "$directory/key.pem" ]] || return 1
    # verify checks the validity period and IP SAN; x509 -checkip alone is insufficient.
    openssl verify -CAfile "$directory/cert.pem" -verify_ip "$wifi_ip" \
        "$directory/cert.pem" >/dev/null 2>&1 || return 1
    cert_public=$(openssl x509 -in "$directory/cert.pem" -pubkey -noout 2>/dev/null) || return 1
    # Fail instead of prompting if an unusable encrypted private key was copied here.
    key_public=$(openssl pkey -in "$directory/key.pem" -passin pass: -pubout 2>/dev/null) || return 1
    [[ -n "$cert_public" && "$cert_public" == "$key_public" ]]
}

if valid_pair "$cert_dir"; then
    echo "HTTPS REUSE: existing certificate is valid for $wifi_ip."
    exit 0
fi

listeners=$(ss -H -ltn 'sport = :8012')
if [[ -n "$listeners" ]]; then
    echo "HTTPS ERROR: port 8012 is in use. Stop the existing program normally before replacing certificates." >&2
    exit 2
fi

temporary_dir=$(mktemp -d "$cert_dir/new.XXXXXX")
trap 'rm -rf -- "$temporary_dir"' EXIT
if ! openssl req -x509 -nodes -sha256 -days 365 -newkey rsa:2048 \
    -keyout "$temporary_dir/key.pem" -out "$temporary_dir/cert.pem" \
    -subj '/CN=g1d-pc2' \
    -addext "subjectAltName=IP:$wifi_ip,IP:127.0.0.1,DNS:localhost" \
    > /dev/null 2>"$temporary_dir/openssl.log"; then
    cat "$temporary_dir/openssl.log" >&2
    echo "HTTPS ERROR: certificate generation failed; existing files were not replaced." >&2
    exit 2
fi
if ! valid_pair "$temporary_dir"; then
    echo "HTTPS ERROR: new certificate validation failed; existing files were not replaced." >&2
    exit 2
fi

# Do not move or overwrite the old pair until the new pair has passed validation.
if [[ -e "$cert_dir/cert.pem" || -L "$cert_dir/cert.pem" ||
      -e "$cert_dir/key.pem" || -L "$cert_dir/key.pem" ]]; then
    backup_dir=$(mktemp -d "$project_root/.local/https-backup-$(date +%Y%m%d-%H%M%S).XXXXXX")
    for name in cert.pem key.pem; do
        if [[ -e "$cert_dir/$name" || -L "$cert_dir/$name" ]]; then
            cp -- "$cert_dir/$name" "$backup_dir/$name"
        fi
    done
    echo "HTTPS BACKUP: $backup_dir"
fi
mv -- "$temporary_dir/key.pem" "$cert_dir/key.pem"
mv -- "$temporary_dir/cert.pem" "$cert_dir/cert.pem"
echo "HTTPS CREATED: certificate for $wifi_ip. PICO may need to accept this new certificate."
echo "PICO page: https://$wifi_ip:8012/?ws=wss://$wifi_ip:8012"
