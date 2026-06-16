import logging
import subprocess
import sys
import time

LED_GPIO = 5
POLL_INTERVAL = 2
RECHECK_DELAY = 5
MAX_RETRIES = 3

REQUIRED_SERVICES = ['pigpiod', 'motor_control.service']

logger = logging.getLogger('watchdog')

SLOW_BLINK = (1.0, 1.0)
FAST_BLINK = (0.2, 0.2)
FATAL_PULSES = 3
FATAL_GAP = 2.0


def _gpio_path(gpio, attr):
    return f'/sys/class/gpio/gpio{gpio}/{attr}'


def _export_gpio(gpio):
    try:
        with open('/sys/class/gpio/export', 'w') as f:
            f.write(str(gpio))
    except IOError:
        pass


def _gpio_write(gpio, value):
    try:
        with open(_gpio_path(gpio, 'direction'), 'w') as f:
            f.write('out')
    except IOError:
        pass
    try:
        with open(_gpio_path(gpio, 'value'), 'w') as f:
            f.write(str(value))
    except IOError:
        pass


def _gpio_cleanup(gpio):
    try:
        with open('/sys/class/gpio/unexport', 'w') as f:
            f.write(str(gpio))
    except IOError:
        pass


class LED:

    def __init__(self, gpio):
        self.gpio = gpio
        _export_gpio(gpio)

    def on(self):
        _gpio_write(self.gpio, 1)

    def off(self):
        _gpio_write(self.gpio, 0)

    def blink(self, on_time, off_time, count=None):
        n = 0
        while count is None or n < count:
            self.on()
            time.sleep(on_time)
            self.off()
            time.sleep(off_time)
            n += 1

    def pattern_slow_blink(self):
        self.blink(*SLOW_BLINK, count=1)

    def pattern_fast_blink(self):
        self.blink(*FAST_BLINK, count=1)

    def pattern_fatal(self):
        for _ in range(FATAL_PULSES):
            self.on()
            time.sleep(0.3)
            self.off()
            time.sleep(0.3)
        time.sleep(FATAL_GAP)

    def cleanup(self):
        self.off()
        _gpio_cleanup(self.gpio)


def service_active(name):
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', name],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() == 'active'
    except (subprocess.TimeoutExpired, FileNotFoundError, TimeoutError):
        return False


def restart_service(name):
    try:
        subprocess.run(
            ['systemctl', 'restart', name],
            capture_output=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        pass


def wait_for_services(led, timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        all_active = all(service_active(s) for s in REQUIRED_SERVICES)
        if all_active:
            return True
        led.pattern_slow_blink()
    return False


def main():
    led = LED(LED_GPIO)

    ready = wait_for_services(led)
    if not ready:
        led.pattern_fatal()
        led.cleanup()
        sys.exit(1)

    led.on()

    retries = {s: 0 for s in REQUIRED_SERVICES}

    try:
        while True:
            all_ok = True
            for service in REQUIRED_SERVICES:
                if not service_active(service):
                    all_ok = False
                    led.pattern_fast_blink()
                    restart_service(service)
                    retries[service] += 1
                    logger.info('Restarting %s (attempt %d/%d)', service, retries[service], MAX_RETRIES)
                    if retries[service] > MAX_RETRIES:
                        logger.error('Service %s failed after %d retries', service, MAX_RETRIES)
                        led.pattern_fatal()
                    time.sleep(RECHECK_DELAY)
                    if service_active(service):
                        retries[service] = 0
                        logger.info('Service %s recovered', service)
                else:
                    retries[service] = 0

            if all_ok:
                led.on()
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        led.cleanup()


if __name__ == '__main__':
    main()
