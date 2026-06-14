import os
import socket
import sys
import threading
import time

from flask import Flask, render_template
from flask_socketio import SocketIO

from motor_control import MotorController, MOTOR_NAMES

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24).hex()
sio = SocketIO(app, cors_allowed_origins='*')

controller = None
_update_lock = threading.Lock()


def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


@app.route('/')
def index():
    return render_template('index.html')


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
    global controller
    try:
        controller = MotorController()
    except ConnectionError as e:
        print(f'FATAL: {e}', file=sys.stderr)
        sys.exit(1)

    ip = get_ip()
    port = 5000
    print(f'Motor controller ready at http://{ip}:{port}')
    print(f'Hostname: {socket.gethostname()}.local:{port}')
    print(f'Motors: {", ".join(MOTOR_NAMES)}')
    sio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()
