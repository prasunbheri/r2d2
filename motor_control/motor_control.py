import threading
import time

import pigpio

PWM_FREQ = 20000
PWM_RANGE = 1000
SLEW_RATE = 7
SLEW_INTERVAL = 0.02
WATCHDOG_TIMEOUT = 2.0

MOTOR_PINS = {
    'FL': {'dir': 6,  'pwm': 12},
    'FR': {'dir': 13, 'pwm': 19},
    'RL': {'dir': 16, 'pwm': 26},
    'RR': {'dir': 20, 'pwm': 21},
}

MOTOR_NAMES = list(MOTOR_PINS.keys())


def validate_motor(motor):
    if motor not in MOTOR_PINS:
        raise ValueError(f"Unknown motor '{motor}'. Valid: {MOTOR_NAMES}")


def clamp_speed(speed):
    return max(-100, min(100, speed))


class MotorController:

    def __init__(self, pi_host=None):
        self.lock = threading.Lock()
        self.pi = pigpio.pi(pi_host)
        if not self.pi.connected:
            raise ConnectionError(
                "Cannot connect to pigpio daemon. "
                "Is pigpiod running? (sudo systemctl start pigpiod)"
            )
        self._target_speed = {m: 0 for m in MOTOR_NAMES}
        self._current_speed = {m: 0 for m in MOTOR_NAMES}
        self._last_cmd_time = time.time()
        self.slew_rate = SLEW_RATE
        self._slew_running = True

        for name in MOTOR_NAMES:
            pins = MOTOR_PINS[name]
            self.pi.set_mode(pins['dir'], pigpio.OUTPUT)
            self.pi.set_mode(pins['pwm'], pigpio.OUTPUT)
            self.pi.set_PWM_frequency(pins['pwm'], PWM_FREQ)
            self.pi.set_PWM_range(pins['pwm'], PWM_RANGE)
            self.pi.write(pins['dir'], 0)
            self.pi.set_PWM_dutycycle(pins['pwm'], 0)

        self._slew_thread = threading.Thread(target=self._slew_loop, daemon=True)
        self._slew_thread.start()

    def _apply_pwm(self, motor):
        speed = self._current_speed[motor]
        pins = MOTOR_PINS[motor]
        for attempt in range(4):
            try:
                if speed >= 0:
                    self.pi.write(pins['dir'], 1)
                    duty = int((speed / 100.0) * PWM_RANGE)
                else:
                    self.pi.write(pins['dir'], 0)
                    duty = int((-speed / 100.0) * PWM_RANGE)
                self.pi.set_PWM_dutycycle(pins['pwm'], duty)
                return
            except Exception:
                if attempt < 3:
                    if not self._reconnect():
                        break
                else:
                    break
        self._target_speed[motor] = 0
        self._current_speed[motor] = 0

    def _reconnect(self):
        for _ in range(3):
            try:
                self.pi.stop()
            except Exception:
                pass
            new_pi = pigpio.pi()
            if new_pi.connected:
                self.pi = new_pi
                for name in MOTOR_NAMES:
                    pins = MOTOR_PINS[name]
                    self.pi.set_mode(pins['dir'], pigpio.OUTPUT)
                    self.pi.set_mode(pins['pwm'], pigpio.OUTPUT)
                    self.pi.set_PWM_frequency(pins['pwm'], PWM_FREQ)
                    self.pi.set_PWM_range(pins['pwm'], PWM_RANGE)
                    self.pi.write(pins['dir'], 0)
                    self.pi.set_PWM_dutycycle(pins['pwm'], 0)
                return True
            time.sleep(0.5)
        return False

    def _slew_loop(self):
        while self._slew_running:
            time.sleep(SLEW_INTERVAL)
            with self.lock:
                if time.time() - self._last_cmd_time > WATCHDOG_TIMEOUT:
                    for m in MOTOR_NAMES:
                        self._target_speed[m] = 0

                for m in MOTOR_NAMES:
                    target = self._target_speed[m]
                    current = self._current_speed[m]
                    if current == target:
                        continue
                    if current * target < 0:
                        self._current_speed[m] = 0
                        self._apply_pwm(m)
                        continue
                    diff = target - current
                    step = self.slew_rate
                    if abs(diff) <= step:
                        self._current_speed[m] = target
                    else:
                        self._current_speed[m] += step if diff > 0 else -step
                    self._apply_pwm(m)

    def set_speed(self, motor, speed):
        validate_motor(motor)
        speed = clamp_speed(speed)
        with self.lock:
            self._target_speed[motor] = speed
            self._last_cmd_time = time.time()

    def set_speeds(self, speeds):
        for motor, speed in speeds.items():
            self.set_speed(motor, speed)

    def set_all(self, speed):
        for motor in MOTOR_NAMES:
            self.set_speed(motor, speed)

    def stop_all(self):
        for name in MOTOR_NAMES:
            self.set_speed(name, 0)

    def get_speed(self, motor):
        validate_motor(motor)
        return self._target_speed[motor]

    def get_all_speeds(self):
        return dict(self._target_speed)

    def cleanup(self):
        self._slew_running = False
        if self._slew_thread:
            self._slew_thread.join(timeout=1.0)
        for m in MOTOR_NAMES:
            pins = MOTOR_PINS[m]
            self.pi.write(pins['dir'], 0)
            self.pi.set_PWM_dutycycle(pins['pwm'], 0)
        self.pi.stop()
