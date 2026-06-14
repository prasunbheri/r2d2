import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import tests.mock_pigpio as mock_pigpio
sys.modules['pigpio'] = mock_pigpio

import importlib
import app as app_module
importlib.reload(app_module)

from motor_control import MotorController, MOTOR_NAMES

app_module.controller = MotorController()
app_module.controller.pi._reset()

app = app_module.app
sio = app_module.sio


class TestIndexRoute:

    def test_index_returns_html(self):
        with app.test_client() as client:
            resp = client.get('/')
            assert resp.status_code == 200
            assert resp.content_type.startswith('text/html')


class TestSetSpeed:

    def _make_client(self):
        return sio.test_client(app)

    def test_set_speed_updates_controller(self):
        app_module.controller.stop_all()
        client = self._make_client()
        client.emit('set_speed', {'motor': 'FL', 'speed': 75})
        received = client.get_received()
        assert len(received) > 0
        status = received[-1]['args'][0]
        assert status['speeds']['FL'] == 75
        client.disconnect()

    def test_set_speed_reverse(self):
        app_module.controller.stop_all()
        client = self._make_client()
        client.emit('set_speed', {'motor': 'FR', 'speed': -60})
        received = client.get_received()
        status = received[-1]['args'][0]
        assert status['speeds']['FR'] == -60
        client.disconnect()

    def test_set_speed_invalid_motor_ignored(self):
        app_module.controller.stop_all()
        client = self._make_client()
        client.emit('set_speed', {'motor': 'XX', 'speed': 50})
        received = client.get_received()
        statuses = [m['args'][0] for m in received if m['name'] == 'status']
        if statuses:
            status = statuses[-1]
            for m in MOTOR_NAMES:
                assert status['speeds'][m] == 0
        client.disconnect()

    def test_connect_sends_current_state(self):
        app_module.controller.set_speed('FL', 40)
        client = self._make_client()
        received = client.get_received()
        statuses = [m['args'][0] for m in received if m['name'] == 'status']
        assert len(statuses) > 0
        assert statuses[-1]['speeds']['FL'] == 40
        client.disconnect()

    def test_set_speeds_multiple(self):
        app_module.controller.stop_all()
        client = self._make_client()
        client.emit('set_speeds', {
            'speeds': {'FL': 50, 'FR': -50, 'RL': 25, 'RR': -25}
        })
        # set_speeds intentionally omits status echo (it creates a thread
        # per call, killing performance). Check controller directly.
        import time; time.sleep(0.05)
        speeds = app_module.controller.get_all_speeds()
        assert speeds['FL'] == 50
        assert speeds['FR'] == -50
        assert speeds['RL'] == 25
        assert speeds['RR'] == -25
        client.disconnect()


class TestStop:

    def test_stop_sets_all_to_zero(self):
        app_module.controller.set_all(80)
        client = sio.test_client(app)
        client.emit('stop')
        received = client.get_received()
        status = received[-1]['args'][0]
        for m in MOTOR_NAMES:
            assert status['speeds'][m] == 0
        client.disconnect()
