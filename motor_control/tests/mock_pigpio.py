OUTPUT = 1
INPUT = 0

_global_connected = True


def set_connected(val):
    global _global_connected
    _global_connected = val


def get_connected():
    return _global_connected


class _Pi:

    def __init__(self):
        self.connected = get_connected()
        self.modes = {}
        self.writes = {}
        self.pwm_freq = {}
        self.pwm_range = {}
        self.pwm_duty = {}

    def set_mode(self, gpio, mode):
        self.modes[gpio] = mode

    def write(self, gpio, value):
        self.writes[gpio] = value

    def read(self, gpio):
        return self.writes.get(gpio, 0)

    def set_PWM_frequency(self, gpio, freq):
        self.pwm_freq[gpio] = freq

    def set_PWM_range(self, gpio, rng):
        self.pwm_range[gpio] = rng

    def set_PWM_dutycycle(self, gpio, duty):
        self.pwm_duty[gpio] = duty

    def get_PWM_dutycycle(self, gpio):
        return self.pwm_duty.get(gpio, 0)

    def stop(self):
        self.connected = False

    def _reset(self):
        self.modes.clear()
        self.writes.clear()
        self.pwm_freq.clear()
        self.pwm_range.clear()
        self.pwm_duty.clear()


def pi(host=None):
    return _Pi()
