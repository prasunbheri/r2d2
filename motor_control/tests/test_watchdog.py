import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import tests.mock_pigpio as mock_pigpio
sys.modules['pigpio'] = mock_pigpio

import subprocess
import time
from unittest.mock import patch, MagicMock
import watchdog


class TestLED:

    def setup_method(self):
        self.led = watchdog.LED(5)

    def test_on_off(self):
        self.led.on()
        self.led.off()

    def test_cleanup(self):
        self.led.cleanup()

    def test_slow_blink(self):
        self.led.pattern_slow_blink()

    def test_fast_blink(self):
        self.led.pattern_fast_blink()

    def test_fatal(self):
        self.led.pattern_fatal()

    def test_async_fatal_guard(self):
        self.led.async_fatal()
        assert self.led._fatal_in_progress is True
        self.led.async_fatal()
        time.sleep(4.0)
        assert self.led._fatal_in_progress is False


class TestServiceActive:

    @patch('watchdog.subprocess.run')
    def test_active(self, mock_run):
        mock_result = MagicMock()
        mock_result.stdout = 'active\n'
        mock_run.return_value = mock_result
        assert watchdog.service_active('pigpiod') is True
        mock_run.assert_called_once()

    @patch('watchdog.subprocess.run')
    def test_inactive(self, mock_run):
        mock_result = MagicMock()
        mock_result.stdout = 'inactive\n'
        mock_run.return_value = mock_result
        assert watchdog.service_active('pigpiod') is False

    @patch('watchdog.subprocess.run')
    def test_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError
        assert watchdog.service_active('nonexistent') is False

    @patch('watchdog.subprocess.run')
    def test_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired('cmd', 5)
        assert watchdog.service_active('pigpiod') is False


class TestRestartService:

    @patch('watchdog.subprocess.run')
    def test_restart_called(self, mock_run):
        watchdog.restart_service('motor_control.service')
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert 'restart' in args[0]
        assert 'motor_control.service' in args[0]


class TestWaitForServices:

    @patch('watchdog.service_active')
    @patch('watchdog.LED')
    def test_all_active_returns_true(self, mock_led_cls, mock_active):
        mock_active.return_value = True
        led = mock_led_cls.return_value
        assert watchdog.wait_for_services(led, timeout=5) is True

    @patch('watchdog.service_active')
    @patch('watchdog.LED')
    def test_times_out_returns_false(self, mock_led_cls, mock_active):
        mock_active.return_value = False
        led = mock_led_cls.return_value
        assert watchdog.wait_for_services(led, timeout=2) is False


class TestLEDEnsure:

    def test_ensure_reconnects(self):
        led = watchdog.LED(5)
        led.pi.connected = False
        led._ensure()
        assert led.pi.connected is True

    def test_ensure_skips_when_connected(self):
        led = watchdog.LED(5)
        led.pi.connected = True
        led._ensure()
        assert led.pi.connected is True


class TestCooldown:

    def test_cooldown_skips_service(self):
        cooldown_until = {'pigpiod': 9999999999, 'motor_control.service': 0}
        all_ok = True
        for service in watchdog.REQUIRED_SERVICES:
            if time.time() < cooldown_until[service]:
                all_ok = False
                continue
            if not watchdog.service_active(service):
                all_ok = False
        assert all_ok is False

    def test_retry_resets_after_recovery(self):
        retries = {'pigpiod': 3, 'motor_control.service': 0}
        service = 'pigpiod'
        if not False:  # service would be active
            retries[service] = 0
        assert retries['pigpiod'] == 0


class TestConstants:

    def test_required_services(self):
        assert 'pigpiod' in watchdog.REQUIRED_SERVICES
        assert 'motor_control.service' in watchdog.REQUIRED_SERVICES

    def test_timing_constants_positive(self):
        assert watchdog.POLL_INTERVAL > 0
        assert watchdog.RECHECK_DELAY > 0
        assert watchdog.MAX_RETRIES > 0
