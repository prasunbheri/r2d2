import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Patch pigpio BEFORE importing motor_control
import tests.mock_pigpio as mock_pigpio
sys.modules['pigpio'] = mock_pigpio

import importlib
import motor_control as mc_module
importlib.reload(mc_module)

from motor_control import MotorController, MOTOR_NAMES, MOTOR_PINS, validate_motor, clamp_speed, PWM_FREQ, PWM_RANGE


def _fresh():
    mc = MotorController()
    mc.pi._reset()
    return mc


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
        assert list(mc._speed.keys()).sort() == MOTOR_NAMES.sort()
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
        assert mc.pi.writes[MOTOR_PINS['FL']['dir']] == 1
        assert mc.pi.pwm_duty[MOTOR_PINS['FL']['pwm']] == 500
        assert mc._speed['FL'] == 50

    def test_reverse(self):
        mc = _fresh()
        mc.set_speed('FL', -50)
        assert mc.pi.writes[MOTOR_PINS['FL']['dir']] == 0
        assert mc.pi.pwm_duty[MOTOR_PINS['FL']['pwm']] == 500
        assert mc._speed['FL'] == -50

    def test_zero(self):
        mc = _fresh()
        mc.set_speed('FR', 0)
        assert mc.pi.pwm_duty[MOTOR_PINS['FR']['pwm']] == 0
        assert mc._speed['FR'] == 0

    def test_full_forward(self):
        mc = _fresh()
        mc.set_speed('RL', 100)
        assert mc.pi.writes[MOTOR_PINS['RL']['dir']] == 1
        assert mc.pi.pwm_duty[MOTOR_PINS['RL']['pwm']] == 1000

    def test_full_reverse(self):
        mc = _fresh()
        mc.set_speed('RR', -100)
        assert mc.pi.writes[MOTOR_PINS['RR']['dir']] == 0
        assert mc.pi.pwm_duty[MOTOR_PINS['RR']['pwm']] == 1000

    def test_clamp_above(self):
        mc = _fresh()
        mc.set_speed('FL', 200)
        assert mc._speed['FL'] == 100

    def test_clamp_below(self):
        mc = _fresh()
        mc.set_speed('FL', -200)
        assert mc._speed['FL'] == -100

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
        assert mc._speed == {'FL': 80, 'FR': -60, 'RL': 30, 'RR': -90}

    def test_reverse_duty_cycle_scaling(self):
        mc = _fresh()
        mc.set_speed('FL', -25)
        assert mc.pi.pwm_duty[MOTOR_PINS['FL']['pwm']] == 250
        assert mc.pi.writes[MOTOR_PINS['FL']['dir']] == 0

    def test_forward_duty_cycle_scaling(self):
        mc = _fresh()
        mc.set_speed('FL', 75)
        assert mc.pi.pwm_duty[MOTOR_PINS['FL']['pwm']] == 750
        assert mc.pi.writes[MOTOR_PINS['FL']['dir']] == 1

    def test_speed_0_forward_dir(self):
        mc = _fresh()
        mc.set_speed('FL', 0)
        assert mc.pi.pwm_duty[MOTOR_PINS['FL']['pwm']] == 0


class TestSetSpeeds:

    def test_multiple(self):
        mc = _fresh()
        mc.set_speeds({'FL': 50, 'FR': -50})
        assert mc._speed['FL'] == 50
        assert mc._speed['FR'] == -50

    def test_partial(self):
        mc = _fresh()
        mc.set_speed('FL', 80)
        mc.set_speeds({'FR': 40})
        assert mc._speed['FL'] == 80
        assert mc._speed['FR'] == 40


class TestSetAll:

    def test_all_same(self):
        mc = _fresh()
        mc.set_all(75)
        for m in MOTOR_NAMES:
            assert mc._speed[m] == 75

    def test_all_reverse(self):
        mc = _fresh()
        mc.set_all(-100)
        for m in MOTOR_NAMES:
            assert mc._speed[m] == -100


class TestStopAll:

    def test_all_zero(self):
        mc = _fresh()
        mc.set_all(80)
        mc.stop_all()
        for m in MOTOR_NAMES:
            assert mc._speed[m] == 0
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


class TestCleanup:

    def test_stops_and_disconnects(self):
        mc = _fresh()
        mc.set_all(50)
        mc.cleanup()
        for m in MOTOR_NAMES:
            assert mc.pi.pwm_duty[MOTOR_PINS[m]['pwm']] == 0
        assert mc.pi.connected is False
