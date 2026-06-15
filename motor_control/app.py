import io
import logging
import os
import socket
import sys
import threading
import time
import traceback

from flask import Flask, jsonify, render_template, Response, request, stream_with_context
from flask_socketio import SocketIO

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


def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def init_camera():
    global camera, camera_available
    try:
        from picamera2 import Picamera2
        w, h = RESOLUTIONS[current_resolution]
        camera = Picamera2()
        config = camera.create_video_configuration(
            main={"size": (w, h)},
            controls={"FrameDurationLimits": (33333, 33333)},
        )
        camera.configure(config)
        camera.start()
        camera_available = True
        logger.info('Camera online %dx%d MJPEG', w, h)
    except Exception as e:
        camera_available = False
        logger.error('Camera unavailable: %s', e)


def camera_capture_loop():
    global latest_frame, camera_fps, _last_frame_time
    while camera_available:
        try:
            buf = io.BytesIO()
            with camera_lock:
                if camera_available:
                    camera.capture_file(buf, format='jpeg')
            buf.seek(0)
            latest_frame = buf.getvalue()
            now = time.time()
            dt = now - _last_frame_time
            if dt >= 0.01:
                camera_fps = 1.0 / dt
            _last_frame_time = now
        except Exception:
            time.sleep(0.1)
        time.sleep(0.1)


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
        camera.stop()
        time.sleep(0.2)
        config = camera.create_video_configuration(
            main={"size": (w, h)},
            controls={"FrameDurationLimits": (33333, 33333)},
        )
        camera.configure(config)
        camera.start()
        current_resolution = res_idx
        latest_frame = None
        logger.info('Camera reconfigured to %dx%d', w, h)
    return jsonify({'ok': True, 'resolution': (w, h)})

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
        if camera_available:
            th = threading.Thread(target=camera_capture_loop, daemon=True)
            th.start()
            logger.info('Camera capture loop started')
    except Exception as e:
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
    sio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()
