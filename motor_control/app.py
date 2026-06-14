import io
import os
import socket
import sys
import threading
import time

from flask import Flask, render_template, Response
from flask_socketio import SocketIO

from motor_control import MotorController, MOTOR_NAMES

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24).hex()
sio = SocketIO(app, cors_allowed_origins='*')

controller = None
_update_lock = threading.Lock()

camera = None
camera_lock = threading.Lock()
camera_available = False
latest_frame = None
_camera_thread = None


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
        camera = Picamera2()
        config = camera.create_video_configuration(
            main={"size": (640, 480)},
            controls={"FrameDurationLimits": (33333, 33333)},
        )
        camera.configure(config)
        camera.start()
        camera_available = True
        print('Camera: online (640x480 MJPEG)')
    except Exception as e:
        camera_available = False
        print(f'Camera: unavailable ({e})')


def camera_capture_loop():
    global latest_frame
    while camera_available:
        try:
            buf = io.BytesIO()
            with camera_lock:
                if camera_available:
                    camera.capture_file(buf, format='jpeg')
            buf.seek(0)
            latest_frame = buf.getvalue()
        except Exception:
            time.sleep(0.1)
        time.sleep(0.05)


@app.route('/')
def index():
    cam_status = '1' if camera_available else '0'
    return render_template('index.html', camera=cam_status)


@app.route('/video_feed')
def video_feed():
    def generate():
        sent = None
        while True:
            global latest_frame
            frame = latest_frame
            if frame is not None and frame is not sent:
                sent = frame
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            time.sleep(0.03)
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')


@sio.on('connect')
def on_connect():
    sio.emit('status', {'speeds': controller.get_all_speeds()})


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
    sio.emit('status', {'speeds': controller.get_all_speeds()})


@sio.on('stop')
def on_stop(_data=None):
    with _update_lock:
        controller.stop_all()
    sio.emit('status', {'speeds': controller.get_all_speeds()})


def main():
    global controller, _camera_thread
    try:
        controller = MotorController()
    except ConnectionError as e:
        print(f'FATAL: {e}', file=sys.stderr)
        sys.exit(1)

    init_camera()
    if camera_available:
        _camera_thread = threading.Thread(target=camera_capture_loop, daemon=True)
        _camera_thread.start()

    ip = get_ip()
    port = 5000
    print(f'Motor controller ready at http://{ip}:{port}')
    print(f'Hostname: {socket.gethostname()}.local:{port}')
    print(f'Motors: {", ".join(MOTOR_NAMES)}')
    sio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()
