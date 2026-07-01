import logging
import threading
import time
from collections import deque
from typing import Dict, Optional

import smbus2

logger = logging.getLogger('battery')

ADS1115_ADDR = 0x48
REG_CONV = 0x00
REG_CONF = 0x01

CONF_HI = 0xC0
CONF_LO = 0x63

DIVIDER_TOP = 33_000
DIVIDER_BOTTOM = 10_000
DIVIDER_RATIO = (DIVIDER_TOP + DIVIDER_BOTTOM) / DIVIDER_BOTTOM

CELLS = 4
V_FULL = 3.6 * CELLS
V_EMPTY = 2.5 * CELLS

POLL_INTERVAL = 5
RETRY_INTERVAL = 30
MAX_RETRIES = 3
SMOOTHING_WINDOW = 5


class BatteryMonitor:

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._bus: Optional[smbus2.SMBus] = None
        self._voltage: float = 0.0
        self._percentage: float = 0.0
        self._available: bool = False
        self._retries: int = 0
        self._raw_readings: deque = deque(maxlen=SMOOTHING_WINDOW)
        self._init_bus()

    def _init_bus(self) -> None:
        try:
            bus = smbus2.SMBus(1)
            bus.write_i2c_block_data(ADS1115_ADDR, REG_CONF, [CONF_HI, CONF_LO])
            time.sleep(0.1)
            with self._lock:
                if self._bus is not None:
                    try:
                        self._bus.close()
                    except Exception:
                        pass
                self._bus = bus
                self._available = True
                self._retries = 0
            logger.info('ADS1115 ready at 0x%02x', ADS1115_ADDR)
        except Exception as e:
            with self._lock:
                self._available = False
            logger.warning('ADS1115 init failed: %s', e)

    def read(self) -> Optional[Dict]:
        try:
            with self._lock:
                if not self._available or self._bus is None:
                    return None
                data = self._bus.read_i2c_block_data(ADS1115_ADDR, REG_CONV, 2)
            raw = (data[0] << 8) | data[1]
            if raw >= 0x8000:
                raw -= 0x10000
            v_adc = raw * 6.144 / 32768.0
            v_bat = v_adc * DIVIDER_RATIO
            self._raw_readings.append(v_bat)
            v_bat_smoothed = sum(self._raw_readings) / len(self._raw_readings)
            pct = self._voltage_to_pct(v_bat_smoothed)
            with self._lock:
                self._voltage = round(v_bat_smoothed, 2)
                self._percentage = pct
                self._retries = 0
            return {'voltage': round(v_bat_smoothed, 2), 'percentage': pct}
        except Exception as e:
            with self._lock:
                self._retries += 1
                if self._retries >= MAX_RETRIES:
                    self._available = False
                    self._bus = None
            logger.warning('ADS1115 read error: %s (retry %d/%d)', e, self._retries, MAX_RETRIES)
            return None

    def try_reconnect(self) -> None:
        if not self._available:
            self._init_bus()

    def get_data(self) -> Dict:
        with self._lock:
            return {
                'voltage': self._voltage,
                'percentage': self._percentage,
                'available': self._available,
            }

    @staticmethod
    def _voltage_to_pct(voltage: float) -> float:
        if voltage >= V_FULL:
            return 100.0
        if voltage <= V_EMPTY:
            return 0.0
        return round((voltage - V_EMPTY) / (V_FULL - V_EMPTY) * 100, 1)

    def run_loop(self) -> None:
        while True:
            try:
                if not self._available:
                    self.try_reconnect()
                if self._available:
                    self.read()
            except Exception:
                logger.exception('Battery poll error')
            time.sleep(POLL_INTERVAL if self._available else RETRY_INTERVAL)
