#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
n2k_bridge_v2.py  —  NMEA 2000 → virtual CAN bridge (version 2.0)
--------------------------------------------------------------------
Reads NMEA 2000 frames from a serial port (CH340/ch341 USB adapter)
and forwards them to a virtual CAN interface (vcan0) for SignalK.

Improvements over v1:
  - Fast sync: scan forward for 0xAA instead of pop(1) per frame
  - Frame validation: DLC ≤ 8, consistent frame structure
  - Timeout on stuck buffer: automatic reset if full of garbage
  - Periodic logging: valid / invalid / dropped frame counts
  - Signal handling: clean shutdown with Ctrl+C
  - Verbose mode for debugging
  - Protection against false AA/55 in data

Adapted for Y2K (Max) — CH340 at 2,000,000 baud.
"""

import serial
import can
import time
import signal
import sys
import os

# ============================================================
# CONFIGURATION
# ============================================================
SERIAL_PORT = '/dev/ttyNMEA2000'
SERIAL_BAUD = 2000000
CAN_INTERFACE = 'vcan0'
FRAME_SIZE = 15          # Each frame is exactly 15 bytes
START_MARKER = 0xAA
END_MARKER = 0x55
MAX_BUFFER_SIZE = 15000  # Reset buffer if it exceeds this threshold
CLEAR_TIMEOUT = 10.0     # Reset buffer if no valid frame seen for X seconds
LOG_INTERVAL = 60        # Print statistics every X seconds
VERBOSE = True           # Print every frame to stdout

# ============================================================
# STATISTICS
# ============================================================
class Stats:
    def __init__(self):
        self.total_raw = 0       # bytes read from serial
        self.frames_valid = 0    # N2K frames forwarded to CAN
        self.frames_invalid = 0  # frames discarded (bad DLC, wrong markers, etc.)
        self.frames_dropped = 0  # lost due to buffer full
        self.sync_resets = 0     # buffer cleared due to sync loss
        self.errors = 0          # total exceptions
        self._last_log = time.monotonic()
        self._start = time.monotonic()

    def log(self):
        uptime = time.monotonic() - self._start
        print(f"\n--- STATS (uptime {uptime:.0f}s) ---")
        print(f"  Valid frames:     {self.frames_valid}")
        print(f"  Invalid frames:   {self.frames_invalid}")
        print(f"  Dropped frames:   {self.frames_dropped}")
        print(f"  Sync resets:      {self.sync_resets}")
        print(f"  Errors:           {self.errors}")
        print(f"  Bytes read:       {self.total_raw}")
        print(f"  Avg speed:        {self.frames_valid / uptime:.1f} frame/s")
        print("-----------------------------\n")
        self._last_log = time.monotonic()

    def maybe_log(self):
        if time.monotonic() - self._last_log >= LOG_INTERVAL:
            self.log()


# ============================================================
# FRAME VALIDATION
# ============================================================
def validate_frame(frame, stats):
    """Verify that a 15-byte frame is a valid N2K frame."""
    if len(frame) != FRAME_SIZE:
        stats.frames_invalid += 1
        return False

    # Check start/end markers
    if frame[0] != START_MARKER or frame[14] != END_MARKER:
        stats.frames_invalid += 1
        return False

    # Check DLC (lower nibble of control byte)
    ctrl = frame[1]
    dlc = ctrl & 0x0F
    if dlc > 8:
        # Invalid DLC for N2K
        stats.frames_invalid += 1
        if VERBOSE:
            print(f"  [VALID] Frame discarded: DLC={dlc} > 8")
        return False

    return True


def extract_can_message(frame):
    """Extract a can.Message from a raw 15-byte frame."""
    ctrl = frame[1]
    is_extended = bool(ctrl & 0x80)
    dlc = ctrl & 0x0F

    # CAN ID in little-endian (4 bytes), masked to 29 bits
    can_id = int.from_bytes(frame[2:6], byteorder='little')
    can_id &= 0x1FFFFFFF

    # Payload (max 8 bytes)
    data = frame[6:6 + dlc]

    return can.Message(
        arbitration_id=can_id,
        data=data,
        is_extended_id=is_extended,
        check=True  # let python-can validate it
    )


# ============================================================
# BUFFER SCAN — FIND THE NEXT VALID FRAME
# ============================================================
def scan_buffer(buffer, stats):
    """
    Scan the buffer for valid frames.
    Returns the buffer with extracted frames removed.
    Much more efficient than pop(1) per iteration.
    """
    consumed = 0
    while consumed < len(buffer):
        # Must keep at least FRAME_SIZE bytes
        remaining = len(buffer) - consumed
        if remaining < FRAME_SIZE:
            break

        # Look for START_MARKER
        if buffer[consumed] != START_MARKER:
            consumed += 1
            continue

        # Check if we have a complete frame
        if buffer[consumed + FRAME_SIZE - 1] == END_MARKER:
            # Candidate frame — extract it
            candidate = buffer[consumed:consumed + FRAME_SIZE]

            if validate_frame(candidate, stats):
                try:
                    msg = extract_can_message(candidate)
                    yield msg
                except Exception as e:
                    stats.frames_invalid += 1
                    if VERBOSE:
                        print(f"  [VALID] Extraction error: {e}")
            else:
                # False AA — skip this byte and continue scan
                consumed += 1
                continue

            # Frame processed — skip ahead by FRAME_SIZE
            consumed += FRAME_SIZE
        else:
            # Found AA but no 55 at +14 — could be:
            # 1) Incomplete frame (waiting for more data)
            # 2) False AA (random byte in data)
            # Distinguish: if another valid AA appears within
            # the next FRAME_SIZE-1 bytes, it's likely false.
            # For safety: skip and look for another valid AA.
            # Look for a valid AA further ahead (with 55 at +14)
            found_valid = False
            scan_pos = consumed + 1
            while scan_pos + FRAME_SIZE <= len(buffer):
                if (buffer[scan_pos] == START_MARKER and
                    buffer[scan_pos + FRAME_SIZE - 1] == END_MARKER):
                    # Found a real frame further ahead
                    # The first AA was a false positive
                    if VERBOSE:
                        print(f"  [SYNC] False AA at offset {consumed}, "
                              f"found valid frame at {scan_pos}")
                    stats.frames_invalid += 1
                    found_valid = True
                    consumed = scan_pos
                    break
                scan_pos += 1

            if not found_valid:
                # No valid frame found — keep the buffer
                # and wait for more data (break from main while)
                break

    # Remove processed bytes
    if consumed > 0:
        del buffer[:consumed]

    return buffer


# ============================================================
# MAIN LOOP
# ============================================================
running = True

def signal_handler(sig, frame):
    global running
    print("\nShutdown signal received. Closing...")
    running = False


def main():
    global running
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    stats = Stats()
    buffer = bytearray()
    last_valid_frame = time.monotonic()

    print(f"\n=== N2K Bridge v2.0 ===")
    print(f"Port:      {SERIAL_PORT} @ {SERIAL_BAUD} baud")
    print(f"CAN:       {CAN_INTERFACE}")
    print(f"Verbose:   {VERBOSE}")
    print(f"========================\n")

    # Open ports
    try:
        ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.1)
        print(f"[OK] Serial opened: {ser.portstr}")
    except Exception as e:
        print(f"[ERROR] Cannot open {SERIAL_PORT}: {e}")
        sys.exit(1)

    try:
        bus = can.interface.Bus(CAN_INTERFACE, bustype='socketcan')
        print(f"[OK] CAN bus opened: {CAN_INTERFACE}")
    except Exception as e:
        print(f"[ERROR] Cannot open {CAN_INTERFACE}: {e}")
        ser.close()
        sys.exit(1)

    print("\nBridge started. Waiting for data...\n")

    # Main loop
    while running:
        try:
            # 1. Read data from serial
            waiting = ser.in_waiting
            if waiting > 0:
                data = ser.read(waiting)
                buffer.extend(data)
                stats.total_raw += len(data)

                # Buffer overflow protection
                if len(buffer) > MAX_BUFFER_SIZE:
                    print(f"[WARN] Buffer overflow ({len(buffer)} bytes) — resetting")
                    buffer.clear()
                    stats.sync_resets += 1
                    stats.frames_dropped += 1

                # 2. Process complete frames in the buffer
                for msg in scan_buffer(buffer, stats):
                    bus.send(msg)
                    stats.frames_valid += 1

                    if time.monotonic() - last_valid_frame > CLEAR_TIMEOUT:
                        print(f"[SYNC] Sync recovered after {CLEAR_TIMEOUT}s of silence")
                    last_valid_frame = time.monotonic()

                    if VERBOSE and stats.frames_valid <= 10:
                        pgn = (msg.arbitration_id >> 8) & 0x3FFFF
                        print(f"  [TX] ID=0x{msg.arbitration_id:08X} "
                              f"PGN=0x{pgn:04X} "
                              f"data={msg.data.hex(' ')}")

                # 3. Sync timeout: if no valid frame received for too long
                if (len(buffer) > 0 and
                    time.monotonic() - last_valid_frame > CLEAR_TIMEOUT and
                    running):
                    print(f"[SYNC] Timeout — no valid frame for {CLEAR_TIMEOUT}s. "
                          f"Buffer={len(buffer)} bytes. Resetting.")
                    if VERBOSE and len(buffer) > 0:
                        print(f"  Buffer dump (max 60 bytes): {buffer[:60].hex(' ')}")
                    buffer.clear()
                    stats.sync_resets += 1

                # 4. Periodic logging
                stats.maybe_log()

            else:
                # No data — brief pause to keep CPU low
                time.sleep(0.001)

        except serial.SerialException as e:
            stats.errors += 1
            print(f"[ERROR] Serial: {e}")
            time.sleep(2)
        except can.CanError as e:
            stats.errors += 1
            print(f"[ERROR] CAN: {e}")
            time.sleep(1)
        except Exception as e:
            stats.errors += 1
            if VERBOSE:
                print(f"[ERROR] Generic: {e}", file=sys.stderr)
            time.sleep(2)

    # Clean shutdown
    print("\nShutting down bridge...")
    stats.log()
    ser.close()
    bus.shutdown()
    print("Bridge stopped.")


# ============================================================
if __name__ == "__main__":
    main()
