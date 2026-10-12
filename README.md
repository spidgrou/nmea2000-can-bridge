# NMEA 2000 → CAN Bridge for SignalK

A Python bridge that reads NMEA 2000 frames from a **Waveshare USB-CAN-A** adapter (CH340 serial, 2M baud) and forwards them to a **virtual CAN interface** (`vcan0`) for consumption by SignalK's N2K provider (`canbus-canboatjs`).

## Quick Start

```bash
# 1. Create virtual CAN interface
sudo ip link add vcan0 type vcan
sudo ip link set vcan0 up

# 2. Run the bridge
python3 n2k_bridge_v2.py
```

## Hardware

### Tested Adapter: Waveshare USB-CAN-A

- **Product**: [Waveshare USB-CAN-A](https://www.waveshare.com/usb-can-a.htm) (SKU 23635)
- **Chipset**: STM32F103 + CH340G USB-serial (VID `1a86`, PID `7523`)
- **Serial baud rate**: 2,000,000 bps (default, factory setting)
- **CAN baud rate**: Configurable 5 kbps – 1 Mbps
- **Standards**: CAN 2.0A (11-bit ID) and CAN 2.0B (29-bit ID)
- **Connection**: CAN-H to CAN-H, CAN-L to CAN-L; 120 Ω termination enabled

> **Note:** This bridge was developed and tested specifically with the Waveshare USB-CAN-A. Other USB-to-CAN adapters may use different serial protocols — check your adapter's frame format before using this bridge.

### NMEA 2000 Network Connection

```
NMEA 2000 Bus ──┬── CAN-H ──── CAN-H (USB-CAN-A)
                └── CAN-L ──── CAN-L (USB-CAN-A)
                     └── USB ──→ /dev/ttyNMEA2000 (CH340)
```

### Serial Device Setup (Linux)

The adapter appears as `/dev/ttyUSB0` (or `/dev/ttyNMEA2000` via udev rule):

```bash
# Verify the device
lsusb | grep -i "1a86\|ch340\|ch341"
# Should show: ID 1a86:7523 QinHeng Electronics CH340 serial converter

# Check the serial port
ls -l /dev/ttyUSB*
```

If the CH340 driver doesn't load automatically:

```bash
sudo modprobe option
sudo sh -c 'echo "1a86 7523" > /sys/bus/usb-serial/drivers/option1/new_id'
```

#### udev Rule (persistent naming)

Create `/etc/udev/rules.d/99-n2k.rules`:

```
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", SYMLINK+="ttyNMEA2000"
```

Then reload:

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

## Protocol

The Waveshare USB-CAN-A uses a **variable-length serial protocol** over USB virtual COM:

| Byte(s) | Field | Description |
|---------|-------|-------------|
| 0 | Start marker | `0xAA` |
| 1 | Control byte | `bit7` = extended frame, `bit4` = data/remote, `bits 0–3` = DLC |
| 2–5 | CAN ID | 29-bit ID in **little-endian** (LSB first) |
| 6–13 | Payload | 0–8 bytes of NMEA 2000 data |
| 14 | End marker | `0x55` |

**Frame size:** 15 bytes (fixed in variable-length mode).

### CAN ID Decoding (29-bit J1939 for N2K)

| Bits | Field |
|------|-------|
| 28–26 | Priority (3 bits) |
| 25–16 | PDU Format (PF) |
| 15–8 | PDU Specific (PS) |
| 7–0 | Source Address |
| — | PGN = `PF << 8` (broadcast) or `(PF << 8) \| PS` (specific) |

## Installation

### Prerequisites

```bash
# Python packages
pip install pyserial python-can

# Virtual CAN interface (kernel module)
sudo modprobe can
sudo modprobe vcan

# Create vcan0 (add to /etc/rc.local or systemd for persistence)
sudo ip link add vcan0 type vcan
sudo ip link set vcan0 up
```

### Clone & Run

```bash
git clone https://github.com/spidgrou/nmea2000-can-bridge.git
cd nmea2000-can-bridge
python3 n2k_bridge_v2.py
```

## SignalK Integration

This bridge is designed to feed NMEA 2000 data into SignalK via its `canbus-canboatjs` provider.

### 1. Prerequisites (native CAN socket addon)

For SocketCAN interfaces (`vcan0`, `can0`), SignalK reads frames through the
`@canboat/canboatjs` library, which ships its **own native addon** — `canSocket.node` —
compiled from C++ during installation. Nothing else is needed.

> ⚠️ **The npm package `socketcan` is no longer used. Do not install it.**
> canboatjs has not depended on it since **3.0 (June 2025)** — the last version to
> use it was 2.12.3 (21 June 2025). Current canboatjs contains no `require('socketcan')`
> anywhere, and `signalk-server/node_modules/socketcan/` is **not supposed to exist**.
> Any guide telling you to `require()` it or `npm rebuild` it is obsolete, including
> older revisions of this README.

**Check the addon is present:**

```bash
ls -l /usr/lib/node_modules/signalk-server/node_modules/@canboat/canboatjs/build/Release/canSocket.node
grep -m1 '"version"' /usr/lib/node_modules/signalk-server/node_modules/@canboat/canboatjs/package.json
```

If the file is there, the Node side is done — skip to step 2.
If it is missing the provider fails with `Failed to load native canSocket module`
and there is **no fallback path**. Build it manually:

```bash
sudo apt install -y build-essential python3
cd /usr/lib/node_modules/signalk-server/node_modules/@canboat/canboatjs
sudo npm run install
```

**Why it can be missing on npm 12+:** npm 12 blocks dependency install scripts by
default (the `allowScripts` policy), and canboatjs builds its addon from a `binding.gyp`
via `node-gyp rebuild`. During a global install there is no project `package.json`, so
the package's own `allowScripts` entry does not apply — the documented mechanism for
global installs is `--allow-scripts`. To install/upgrade SignalK with the addon built:

```bash
sudo npm install -g --allow-scripts=@canboat/canboatjs signalk-server@latest
# and to keep it working on future upgrades:
npm config set allow-scripts=@canboat/canboatjs --location=user
```

**Functional test** — a bare `require()` only proves the JS wrapper loads, so create a
real CAN channel instead (works with the bridge running or stopped):

```bash
node -e "const {CanChannel}=require('/usr/lib/node_modules/signalk-server/node_modules/@canboat/canboatjs/dist/canSocket.js'); const ch=new CanChannel('vcan0'); ch.start(); console.log('CAN OK'); setTimeout(()=>{ch.stop();process.exit(0)},2000)"
```

`CAN OK` means the native layer is working. (Being N-API based, this addon is
ABI-stable: it does **not** need rebuilding every time Node.js is updated, unlike
the old `socketcan` module.)

### 2. SignalK Provider Configuration

Edit `/home/pi/.signalk/settings.json` (or your SignalK config path) and add this pipe element:

```json
{
    "pipeElements": [{
        "type": "providers/simple",
        "options": {
            "type": "NMEA2000",
            "subOptions": {
                "type": "canbus-canboatjs",
                "interface": "vcan0",
                "uniqueNumber": 1199691,
                "useCamelCompat": false
            }
        }
    }],
    "id": "N2K",
    "enabled": true
}
```

### 3. Verify Data Flow

Verify from the bottom up — if `vcan0` is empty there is nothing for SignalK to read,
and the problem is upstream of the provider.

```bash
# Step 0 — the bridge and vcan0: are frames reaching the virtual interface?
ip link show vcan0          # must exist and be UP
candump -x vcan0            # frames from the bridge (apt install can-utils)
# If this is empty, fix the bridge first — SignalK cannot do anything about it.

# Check that SignalK sees the N2K source
curl -s http://localhost:3000/signalk/v1/api/sources | python3 -m json.tool
# Look for "N2K" in the sources list

# Check NMEA 2000 data
curl -s http://localhost:3000/signalk/v1/api/ | python3 -m json.tool
```

If the source is missing, look at the provider's own error first:

```bash
journalctl -u signalk --no-pager | grep -i "canSocket\|unable to open can\|socketcan" | tail
```

### 4. Troubleshooting

**`Cannot find module '.../signalk-server/node_modules/socketcan'`**
Nothing is broken. That module is obsolete (see step 1) and it is normal for the
folder not to exist. The only case where `socketcan` is still required is
canboatjs 2.x — check the version as shown in step 1: if it reports `2.x`, your
SignalK predates June 2025 and is better off updated to a current release.

**`Cannot find module '../build/Release/can.node'`**
This is the signature of a hand-installed `socketcan` whose install script was blocked
by npm (npm 12 blocks dependency install scripts by default), so the module was left
without its compiled binary. Remove it — it cannot help, because canboatjs never loads it:

```bash
sudo rm -rf /usr/lib/node_modules/signalk-server/node_modules/socketcan
```

Installing `socketcan` by hand inside SignalK's `node_modules` also drags in hundreds of
unrelated packages. Avoid it.

**`Failed to load native canSocket module`**
The native addon in step 1 is missing or not loadable — build it as shown there
(`sudo npm run install` inside the `@canboat/canboatjs` folder, then restart SignalK).
On npm 12+, a fresh `sudo npm install -g signalk-server` skips that build unless
`--allow-scripts=@canboat/canboatjs` is passed.

**`candump` reports `vcan0: No such device`**
The virtual interface was not created (or was lost on reboot): `sudo ip link add vcan0
type vcan && sudo ip link set vcan0 up`, and make it persistent — see the systemd unit
below or add the two commands to `/etc/rc.local`.

## Running as a Service (systemd)

Create `/etc/systemd/system/n2k-bridge.service`:

```ini
[Unit]
Description=NMEA 2000 to vCAN Bridge
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/n2k_bridge_v2.py
Restart=always
RestartSec=5
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable n2k-bridge.service
sudo systemctl start n2k-bridge.service
```

## Testing

Run the offline parser test (no hardware required):

```bash
python3 test_parser.py
```

This validates:
- Real N2K frame parsing (6 frames → 6 valid)
- Dirty data with false `0xAA` markers → 1 valid, 1 invalid
- Invalid DLC (15 > 8) → correctly rejected
- Partial frames spanning multiple reads → buffered until complete

## Why This Bridge?

As of **May 1, 2026**, a thorough search of the SignalK ecosystem and all of GitHub confirms:

**No existing project bridges the Waveshare USB-CAN-A to a virtual CAN interface for SignalK.**

### What SignalK supports natively

SignalK's NMEA 2000 provider (`canbus-canboatjs`) only works with:
- **SocketCAN interfaces** (`can0`, `vcan0` — PiCAN-M, MCP2515 hats, etc.)
- **Serial protocol gateways**: Actisense NGT-1/W2K-1, Yacht Devices YDWG-02, Digital Yacht iKonvert, MiniPlex-3-N2K

The Waveshare USB-CAN-A uses a **proprietary 15-byte serial frame protocol** (0xAA/0x55 markers at 2M baud) that doesn't match any of these formats — so SignalK cannot use it directly.

### What others do differently

| Project | Approach | Output | SignalK compatible? |
|---------|----------|--------|-------------------|
| **tomer-w/nmea2000** | Reads Waveshare, decodes N2K in Python | **JSON** | No (bypasses canboatjs) |
| **Python-Class-for-Waveshare-USB-CAN-A** | Raw CAN serial driver | Raw frames | No |
| **canusbpy** | Serial CAN dump | Terminal output | No |
| **Our bridge** | Waveshare serial → vcan0 | **SocketCAN** | ✅ Yes |

### What makes this project unique

Our bridge acts as a **translation layer**: it reads the Waveshare's proprietary serial protocol and writes standard CAN frames onto `vcan0`. SignalK's existing canboatjs provider then consumes them exactly as if they came from a physical CAN interface — **no modifications to SignalK required**.

This means you can use a **$18 Waveshare USB-CAN-A** instead of a **€200+ Actisense NGT-1** and get the same result in SignalK.

## Architecture

```
NMEA 2000 Bus
      │
      ▼
Waveshare USB-CAN-A (CH340, 2M baud)
      │
      ▼
/dev/ttyNMEA2000
      │
      ▼
n2k_bridge_v2.py  ←  parses 15-byte frames, validates, forwards
      │
      ▼
vcan0 (Virtual CAN interface)
      │
      ▼
SignalK (canbus-canboatjs provider)
      │
      ▼
SignalK JSON API (port 3000)
```

## License

MIT — see [LICENSE](LICENSE).
