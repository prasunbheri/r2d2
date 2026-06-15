#!/bin/bash
# Deploy R2 Motor Control stack to Raspberry Pi Zero W
# Usage: ./deploy.sh [pi-host]

PI_HOST="${1:-r2tele@192.168.0.160}"
PI_DIR="/home/r2tele/motor_control"

echo "Deploying to $PI_HOST:$PI_DIR ..."

# Create remote directory
ssh "$PI_HOST" "mkdir -p $PI_DIR/templates $PI_DIR/tests"

# Copy application files
scp motor_control.py app.py watchdog.py "$PI_HOST:$PI_DIR/"
scp templates/index.html "$PI_HOST:$PI_DIR/templates/"
scp motor_control.service watchdog.service "$PI_HOST:$PI_DIR/"

# Copy tests
scp tests/__init__.py tests/mock_pigpio.py tests/test_motor_control.py tests/test_app.py tests/test_watchdog.py "$PI_HOST:$PI_DIR/tests/"

# Install system dependencies
ssh "$PI_HOST" "sudo apt update && sudo apt install -y pigpio python3-pip"
ssh "$PI_HOST" "sudo systemctl enable pigpiod && sudo systemctl start pigpiod"
ssh "$PI_HOST" "pip3 install flask flask-socketio waitress"

# Install systemd services
ssh "$PI_HOST" "sudo cp $PI_DIR/motor_control.service /etc/systemd/system/"
ssh "$PI_HOST" "sudo cp $PI_DIR/watchdog.service /etc/systemd/system/"
ssh "$PI_HOST" "sudo systemctl daemon-reload"
ssh "$PI_HOST" "sudo systemctl enable motor_control.service watchdog.service"

# Run tests
ssh "$PI_HOST" "cd $PI_DIR && python3 -m pytest tests/ -v"

echo ""
echo "Deploy complete. Reboot the Pi or run:"
echo "  ssh $PI_HOST 'sudo systemctl start motor_control.service watchdog.service'"
