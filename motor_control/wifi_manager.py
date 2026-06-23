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
import threading
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
KNOWN_CREDS_FILE = os.path.join(CONFIG_DIR, 'wifi_known.json')
MAX_KNOWN = 5
AP_SSID = 'r2tele'
AP_PASSWORD = 'r2tele'
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
    """Filter known-harmless nmcli warnings from stderr, keep real errors."""
    lines = [l for l in stderr.strip().split('\n') if l.strip()]
    filtered = [l for l in lines if not l.strip().startswith('Warning:')]
    return '\n'.join(filtered) if filtered else ''


def _describe_nmcli_failure(result: subprocess.CompletedProcess, ssid: str) -> str:
    """Return a human-readable error message from a failed nmcli connect."""
    stderr = _clean_stderr(result.stderr)
    stdout = (result.stdout or '').strip()
    combined = (stderr + ' ' + stdout).lower()

    if result.returncode == -1:
        return 'Connection timed out (network may be out of range)'
    if 'no network with ssid' in combined:
        return f'Network "{ssid}" not found (out of range or hidden)'
    if 'secrets were required' in combined:
        return 'Wrong password or authentication rejected'
    if 'access point does not have the expected strength' in combined:
        return 'Wrong password or connection rejected by access point'
    if 'invalid' in combined and 'password' in combined:
        return 'Invalid password format'
    if 'key-mgmt' in combined or 'property is missing' in combined:
        return 'Could not detect network security type (try scanning again or check SSID)'
    if 'could not find a compatible' in combined:
        return 'Network authentication type not supported'
    if 'not found' in combined:
        return f'Network "{ssid}" not found'
    if stderr:
        return stderr
    return 'Connection did not establish (check password or signal strength)'


def scan() -> List[Dict]:
    result = _nmcli(['-t', '-f', 'SSID,SIGNAL,SECURITY', 'device', 'wifi', 'list'], timeout=20)
    networks = []
    for line in result.stdout.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split(':', 2)
        if len(parts) >= 3 and parts[0]:
            networks.append({
                'ssid': parts[0],
                'signal': int(parts[1]) if parts[1].isdigit() else 0,
                'security': parts[2],
            })
    _ssids = [n['ssid'] for n in networks]
    logger.info('Scan found %d networks: %s', len(networks), _ssids)
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
    """Verify the default gateway (router) is reachable. Does NOT require internet."""
    try:
        r = subprocess.run(['ip', 'route', 'show', 'default'],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r'via\s+(\d+\.\d+\.\d+\.\d+)', r.stdout)
        if m:
            gateway = m.group(1)
            r = subprocess.run(['ping', '-c', '1', '-W', '5', gateway],
                               capture_output=True, timeout=8)
            return r.returncode == 0
    except Exception:
        pass
    return False


def _cleanup_connection_profile(ssid: str):
    """Delete any existing NM connection profile for the SSID to avoid stale config."""
    result = _nmcli(['-t', 'connection', 'show'], timeout=8)
    for line in result.stdout.strip().split('\n'):
        parts = line.split(':')
        if len(parts) >= 4 and parts[2] == '802-11-wireless':
            conn_name = parts[0]
            r = _nmcli(['connection', 'show', conn_name], timeout=8)
            for cline in r.stdout.split('\n'):
                if cline.strip().startswith('802-11-wireless.ssid:') and \
                   cline.split(':', 1)[1].strip() == ssid:
                    logger.info('Deleting stale connection profile %s for SSID %s', conn_name, ssid)
                    subprocess.run(['sudo', 'nmcli', 'connection', 'delete', conn_name],
                                   capture_output=True, timeout=10)
                    return


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
    if not password or (password or '').strip() == '':
        return {'ok': False, 'error': 'Password is required'}
    if len(password) < 8:
        return {'ok': False, 'error': 'Password must be at least 8 characters'}

    prev_ssid = current_ssid()
    prev_conn_name = _connection_name_for_ssid(prev_ssid) if prev_ssid else None
    known = load_credentials()

    logger.info('Connecting to %s (current: %s)', ssid, prev_ssid or 'none')

    _cleanup_connection_profile(ssid)
    stop_ap()

    # Pre-scan so nmcli has the SSID in its cache before connecting
    logger.info('Pre-scanning for %s...', ssid)
    scan()
    time.sleep(1)

    connect_result = _nmcli([
        'device', 'wifi', 'connect', ssid,
        'password', password,
        'ifname', 'wlan0',
    ], timeout=CONNECT_TIMEOUT)

    logger.info('nmcli connect rc=%d stderr=%s stdout=%s',
                connect_result.returncode,
                connect_result.stderr.strip() or '(none)',
                connect_result.stdout.strip() or '(none)')

    # Poll for SSID to become active (DHCP may take time)
    connected_ssid = None
    for attempt in range(10):
        time.sleep(1.5)
        connected_ssid = current_ssid()
        if connected_ssid == ssid:
            logger.info('SSID %s confirmed active after %ds', ssid, (attempt + 1) * 1.5)
            break
    else:
        logger.warning('SSID %s never became active after connect (last seen: %s)',
                       ssid, connected_ssid or 'none')

    if connected_ssid != ssid:
        err = _describe_nmcli_failure(connect_result, ssid)
        logger.warning('Did not connect to %s (currently on %s): %s',
                       ssid, connected_ssid or 'none', err)
        return _revert_or_ap(prev_conn_name, known, None, err)

    # SSID connected successfully
    if verify and prev_ssid and prev_ssid != ssid:
        logger.info('Verifying gateway on new network %s...', ssid)
        if _verify_gateway():
            logger.info('Gateway reachable on %s', ssid)
        else:
            logger.warning('Gateway not reachable on %s, but connection accepted', ssid)

    save_credentials(ssid, password)
    ip = current_ip() or ''
    return {'ok': True, 'ssid': ssid, 'ip': ip}


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
    with _hotspot_lock:
        stop_ap()

        pwd = AP_PASSWORD
        ssid = AP_SSID

        try:
            proc = subprocess.Popen([
                'sudo', 'nmcli', 'device', 'wifi', 'hotspot',
                'ifname', 'wlan0', 'ssid', ssid, 'password', pwd
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            time.sleep(5)
            if proc.poll() is not None and proc.returncode != 0:
                logger.error('AP start failed: nmcli exited with code %d', proc.returncode)
                _hotspot_process = None
                return None

            _hotspot_process = proc
            ip = current_ip()

            logger.info('AP mode: SSID=%s IP=%s', ssid, ip or 'unknown')
            return {'ap_ssid': ssid, 'ap_password': pwd, 'ap_ip': ip or '10.42.0.1'}
        except Exception as e:
            _hotspot_process = None
            logger.error('AP start failed: %s', e)
            return None


def stop_ap():
    global _hotspot_process
    with _hotspot_lock:
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
    with _hotspot_lock:
        hp = _hotspot_process
    if hp and hp.poll() is None:
        return 'ap'
    ssid = current_ssid()
    return 'station' if ssid else 'none'
