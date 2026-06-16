"""
WiFi management for R2 Motor Control.
All operations use nmcli (NetworkManager CLI) via sudo.
"""
import subprocess
import json
import os
import time
import socket
import re
import logging
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
KNOWN_CREDS_FILE = os.path.join(CONFIG_DIR, 'wifi_known.json')
MAX_KNOWN = 5
AP_SSID = 'r2tele'
AP_PASSWORD = 'r2tele'
VERIFY_HOSTS = ['8.8.8.8', '1.1.1.1']
CONNECT_TIMEOUT = 25

_hotspot_process = None


def _nmcli(args: list, timeout: int = 15) -> subprocess.CompletedProcess:
    if len(args) >= 2 and args[0] == 'connection' and args[1] == 'show':
        full_args = ['nmcli'] + args
    else:
        full_args = ['sudo', 'nmcli'] + args
    try:
        return subprocess.run(full_args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning('nmcli %s timed out', args)
        return subprocess.CompletedProcess(full_args, -1, '', '')
    except FileNotFoundError:
        logger.error('nmcli not found')
        return subprocess.CompletedProcess(full_args, -1, '', 'nmcli not found')


def _clean_stderr(stderr: str) -> str:
    """Filter known-harmless nmcli warnings from stderr."""
    lines = [l for l in stderr.strip().split('\n') if l.strip()]
    harmless = [
        'key-mgmt',
        'Warning: ',
    ]
    filtered = [l for l in lines if not any(h in l for h in harmless)]
    return '\n'.join(filtered) if filtered else ''


def scan() -> List[Dict]:
    result = _nmcli(['-t', '-f', 'SSID,SIGNAL,SECURITY', 'device', 'wifi', 'list'], timeout=20)
    networks = []
    for line in result.stdout.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split(':')
        if len(parts) >= 3 and parts[0]:
            networks.append({
                'ssid': parts[0],
                'signal': int(parts[1]) if parts[1].isdigit() else 0,
                'security': parts[2],
            })
    return networks


def current_ssid() -> Optional[str]:
    result = _nmcli(['-t', 'connection', 'show', '--active'], timeout=8)
    for line in result.stdout.strip().split('\n'):
        parts = line.split(':')
        if len(parts) >= 4 and parts[2] == '802-11-wireless':
            conn_name = parts[0]
            r = _nmcli(['connection', 'show', conn_name], timeout=8)
            for cline in r.stdout.split('\n'):
                if cline.strip().startswith('802-11-wireless.ssid:'):
                    ssid = cline.split(':', 1)[1].strip()
                    return ssid if ssid else None
    return None


def current_signal() -> Optional[int]:
    ssid = current_ssid()
    if not ssid:
        return None
    for net in scan():
        if net['ssid'] == ssid:
            return net['signal']
    return None


def current_ip() -> Optional[str]:
    try:
        r = subprocess.run(['ip', '-4', 'addr', 'show', 'wlan0'],
                           capture_output=True, text=True, timeout=5)
        for line in r.stdout.split('\n'):
            m = re.search(r'inet\s+(\d+\.\d+\.\d+\.\d+)', line)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def _connection_name_for_ssid(ssid: str) -> Optional[str]:
    """Find NM connection profile name for a given SSID."""
    result = _nmcli(['-t', 'connection', 'show'], timeout=8)
    for line in result.stdout.strip().split('\n'):
        parts = line.split(':')
        if len(parts) >= 4 and parts[2] == '802-11-wireless':
            conn_name = parts[0]
            r = _nmcli(['connection', 'show', conn_name], timeout=8)
            for cline in r.stdout.split('\n'):
                if cline.strip().startswith('802-11-wireless.ssid:') and cline.split(':', 1)[1].strip() == ssid:
                    return conn_name
    return None


def _verify_gateway() -> bool:
    """Verify WiFi works by pinging the default gateway (router)."""
    try:
        r = subprocess.run(['ip', 'route', 'show', 'default'],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r'via\s+(\d+\.\d+\.\d+\.\d+)', r.stdout)
        if m:
            gateway = m.group(1)
            r = subprocess.run(['ping', '-c', '1', '-W', '5', gateway],
                               capture_output=True, timeout=8)
            if r.returncode == 0:
                return True
        for host in VERIFY_HOSTS:
            r = subprocess.run(['ping', '-c', '1', '-W', '3', host],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                return True
    except Exception:
        pass
    return False


def save_credentials(ssid: str, password: str):
    known = load_credentials()
    entry = {'ssid': ssid, 'password': password, 'last_verified': time.time()}
    known = [e for e in known if e['ssid'] != ssid]
    known.insert(0, entry)
    known = known[:MAX_KNOWN]
    with open(KNOWN_CREDS_FILE, 'w') as f:
        json.dump(known, f)
    logger.info('Saved credentials for %s (%d known)', ssid, len(known))


def load_credentials() -> List[Dict]:
    try:
        if os.path.exists(KNOWN_CREDS_FILE):
            with open(KNOWN_CREDS_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return [data]
            if isinstance(data, list):
                return data
    except Exception as e:
        logger.warning('Failed to load credentials: %s', e)
    return []


def try_connect(ssid: str, password: str, verify: bool = True) -> Dict:
    if not ssid or not ssid.strip():
        return {'ok': False, 'error': 'SSID is required'}
    ssid = ssid.strip()
    if not password or password.strip() == '':
        return {'ok': False, 'error': 'Password is required'}
    if len(password) < 8:
        return {'ok': False, 'error': 'Password must be at least 8 characters'}

    prev_ssid = current_ssid()
    prev_conn_name = _connection_name_for_ssid(prev_ssid) if prev_ssid else None
    known = load_credentials()

    logger.info('Connecting to %s (current: %s)', ssid, prev_ssid or 'none')

    connect_result = _nmcli([
        'device', 'wifi', 'connect', ssid,
        'password', password
    ], timeout=CONNECT_TIMEOUT)

    time.sleep(3)

    connected_ssid = current_ssid()
    if connected_ssid == ssid:
        if verify:
            logger.info('Verifying connectivity via %s...', ssid)
            if _verify_gateway():
                logger.info('Verified connectivity on %s', ssid)
                save_credentials(ssid, password)
                return {'ok': True, 'ssid': ssid, 'ip': current_ip() or ''}
            else:
                logger.warning('Gateway check failed on %s, reverting', ssid)
                return _revert_or_ap(prev_conn_name, known, ssid,
                                     'Gateway not reachable (wrong password or no internet)')
        save_credentials(ssid, password)
        return {'ok': True, 'ssid': ssid, 'ip': current_ip() or ''}

    err = _clean_stderr(connect_result.stderr) or 'Connection did not establish'
    logger.warning('Did not connect to %s (currently on %s): %s', ssid, connected_ssid or 'none', err)
    return _revert_or_ap(prev_conn_name, known, None, err)


def _revert_or_ap(prev_conn: Optional[str], known: List[Dict],
                  revert_ssid: Optional[str], error: str) -> Dict:
    """Try reverting via NM profile, then known credentials, fall back to AP."""
    if prev_conn:
        logger.info('Reverting to connection %s...', prev_conn)
        r = _nmcli(['connection', 'up', prev_conn], timeout=30)
        if r.returncode == 0:
            time.sleep(3)
            logger.info('Reverted to %s', prev_conn)
            return {'ok': False, 'fallback': 'previous', 'ssid': current_ssid() or '',
                    'error': error}

    for cred in known:
        ssid = cred.get('ssid')
        password = cred.get('password')
        if not ssid or not password:
            continue
        if ssid == revert_ssid:
            continue
        logger.info('Trying known SSID %s...', ssid)
        r = _nmcli(['device', 'wifi', 'connect', ssid, 'password', password], timeout=30)
        if r.returncode == 0:
            time.sleep(3)
            if current_ssid() == ssid and _verify_gateway():
                logger.info('Reverted to known SSID %s', ssid)
                return {'ok': False, 'fallback': 'known', 'ssid': ssid, 'error': error}

    ap = start_ap()
    if ap:
        return {'ok': False, 'fallback': 'ap', 'error': error, **ap}

    return {'ok': False, 'error': f'{error}. Also failed to start AP mode.'}


def start_ap() -> Optional[Dict]:
    global _hotspot_process
    stop_ap()

    pwd = AP_PASSWORD
    ssid = AP_SSID

    try:
        _hotspot_process = subprocess.Popen([
            'sudo', 'nmcli', 'device', 'wifi', 'hotspot',
            'ifname', 'wlan0', 'ssid', ssid, 'password', pwd
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        time.sleep(5)
        ip = current_ip()

        logger.info('AP mode: SSID=%s IP=%s', ssid, ip or 'unknown')
        return {'ap_ssid': ssid, 'ap_password': pwd, 'ap_ip': ip or '10.42.0.1'}
    except Exception as e:
        logger.error('AP start failed: %s', e)
        return None


def stop_ap():
    global _hotspot_process
    if _hotspot_process:
        try:
            _hotspot_process.terminate()
            _hotspot_process.wait(timeout=5)
        except Exception:
            try:
                _hotspot_process.kill()
            except Exception:
                pass
        _hotspot_process = None
    for cmd in [
        ['sudo', 'nmcli', 'connection', 'down', 'Hotspot'],
        ['sudo', 'nmcli', 'connection', 'delete', 'Hotspot'],
    ]:
        try:
            subprocess.run(cmd, capture_output=True, timeout=10)
        except Exception:
            pass


def current_mode() -> str:
    """Return 'station', 'ap', or 'none'."""
    if _hotspot_process and _hotspot_process.poll() is None:
        return 'ap'
    ssid = current_ssid()
    return 'station' if ssid else 'none'
