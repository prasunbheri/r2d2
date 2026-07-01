import logging
import threading
import time
from typing import Dict, List, Optional

import pigpio

logger = logging.getLogger('motor')

PWM_FREQ: int = 20000
PWM_RANGE: int = 1000
SLEW_RATE: int = 7
SLEW_INTERVAL: float = 0.02
WATCHDOG_TIMEOUT: float = 0.5
PIGPIO_RETRY_ATTEMPTS: int = 15
PIGPIO_RETRY_INTERVAL: float = 1.0

MOTOR_PINS: Dict[str, dict] = {
    'FL': {'dir': 6,  'pwm': 12},
    'FR': {'dir': 13, 'pwm': 19},
    'RL': {'dir': 16, 'pwm': 26},
    'RR': {'dir': 20, 'pwm': 21},
}

MOTOR_NAMES: List[str] = list(MOTOR_PINS.keys())


def validate_motor(motor: str) -> None:
    if motor not in MOTOR_PINS:
        raise ValueError(f"Unknown motor '{motor}'. Valid: {MOTOR_NAMES}")


def clamp_speed(speed: object) -> int:
    if not isinstance(speed, (int, float)):
        logger.warning('clamp_speed received non-numeric %s', type(speed).__name__)
        return 0
    if isinstance(speed, float) and (speed != speed):
        logger.warning('clamp_speed received NaN')
        return 0
    return max(-100, min(100, int(speed)))


class MotorController:

    def __init__(self, pi_host: Optional[str] = None) -> None:
        self.lock: threading.Lock = threading.Lock()
        self._pi_host: Optional[str] = pi_host
        self.pi: pigpio.pi = pigpio.pi(pi_host)
        for _ in range(PIGPIO_RETRY_ATTEMPTS):
            if self.pi.connected:
                break
            time.sleep(PIGPIO_RETRY_INTERVAL)
            self.pi = pigpio.pi(pi_host)
        if not self.pi.connected:
            raise ConnectionError(
                f"Cannot connect to pigpio daemon after "
                f"{int(PIGPIO_RETRY_ATTEMPTS * PIGPIO_RETRY_INTERVAL)}s. "
                "Is pigpiod running? (sudo systemctl start pigpiod)"
            )
        self._target_speed: Dict[str, int] = {m: 0 for m in MOTOR_NAMES}
        self._current_speed: Dict[str, int] = {m: 0 for m in MOTOR_NAMES}
        self._last_cmd_time: float = time.time()
        self.slew_rate: int = SLEW_RATE
        self._slew_running: bool = True

        for name in MOTOR_NAMES:
            pins = MOTOR_PINS[name]
            self.pi.set_mode(pins['dir'], pigpio.OUTPUT)
            self.pi.set_mode(pins['pwm'], pigpio.OUTPUT)
            self.pi.set_PWM_frequency(pins['pwm'], PWM_FREQ)
            self.pi.set_PWM_range(pins['pwm'], PWM_RANGE)
            self.pi.write(pins['dir'], 0)
            self.pi.set_PWM_dutycycle(pins['pwm'], 0)

        self._slew_thread: threading.Thread = threading.Thread(target=self._slew_loop, daemon=True)
        self._slew_thread.start()

    def _apply_pwm(self, motor: str) -> None:
        speed = self._current_speed[motor]
        pins = MOTOR_PINS[motor]
        for attempt in range(4):
            try:
                if speed == 0:
                    duty = 0
                    self.pi.set_PWM_dutycycle(pins['pwm'], duty)
                    return
                if speed > 0:
                    self.pi.write(pins['dir'], 1)
                    duty = int((speed / 100.0) * PWM_RANGE)
                else:
                    self.pi.write(pins['dir'], 0)
                    duty = int((-speed / 100.0) * PWM_RANGE)
                self.pi.set_PWM_dutycycle(pins['pwm'], duty)
                return
            except Exception as e:
                if attempt < 3:
                    logger.warning('pigpio write failed for %s (attempt %d): %s', motor, attempt + 1, e)
                    if not self._reconnect():
                        break
                else:
                    logger.error('pigpio write failed for %s after 3 retries: %s', motor, e)
                    break
        if self._current_speed[motor] != 0:
            logger.error('Zeroing motor %s after pigpio failure', motor)
            self._current_speed[motor] = 0

    def _reconnect(self) -> bool:
        for attempt in range(3):
            new_pi = pigpio.pi(self._pi_host)
            if new_pi.connected:
                try:
                    self.pi.stop()
                except Exception:
                    pass
                self.pi = new_pi
                for name in MOTOR_NAMES:
                    pins = MOTOR_PINS[name]
                    self.pi.set_mode(pins['dir'], pigpio.OUTPUT)
                    self.pi.set_mode(pins['pwm'], pigpio.OUTPUT)
                    self.pi.set_PWM_frequency(pins['pwm'], PWM_FREQ)
                    self.pi.set_PWM_range(pins['pwm'], PWM_RANGE)
                    self.pi.write(pins['dir'], 0)
                    self.pi.set_PWM_dutycycle(pins['pwm'], 0)
                logger.info('Reconnected to pigpiod after %d attempt(s)', attempt + 1)
                return True
            new_pi.stop()
            time.sleep(0.5)
        logger.error('Failed to reconnect to pigpiod after 3 attempts')
        return False

    def _slew_loop(self) -> None:
        while self._slew_running:
            try:
                time.sleep(SLEW_INTERVAL)
                if not self.pi.connected and not self._reconnect():
                    continue

                # Collect pending PWM updates under lock, apply outside to avoid
                # holding the lock during slow pigpio I/O (which can retry for seconds).
                pending = []
                with self.lock:
                    if time.time() - self._last_cmd_time > WATCHDOG_TIMEOUT:
                        for m in MOTOR_NAMES:
                            self._target_speed[m] = 0
                            self._current_speed[m] = 0
                            pending.append(m)
                    else:
                        for m in MOTOR_NAMES:
                            target = self._target_speed[m]
                            current = self._current_speed[m]
                            if current == target:
                                continue
                            if current * target < 0:
                                self._current_speed[m] = 0
                                pending.append(m)
                                continue
                            diff = target - current
                            step = self.slew_rate
                            if abs(diff) <= step:
                                self._current_speed[m] = target
                            else:
                                self._current_speed[m] += step if diff > 0 else -step
                            pending.append(m)

                for m in pending:
                    self._apply_pwm(m)
            except Exception:
                logger.exception('Slew loop error')

    def set_speed(self, motor: str, speed: int) -> None:
        validate_motor(motor)
        speed = clamp_speed(speed)
        with self.lock:
            self._set_speed_unlocked(motor, speed)

    def _set_speed_unlocked(self, motor: str, speed: int) -> None:
        self._target_speed[motor] = speed
        self._last_cmd_time = time.time()

    def set_speeds(self, speeds: Dict[str, int]) -> None:
        for motor in speeds:
            validate_motor(motor)
        now = time.time()
        with self.lock:
            for motor, speed in speeds.items():
                self._target_speed[motor] = clamp_speed(speed)
            self._last_cmd_time = now

    def set_all(self, speed: int) -> None:
        speed = clamp_speed(speed)
        now = time.time()
        with self.lock:
            for motor in MOTOR_NAMES:
                self._target_speed[motor] = speed
            self._last_cmd_time = now

    def stop_all(self) -> None:
        now = time.time()
        with self.lock:
            for motor in MOTOR_NAMES:
                self._target_speed[motor] = 0
                self._current_speed[motor] = 0
            self._last_cmd_time = now
        for motor in MOTOR_NAMES:
            self._apply_pwm(motor)

    def get_speed(self, motor: str) -> int:
        validate_motor(motor)
        with self.lock:
            return self._target_speed[motor]

    def get_all_speeds(self) -> Dict[str, int]:
        with self.lock:
            return dict(self._target_speed)

    @property
    def last_cmd_time(self) -> float:
        with self.lock:
            return self._last_cmd_time

    def cleanup(self) -> None:
        self._slew_running = False
        if self._slew_thread:
            self._slew_thread.join(timeout=2.5)
        for m in MOTOR_NAMES:
            pins = MOTOR_PINS[m]
            self.pi.write(pins['dir'], 0)
            self.pi.set_PWM_dutycycle(pins['pwm'], 0)
        self.pi.stop()
