import logging
import os
import socket
import sys
import threading
import time
import traceback

from flask import Flask, jsonify, render_template, Response, request, stream_with_context
from flask_socketio import SocketIO
from waitress import serve

from motor_control import MotorController, MOTOR_NAMES

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
    stream=sys.stdout,
)
logger = logging.getLogger('r2')

def log_unhandled(exc_type, exc_value, exc_tb):
    logger.error('Unhandled exception', exc_info=(exc_type, exc_value, exc_tb))
sys.excepthook = log_unhandled
threading.excepthook = lambda args: logger.error(
    'Thread exception', exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24).hex()
sio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

controller = None
_update_lock = threading.Lock()

camera = None
camera_lock = threading.Lock()
camera_available = False
latest_frame = None
camera_fps = 0.0
_last_frame_time = 0.0

RESOLUTIONS = [(320, 240), (640, 480), (800, 600), (1024, 768), (1280, 960)]
current_resolution = 1  # index into RESOLUTIONS
target_fps = 12  # desired framerate; None = uncapped
_fps_controls = None  # (min_dur, max_dur) last applied via set_controls
_fps_lock = threading.Lock()


def get_ip():
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
            global latest_frame, camera_fps, _last_frame_time
            latest_frame = bytes(frame)
            now = time.time()
            dt = now - _last_frame_time
            if dt >= 0.01:
                camera_fps = 1.0 / dt
            _last_frame_time = now

    return _CircularOutput()


def _start_recording():
    """Configure camera and start HW MJPEG recording via V4L2 encoder."""
    from picamera2.encoders import MJPEGEncoder, Quality
    encoder = MJPEGEncoder()
    output = _make_camera_output()
    camera.start_recording(encoder, output, quality=Quality.VERY_HIGH)


def _apply_framerate():
    global target_fps, _fps_controls
    with _fps_lock:
        if target_fps is None:
            ctrl = (1, 200000)
        else:
            dur = max(16666, int(1_000_000 / target_fps))
            ctrl = (dur, dur)
        try:
            camera.set_controls({"FrameDurationLimits": ctrl})
            _fps_controls = ctrl
        except Exception:
            logger.warning('set_controls FrameDurationLimits failed')


def init_camera():
    global camera, camera_available
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
        _apply_framerate()
        camera_available = True
        logger.info('Camera online %dx%d HW MJPEG', w, h)
    except Exception as e:
        camera_available = False
        logger.error('Camera unavailable: %s', e)


@app.route('/api/camera_status')
def api_camera_status():
    return jsonify({'available': camera_available, 'fps': camera_fps})

@app.route('/api/set_resolution', methods=['POST'])
def api_set_resolution():
    global camera_available, latest_frame, current_resolution
    res_idx = request.json.get('index', 1)
    if not isinstance(res_idx, int) or res_idx < 0 or res_idx >= len(RESOLUTIONS):
        return jsonify({'ok': False, 'error': 'invalid index'}), 400
    if not camera_available:
        return jsonify({'ok': False, 'error': 'camera unavailable'}), 503
    w, h = RESOLUTIONS[res_idx]
    with camera_lock:
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
        logger.info('Camera reconfigured to %dx%d HW MJPEG', w, h)
    return jsonify({'ok': True, 'resolution': (w, h)})

@app.route('/api/set_framerate', methods=['POST'])
def api_set_framerate():
    global target_fps, camera_available
    val = request.json.get('fps')
    if not isinstance(val, int) or val < 0 or val > 60:
        return jsonify({'ok': False, 'error': 'invalid fps'}), 400
    with _fps_lock:
        if val == 60:
            target_fps = None
        else:
            target_fps = val + 1
    if camera_available:
        with camera_lock:
            _apply_framerate()
    logger.info('Framerate set to %s', target_fps if target_fps else 'uncapped')
    return jsonify({'ok': True, 'fps': target_fps if target_fps else 'uncapped'})

@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    th = threading.Thread(target=lambda: os.system('echo r2tele | sudo -S shutdown -h now'), daemon=True)
    th.start()
    return jsonify({'ok': True})

@app.route('/api/debug')
def api_debug():
    import os
    return jsonify({
        'root_path': app.root_path,
        'template_folder': app.template_folder,
        'thread_count': threading.active_count(),
        'camera_available': camera_available,
        'camera_fps': camera_fps,
        'camera_resolution': RESOLUTIONS[current_resolution],
        'file_size': os.path.getsize(os.path.join(app.root_path, 'templates', 'index.html')),
    })

_stats_prev = {'tx_bytes': None, 'rx_bytes': None, 'time': 0.0}
_stats_lock = threading.Lock()

@app.route('/api/stats')
def api_stats():
    mem = {}
    load = []
    net = {}
    temp = ''
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                parts = line.split(':')
                if parts[0] in ('MemTotal', 'MemFree', 'MemAvailable', 'Buffers', 'Cached'):
                    mem[parts[0]] = parts[1].strip()
    except Exception:
        mem = {'error': 'unavailable'}
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
    try:
        import subprocess
        temp = subprocess.run(['vcgencmd', 'measure_temp'], capture_output=True, text=True, timeout=2).stdout.strip()
    except Exception:
        temp = '?'

    now = time.time()
    tx_rate = rx_rate = 0.0
    with _stats_lock:
        prev = _stats_prev
        if net and 'wlan0' in net and prev['tx_bytes'] is not None:
            dt = now - prev['time']
            if dt >= 1.0:
                tx_rate = (net['wlan0']['tx_bytes'] - prev['tx_bytes']) / dt / 1024
                rx_rate = (net['wlan0']['rx_bytes'] - prev['rx_bytes']) / dt / 1024
        if net and 'wlan0' in net:
            prev['tx_bytes'] = net['wlan0']['tx_bytes']
            prev['rx_bytes'] = net['wlan0']['rx_bytes']
            prev['time'] = now

    return jsonify({
        'memory': mem,
        'load': load,
        'temp': temp,
        'tx_rate_kbps': round(tx_rate, 1),
        'rx_rate_kbps': round(rx_rate, 1),
        'fps': round(camera_fps, 1) if camera_available else 0,
        'thread_count': threading.active_count(),
        'resolution': list(RESOLUTIONS[current_resolution]),
        'fps_target': target_fps if target_fps is not None else 'uncapped',
    })


def heartbeat_log():
    while True:
        time.sleep(60)
        logger.info('heartbeat threads=%d cam=%s fps=%.1f',
                     threading.active_count(), camera_available, camera_fps)

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/video_feed')
def video_feed():
    def generate():
        try:
            while True:
                frame = latest_frame
                if frame is not None:
                    yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
                time.sleep(0.03)
        except GeneratorExit:
            pass
    return Response(stream_with_context(generate()),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@sio.on('connect')
def on_connect():
    sio.emit('status', {'speeds': controller.get_all_speeds()})
    sio.emit('camera_status', {'available': camera_available})


@sio.on('set_speed')
def on_set_speed(data):
    motor = data.get('motor')
    speed = data.get('speed', 0)
    if motor not in MOTOR_NAMES:
        return
    with _update_lock:
        controller.set_speed(motor, speed)
    sio.emit('status', {'speeds': controller.get_all_speeds()})


@sio.on('set_speeds')
def on_set_speeds(data):
    speeds = data.get('speeds', {})
    with _update_lock:
        controller.set_speeds(speeds)
    # No status echo — client is authoritative and ignores it.
    # Broadcasting here creates a thread per frame (up to 60/s).


@sio.on('stop')
def on_stop(_data=None):
    with _update_lock:
        controller.stop_all()
    sio.emit('status', {'speeds': controller.get_all_speeds()})


def init_camera_async():
    global camera_available
    try:
        init_camera()
    except Exception as e:
        camera_available = False
        logger.error('Camera init thread error: %s', e)


def main():
    global controller
    try:
        controller = MotorController()
    except ConnectionError as e:
        logger.error('FATAL: %s', e)
        sys.exit(1)

    cam_thread = threading.Thread(target=init_camera_async, daemon=True)
    cam_thread.start()

    hb = threading.Thread(target=heartbeat_log, daemon=True)
    hb.start()

    ip = get_ip()
    port = 5000
    logger.info('=' * 40)
    logger.info('R2 Motor Controller v2 starting')
    logger.info('http://%s:%d', ip, port)
    logger.info('Hostname: %s.local:%d', socket.gethostname(), port)
    logger.info('Motors: %s', ', '.join(MOTOR_NAMES))
    logger.info('Camera initializing in background...')
    logger.info('=' * 40)
    serve(app, host='0.0.0.0', port=port, threads=8)


if __name__ == '__main__':
    main()
