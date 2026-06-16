# R2 Motor Controller

A web-controlled 4-motor differential drive system for a Raspberry Pi Zero W, featuring a virtual joystick dashboard, HW MJPEG camera feed, status LED watchdog, and silent 20 kHz PWM motor control with slew rate limiting and auto-stop watchdog.

```
┌─────────────────────────────────────────────┐
│   R2 Control Bridge                    ● online │
│  ┌──────────┐                                │
│  │ [CAMERA] │    ┌─────┐                     │
│  │  stream  │    │  ▲  │   ┌──────────┐      │
│  │  640x480 │ ◄──│ ●  │──►│ FL: +45  │      │
│  │  fps: 9  │    │  ▼  │   │ FR: +30  │      │
│  └──────────┘    └─────┘   │ RL: +45  │      │
│       ┌────┐ ┌──────┐      │ RR: +30  │      │
│       │ ⚙  │ │AUTO  │STOP  └──────────┘      │
│       └────┘ │CENTER│                         │
│              └──────┘                         │
└─────────────────────────────────────────────┘
```

## Table of Contents

- [Hardware Architecture](#hardware-architecture)
- [Software Stack](#software-stack)
- [GPIO Pin Assignment](#gpio-pin-assignment)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
  - [Deployment](#deployment)
- [System Architecture](#system-architecture)
  - [Motor Controller](#1-motor-controller-motor_controlpy)
  - [Web Server](#2-web-server-apppy)
  - [Watchdog](#3-watchdog-watchdogpy)
  - [Dashboard](#4-web-dashboard-templatesindexhtml)
- [API Reference](#api-reference)
  - [HTTP Endpoints](#http-endpoints)
  - [SocketIO Events](#socketio-events)
- [LED Status Patterns](#led-status-patterns)
- [Testing](#testing)
- [Configuration Reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)
- [Hardware Wiring Guide](#hardware-wiring-guide)
- [Project Structure](#project-structure)
- [License](#license)

---

## Hardware Architecture

```
                    ┌──────────────────┐
                    │  12V Battery     │
                    └────────┬─────────┘
                             │
              ┌──────────────┴──────────────┐
              │         Power Split          │
              ├────────────────┬─────────────┤
              │   Motor Power  │   Pi Power  │
              │    (12V)       │   (5V via   │
              │                │   buck/reg) │
              └───────┬────────┴─────────────┘
                      │
        ┌─────────────┼─────────────────┐
        │             │                 │
   ┌────┴────┐  ┌────┴────┐      ┌─────┴──────┐
   │MDD20A A │  │MDD20A B │      │ Pi Zero W  │
   │         │  │         │      │            │
   │ DRV:FL  │  │ DRV:RL  │      │ GPIO 6-21  │
   │ DRV:FR  │  │ DRV:RR  │      │ Camera CSI │
   └┬───┬────┘  └┬───┬────┘      │ HDMI debug │
    │   │        │   │           └────────────┘
   FL  FR       RL  RR
   │   │        │   │
   ┌┴───┴────────┴───┴┐
   │  4 × MY6812      │
   │  12V 120W 3350RPM│
   └──────────────────┘
```

### Components

| Component | Specification |
|---|---|
| **Controller** | Raspberry Pi Zero W (1 GHz single-core, 512 MB RAM) |
| **Network** | WiFi, connects to phone hotspot / home network |
| **Motors** | 4 × MY6812 12V 120W 3350RPM brushed DC |
| **Motor Drivers** | 2 × Cytron MDD20A (dual-channel, 20A continuous per channel) |
| **Camera** | OV5647 5MP Raspberry Pi Camera Module (CSI interface) |
| **Battery** | 12V (appropriate Ah for runtime) |
| **Status LED** | 1 × standard LED on GPIO 5 with current-limiting resistor |
| **Voltage Regulator** | 12V → 5V buck converter for Pi power |

---

## Software Stack

```
┌──────────────────────────────────────────────────────────┐
│                    Phone Browser (Client)                  │
│  ┌──────────────────────────────────────────────────┐    │
│  │  index.html — Virtual Joystick + Camera + Status  │    │
│  │  socket.io.min.js (v4.7.5, served locally)      │    │
│  └──────────────────────┬───────────────────────────┘    │
│                         │ HTTP + WebSocket                │
│  ┌──────────────────────┴───────────────────────────┐    │
│  │  Raspberry Pi Zero W (Server)                     │    │
│  │                                                    │    │
│  │  ┌──────────────────┐  ┌──────────────────────┐   │    │
│  │  │   Flask +        │  │  watchdog.py         │   │    │
│  │  │   Flask-SocketIO │  │  (sysfs GPIO, root)  │   │    │
│  │  │   (port 5000)    │  │  ┌────────────────┐   │   │    │
│  │  │   ┌────────────┐ │  │  │ GPIO 5 LED     │   │   │    │
│  │  │   │ app.py     │ │  │  │ blink patterns │   │   │    │
│  │  │   │ motor_     │ │  │  └────────────────┘   │   │    │
│  │  │   │ control.py │ │  │                       │   │    │
│  │  │   │ picamera2  │ │  └──────────────────────┘   │   │    │
│  │  │   └────────────┘ │                              │   │    │
│  │  └──────────────────┘                              │   │    │
│  │              │                                     │   │    │
│  │     ┌────────┴────────┐                            │   │    │
│  │     │   pigpiod       │                            │   │    │
│  │     │   (DMA PWM)     │                            │   │    │
│  │     │   20 kHz        │                            │   │    │
│  │     └────────┬────────┘                            │   │    │
│  │              │ GPIO                               │   │    │
│  │     ┌────────┴────────┐                            │   │    │
│  │     │  MDD20A x2      │                            │   │    │
│  │     │  4 motors       │                            │   │    │
│  │     └─────────────────┘                            │   │    │
│  └─────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

| Layer | Technology | Purpose |
|---|---|---|---|
| **GPIO/PWM** | `pigpio` (pigpiod daemon) | Hardware-timed DMA PWM on any GPIO, 20 kHz, no jitter, silent operation |
| **Motor Control** | `motor_control.py` | 4-motor abstraction with threading lock, target/current speed, 50Hz slew (7%/tick), 2s watchdog, pigpiod reconnect |
| **Web Server** | `app.py` (Flask + Flask-SocketIO + Waitress) | HTTP server (waitress threads=6), polling-only SocketIO, HW MJPEG camera stream, REST API |
| **Dashboard** | `templates/index.html` | Mobile-first dark-theme SPA with virtual joystick, FPS counter, settings, stats overlay, reconnection overlay |
| **Watchdog** | `watchdog.py` | Independent sysfs GPIO monitor (no pigpio), auto-restarts failed services, structured logging |
| **Camera** | `picamera2` + HW MJPEG (`/dev/video11`) | GPU-encoded MJPEG via `start_recording(MJPEGEncoder)`, YUV420 output, minimal CPU cost |
| **Process Management** | systemd units | `pigpiod.service`, `motor_control.service`, `watchdog.service` with dependency chain and auto-restart |
| **Deployment** | `deploy.py` (pexpect), `deploy.sh` (bash) | One-command SSH deploy to Pi, now includes `static/` directory |

---

## GPIO Pin Assignment

All motor control pins use the **physical pins 31–40** block (BCM numbering), giving a clean physical layout.

| Physical Pin | BCM GPIO | Function | Direction | Motor |
|---|---|---|---|---|
| 31 | 6 | DIR | Output | FL (Front Left) |
| 32 | 12 | PWM | Output | FL (Front Left) |
| 33 | 13 | DIR | Output | FR (Front Right) |
| 35 | 19 | PWM | Output | FR (Front Right) |
| 36 | 16 | DIR | Output | RL (Rear Left) |
| 37 | 26 | PWM | Output | RL (Rear Left) |
| 38 | 20 | DIR | Output | RR (Rear Right) |
| 40 | 21 | PWM | Output | RR (Rear Right) |
| 34 | — | GND | Ground | Common ground |
| 39 | — | GND | Ground | Common ground |

**Status LED**: BCM GPIO 5 (driven by watchdog via sysfs)

> **Note:** Pins 31–40 are chosen to avoid audio output conflict on GPIO 12/13 which have alt-function for PWM0/PWM1. Since `pigpio` uses its own DMA-based PWM, this is not an issue, but the physical block is convenient for wiring.

### Motor Driver Wiring (MDD20A)

Each MDD20A is a dual-channel driver. Wire one per pair of motors (e.g., MDD20A-A drives FL+FR, MDD20A-B drives RL+RR).

| MDD20A Terminal | Connection |
|---|---|
| **DIR** (per channel) | → Pi GPIO (DIR pin) |
| **PWM** (per channel) | → Pi GPIO (PWM pin) |
| **Motor A+/A-** | → Brushed DC motor |
| **Power GND** | → Battery negative |
| **Power VM** | → Battery positive (12V) |
| **Logic GND** | → Pi GND (common ground) |

> **Important:** Ensure common ground between Pi, MDD20A drivers, and battery. The PWM signal is referenced to the logic ground.

---

## Getting Started

### Prerequisites

- Raspberry Pi Zero W (or any Pi with GPIO and camera)
- Raspberry Pi OS (Bookworm or newer, 64-bit recommended)
- Internet connection for initial package installation
- Python 3.9+ (3.13 on latest Raspbian trixie)

### Installation

**System packages:**

```bash
sudo apt update
sudo apt install -y pigpio python3-pip python3-picamera2 libcamera-v4l2
```

**Python packages:**

```bash
pip3 install flask flask-socketio waitress pytest --break-system-packages
```

**Enable and start pigpiod:**

```bash
sudo systemctl enable pigpiod
sudo systemctl start pigpiod
```

**Clone and set up:**

```bash
git clone git@github.com:prasunbheri/r2d2.git /home/pi/r2d2
cd /home/pi/r2d2/motor_control

# Copy service files
sudo cp motor_control.service /etc/systemd/system/
sudo cp watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable motor_control.service watchdog.service
```

**Start manually (for testing):**

```bash
sudo systemctl start motor_control.service
```

### Deployment

**Using deploy.py (recommended):**

```bash
python3 deploy.py [hostname]
# Default: r2tele@192.168.0.160
```

The script:
1. Tests SSH connectivity
2. Creates remote directories
3. SCP uploads all source files
4. Installs system packages
5. Installs Python packages
6. Enables and starts all systemd services
7. Runs the test suite remotely

**Using deploy.sh (simpler):**

```bash
chmod +x deploy.sh
./deploy.sh [hostname]
```

---

## System Architecture

### 1. Motor Controller (`motor_control.py`)

Core abstraction over `pigpio` for controlling 4 brushed DC motors via H-bridge drivers. Uses **target/current speed split** with a **slew daemon** and internal lock.

```
MotorController
│
├── __init__(pi_host=None)
│   ├── Connect to pigpiod daemon
│   ├── Configure all 8 GPIOs as outputs
│   ├── Set PWM frequency to 20 kHz
│   ├── Set PWM range to 1000
│   ├── Initialize _target_speed and _current_speed to 0
│   ├── Create threading.Lock for all pigpio writes
│   └── Start 50Hz slew daemon thread
│
├── set_speed(motor, speed)
│   ├── Validate motor name
│   ├── Clamp speed to [-100, 100]
│   └── Write _target_speed[motor] (slew thread ramps gradually)
│
├── set_speeds(speeds_dict)
│   └── Bulk set multiple motors (all target-speed writes)
│
├── set_all(speed)
│   └── Set all 4 motors to same target speed
│
├── stop_all()
│   └── Set all targets to 0 + reset watchdog timer
│
├── get_speed(motor) / get_all_speeds()
│   └── Return _target_speed (commanded speed, not current)
│
├── _slew_loop()  (daemon thread, 50Hz)
│   ├── For each motor:
│   │   ├── If direction changed: brake to 0 instantly
│   │   ├── Else: ramp _current_speed toward _target at 7%/tick
│   │   └── Write DIR + PWM only when _current_speed changes
│   ├── 2s watchdog: zero all targets if no set_speed call
│   └── sleep(0.02) between iterations
│
├── _write_motor(motor, speed, dir)
│   ├── Acquire threading.Lock
│   ├── Write DIR pin (1=forward, 0=reverse)
│   ├── Compute duty: |speed|/100 * 1000
│   ├── Write PWM dutycycle
│   └── On pigpio error → _reconnect_pigpio (3 retries × 0.5s)
│
├── _reconnect_pigpio()
│   ├── Stop + close old pi connection
│   ├── Connect to new pi() — up to 3 attempts
│   ├── Re-initialize all 8 GPIO pins
│   └── On total failure: zero all motors via _write_motor(fallback=0)
│
└── cleanup()
    ├── Set all targets to 0, wait for slew
    └── Stop + disconnect pigpio
```

**PWM Configuration:**

| Parameter | Value |
|---|---|
| Frequency | 20,000 Hz (inaudible, optimal for MDD20A) |
| Range | 0–1000 (1,000 steps ≈ 0.1% resolution) |
| Method | pigpio DMA (hardware-timed, no CPU jitter) |

**Speed Mapping:**

```
  +100 ───────────────────── Full forward (DIR=1, duty=1000)
    0  ───────────────────── Stopped  (DIR=1, duty=0)
  -100 ───────────────────── Full reverse (DIR=0, duty=1000)
```

**Differential Steering Mixing:**

The dashboard computes motor speeds from raw joystick (x, y) coordinates:

```
FL = clamp(y + x, -100, 100)
FR = clamp(y - x, -100, 100)
RL = clamp(y + x, -100, 100)
RR = clamp(y - x, -100, 100)
```

This gives tank-like differential steering:
- Push up (y>0, x=0): all motors forward → drive forward
- Push left (y=0, x<0): FL/RL reverse, FR/RR forward → spin left
- Push diagonal (y>0, x>0): FL/RL slower, FR/RR faster → curve right
- Push down (y<0, x=0): all motors reverse → drive backward

### 2. Web Server (`app.py`)

Flask + Flask-SocketIO application served via **Waitress** (threads=6), with polling-only SocketIO (no WebSocket upgrade under waitress). Serves dashboard, HW MJPEG camera stream, REST APIs, and handles joystick commands.

**Startup Sequence:**

```
main()
├── Create MotorController (pigpio connection)
├── Initialize camera in background thread
│   └── init_camera_async()
│       ├── Acquire camera_lock
│       ├── Create Picamera2 instance
│       ├── Configure video config (YUV420, default 640×480)
│       ├── Create MJPEGEncoder → CircularOutput
│       ├── camera.start_recording(encoder, output)
│       └── Set frame duration limits (default 9 FPS)
├── Start heartbeat thread (every 60s)
│   ├── Log motor speeds + stale warning
│   ├── Retry camera init if failed (target_fps != 0)
│   └── Log thread count
├── Log startup banner
└── waitress.serve(app, host='0.0.0.0', port=5000, threads=6)
```

**SocketIO Transport:**

- Forced to `transports=['polling']` on both server and client
- WebSocket upgrade fails with `RuntimeError` under waitress
- Reconnection uses exponential backoff: 250ms initial, ×1.5 per attempt, 3s cap, 0.3 randomization

**Thread Safety:**

- `MotorController._lock` — internal threading.Lock wraps all pigpio writes (motor speed updates, slew thread, watchdog)
- `camera_lock` — threading.Lock for camera lifecycle (`start_recording` / `stop_recording` / reconfiguration must be atomic)
- All background threads are daemon (die with main process)

**Camera (HW MJPEG):**

Uses VideoCore GPU encoder via `/dev/video11` (`bcm2835-codec-decode`):

- `Picamera2` outputs **YUV420** (NV12 not in picamera2 V4L2 lookup table)
- `MJPEGEncoder` → `CircularOutput` produces HW-encoded JPEG frames
- `outputframe` in `CircularOutput` receives raw bytes (no copy needed)
- Adaptive sleep: `1.0/target_fps * 0.8` (minimum 10ms)
- Format change cleared by `stop_recording()` → `stop()` → `close()` → full re-init

**Camera Reconfiguration (Resolution):**

Resolution changes require full pipeline restart:

```python
with camera_lock:
    camera.stop_recording()
    camera.stop()
    camera.close()
    camera = Picamera2()
    config = camera.create_video_configuration(main={"size": (w, h), "format": "YUV420"})
    camera.configure(config)
    encoder = MJPEGEncoder()
    output = CircularOutput()
    camera.start_recording(encoder, output)
```

**Camera Auto-Retry:**

- Heartbeat checks `not camera_available and target_fps != 0` every 60s
- Spawns `init_camera_async()` in daemon thread
- `_start_camera()` guarded by `camera_lock`, returns early if already available

**Camera Leak Protection:**

- Exception in `_start_camera()` calls `stop_recording()` → `stop()` → `close()` → sets `camera = None`
- Prevents Picamera2 memory leak on init failure

**Frame Rate Control:**

- Slider: 0 = camera off, 1–59 = value FPS, 60 = Uncapped
- Dynamic via `camera.set_controls({"FrameDurationLimits": (int(1e6/fps), int(1e6/fps))})` while recording
- FPS slider=0 calls `_stop_camera()` (stop recording + close)

**Settings Persistence:**

- `GET /api/settings` returns `{"joystick_speed", "max_speed_limiter", "resolution_index", "fps"}`
- `POST /api/settings` saves `joystick_speed` and `max_speed_limiter`
- Page loads saved values on connect and applies to UI sliders

**Shutdown:**

- `POST /api/shutdown` calls `controller.stop_all()` (ramps motors to 0)
- Then `echo r2tele | sudo -S shutdown -h now` in daemon thread

### 3. Watchdog (`watchdog.py`)

Independent service monitor that runs as root (needed for sysfs GPIO access). It does **not** depend on `pigpio` — it uses the Linux sysfs GPIO interface directly. Uses Python `logging` for restart/recover/fatal events.

```
watchdog.py
│
├── main()
│   ├── Export GPIO 5 via sysfs
│   ├── wait_for_services(timeout=60s)
│   │   ├── Poll systemctl is-active for REQUIRED_SERVICES
│   │   ├── Blink LED slow (1s on / 1s off) while waiting
│   │   └── Return True if all active, False on timeout
│   │
│   ├── If not ready → fatal pattern → exit 1
│   ├── Turn LED solid on
│   │
│   └── Main loop (every 2s):
│       ├── Check each REQUIRED_SERVICE with systemctl is-active
│       ├── If any inactive:
│       │   ├── logger.info("Restart attempt %d/%d for %s", ...)
│       │   ├── Blink LED fast (0.2s on / 0.2s off)
│       │   ├── systemctl restart <service>
│       │   ├── Increment retry counter
│       │   ├── If retries > MAX_RETRIES (3):
│       │   │   ├── logger.error("Max retries exceeded for %s", ...)
│       │   │   └── Fatal pattern (3 × 300ms pulses, 2s gap)
│       │   └── Wait RECHECK_DELAY (5s), check again
│       └── If all ok → LED solid on
```

**Key Design Decisions:**

- **No pigpio dependency** — watchdog can run before pigpiod starts, can recover it if it crashes
- **Runs as root** — sysfs GPIO requires root; watchdog.service sets `User=root`
- **Independent systemd unit** — `Requires=pigpiod.service` (not `BindsTo=`), so `systemctl stop motor_control` doesn't cascade-kill the watchdog
- **Max 3 retries** — after 3 consecutive failures per service, shows fatal pattern and continues (doesn't exit — tries forever at 3-retry blocks)
- **Structured logging** — `logging.getLogger('watchdog')` logs to stderr → systemd journal

### 4. Web Dashboard (`templates/index.html`)

Single-page application with dark theme, mobile-first responsive design, and offline-first architecture.

**Key UI Components:**

| Component | Description |
|---|---|
| **Connection Indicator** | Green/red dot + "online"/"offline" label at top-right |
| **Camera Feed** | MJPEG stream via `<img src="/video_feed">` with FPS counter overlay |
| **Virtual Joystick** | 200×200px circular base with 64px knob, mouse + touch support, cardinal direction labels |
| **Auto-Center Toggle** | CSS switch: when on, joystick snaps to zero on release; when off, holds last position |
| **STOP Button** | Emergency stop — sets all motors to 0 immediately |
| **Motor Speed Display** | 4-panel grid showing FL/FR/RL/RR speeds (green for forward, red for reverse) |
| **Settings Overlay** | Gear button top-left opens modal with sliders for Joystick Speed (`step=5`, `min=10`), Speed Limiter (`step=5`), Resolution, Frame Rate |
| **Shutdown Button** | ⏻ icon bottom-left — 5-second long-press with countdown, then stops motors + `sudo shutdown -h now` |
| **Stats Button** | ℹ icon bottom-right — opens overlay polling `/api/stats` every 5s |
| **Reconnection Overlay** | Full-screen dimmed overlay with CSS animated dots when connection lost |
| **Landscape Layout** | CSS media query rearranges to video-left / joystick-right layout on phones in landscape |

**JavaScript Architecture:**

```
index.html JS
│
├── SocketIO Client (v4.7.5, served from /static/socket.io.min.js, polling transport)
│   ├── connect → emit 'connect', receive status + camera_status
│   ├── set_speed → send motor speed update
│   ├── set_speeds → send bulk speed update (throttled 50ms)
│   ├── stop → emergency stop
│   ├── disconnect → show reconnection overlay
│   └── reconnect → hide overlay, reload camera image
│
├── Joystick Engine
│   ├── Touch/mouse event handlers
│   ├── computeMotorSpeeds(x, y) → differential steering
│   ├── Chase animation with requestAnimationFrame
│   ├── Auto-center return when toggle is on
│   └── Speed scaling via Speed Limiter slider
│
├── Settings Persistence
│   ├── On connect: GET /api/settings, apply saved values
│   ├── On slider change: POST /api/settings to save
│   └── Applies joystick_speed + max_speed_limiter
│
├── Frame Rate Control
│   ├── Slider 0–60 → POST /api/set_framerate
│   ├── 0 = camera off, 1–59 = value FPS, 60 = Uncapped
│   └── FPS counter polls /api/camera_status every 2s
│
├── Resolution Control
│   ├── Slider 0–4 → POST /api/set_resolution (async)
│   └── Cache-busting via Date.now() query param
│
├── Stats Overlay
│   ├── ℹ button opens overlay polling /api/stats every 5s
│   └── Shows memory, load, net TX/RX, uptime, temperature
│
└── Shutdown Logic
    ├── mousedown/mousedown → start 5s countdown
    ├── Visual feedback (orange .holding) + countdown text
    └── Release aborts → POST /api/shutdown (stops motors + poweroff)
```

**Chase Animation:**

All joystick movement (grab, drag, return-to-center) uses constant-speed per-frame animation:

```
MIN_SPEED = 1
MAX_SPEED = 15
per_frame_speed = MIN_SPEED + (MAX_SPEED - MIN_SPEED) * (sliderValue / 100)
```

Each animation frame moves the knob `per_frame_speed` pixels toward the target, giving smooth, predictable motion regardless of frame rate.

**Responsive Design:**

| Orientation | Layout |
|---|---|
| Portrait (default) | Camera on top, joystick center, controls below |
| Landscape (max-height ≤ 500px) | Video left (flex: 1), right column with semi-transparent joystick + controls |

---

## API Reference

### HTTP Endpoints

| Endpoint | Method | Description | Request | Response |
|---|---|---|---|---|---|
| `/` | GET | Serve dashboard HTML | — | `text/html` |
| `/video_feed` | GET | MJPEG camera stream | — | `multipart/x-mixed-replace` |
| `/api/camera_status` | GET | Camera availability + FPS | — | `{"available": bool, "fps": float}` |
| `/api/settings` | GET | Load saved settings | — | `{"joystick_speed": int, "max_speed_limiter": int, "resolution_index": int, "fps": int}` |
| `/api/settings` | POST | Save joystick_speed + max_speed_limiter | `{"joystick_speed": 70, "max_speed_limiter": 50}` | `{"ok": true}` |
| `/api/set_resolution` | POST | Change camera resolution | `{"index": 0-4}` | `{"ok": true, "resolution": [w, h]}` |
| `/api/set_framerate` | POST | Change camera framerate (0=off) | `{"fps": 9}` | `{"ok": true, "fps": 9}` |
| `/api/shutdown` | POST | Stop motors + shutdown the Pi | — | `{"ok": true}` |
| `/api/stats` | GET | System stats (memory/load/net/uptime/temp) | — | `{"mem": {...}, "load": [...], "net": {...}, "uptime": float, "temp": float}` |
| `/api/debug` | GET | Debug information | — | `{"thread_count": int, "camera_available": bool, ...}` |

**Resolution Index Mapping:**

| Index | Resolution | Aspect Ratio |
|---|---|---|
| 0 | 320 × 240 | 4:3 |
| 1 | 640 × 480 | 4:3 |
| 2 | 800 × 600 | 4:3 |
| 3 | 1024 × 768 | 4:3 |
| 4 | 1280 × 960 | 4:3 |

### SocketIO Events

**Client → Server:**

| Event | Payload | Description |
|---|---|---|
| `connect` | — | Client connects; server responds with `status` and `camera_status` |
| `set_speed` | `{"motor": "FL", "speed": 75}` | Set speed for one motor |
| `set_speeds` | `{"speeds": {"FL": 50, "FR": -30, ...}}` | Bulk set all motor speeds (throttled to 50ms) |
| `stop` | — | Emergency stop all motors |

**Server → Client:**

| Event | Payload | Description |
|---|---|---|
| `status` | `{"speeds": {"FL": 50, ...}}` | Current motor speeds (confirming update) |
| `camera_status` | `{"available": true}` | Camera availability status |

---

## LED Status Patterns

The watchdog drives a standard LED on **GPIO 5** (via sysfs, not pigpio).

| Pattern | Visual | Meaning |
|---|---|---|
| **Solid On** | ● | All services healthy |
| **Slow Blink** | ◐◑◐◑ (1s cycle) | Startup — waiting for all services |
| **Fast Blink** | ◐◑◐◑ (0.4s cycle) | Service failure detected, attempting restart |
| **Fatal Pattern** | ◐◐◐ ___ ◐◐◐ ___ (3×300ms, 2s gap) | Max retries exceeded for a service |
| **Off** | ○ | System off or watchdog not running |

---

## Testing

The project includes **51 unit tests** with full hardware mocking — no Pi required.

```
tests/
├── __init__.py
├── mock_pigpio.py          # Complete pigpio mock
├── test_motor_control.py   # 29 tests — MotorController
├── test_app.py             # 12 tests — Flask + SocketIO
└── test_watchdog.py        # 10 tests — Watchdog
```

**Run tests:**

```bash
cd motor_control
python3 -m pytest tests/ -v
```

**Mock Architecture:**

`mock_pigpio.py` provides a drop-in replacement for the `pigpio` module:
- `OUTPUT`/`INPUT` constants
- `pi()` constructor with configurable `connected` state
- Records all `set_mode`, `write`, `set_PWM_frequency`, `set_PWM_range`, `set_PWM_dutycycle`, `stop` calls
- `_reset()` to clear recorded state between tests
- `set_connected(bool)` / `get_connected()` for simulating pi availability

Tests patch `sys.modules['pigpio']` before importing `motor_control`:

```python
import tests.mock_pigpio as mock_pigpio
sys.modules['pigpio'] = mock_pigpio
from motor_control import MotorController, clamp_speed, validate_motor
```

**Test Coverage:**

| Module | Coverage Highlights |
|---|---|
| `motor_control` | Init, set_speed (fwd/rev/zero/bounds), set_speeds, set_all, stop_all, get_speed, cleanup, error handling, lock/slew/watchdog |
| `app` | HTTP routes, SocketIO events, connection, status emission, stop handling |
| `watchdog` | LED patterns, service_active, restart_service, wait_for_services, fatal handling |

---

## Configuration Reference

### Motor Constants (`motor_control.py`)

```python
PWM_FREQ = 20000      # PWM frequency in Hz
PWM_RANGE = 1000      # PWM resolution (0-1000)

MOTOR_PINS = {
    'FL': {'dir': 6,  'pwm': 12},
    'FR': {'dir': 13, 'pwm': 19},
    'RL': {'dir': 16, 'pwm': 26},
    'RR': {'dir': 20, 'pwm': 21},
}
```

### Watchdog Constants (`watchdog.py`)

```python
LED_GPIO = 5          # Status LED GPIO
POLL_INTERVAL = 2     # Seconds between service checks
RECHECK_DELAY = 5     # Seconds after restart before rechecking
MAX_RETRIES = 3       # Max consecutive failures per service
REQUIRED_SERVICES = ['pigpiod', 'motor_control.service']
```

### Systemd Services

**motor_control.service:**

```ini
Restart=on-failure
RestartSec=2
StartLimitInterval=60
StartLimitBurst=3
```

**watchdog.service:**

```ini
Restart=on-failure
RestartSec=5
```

### Static Assets

- SocketIO client v4.7.5 served from `/home/r2tele/motor_control/static/socket.io.min.js`
- No CDN dependency — works in air-gapped environments (phone hotspot without internet)

---

## Troubleshooting

### pigpio connection refused

```bash
sudo systemctl status pigpiod    # Check if running
sudo systemctl start pigpiod     # Start if stopped
sudo systemctl enable pigpiod    # Enable at boot
```

### Camera not available

```bash
libcamera-hello                   # Test camera hardware
v4l2-ctl --list-devices           # Check V4L2 devices
v4l2-ctl -d /dev/video11 --all    # Verify MJPEG encoder
sudo systemctl restart motor_control.service  # Restart to re-init
```

The camera initializes in a background thread, so it may take 5–15 seconds after the web server starts. The dashboard polls `/api/camera_status` every 3s until the camera is available. Set FPS slider to 0 then back to a value to force re-init from the UI.

### Motor not responding

1. Check wiring: DIR and PWM pins match `motor_control.py` config
2. Verify common ground between Pi, drivers, and battery
3. Check battery voltage (under load, should be ≥ 10V for 12V motors)
4. Test individual motor with `set_speed` SocketIO event (single motor command)
5. Check LED: solid on means watchdog sees all services as active

### Service won't start

```bash
sudo journalctl -u motor_control.service -n 50 --no-pager  # Check logs
sudo journalctl -u watchdog.service -n 50 --no-pager        # Check watchdog
```

### Thread count growing

The `set_speeds` event is throttled client-side to once every 50ms. If you see the thread count growing in the heartbeat log, verify the client-side throttle:

```javascript
// index.html — throttle logic
let last_send = 0;
const THROTTLE_MS = 50;

function sendSpeeds(speeds) {
    const now = Date.now();
    if (now - last_send < THROTTLE_MS) return;
    last_send = now;
    socket.emit('set_speeds', { speeds });
}
```

### Shutdown not working

The shutdown endpoint calls `controller.stop_all()` then runs `sudo shutdown -h now` with password piped via `echo r2tele | sudo -S`. If the password has changed, update `app.py`:

```python
os.system('echo YOUR_PASSWORD | sudo -S shutdown -h now')
```

Alternatively, configure passwordless sudo for the shutdown command:

```bash
echo 'r2tele ALL=(ALL) NOPASSWD: /sbin/shutdown' | sudo tee /etc/sudoers.d/shutdown
```

Then simplify to:

```python
os.system('sudo shutdown -h now')
```

---

## Hardware Wiring Guide

### Step 1: Connect Motor Drivers to Battery

```
Battery (+) ──────► MDD20A-A VM ──────► MDD20A-B VM
Battery (-) ──────► MDD20A-A GND ─────► MDD20A-B GND
                  (common ground)
```

### Step 2: Connect Motors

```
MDD20A-A CH1 (+) ──► FL motor (+)
MDD20A-A CH1 (-) ──► FL motor (-)
MDD20A-A CH2 (+) ──► FR motor (+)
MDD20A-A CH2 (-) ──► FR motor (-)

MDD20A-B CH1 (+) ──► RL motor (+)
MDD20A-B CH1 (-) ──► RL motor (-)
MDD20A-B CH2 (+) ──► RR motor (+)
MDD20A-B CH2 (-) ──► RR motor (-)
```

### Step 3: Connect Signal Wires

```
Pi GPIO 6 (DIR FL)  ──► MDD20A-A CH1 DIR
Pi GPIO 12 (PWM FL) ──► MDD20A-A CH1 PWM
Pi GPIO 13 (DIR FR) ──► MDD20A-A CH2 DIR
Pi GPIO 19 (PWM FR) ──► MDD20A-A CH2 PWM
Pi GPIO 16 (DIR RL) ──► MDD20A-B CH1 DIR
Pi GPIO 26 (PWM RL) ──► MDD20A-B CH1 PWM
Pi GPIO 20 (DIR RR) ──► MDD20A-B CH2 DIR
Pi GPIO 21 (PWM RR) ──► MDD20A-B CH2 PWM
```

### Step 4: Common Ground

```
Pi GND (pin 34) ────► MDD20A-A Logic GND
Pi GND (pin 39) ────► MDD20A-B Logic GND
Battery (-) ────────► MDD20A-A Power GND
Battery (-) ────────► MDD20A-B Power GND
```

> **All grounds must be common** — Pi, both drivers, and battery negative. Missing common ground is the #1 cause of erratic motor behavior.

### Step 5: Status LED

```
GPIO 5 ──┬── 220Ω resistor ──► LED (+)──► GND
         └► (or use a ready-made LED module)
```

### Step 6: Camera

Connect the Pi Camera ribbon cable to the CSI port on the Pi Zero W (latch side toward the board, contacts facing the HDMI port).

### Step 7: Power the Pi

Use a 12V → 5V buck converter from the battery to power the Pi via GPIO pins 2 (5V) and 6 (GND), or through the micro-USB port.

---

## Project Structure

```
r2d2/
├── README.md
├── gen_doc.py
├── R2_Motor_Control_Plan.docx
├── .gitignore
│
├── motor_control/
│   ├── app.py                     # Flask + SocketIO web server, camera management
│   ├── motor_control.py           # 4-motor pigpio abstraction
│   ├── watchdog.py                # sysfs GPIO service monitor + LED patterns
│   ├── motor_control.service      # systemd unit for web server
│   ├── watchdog.service           # systemd unit for watchdog
│   ├── deploy.py                  # Python deployment script (pexpect)
│   ├── deploy.sh                  # Bash deployment script (scp + ssh)
│   │
│   ├── templates/
│   │   └── index.html             # Dashboard SPA (856 lines)
│   │
│   ├── static/
│   │   └── socket.io.min.js       # SocketIO client v4.7.5 (served locally)
│   │
│   └── tests/
│       ├── __init__.py            # Package marker
│       ├── mock_pigpio.py         # Complete pigpio mock for testing
│       ├── test_motor_control.py  # 29 tests
│       ├── test_app.py            # 12 tests
│       └── test_watchdog.py       # 10 tests
│
└── (other project files)
```

---

## License

MIT License — see LICENSE file if present, or use freely.

---

*R2 Motor Controller v2.1 — Built for Raspberry Pi Zero W with Cytron MDD20A drivers and MY6812 motors.*
