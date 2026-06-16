#!/bin/bash
# R2 Motor Control — Raspberry Pi Boot Optimization
# Saves 90-150s on Pi Zero W by eliminating network timeout, disabling
# unnecessary services, and reducing fsck/systemd delays.
#
# Usage: sudo bash boot_optimize.sh [--dry-run] [--readonly]
#   --dry-run   Print what would be done without making changes
#   --readonly  Also configure read-only rootfs (more invasive)

set -euo pipefail

DRY_RUN=false
READONLY=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --readonly) READONLY=true ;;
        --help|-h)
            head -10 "$0" | grep '^#' | sed 's/^# //'
            exit 0
            ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Must run as root (use sudo)." >&2
    exit 1
fi

# ---- helpers ----
run() {
    if [[ "$DRY_RUN" == true ]]; then
        echo "[DRY-RUN] $*"
    else
        echo "[RUN] $*"
        "$@"
    fi
}

backup_file() {
    local src="$1"
    if [[ -f "$src" && ! -f "${src}.bak" ]]; then
        run cp "$src" "${src}.bak"
        echo "  backed up to ${src}.bak"
    fi
}

ensure_dir() {
    if [[ ! -d "$1" ]]; then
        run mkdir -p "$1"
    fi
}

# ---- detect boot config path (Bookworm moved to /boot/firmware) ----
if [[ -f /boot/firmware/cmdline.txt ]]; then
    CMDLINE_FILE=/boot/firmware/cmdline.txt
elif [[ -f /boot/cmdline.txt ]]; then
    CMDLINE_FILE=/boot/cmdline.txt
else
    echo "ERROR: cannot find cmdline.txt" >&2
    exit 1
fi
echo "--- Detected boot config: $CMDLINE_FILE ---"

# ===============================================================
# STEP 1 — cmdline.txt flags (fastboot, quiet logging)
# ===============================================================
echo ""
echo "=== STEP 1: cmdline.txt optimizations ==="
backup_file "$CMDLINE_FILE"

for token in fastboot systemd.log_level=emerg quiet; do
    if grep -qw "$token" "$CMDLINE_FILE"; then
        echo "  already present: $token"
    else
        run sed -i "s/$/ $token/" "$CMDLINE_FILE"
        echo "  added: $token"
    fi
done

# ===============================================================
# STEP 2 — Kill network-wait-online (saves 60-120s)
# ===============================================================
echo ""
echo "=== STEP 2: Disable network-wait services ==="
# On Raspberry Pi OS, either systemd-networkd or NetworkManager
# is responsible for wait-online. Mask both unconditionally.
for svc in systemd-networkd-wait-online.service \
           NetworkManager-wait-online.service; do
    if systemctl is-enabled "$svc" &>/dev/null; then
        echo "  masking $svc"
        run systemctl mask "$svc"
    else
        echo "  not enabled: $svc"
    fi
done

# If using dhcpcd, background it so it doesn't block boot
if systemctl is-enabled dhcpcd &>/dev/null; then
    echo "  configuring dhcpcd to background"
    ensure_dir /etc/systemd/system/dhcpcd.service.d
    cat > /tmp/dhcpcd-background.conf << 'EOF'
[Service]
ExecStart=
ExecStart=/usr/sbin/dhcpcd -b -q
EOF
    if [[ ! -f /etc/systemd/system/dhcpcd.service.d/background.conf ]] || \
       ! diff -q /tmp/dhcpcd-background.conf /etc/systemd/system/dhcpcd.service.d/background.conf &>/dev/null; then
        backup_file /etc/systemd/system/dhcpcd.service.d/background.conf 2>/dev/null || true
        run cp /tmp/dhcpcd-background.conf /etc/systemd/system/dhcpcd.service.d/background.conf
    fi
    rm /tmp/dhcpcd-background.conf
    run systemctl daemon-reload
fi

# ===============================================================
# STEP 3 — Disable services not needed on a robot
# Note: avahi-daemon intentionally kept — needed for r2tele.local
# ===============================================================
echo ""
echo "=== STEP 3: Disable unnecessary services ==="
for svc in bluetooth hciuart triggerhappy \
           whoopsie.path whoopsie.service \
           systemd-timesyncd.service; do
    if systemctl is-enabled "$svc" &>/dev/null 2>&1; then
        echo "  disabling and masking $svc"
        run systemctl disable "$svc" 2>/dev/null || true
        run systemctl mask "$svc" 2>/dev/null || true
    else
        echo "  not enabled: $svc"
    fi
done

# ===============================================================
# STEP 4 — Reduce systemd timeouts
# ===============================================================
echo ""
echo "=== STEP 4: Reduce systemd timeouts ==="
ensure_dir /etc/systemd/system.conf.d
cat > /tmp/50-timeouts.conf << 'EOF'
[Manager]
DefaultTimeoutStartSec=15s
DefaultTimeoutStopSec=15s
EOF
if [[ ! -f /etc/systemd/system.conf.d/50-timeouts.conf ]] || \
   ! diff -q /tmp/50-timeouts.conf /etc/systemd/system.conf.d/50-timeouts.conf &>/dev/null; then
    backup_file /etc/systemd/system.conf.d/50-timeouts.conf 2>/dev/null || true
    run cp /tmp/50-timeouts.conf /etc/systemd/system.conf.d/50-timeouts.conf
fi
rm /tmp/50-timeouts.conf

# ===============================================================
# STEP 5 — Volatile journal (tmpfs for logs)
# ===============================================================
echo ""
echo "=== STEP 5: Volatile journal (tmpfs, no SD writes) ==="
ensure_dir /etc/systemd/journald.conf.d
cat > /tmp/50-volatile-journal.conf << 'EOF'
[Journal]
Storage=volatile
RuntimeMaxUse=50M
EOF
if [[ ! -f /etc/systemd/journald.conf.d/50-volatile-journal.conf ]] || \
   ! diff -q /tmp/50-volatile-journal.conf /etc/systemd/journald.conf.d/50-volatile-journal.conf &>/dev/null; then
    backup_file /etc/systemd/journald.conf.d/50-volatile-journal.conf 2>/dev/null || true
    run cp /tmp/50-volatile-journal.conf /etc/systemd/journald.conf.d/50-volatile-journal.conf
fi
rm /tmp/50-volatile-journal.conf

# ===============================================================
# STEP 6 — Reduce fsck frequency (adds fastboot check)
# ===============================================================
echo ""
echo "=== STEP 6: Disable periodic fsck ==="
# fastboot in cmdline.txt already skips fsck, but also tell
# tune2fs to never force one by interval/mount-count
if command -v tune2fs &>/dev/null; then
    ROOT_DEV=$(findmnt -n -o SOURCE /)
    if [[ -n "$ROOT_DEV" && "$ROOT_DEV" =~ ^/dev/ ]]; then
        echo "  setting fsck interval to 0 for $ROOT_DEV"
        run tune2fs -i 0 -c 0 "$ROOT_DEV" 2>/dev/null || \
            echo "  WARNING: tune2fs failed (maybe ext4 not detected)"
    fi
fi

# ===============================================================
# STEP 7 — (optional) Read-only root filesystem with tmpfs overlays
# ===============================================================
if [[ "$READONLY" == true ]]; then
    echo ""
    echo "=== STEP 7: Read-only root filesystem ==="

    # Add ro flag to cmdline.txt if not present
    if grep -qw "ro" "$CMDLINE_FILE"; then
        echo "  ro flag already in cmdline.txt"
    else
        run sed -i "s/$/ ro/" "$CMDLINE_FILE"
        echo "  added ro to cmdline.txt"
    fi

    # Ensure critical directories have tmpfs entries in fstab
    for tmpdir in /tmp /var/log /var/tmp; do
        if grep -q "$tmpdir" /etc/fstab 2>/dev/null; then
            echo "  $tmpdir already in fstab"
        else
            backup_file /etc/fstab
            run sh -c "echo 'tmpfs $tmpdir tmpfs defaults,noatime,nosuid,size=32M 0 0' >> /etc/fstab"
            echo "  added tmpfs for $tmpdir"
        fi
    done

    # Disable swap on SD card (not needed, saves writes)
    if swapon --show=NAME --noheadings 2>/dev/null | grep -q "^/"; then
        run dphys-swapfile swapoff 2>/dev/null || true
        run dphys-swapfile uninstall 2>/dev/null || true
        run systemctl mask dphys-swapfile.service 2>/dev/null || true
        echo "  disabled SD swap"
    fi
fi

# ===============================================================
# Done
# ===============================================================
echo ""
echo "=== DONE ==="
echo "Optimizations applied. Reboot to take effect."
echo ""
echo "Changes made:"
echo "  $CMDLINE_FILE     — added fastboot systemd.log_level=emerg quiet${READONLY:+" ro"}"
echo "  systemd-network-wait services — masked (won't block boot)"
echo "  dhcpcd — background mode (won't block boot)"
echo "  bluetooth, hciuart, triggerhappy — masked"
echo "  systemd timeouts — reduced to 15s"
echo "  journal — volatile (tmpfs, 50M max)"
echo "  fsck — disabled periodic checks"
if [[ "$READONLY" == true ]]; then
    echo "  rootfs — read-only, tmpfs for /tmp /var/log /var/tmp"
    echo "  swap — disabled"
fi
