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

### 1. Prerequisites (socketcan)

SignalK's N2K provider requires the `socketcan` Node.js native module:

```bash
# Verify socketcan loads
node -e "const s = require('/usr/lib/node_modules/signalk-server/node_modules/socketcan'); console.log('OK:', Object.keys(s));"
```

If it fails with `Module did not self-register`, rebuild it:

```bash
sudo npm rebuild --prefix /usr/lib/node_modules/signalk-server/node_modules/socketcan
```

> **Important:** Every time Node.js is updated, socketcan must be rebuilt (`sudo npm rebuild`).

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

```bash
# Check frames on vcan0
candump -x vcan0

# Check that SignalK sees the N2K source
curl -s http://localhost:3000/signalk/v1/api/sources | python3 -m json.tool
# Look for "N2K" in the sources list

# Check NMEA 2000 data
curl -s http://localhost:3000/signalk/v1/api/ | python3 -m json.tool
```

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
