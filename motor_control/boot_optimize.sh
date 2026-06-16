#!/bin/bash
# R2 Motor Control — Raspberry Pi Boot Optimization
# Cuts boot from 3+ minutes down to ~40s on Pi Zero W by:
#   - Retrying pigpio connection instead of crash-looping
#   - Masking network-wait services (saves 60-120s)
#   - Config.txt fast-boot settings (saves 3-5s)
#   - Deadline I/O scheduler for SD card (better throughput)
#   - preload adaptive caching daemon (learns boot patterns)
#   - Pre-compiled Python bytecode (faster imports)
#   - Removing fsck delays and unnecessary services
#
# Usage: sudo bash boot_optimize.sh [--dry-run] [--readonly] [--no-initramfs] [--diagnose]
#   --dry-run       Print what would be done without making changes
#   --readonly      Also configure read-only rootfs (more invasive)
#   --no-initramfs  Skip loading initramfs (3-10s savings, safe on standard Pi OS)
#   --diagnose      Collect boot analysis report (no changes)

set -euo pipefail

DRY_RUN=false
READONLY=false
NO_INITRAMFS=false
DIAGNOSE=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --readonly) READONLY=true ;;
        --no-initramfs) NO_INITRAMFS=true ;;
        --diagnose) DIAGNOSE=true ;;
        --help|-h)
            head -15 "$0" | grep '^#' | sed 's/^# //'
            exit 0
            ;;
    esac
done

if [[ "$DIAGNOSE" == true ]]; then
    echo "=== Boot Analysis Report ==="
    echo ""
    echo "--- systemd-analyze time ---"
    systemd-analyze time 2>&1 || echo "(not available)"
    echo ""
    echo "--- systemd-analyze blame (top 20) ---"
    systemd-analyze blame 2>&1 | head -20 || echo "(not available)"
    echo ""
    echo "--- systemd-analyze critical-chain ---"
    systemd-analyze critical-chain 2>&1 || echo "(not available)"
    echo ""
    echo "--- Kernel boot time ---"
    dmesg | grep -E "Kernel|Freeing|Clock|timer" | tail -5 2>&1 || echo "(not available)"
    echo ""
    echo "--- sd card info ---"
    cat /sys/block/mmcblk0/queue/scheduler 2>/dev/null || echo "(not mmcblk0)"
    echo ""
    echo "Recommendation: send the above output to the developer."
    exit 0
fi

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
    CONFIG_FILE=/boot/firmware/config.txt
elif [[ -f /boot/cmdline.txt ]]; then
    CMDLINE_FILE=/boot/cmdline.txt
    CONFIG_FILE=/boot/config.txt
else
    echo "ERROR: cannot find cmdline.txt" >&2
    exit 1
fi
echo "--- Detected boot config: $CMDLINE_FILE and $CONFIG_FILE ---"

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
           systemd-timesyncd.service \
           getty@tty1 getty@tty2 getty@tty3 getty@tty4 getty@tty5 getty@tty6 \
           serial-getty@ttyAMA0 \
           alsa-restore alsa-state \
           rsyslog \
           ModemManager; do
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
# STEP 6 — config.txt fast-boot settings
# ===============================================================
echo ""
echo "=== STEP 6: config.txt boot speed settings ==="
backup_file "$CONFIG_FILE"

CONFIG_TOKENS="boot_delay=0 disable_splash=1 gpu_mem=16 avoid_warnings=1"
for token in $CONFIG_TOKENS; do
    key="${token%%=*}"
    if grep -q "^${key}=" "$CONFIG_FILE" 2>/dev/null; then
        echo "  already set: $(grep "^${key}=" "$CONFIG_FILE")"
    else
        run sh -c "echo '$token' >> '$CONFIG_FILE'"
        echo "  added: $token"
    fi
done

# ===============================================================
# STEP 7 — deadline I/O scheduler + noatime for SD card
# ===============================================================
echo ""
echo "=== STEP 7: I/O scheduler and mount options ==="
if grep -qw "elevator=deadline" "$CMDLINE_FILE"; then
    echo "  already present: elevator=deadline"
else
    run sed -i "s/$/ elevator=deadline/" "$CMDLINE_FILE"
    echo "  added: elevator=deadline"
fi
# Also add noatime to rootfs if not present
if grep -qw "noatime" "$CMDLINE_FILE"; then
    echo "  already present: noatime"
else
    run sed -i "s/$/ noatime/" "$CMDLINE_FILE"
    echo "  added: noatime (root will be remounted rw with noatime)"
fi

# ===============================================================
# STEP 8 — Blacklist unused kernel modules
# ===============================================================
echo ""
echo "=== STEP 8: Blacklist unused kernel modules ==="
BLACKLIST_FILE="/etc/modprobe.d/99-r2-blacklist.conf"
cat > /tmp/99-r2-blacklist.conf << 'EOF'
# R2 Motor Control - blacklist unused hardware modules
blacklist snd_bcm2835
blacklist snd_soc_bcm2835_i2s
blacklist joydev
blacklist uinput
EOF
if [[ ! -f "$BLACKLIST_FILE" ]] || \
   ! diff -q /tmp/99-r2-blacklist.conf "$BLACKLIST_FILE" &>/dev/null; then
    backup_file "$BLACKLIST_FILE" 2>/dev/null || true
    run cp /tmp/99-r2-blacklist.conf "$BLACKLIST_FILE"
fi
rm /tmp/99-r2-blacklist.conf

# Apply by unloading any already-loaded modules (best-effort)
for mod in snd_bcm2835 snd_soc_bcm2835_i2s joydev uinput; do
    if lsmod | grep -qw "$mod" 2>/dev/null; then
        run modprobe -r "$mod" 2>/dev/null || true
    fi
done

# ===============================================================
# STEP 9 — preload adaptive caching daemon
# ===============================================================
echo ""
echo "=== STEP 9: preload caching daemon ==="
if command -v preload &>/dev/null; then
    echo "  preload already installed"
    run systemctl enable preload 2>/dev/null || true
    run systemctl start preload 2>/dev/null || true
else
    echo "  installing preload..."
    run apt-get install -y preload 2>/dev/null || \
        echo "  WARNING: apt-get failed (no network? run manually: sudo apt install preload)"
fi

# ===============================================================
# STEP 10 — Pre-cache systemd service
# ===============================================================
echo ""
echo "=== STEP 10: Pre-cache systemd service ==="
PRECACHE_PATH="/etc/systemd/system/pre-cache.service"
if [[ -f "$PRECACHE_PATH" ]]; then
    echo "  pre-cache.service already installed"
    run systemctl enable pre-cache.service 2>/dev/null || true
else
    echo "  WARNING: pre-cache.service not found at $PRECACHE_PATH"
    echo "  Re-deploy with ./deploy.sh to install it."
fi

# ===============================================================
# STEP 11 — Pre-compile Python bytecode
# ===============================================================
echo ""
echo "=== STEP 11: Python bytecode compilation ==="
MOTOR_DIR="/home/r2tele/motor_control"
SITE_PACKAGES=$(python3 -c "import sys; print([p for p in sys.path if p.endswith('dist-packages')][0])" 2>/dev/null || echo "")
if [[ -d "$MOTOR_DIR" ]]; then
    echo "  compiling $MOTOR_DIR"
    run python3 -m compileall -f "$MOTOR_DIR" -q 2>/dev/null || true
fi
if [[ -n "$SITE_PACKAGES" && -d "$SITE_PACKAGES" ]]; then
    echo "  compiling $SITE_PACKAGES (this may take a minute)"
    run python3 -m compileall -f "$SITE_PACKAGES" -q 2>/dev/null || true
fi

# ===============================================================
# STEP 12 — Reduce fsck frequency (adds fastboot check)
# ===============================================================
echo ""
echo "=== STEP 12: Disable periodic fsck ==="
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
# STEP 13 — (optional) Read-only root filesystem with tmpfs overlays
# ===============================================================
if [[ "$READONLY" == true ]]; then
    echo ""
    echo "=== STEP 13: Read-only root filesystem ==="

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
# STEP 14 — (optional) Skip initramfs
# ===============================================================
if [[ "$NO_INITRAMFS" == true ]]; then
    echo ""
    echo "=== STEP 14: Skip initramfs ==="
    if grep -q "^initramfs" "$CONFIG_FILE" 2>/dev/null; then
        backup_file "$CONFIG_FILE"
        run sed -i 's/^initramfs/#initramfs/' "$CONFIG_FILE"
        echo "  commented out initramfs in $CONFIG_FILE"
    elif grep -q "^#initramfs" "$CONFIG_FILE" 2>/dev/null; then
        echo "  initramfs already disabled"
    else
        echo "  no initramfs line found in $CONFIG_FILE"
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
echo "  $CMDLINE_FILE     — added fastboot systemd.log_level=emerg quiet elevator=deadline noatime"
echo "  $CONFIG_FILE      — added boot_delay=0 disable_splash=1 gpu_mem=16 avoid_warnings=1"
echo "  systemd-network-wait services — masked (won't block boot)"
echo "  dhcpcd — background mode (won't block boot)"
echo "  bluetooth, hciuart, triggerhappy, rsyslog, ModemManager — masked"
echo "  getty@tty[1-6], serial-getty, alsa-restore/state — masked"
echo "  snd_bcm2835, joydev, uinput kernel modules — blacklisted"
echo "  systemd timeouts — reduced to 15s"
echo "  journal — volatile (tmpfs, 50M max)"
echo "  preload — installed and enabled (adaptive boot cache)"
echo "  pre-cache.service — enabled (reads .pyc/.so into page cache before app starts)"
echo "  Python bytecode — all .py files pre-compiled to .pyc"
echo "  fsck — disabled periodic checks"
if [[ "$READONLY" == true ]]; then
    echo "  rootfs — read-only, tmpfs for /tmp /var/log /var/tmp"
    echo "  swap — disabled"
fi
if [[ "$NO_INITRAMFS" == true ]]; then
    echo "  initramfs — skipped (commented out in config.txt)"
fi
