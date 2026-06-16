#!/bin/bash
# Deploy R2 Motor Control stack to Raspberry Pi Zero W
# Usage: ./deploy.sh [pi-host]
#   Set SKIP_OPTIMIZE=true to skip boot optimization

set -euo pipefail

PI_HOST="${1:-r2tele@r2tele.local}"
PI_DIR="/home/r2tele/motor_control"

echo "Deploying to $PI_HOST:$PI_DIR ..."

# Create remote directory
ssh "$PI_HOST" "mkdir -p $PI_DIR/templates $PI_DIR/tests $PI_DIR/static"

# Copy application files
scp motor_control.py app.py watchdog.py requirements.txt motor_control.sudoers boot_optimize.sh "$PI_HOST:$PI_DIR/"
scp templates/index.html "$PI_HOST:$PI_DIR/templates/"
scp static/socket.io.min.js "$PI_HOST:$PI_DIR/static/"
scp motor_control.service watchdog.service pre-cache.service "$PI_HOST:$PI_DIR/"

# Copy tests
scp tests/__init__.py tests/mock_pigpio.py tests/test_motor_control.py tests/test_app.py tests/test_watchdog.py "$PI_HOST:$PI_DIR/tests/"

# Install system dependencies
ssh "$PI_HOST" "sudo apt update && sudo apt install -y pigpio python3-pip"
ssh "$PI_HOST" "sudo systemctl enable pigpiod && sudo systemctl start pigpiod"
ssh "$PI_HOST" "cd $PI_DIR && pip3 install --break-system-packages -r requirements.txt"

# Install sudoers rule for passwordless shutdown
ssh "$PI_HOST" "sudo cp $PI_DIR/motor_control.sudoers /etc/sudoers.d/motor_control"
ssh "$PI_HOST" "sudo chmod 440 /etc/sudoers.d/motor_control"

# Install systemd services
ssh "$PI_HOST" "sudo cp $PI_DIR/motor_control.service /etc/systemd/system/"
ssh "$PI_HOST" "sudo cp $PI_DIR/watchdog.service /etc/systemd/system/"
ssh "$PI_HOST" "sudo cp $PI_DIR/pre-cache.service /etc/systemd/system/"
ssh "$PI_HOST" "sudo systemctl daemon-reload"
ssh "$PI_HOST" "sudo systemctl enable pre-cache.service motor_control.service watchdog.service"

# Apply boot optimizations (skip with SKIP_OPTIMIZE=true)
if [[ "${SKIP_OPTIMIZE:-false}" != "true" ]]; then
    echo ""
    echo "Applying boot optimizations..."
    ssh "$PI_HOST" "sudo bash $PI_DIR/boot_optimize.sh"
else
    echo "Skipping boot optimizations (SKIP_OPTIMIZE=true)"
fi

# Run tests
ssh "$PI_HOST" "cd $PI_DIR && python3 -m pytest tests/ -v"

# Pre-compile Python bytecode for faster startup
echo ""
echo "Pre-compiling Python bytecode..."
ssh "$PI_HOST" "python3 -m compileall -f $PI_DIR/ -q 2>/dev/null" || true

echo ""
echo "Deploy complete. Reboot the Pi or run:"
echo "  ssh $PI_HOST 'sudo systemctl start motor_control.service watchdog.service'"
