import atexit
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, render_template, Response, request, stream_with_context
from flask_socketio import SocketIO, emit
from waitress import serve

from motor_control import MotorController, MOTOR_NAMES
import wifi_manager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
    stream=sys.stdout,
)
logger = logging.getLogger('r2')

def log_unhandled(exc_type: type, exc_value: BaseException, exc_tb: object) -> None:
    logger.error('Unhandled exception', exc_info=(exc_type, exc_value, exc_tb))
sys.excepthook = log_unhandled
threading.excepthook = lambda args: logger.error(
    'Thread exception', exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24).hex()
sio = SocketIO(app, cors_allowed_origins='*', async_mode='threading', transports=['polling'])

controller: Optional[MotorController] = None

camera: Any = None
camera_lock: threading.Lock = threading.Lock()
fps_lock: threading.Lock = threading.Lock()
camera_available: bool = False
latest_frame: Optional[bytes] = None
latest_frame_time: float = 0.0
camera_fps: float = 0.0
_last_frame_time: float = 0.0
_frame_cond: threading.Condition = threading.Condition()

RESOLUTIONS: List[Tuple[int, int]] = [(320, 240), (640, 480), (800, 600), (1024, 768), (1280, 960)]
current_resolution: int = 1  # index into RESOLUTIONS
target_fps: Any = 5  # desired framerate; None = uncapped
joystick_speed: int = 70  # 0-100
max_speed_limiter: int = 50  # 0-100
_fps_controls: Optional[Tuple[int, int]] = None  # (min_dur, max_dur) last applied via set_controls

_active_streams: int = 0
_streams_lock: threading.Lock = threading.Lock()
MAX_STREAMS: int = 3

_video_owner_sid: Optional[str] = None
_video_owner_lock: threading.Lock = threading.Lock()

_camera_retry_in_flight: bool = False  # guard against unbounded retry threads

_mem_cache: Dict[str, str] = {}  # cached /proc/meminfo dict
_mem_cache_time: float = 0.0
_mem_cache_lock: threading.Lock = threading.Lock()

_template_size: int = 0  # cached size of templates/index.html

_THROTTLED_FLAGS = [
    (0x1, 'Under-voltage'),
    (0x2, 'Freq Capped'),
    (0x4, 'Throttled'),
    (0x8, 'Soft Temp Limit'),
]


def _parse_throttled() -> dict:
    try:
        out = subprocess.run(['vcgencmd', 'get_throttled'], capture_output=True, text=True, timeout=2).stdout.strip()
        val = int(out.split('=')[1], 0)
    except Exception:
        return {'raw': 0, 'flags': []}
    active = []
    for bit, label in _THROTTLED_FLAGS:
        if val & bit:
            active.append(label)
    return {'raw': hex(val), 'flags': active}


def get_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def _make_camera_output():
    """Create an Output that stores the latest HW-encoded MJPEG frame."""
    from picamera2.outputs import Output

    class _CircularOutput(Output):
        def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):
            global latest_frame, latest_frame_time, camera_fps, _last_frame_time
            latest_frame = frame
            latest_frame_time = time.time()
            dt = latest_frame_time - _last_frame_time
            if dt >= 0.01:
                camera_fps = 1.0 / dt
            _last_frame_time = latest_frame_time
            with _frame_cond:
                _frame_cond.notify_all()

    return _CircularOutput()


def _start_recording():
    """Configure camera and start HW MJPEG recording via V4L2 encoder."""
    from picamera2.encoders import MJPEGEncoder, Quality
    encoder = MJPEGEncoder()
    output = _make_camera_output()
    camera.start_recording(encoder, output, quality=Quality.HIGH)


def _apply_framerate():
    global target_fps, _fps_controls
    if target_fps == 0:
        return
    if target_fps is None:
        ctrl = (1, 200000)
    else:
        dur = max(16666, int(1_000_000 / target_fps))
        ctrl = (dur, dur)
    if camera is None:
        return
    try:
        camera.set_controls({"FrameDurationLimits": ctrl})
        _fps_controls = ctrl
    except Exception:
        logger.warning('set_controls FrameDurationLimits failed')


def _start_camera():
    global camera, camera_available, latest_frame
    with camera_lock:
        if camera_available:
            return
        try:
            from picamera2 import Picamera2
            w, h = RESOLUTIONS[current_resolution]
            camera = Picamera2()
            config = camera.create_video_configuration(
                main={"size": (w, h), "format": "YUV420"},
            )
            camera.configure(config)
            camera.start()
            _start_recording()
            if target_fps != 0:
                _apply_framerate()
            camera_available = True
            latest_frame = None
            latest_frame_time = 0.0
            sio.emit('camera_status', {'available': True})
            logger.info('Camera online %dx%d HW MJPEG', w, h)
        except Exception as e:
            camera_available = False
            if camera is not None:
                try:
                    camera.stop_recording()
                except Exception:
                    pass
                try:
                    camera.stop()
                except Exception:
                    pass
                try:
                    camera.close()
                except Exception:
                    pass
                camera = None
            logger.error('Camera start failed: %s', e)


def _stop_camera():
    global camera, camera_available, latest_frame, camera_fps, _last_frame_time
    with camera_lock:
        if not camera_available or camera is None:
            return
        try:
            camera.stop_recording()
        except Exception:
            pass
        try:
            camera.stop()
        except Exception:
            pass
        try:
            camera.close()
        except Exception:
            pass
        camera = None
        camera_available = False
        latest_frame = None
        latest_frame_time = 0.0
        camera_fps = 0.0
        _last_frame_time = 0.0
        sio.emit('camera_status', {'available': False})
        logger.info('Camera stopped')


def init_camera():
    _start_camera()


@app.route('/api/camera_status')
def api_camera_status():
    return jsonify({'available': camera_available, 'fps': camera_fps})

@app.route('/api/set_resolution', methods=['POST'])
def api_set_resolution():
    global current_resolution
    res_idx = request.json.get('index', 1)
    if not isinstance(res_idx, int) or res_idx < 0 or res_idx >= len(RESOLUTIONS):
        return jsonify({'ok': False, 'error': 'invalid index'}), 400
    if not camera_available:
        return jsonify({'ok': False, 'error': 'camera unavailable'}), 503
    w, h = RESOLUTIONS[res_idx]
    th = threading.Thread(target=_reconfigure_camera, args=(res_idx, w, h), daemon=True)
    th.start()
    logger.info('Camera reconfiguring to %dx%d HW MJPEG (async)', w, h)
    return jsonify({'ok': True, 'resolution': (w, h)})


def _reconfigure_camera(res_idx, w, h):
    global current_resolution, latest_frame, camera_available, camera
    with camera_lock:
        try:
            camera.stop_recording()
            camera.stop()
            time.sleep(0.2)
            config = camera.create_video_configuration(
                main={"size": (w, h), "format": "YUV420"},
            )
            camera.configure(config)
            camera.start()
            _start_recording()
            _apply_framerate()
            current_resolution = res_idx
            latest_frame = None
            latest_frame_time = 0.0
            logger.info('Camera reconfigured to %dx%d HW MJPEG', w, h)
        except Exception as e:
            logger.error('Camera reconfigure to %dx%d failed: %s', w, h, e)
            try:
                camera.close()
            except Exception:
                pass
            camera = None
            camera_available = False

@app.route('/api/set_framerate', methods=['POST'])
def api_set_framerate():
    global target_fps, camera_available
    val = request.json.get('fps')
    if not isinstance(val, int) or val < 0 or val > 60:
        return jsonify({'ok': False, 'error': 'invalid fps'}), 400
    with fps_lock:
        if val == 0:
            target_fps = 0
        elif val == 60:
            target_fps = None
        else:
            target_fps = val
        if val == 0:
            if camera_available:
                _stop_camera()
        else:
            if not camera_available:
                _start_camera()
            else:
                with camera_lock:
                    if camera is None:
                        logger.warning('set_framerate: camera went away before apply')
                    else:
                        _apply_framerate()
    label = 'off' if val == 0 else (str(target_fps) if target_fps else 'uncapped')
    logger.info('Framerate set to %s', label)
    return jsonify({'ok': True, 'fps': label})

@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    global joystick_speed, max_speed_limiter
    if request.method == 'POST':
        data = request.json or {}
        if 'joystick_speed' in data:
            val = data['joystick_speed']
            if isinstance(val, (int, float)):
                joystick_speed = max(0, min(100, int(val)))
        if 'max_speed_limiter' in data:
            val = data['max_speed_limiter']
            if isinstance(val, (int, float)):
                max_speed_limiter = max(0, min(100, int(val)))
        return jsonify({'ok': True})
    return jsonify({
        'joystick_speed': joystick_speed,
        'max_speed_limiter': max_speed_limiter,
        'resolution_index': current_resolution,
        'fps': 60 if target_fps is None else target_fps,
    })

@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    if controller is None:
        return jsonify({'ok': False, 'error': 'controller not initialized'}), 503
    controller.stop_all()
    time.sleep(0.3)
    th = threading.Thread(
        target=lambda: subprocess.run(['sudo', 'shutdown', '-h', 'now'], timeout=30),
        daemon=True,
    )
    th.start()
    logger.info('Shutdown initiated, motors stopped')
    return jsonify({'ok': True})

@app.route('/api/health')
def api_health():
    return jsonify({'ok': True})


@app.route('/api/debug')
def api_debug():
    return jsonify({
        'root_path': app.root_path,
        'template_folder': app.template_folder,
        'thread_count': threading.active_count(),
        'camera_available': camera_available,
        'camera_fps': camera_fps,
        'camera_resolution': RESOLUTIONS[current_resolution],
        'file_size': _template_size,
    })

_stats_prev = {'tx_bytes': None, 'rx_bytes': None, 'time': 0.0}
_stats_lock = threading.Lock()
_stats_cache = None
_stats_cache_lock = threading.Lock()
_stats_cache_time = 0.0

def _read_meminfo():
    global _mem_cache, _mem_cache_time
    now = time.time()
    with _mem_cache_lock:
        if now - _mem_cache_time < 3.0:
            return _mem_cache
    mem = {}
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                parts = line.split(':')
                if parts[0] in ('MemTotal', 'MemFree', 'MemAvailable', 'Buffers', 'Cached'):
                    mem[parts[0]] = parts[1].strip()
    except Exception:
        mem = {'error': 'unavailable'}
    with _mem_cache_lock:
        _mem_cache = mem
        _mem_cache_time = now
        return _mem_cache

@app.route('/api/stats')
def api_stats():
    global _stats_cache, _stats_cache_time
    with _stats_cache_lock:
        now = time.time()
        if now - _stats_cache_time < 0.5 and _stats_cache is not None:
            return _stats_cache
    load = []
    net = {}
    temp = ''
    mem = _read_meminfo()
    try:
        with open('/proc/loadavg') as f:
            load = f.read().strip().split()[:3]
    except Exception:
        load = ['?', '?', '?']
    try:
        with open('/proc/net/dev') as f:
            for line in f:
                if 'wlan' in line:
                    parts = line.strip().split()
                    name = parts[0].rstrip(':')
                    rx_bytes = int(parts[1])
                    tx_bytes = int(parts[9])
                    net[name] = {'rx_bytes': rx_bytes, 'tx_bytes': tx_bytes}
    except Exception:
        net = {'error': 'unavailable'}
    with _stats_lock:
        now = time.time()
        if now - _stats_prev.get('temp_time', 0) > 10.0 or 'temp' not in _stats_prev:
            try:
                temp = subprocess.run(['vcgencmd', 'measure_temp'], capture_output=True, text=True, timeout=2).stdout.strip()
            except Exception:
                temp = '?'
            _stats_prev['temp'] = temp
            _stats_prev['temp_time'] = now
        else:
            temp = _stats_prev['temp']

    now = time.time()
    tx_rate = rx_rate = 0.0
    iface = next((k for k in net if k.startswith('wlan')), None)
    with _stats_lock:
        prev = _stats_prev
        if iface and prev['tx_bytes'] is not None:
            dt = now - prev['time']
            if dt >= 1.0:
                tx_rate = (net[iface]['tx_bytes'] - prev['tx_bytes']) / dt / 1024
                rx_rate = (net[iface]['rx_bytes'] - prev['rx_bytes']) / dt / 1024
        if iface:
            prev['tx_bytes'] = net[iface]['tx_bytes']
            prev['rx_bytes'] = net[iface]['rx_bytes']
            prev['time'] = now

    cpu_count = os.cpu_count() or 1
    try:
        with open('/proc/uptime') as f:
            uptime = float(f.read().split()[0])
    except Exception:
        uptime = 0
    try:
        du = shutil.disk_usage('/')
        disk_usage = {
            'total_gb': round(du.total / (1024**3), 1),
            'used_gb': round(du.used / (1024**3), 1),
            'free_gb': round(du.free / (1024**3), 1),
            'percent': round(du.used / du.total * 100, 1),
        }
    except Exception:
        disk_usage = {'total_gb': 0, 'used_gb': 0, 'free_gb': 0, 'percent': 0}
    with _stats_lock:
        if now - _stats_prev.get('throttled_time', 0) > 10.0 or 'throttled' not in _stats_prev:
            throttled = _parse_throttled()
            try:
                out = subprocess.run(['vcgencmd', 'measure_clock', 'arm'], capture_output=True, text=True, timeout=2).stdout.strip()
                cpu_freq = int(out.split('=')[1]) // 1_000_000
            except Exception:
                cpu_freq = 0
            _stats_prev['throttled'] = throttled
            _stats_prev['cpu_freq'] = cpu_freq
            _stats_prev['throttled_time'] = now
        else:
            throttled = _stats_prev['throttled']
            cpu_freq = _stats_prev['cpu_freq']

    data = jsonify({
        'memory': mem,
        'load': load,
        'cpu_count': cpu_count,
        'temp': temp,
        'tx_rate_kbps': round(tx_rate, 1),
        'rx_rate_kbps': round(rx_rate, 1),
        'fps': round(camera_fps, 1) if camera_available else 0,
        'thread_count': threading.active_count(),
        'resolution': list(RESOLUTIONS[current_resolution]),
        'fps_target': target_fps if target_fps is not None else 'uncapped',
        'server_time': time.time(),
        'frame_time': latest_frame_time,
        'uptime': uptime,
        'disk_usage': disk_usage,
        'cpu_freq': cpu_freq,
        'throttled': throttled,
    })
    with _stats_cache_lock:
        _stats_cache = data
        _stats_cache_time = time.time()
    return data


@app.before_request
def log_request():
    if request.path not in ('/api/stats', '/api/camera_status', '/video_feed', '/api/health'):
        logger.info('%s %s', request.method, request.path)


def heartbeat_log():
    while True:
        time.sleep(60)
        if controller is None:
            continue
        speeds = controller.get_all_speeds()
        non_zero = {m: s for m, s in speeds.items() if s != 0}
        stale = ''
        if non_zero:
            age = time.time() - controller.last_cmd_time
            if age > 2:
                stale = ' STALE'
        if not camera_available and target_fps != 0:
            global _camera_retry_in_flight
            if _camera_retry_in_flight:
                logger.info('Camera retry already in progress, skipping')
            else:
                _camera_retry_in_flight = True
                logger.info('Camera unavailable, retrying init...')
                th = threading.Thread(target=init_camera_async, daemon=True)
                th.start()
        logger.info('heartbeat threads=%d cam=%s fps=%.1f motors=%s%s',
                     threading.active_count(), camera_available, camera_fps,
                     non_zero, stale)

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/video_feed')
def video_feed():
    global _active_streams
    sid = request.args.get('sid', '')
    with _video_owner_lock:
        if _video_owner_sid is None:
            return jsonify({'error': 'no video owner'}), 503
        if _video_owner_sid != sid:
            return jsonify({'error': 'video owned elsewhere', 'owner': _video_owner_sid}), 403
    with _streams_lock:
        if _active_streams >= MAX_STREAMS:
            return jsonify({'error': 'too many streams'}), 503
        _active_streams += 1

    _last_stale_log = 0.0

    def generate():
        nonlocal _last_stale_log
        try:
            while True:
                with _video_owner_lock:
                    still_owner = _video_owner_sid == sid
                if not still_owner:
                    break
                frame = latest_frame
                if frame is not None:
                    age = time.time() - latest_frame_time
                    if age < 10.0:
                        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
                    else:
                        if latest_frame_time > 0.0 and age - _last_stale_log > 30:
                            _last_stale_log = age
                            logger.warning('video_feed: stale frame %.1fs old', age)
                        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
                with _frame_cond:
                    _frame_cond.wait(timeout=1.0)
        except GeneratorExit:
            pass
        finally:
            with _streams_lock:
                global _active_streams
                _active_streams -= 1

    return Response(stream_with_context(generate()),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@sio.on('connect')
def on_connect():
    with _video_owner_lock:
        emit('video_owner', {'sid': _video_owner_sid})
    if controller is not None:
        emit('status', {'speeds': controller.get_all_speeds()})
    emit('camera_status', {'available': camera_available})


@sio.on('claim_video')
def on_claim_video(_data=None):
    global _video_owner_sid
    sid = request.sid
    with _video_owner_lock:
        old_owner = _video_owner_sid
        _video_owner_sid = sid
    if old_owner and old_owner != sid:
        sio.emit('video_lost', {'new_owner': sid}, to=old_owner)
        logger.info('Video owner: %s → %s', old_owner[:8], sid[:8])
    else:
        logger.info('Video owner: %s', sid[:8])
    emit('video_owner', {'sid': sid})


@sio.on('release_video')
def on_release_video(_data=None):
    global _video_owner_sid
    with _video_owner_lock:
        if _video_owner_sid == request.sid:
            _video_owner_sid = None
            logger.info('Video released by %s', request.sid[:8])


@sio.on('disconnect')
def on_disconnect():
    global _video_owner_sid
    with _video_owner_lock:
        if _video_owner_sid == request.sid:
            _video_owner_sid = None
            logger.info('Video owner disconnected: %s', request.sid[:8])


@sio.on('set_speed')
def on_set_speed(data):
    if controller is None:
        return
    if not isinstance(data, dict):
        logger.warning('set_speed: invalid data type %s', type(data).__name__)
        return
    motor = data.get('motor')
    speed = data.get('speed', 0)
    if not isinstance(speed, (int, float)):
        logger.warning('set_speed: non-numeric speed %s', type(speed).__name__)
        speed = 0
    if motor not in MOTOR_NAMES:
        return
    controller.set_speed(motor, speed)
    emit('status', {'speeds': controller.get_all_speeds()})


@sio.on('set_speeds')
def on_set_speeds(data):
    if controller is None:
        return
    if not isinstance(data, dict):
        logger.warning('set_speeds: invalid data type %s', type(data).__name__)
        return
    speeds = data.get('speeds', {})
    if not isinstance(speeds, dict):
        logger.warning('set_speeds: speeds not a dict %s', type(speeds).__name__)
        return
    cleaned = {}
    for m, s in speeds.items():
        if m not in MOTOR_NAMES:
            logger.warning('set_speeds: unknown motor %s', m)
            continue
        if isinstance(s, (int, float)):
            cleaned[m] = s
        else:
            logger.warning('set_speeds: non-numeric speed for motor %s', m)
    if not cleaned:
        return
    controller.set_speeds(cleaned)


@sio.on('stop')
def on_stop(_data=None):
    if controller is None:
        return
    controller.stop_all()
    emit('status', {'speeds': controller.get_all_speeds()})
    logger.info('Emergency stop triggered')


def init_camera_async():
    global camera_available, _camera_retry_in_flight
    try:
        init_camera()
    except Exception as e:
        camera_available = False
        logger.error('Camera init thread error: %s', e)
    finally:
        _camera_retry_in_flight = False


def _cleanup():
    global controller
    if controller is not None:
        try:
            controller.stop_all()
            time.sleep(0.3)
            controller.cleanup()
        except Exception:
            pass


@app.route('/api/wifi/scan')
def api_wifi_scan():
    networks = wifi_manager.scan()
    current = wifi_manager.current_ssid()
    return jsonify({'networks': networks, 'current': current})


@app.route('/api/wifi/status')
def api_wifi_status():
    return jsonify({
        'ssid': wifi_manager.current_ssid(),
        'signal': wifi_manager.current_signal(),
        'ip': wifi_manager.current_ip(),
        'mode': wifi_manager.current_mode(),
    })


@app.route('/api/wifi/connect', methods=['POST'])
def api_wifi_connect():
    data = request.get_json(silent=True)
    if not data or not isinstance(data, dict):
        return jsonify({'ok': False, 'error': 'Invalid request'}), 400
    ssid = data.get('ssid', '').strip()
    password = data.get('password', '').strip()
    result = wifi_manager.try_connect(ssid, password)
    return jsonify(result)


def main():
    global controller, _template_size
    try:
        controller = MotorController()
    except ConnectionError as e:
        logger.error('FATAL: %s', e)
        sys.exit(1)

    cam_thread = threading.Thread(target=init_camera_async, daemon=True)
    cam_thread.start()

    hb = threading.Thread(target=heartbeat_log, daemon=True)
    hb.start()

    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    ip = get_ip()
    port = 5000
    try:
        _template_size = os.path.getsize(os.path.join(app.root_path, 'templates', 'index.html'))
    except Exception:
        _template_size = 0
    logger.info('=' * 40)
    logger.info('R2 Motor Controller v2 starting')
    logger.info('http://%s:%d', ip, port)
    logger.info('Hostname: %s.local:%d', socket.gethostname(), port)
    logger.info('Motors: %s', ', '.join(MOTOR_NAMES))
    logger.info('Camera initializing in background...')
    logger.info('=' * 40)
    serve(app, host='0.0.0.0', port=port, threads=4)


if __name__ == '__main__':
    main()
