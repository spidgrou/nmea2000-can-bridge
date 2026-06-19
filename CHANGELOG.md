# Changelog

## [2.1.0] — 2026-06-19

### Added
- **Auto-recovery on serial I/O errors**: after 20 consecutive errors (e.g. CH340 USB disconnect/reconnect), the bridge automatically closes and re-opens the serial port, restoring normal operation without manual intervention.
- New config constant `MAX_CONSECUTIVE_ERRORS` (default: 20) to control the threshold.

### Changed
- Incremented version from 2.0 to 2.1.
- All three exception handlers (SerialException, CanError, generic) now track consecutive errors and trigger recovery.

## [2.0.0] — 2026-05-02

Complete rewrite of the NMEA 2000 → CAN bridge.

### Improvements over v1
- Fast sync: scan forward for start marker instead of pop(1) per frame
- Frame validation: DLC ≤ 8 checked, false AA/55 markers detected
- Timeout-based buffer reset: auto-clear after 10s without valid frames
- Periodic logging: valid/invalid/dropped frame counts every 60s
- Signal handling: clean shutdown on SIGINT/SIGTERM
- Robust: tested 51+ hours continuous, ~17.5 frame/s, 3.24M+ valid frames, zero errors
- All code comments and log output in English
- MIT License

## [1.0.0] — 2026-04-XX

Initial bridge implementation (v1). Serial → CAN forwarding with basic sync logic.
