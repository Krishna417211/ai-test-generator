#!/usr/bin/env bash
#
# One-time setup for the EC2 instance. Run once, as root, on a fresh Amazon
# Linux 2023 box:
#
#   sudo bash bootstrap.sh /dev/nvme1n1
#
# After this, deploys are automatic — CI pushes images to ECR and rolls the
# stack over SSM. Nothing here runs again on a deploy.
#
# What it does: installs Docker, puts a filesystem on the EBS data volume (only
# if it has none), mounts it at /var/data so it survives instance replacement,
# and lays out the directories the compose file expects.
set -euo pipefail

DEVICE="${1:-/dev/nvme1n1}"
MOUNT="/var/data"
REPO_URL="${REPO_URL:-https://github.com/Krishna417211/ai-test-generator.git}"
CHECKOUT="/opt/testra"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run this with sudo"
[[ -b "$DEVICE" ]] || die "$DEVICE is not a block device. Check 'lsblk' — on Nitro instances the data volume is usually /dev/nvme1n1, and the root volume is /dev/nvme0n1. Do not pass the root device."

# ── The one destructive step, and the guard on it ──────────────────────────
# mkfs on the wrong device wipes the box, and on a second run would wipe every
# account and paid plan on it. So: format only a device that has no filesystem,
# and never touch one that is already mounted.
if findmnt --source "$DEVICE" >/dev/null 2>&1; then
    die "$DEVICE is already mounted. Refusing to touch it."
fi

EXISTING_FS="$(blkid -o value -s TYPE "$DEVICE" 2>/dev/null || true)"
if [[ -z "$EXISTING_FS" ]]; then
    log "No filesystem on $DEVICE — creating ext4"
    mkfs -t ext4 "$DEVICE"
else
    log "$DEVICE already has a $EXISTING_FS filesystem — keeping it (this is a re-run, or a volume with data)"
fi

log "Mounting $DEVICE at $MOUNT"
mkdir -p "$MOUNT"

# fstab by UUID, not device name: NVMe device numbering is not stable across
# reboots, and an fstab pointing at the wrong disk fails the boot.
UUID="$(blkid -o value -s UUID "$DEVICE")"
[[ -n "$UUID" ]] || die "could not read a UUID from $DEVICE"

if ! grep -q "$UUID" /etc/fstab; then
    # nofail: a missing volume should degrade to a broken app, not an instance
    # that won't boot and can't be SSH'd into to fix.
    echo "UUID=$UUID  $MOUNT  ext4  defaults,nofail  0  2" >> /etc/fstab
fi
mount -a
findmnt --target "$MOUNT" >/dev/null || die "$MOUNT did not mount"

log "Creating the directories the compose file expects"
# db/ is the SQLite database; caddy/ holds issued TLS certs.
mkdir -p "$MOUNT/db" "$MOUNT/caddy" "$MOUNT/caddy-config"

# ── Swap ───────────────────────────────────────────────────────────────────
# A free-tier t3.micro has 1GB, and Docker + uvicorn + Caddy sit close enough to
# it that a traffic spike can trip the OOM killer — which picks the biggest
# process, i.e. the API. Swap makes that case slow instead of dead. Skipped on
# instances with room to spare.
TOTAL_MB="$(free -m | awk '/^Mem:/ {print $2}')"
if [[ "$TOTAL_MB" -lt 2048 && ! -f /swapfile ]]; then
    log "Only ${TOTAL_MB}MB RAM — adding a 2GB swap file"
    # fallocate can produce a sparse file that swapon rejects; dd is slower and
    # always works.
    dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    # Prefer reclaiming page cache over swapping; swap here is a safety net, not
    # somewhere to run from.
    sysctl -w vm.swappiness=10 >/dev/null
    grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
fi

log "Installing Docker"
dnf -y update
dnf -y install docker git
systemctl enable --now docker

# The compose plugin isn't in the AL2023 repos; install it where Docker looks.
if ! docker compose version >/dev/null 2>&1; then
    log "Installing the Docker Compose plugin"
    PLUGIN_DIR="/usr/libexec/docker/cli-plugins"
    mkdir -p "$PLUGIN_DIR"
    ARCH="$(uname -m)"
    curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${ARCH}" \
        -o "$PLUGIN_DIR/docker-compose"
    chmod +x "$PLUGIN_DIR/docker-compose"
fi
docker compose version >/dev/null || die "docker compose still not working"

log "Checking out the repo at $CHECKOUT"
# Only the compose file is needed here; the images come from ECR. CI checks out
# the deployed SHA into this clone so the compose file matches the images.
if [[ -d "$CHECKOUT/.git" ]]; then
    git -C "$CHECKOUT" remote set-url origin "$REPO_URL"
else
    git clone "$REPO_URL" "$CHECKOUT"
fi

# ── Secrets ────────────────────────────────────────────────────────────────
# Written once, by hand, and never by CI: keeping them on the box means the
# deploy pipeline never needs to hold a production secret, so a compromised
# workflow token cannot read them.
ENV_FILE="$MOUNT/testra.env"
if [[ ! -f "$ENV_FILE" ]]; then
    log "Creating $ENV_FILE — you must fill this in before the app is useful"
    cat > "$ENV_FILE" <<'EOF'
# Production secrets. Not in git, not in the image, not in GitHub Actions.
# Fill these in, then: docker compose -f /opt/testra/deploy/docker-compose.prod.yml up -d

# At least one LLM provider key, or generation cannot run.
GEMINI_API_KEY_1=
GROQ_API_KEY_1=
ANTHROPIC_API_KEY_1=

# Public URLs. Both are this site's own origin, because Caddy serves the SPA
# and the API together.
FRONTEND_URL=https://CHANGE-ME
BACKEND_URL=https://CHANGE-ME
CORS_ORIGINS=https://CHANGE-ME

# Encrypts linked GitHub tokens at rest. Generate with:
#   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
# Changing it later forces every user to re-link GitHub.
SESSION_SECRET=

# GitHub OAuth app. Callback must be <BACKEND_URL>/api/auth/github/callback
GITHUB_CLIENT_ID=
GITHUB_CLIENT_SECRET=

# SMTP. In production an unconfigured relay is refused, not skipped — signup
# verification and login OTP will fail closed without it.
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=
SMTP_FROM_NAME=Testra

# Comma-separated admin emails. Each must belong to a verified account.
ADMIN_EMAILS=

# Billing stays OFF until these are set. Leave empty and no payment is possible,
# which is the correct state until the database is known to be durable.
# See docs/DEPLOY.md before filling these in.
BILLING_CHECKOUT_URL_MONTHLY=
BILLING_CHECKOUT_URL_YEARLY=
BILLING_WEBHOOK_SECRET=
BILLING_CONTACT_EMAIL=
EOF
    chmod 600 "$ENV_FILE"
else
    log "$ENV_FILE already exists — leaving it alone"
fi

log "Done."
cat <<EOF

Next:
  1. Fill in $ENV_FILE          (it is a skeleton; the app will not work until you do)
  2. Point DNS at this instance's Elastic IP
  3. Set SITE_URL / SITE_ADDRESS in the GitHub repo variables
  4. Push to master — CI builds, pushes to ECR, and rolls this box

Data lives at $MOUNT and survives instance replacement. Everything else here
is disposable.
EOF
