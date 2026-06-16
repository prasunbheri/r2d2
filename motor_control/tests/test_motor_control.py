import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import tests.mock_pigpio as mock_pigpio
sys.modules['pigpio'] = mock_pigpio

import importlib
import motor_control as mc_module
importlib.reload(mc_module)

from motor_control import MotorController, MOTOR_NAMES, MOTOR_PINS, validate_motor, clamp_speed, PWM_FREQ, PWM_RANGE, SLEW_INTERVAL


def _fresh():
    mc = MotorController()
    mc.pi._reset()
    mc.slew_rate = 1000
    for name in MOTOR_NAMES:
        pins = MOTOR_PINS[name]
        mc.pi.set_mode(pins['dir'], mock_pigpio.OUTPUT)
        mc.pi.set_mode(pins['pwm'], mock_pigpio.OUTPUT)
        mc.pi.set_PWM_frequency(pins['pwm'], PWM_FREQ)
        mc.pi.set_PWM_range(pins['pwm'], PWM_RANGE)
        mc.pi.write(pins['dir'], 0)
        mc.pi.set_PWM_dutycycle(pins['pwm'], 0)
    return mc


def _sync(mc, timeout=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(mc._current_speed[m] == mc._target_speed[m] for m in MOTOR_NAMES):
            time.sleep(0.01)
            return
        time.sleep(0.005)


class TestValidateMotor:

    def test_valid_motors(self):
        for m in MOTOR_NAMES:
            validate_motor(m)

    def test_invalid_motor_raises(self):
        try:
            validate_motor('XX')
            assert False
        except ValueError:
            pass


class TestClampSpeed:

    def test_within_range(self):
        assert clamp_speed(50) == 50
        assert clamp_speed(0) == 0
        assert clamp_speed(-50) == -50

    def test_above_max(self):
        assert clamp_speed(150) == 100

    def test_below_min(self):
        assert clamp_speed(-150) == -100

    def test_boundaries(self):
        assert clamp_speed(100) == 100
        assert clamp_speed(-100) == -100


class TestInit:

    def test_all_motors_initialized(self):
        mc = MotorController()
        assert list(mc._target_speed.keys()).sort() == MOTOR_NAMES.sort()
        assert list(mc._current_speed.keys()).sort() == MOTOR_NAMES.sort()
        for n in MOTOR_NAMES:
            pins = MOTOR_PINS[n]
            assert mc.pi.modes[pins['dir']] == mock_pigpio.OUTPUT
            assert mc.pi.modes[pins['pwm']] == mock_pigpio.OUTPUT
            assert mc.pi.pwm_freq[pins['pwm']] == PWM_FREQ
            assert mc.pi.pwm_range[pins['pwm']] == PWM_RANGE
            assert mc.pi.writes[pins['dir']] == 0
            assert mc.pi.pwm_duty[pins['pwm']] == 0

    def test_fails_on_disconnected(self):
        mock_pigpio.set_connected(False)
        try:
            MotorController()
            assert False
        except ConnectionError:
            pass
        finally:
            mock_pigpio.set_connected(True)


class TestSetSpeed:

    def test_forward(self):
        mc = _fresh()
        mc.set_speed('FL', 50)
        _sync(mc)
        assert mc.pi.writes[MOTOR_PINS['FL']['dir']] == 1
        assert mc.pi.pwm_duty[MOTOR_PINS['FL']['pwm']] == 500
        assert mc._current_speed['FL'] == 50

    def test_reverse(self):
        mc = _fresh()
        mc.set_speed('FL', -50)
        _sync(mc)
        assert mc.pi.writes[MOTOR_PINS['FL']['dir']] == 0
        assert mc.pi.pwm_duty[MOTOR_PINS['FL']['pwm']] == 500
        assert mc._current_speed['FL'] == -50

    def test_zero(self):
        mc = _fresh()
        mc.set_speed('FR', 0)
        _sync(mc)
        assert mc.pi.pwm_duty[MOTOR_PINS['FR']['pwm']] == 0
        assert mc._current_speed['FR'] == 0

    def test_full_forward(self):
        mc = _fresh()
        mc.set_speed('RL', 100)
        _sync(mc)
        assert mc.pi.writes[MOTOR_PINS['RL']['dir']] == 1
        assert mc.pi.pwm_duty[MOTOR_PINS['RL']['pwm']] == 1000

    def test_full_reverse(self):
        mc = _fresh()
        mc.set_speed('RR', -100)
        _sync(mc)
        assert mc.pi.writes[MOTOR_PINS['RR']['dir']] == 0
        assert mc.pi.pwm_duty[MOTOR_PINS['RR']['pwm']] == 1000

    def test_clamp_above(self):
        mc = _fresh()
        mc.set_speed('FL', 200)
        _sync(mc)
        assert mc._current_speed['FL'] == 100

    def test_clamp_below(self):
        mc = _fresh()
        mc.set_speed('FL', -200)
        _sync(mc)
        assert mc._current_speed['FL'] == -100

    def test_invalid_motor(self):
        mc = _fresh()
        try:
            mc.set_speed('XX', 50)
            assert False
        except ValueError:
            pass

    def test_independent_channels(self):
        mc = _fresh()
        for m, s in {'FL': 80, 'FR': -60, 'RL': 30, 'RR': -90}.items():
            mc.set_speed(m, s)
        _sync(mc)
        assert mc._current_speed == {'FL': 80, 'FR': -60, 'RL': 30, 'RR': -90}

    def test_reverse_duty_cycle_scaling(self):
        mc = _fresh()
        mc.set_speed('FL', -25)
        _sync(mc)
        assert mc.pi.pwm_duty[MOTOR_PINS['FL']['pwm']] == 250
        assert mc.pi.writes[MOTOR_PINS['FL']['dir']] == 0

    def test_forward_duty_cycle_scaling(self):
        mc = _fresh()
        mc.set_speed('FL', 75)
        _sync(mc)
        assert mc.pi.pwm_duty[MOTOR_PINS['FL']['pwm']] == 750
        assert mc.pi.writes[MOTOR_PINS['FL']['dir']] == 1

    def test_speed_0_forward_dir(self):
        mc = _fresh()
        mc.set_speed('FL', 0)
        _sync(mc)
        assert mc.pi.pwm_duty[MOTOR_PINS['FL']['pwm']] == 0


class TestSetSpeeds:

    def test_multiple(self):
        mc = _fresh()
        mc.set_speeds({'FL': 50, 'FR': -50})
        _sync(mc)
        assert mc._current_speed['FL'] == 50
        assert mc._current_speed['FR'] == -50

    def test_partial(self):
        mc = _fresh()
        mc.set_speed('FL', 80)
        mc.set_speeds({'FR': 40})
        _sync(mc)
        assert mc._current_speed['FL'] == 80
        assert mc._current_speed['FR'] == 40


class TestSetAll:

    def test_all_same(self):
        mc = _fresh()
        mc.set_all(75)
        _sync(mc)
        for m in MOTOR_NAMES:
            assert mc._current_speed[m] == 75

    def test_all_reverse(self):
        mc = _fresh()
        mc.set_all(-100)
        _sync(mc)
        for m in MOTOR_NAMES:
            assert mc._current_speed[m] == -100


class TestStopAll:

    def test_all_zero(self):
        mc = _fresh()
        mc.set_all(80)
        _sync(mc)
        mc.stop_all()
        _sync(mc)
        for m in MOTOR_NAMES:
            assert mc._current_speed[m] == 0
            assert mc.pi.pwm_duty[MOTOR_PINS[m]['pwm']] == 0


class TestGetSpeed:

    def test_get(self):
        mc = _fresh()
        mc.set_speed('FL', 60)
        assert mc.get_speed('FL') == 60

    def test_get_all(self):
        mc = _fresh()
        mc.set_speed('FL', 10)
        mc.set_speed('FR', 20)
        a = mc.get_all_speeds()
        assert a['FL'] == 10
        assert a['FR'] == 20
        assert a['RL'] == 0
        assert a['RR'] == 0

    def test_invalid(self):
        mc = _fresh()
        try:
            mc.get_speed('XX')
            assert False
        except ValueError:
            pass


class TestWatchdog:

    def test_timeout_stops_motors_instantly(self):
        mc = _fresh()
        mc.set_all(100)
        _sync(mc)
        assert mc._current_speed['FL'] == 100
        assert mc.pi.pwm_duty[MOTOR_PINS['FL']['pwm']] == 1000
        mc._last_cmd_time = time.time() - 5
        time.sleep(SLEW_INTERVAL * 3)
        assert mc._current_speed['FL'] == 0
        assert mc.pi.pwm_duty[MOTOR_PINS['FL']['pwm']] == 0

    def test_timeout_resets_on_new_command(self):
        mc = _fresh()
        mc.set_all(100)
        _sync(mc)
        mc._last_cmd_time = time.time() - 5
        time.sleep(SLEW_INTERVAL * 3)
        assert mc._current_speed['FL'] == 0
        mc.set_speed('FL', 50)
        _sync(mc)
        assert mc._current_speed['FL'] == 50


class TestReconnect:

    def test_reconnect_succeeds(self):
        mc = _fresh()
        mc.pi.connected = False
        assert mc._reconnect() is True
        assert mc.pi.connected is True
        for name in MOTOR_NAMES:
            pins = MOTOR_PINS[name]
            assert mc.pi.modes[pins['dir']] == mock_pigpio.OUTPUT
            assert mc.pi.modes[pins['pwm']] == mock_pigpio.OUTPUT

    def test_reconnect_fails(self):
        mc = _fresh()
        mock_pigpio.set_connected(False)
        try:
            mc.pi.connected = False
            assert mc._reconnect() is False
        finally:
            mock_pigpio.set_connected(True)


class TestCleanup:

    def test_stops_and_disconnects(self):
        mc = _fresh()
        mc.set_all(50)
        _sync(mc)
        mc.cleanup()
        for m in MOTOR_NAMES:
            assert mc.pi.pwm_duty[MOTOR_PINS[m]['pwm']] == 0
        assert mc.pi.connected is False


class TestApplyPwm:

    def test_preserves_target_speed_on_error(self):
        mc = _fresh()
        mc.set_speed('FL', 50)
        mc._current_speed['FL'] = 50
        original_write = mc.pi.write
        original_reconnect = mc._reconnect
        mc.pi.write = lambda *a, **kw: (_ for _ in ()).throw(IOError('mock'))
        mc._reconnect = lambda: False
        target_before = mc._target_speed['FL']
        mc._apply_pwm('FL')
        assert mc._target_speed['FL'] == target_before
        assert mc._current_speed['FL'] == 0
        mc.pi.write = original_write
        mc._reconnect = original_reconnect
