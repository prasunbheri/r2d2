import pigpio

PWM_FREQ = 20000
PWM_RANGE = 1000

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
        self.pi = pigpio.pi(pi_host)
        if not self.pi.connected:
            raise ConnectionError(
                "Cannot connect to pigpio daemon. "
                "Is pigpiod running? (sudo systemctl start pigpiod)"
            )
        self._speed = {m: 0 for m in MOTOR_NAMES}
        for name in MOTOR_NAMES:
            pins = MOTOR_PINS[name]
            self.pi.set_mode(pins['dir'], pigpio.OUTPUT)
            self.pi.set_mode(pins['pwm'], pigpio.OUTPUT)
            self.pi.set_PWM_frequency(pins['pwm'], PWM_FREQ)
            self.pi.set_PWM_range(pins['pwm'], PWM_RANGE)
            self.pi.write(pins['dir'], 0)
            self.pi.set_PWM_dutycycle(pins['pwm'], 0)

    def set_speed(self, motor, speed):
        validate_motor(motor)
        speed = clamp_speed(speed)
        pins = MOTOR_PINS[motor]
        if speed >= 0:
            self.pi.write(pins['dir'], 1)
            duty = int((speed / 100.0) * PWM_RANGE)
        else:
            self.pi.write(pins['dir'], 0)
            duty = int((-speed / 100.0) * PWM_RANGE)
        self.pi.set_PWM_dutycycle(pins['pwm'], duty)
        self._speed[motor] = speed

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
        return self._speed[motor]

    def get_all_speeds(self):
        return dict(self._speed)

    def cleanup(self):
        self.stop_all()
        self.pi.stop()
