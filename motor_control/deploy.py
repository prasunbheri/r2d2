#!/usr/bin/env python3
"""Deploy R2 Motor Control stack to Pi via SSH with password."""
import pexpect
import sys
import os

HOST = sys.argv[1] if len(sys.argv) > 1 else 'r2tele@192.168.0.160'
SSH_PASS = 'r2tele'
SUDO_PASS = 'r2tele'
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE_DIR = '/home/r2tele/motor_control'

FILES = [
    'motor_control.py', 'app.py', 'watchdog.py',
    'motor_control.service', 'watchdog.service',
    'deploy.sh',
    ('templates/index.html', 'templates/'),
    ('tests/__init__.py', 'tests/'),
    ('tests/mock_pigpio.py', 'tests/'),
    ('tests/test_motor_control.py', 'tests/'),
    ('tests/test_app.py', 'tests/'),
    ('tests/test_watchdog.py', 'tests/'),
    ('static/socket.io.min.js', 'static/'),
]

SSH_OPTS = '-o StrictHostKeyChecking=no -o PreferredAuthentications=password'


def ssh(cmd, timeout=30):
    child = pexpect.spawn(f'ssh {SSH_OPTS} {HOST} bash -c {repr(cmd)}')
    child.expect('password:', timeout=10)
    child.sendline(SSH_PASS)
    child.expect(pexpect.EOF, timeout=timeout)
    return child.before.decode()


def sudo(cmd, timeout=30):
    full_cmd = f'echo {SUDO_PASS} | sudo -S sh -c {repr(cmd)}'
    return ssh(full_cmd, timeout)


def scp_upload(local, remote):
    child = pexpect.spawn(f'scp {SSH_OPTS} {local} {HOST}:{remote}')
    child.expect('password:', timeout=15)
    child.sendline(SSH_PASS)
    child.expect(pexpect.EOF, timeout=30)


try:
    print(f'Connecting to {HOST} ...')
    ssh('echo OK', timeout=10)
    print('Connected.')

    print('Creating remote directories...')
    ssh(f'mkdir -p {REMOTE_DIR}/templates {REMOTE_DIR}/tests {REMOTE_DIR}/static')

    for entry in FILES:
        if isinstance(entry, tuple):
            local_rel, dest_subdir = entry
        else:
            local_rel, dest_subdir = entry, ''
        local_path = os.path.join(LOCAL_DIR, local_rel)
        remote_path = f'{REMOTE_DIR}/{dest_subdir}{os.path.basename(local_rel)}'
        print(f'  Uploading {local_rel} ...')
        scp_upload(local_path, remote_path)

    print('Installing system packages...')
    out = sudo('apt update && apt install -y pigpio python3-pip', timeout=180)
    lines = [l for l in out.split('\n') if l.strip()]
    print('\n'.join(lines[-5:]))

    print('Installing Python packages...')
    out = ssh('pip3 install flask flask-socketio waitress', timeout=60)
    print('\n'.join(out.split('\n')[-3:]))

    print('Enabling pigpiod...')
    sudo('systemctl enable pigpiod && systemctl start pigpiod')

    print('Installing systemd services...')
    cmds = (
        f'cp {REMOTE_DIR}/motor_control.service /etc/systemd/system/ && '
        f'cp {REMOTE_DIR}/watchdog.service /etc/systemd/system/ && '
        f'systemctl daemon-reload && '
        f'systemctl enable motor_control.service watchdog.service'
    )
    sudo(cmds)

    print('Running tests...')
    out = ssh(f'cd {REMOTE_DIR} && python3 -m pytest tests/ -v', timeout=60)
    print(out)

    print('\n=== DEPLOY COMPLETE ===')
    print('Reboot or start services manually:')
    print(f'  ssh {HOST} "echo {SUDO_PASS} | sudo -S systemctl start motor_control.service watchdog.service"')

except Exception as e:
    print(f'Deploy failed: {e}', file=sys.stderr)
    import traceback
    traceback.print_exc()
    sys.exit(1)
