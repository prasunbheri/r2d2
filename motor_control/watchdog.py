import atexit
import logging
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

import pigpio

LED_GPIO: int = 5
POLL_INTERVAL: int = 2
RECHECK_DELAY: int = 5
MAX_RETRIES: int = 3

REQUIRED_SERVICES: List[str] = ['pigpiod', 'motor_control.service']

logger: logging.Logger = logging.getLogger('watchdog')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
    stream=sys.stdout,
)

SLOW_BLINK: Tuple[float, float] = (1.0, 1.0)
FAST_BLINK: Tuple[float, float] = (0.2, 0.2)
FATAL_PULSES: int = 3
FATAL_GAP: float = 2.0


class LED:

    def __init__(self, gpio: int) -> None:
        self.gpio: int = gpio
        self.pi: pigpio.pi = pigpio.pi()
        if self.pi.connected:
            self.pi.set_mode(gpio, pigpio.OUTPUT)
            self.pi.write(gpio, 0)
        self._blink_in_progress: bool = False
        self._blink_lock: threading.Lock = threading.Lock()
        self._fatal_in_progress: bool = False
        self._fatal_lock: threading.Lock = threading.Lock()

    def _ensure(self) -> None:
        if not self.pi.connected:
            old: pigpio.pi = self.pi
            self.pi = pigpio.pi()
            if self.pi.connected:
                self.pi.set_mode(self.gpio, pigpio.OUTPUT)
                self.pi.write(self.gpio, 0)
            try:
                old.stop()
            except Exception:
                pass

    def on(self) -> None:
        self._ensure()
        if self.pi.connected:
            self.pi.write(self.gpio, 1)

    def off(self) -> None:
        self._ensure()
        if self.pi.connected:
            self.pi.write(self.gpio, 0)

    def blink(self, on_time: float, off_time: float, count: Optional[int] = None) -> None:
        n = 0
        while count is None or n < count:
            self.on()
            time.sleep(on_time)
            self.off()
            time.sleep(off_time)
            n += 1

    def pattern_slow_blink(self) -> None:
        self.blink(*SLOW_BLINK, count=1)

    def pattern_fast_blink(self) -> None:
        self.blink(*FAST_BLINK, count=1)

    def async_fast_blink(self) -> None:
        with self._blink_lock:
            if self._blink_in_progress:
                return
            self._blink_in_progress = True
        threading.Thread(target=self._fast_blink_impl, daemon=True).start()

    def _fast_blink_impl(self) -> None:
        try:
            self.blink(*FAST_BLINK, count=1)
        finally:
            with self._blink_lock:
                self._blink_in_progress = False

    def pattern_fatal(self) -> None:
        for _ in range(FATAL_PULSES):
            self.on()
            time.sleep(0.3)
            self.off()
            time.sleep(0.3)
        time.sleep(FATAL_GAP)

    def async_fatal(self) -> None:
        with self._fatal_lock:
            if self._fatal_in_progress:
                return
            self._fatal_in_progress = True
        threading.Thread(target=self._fatal_wrapper, daemon=True).start()

    def _fatal_wrapper(self) -> None:
        try:
            self._fatal_impl()
        finally:
            with self._fatal_lock:
                self._fatal_in_progress = False

    def _fatal_impl(self) -> None:
        for _ in range(FATAL_PULSES):
            self.on()
            time.sleep(0.3)
            self.off()
            time.sleep(0.3)
        time.sleep(FATAL_GAP)

    def cleanup(self) -> None:
        self.off()
        self.pi.stop()


def service_active(name: str) -> bool:
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', name],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() == 'active'
    except (subprocess.TimeoutExpired, FileNotFoundError, TimeoutError) as e:
        logger.warning('service_active(%s) failed: %s', name, e)
        return False


def restart_service(name: str) -> None:
    try:
        subprocess.run(
            ['systemctl', 'restart', name],
            capture_output=True, timeout=10
        )
    except Exception as e:
        logger.warning('restart_service(%s) failed: %s', name, e)


def wait_for_services(led: LED, timeout: float = 60) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        all_active = all(service_active(s) for s in REQUIRED_SERVICES)
        if all_active:
            return True
        led.pattern_slow_blink()
    return False


def main():
    led = LED(LED_GPIO)

    def _cleanup():
        try:
            led.cleanup()
        except Exception:
            pass

    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    ready = wait_for_services(led)
    if not ready:
        led.pattern_fatal()
        led.cleanup()
        sys.exit(1)

    led.on()

    retries = {s: 0 for s in REQUIRED_SERVICES}
    cooldown_until = {s: 0 for s in REQUIRED_SERVICES}

    try:
        while True:
            all_ok = True
            for service in REQUIRED_SERVICES:
                if time.time() < cooldown_until[service]:
                    all_ok = False
                    continue
                if not service_active(service):
                    all_ok = False
                    led.async_fast_blink()
                    restart_service(service)
                    retries[service] += 1
                    logger.info('Restarting %s (attempt %d/%d)', service, retries[service], MAX_RETRIES)
                    if retries[service] > MAX_RETRIES:
                        logger.error('Service %s failed after %d retries', service, MAX_RETRIES)
                        led.async_fatal()
                        cooldown_until[service] = time.time() + 60
                        retries[service] = 0
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
