#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
n2k_bridge_v2.py  —  NMEA 2000 → virtual CAN bridge (versione 2.0)
--------------------------------------------------------------------
Legge frame NMEA 2000 da una porta seriale (adattatore USB CH340/ch341)
e li inoltra su un'interfaccia CAN virtuale (vcan0) per SignalK.

Miglioramenti rispetto a v1:
  - Sync rapido: scan avanti per 0xAA invece di pop(1) per frame
  - Validazione frame: DLC ≤ 8, struttura frame consistente
  - Timeout su buffer bloccato: reset automatico se pieno di spazzatura
  - Logging periodico: conteggio frame validi / invalidi / scartati
  - Gestione segnali: chiusura pulita con Ctrl+C
  - Modalità verbose per debug
  - Protezione contro AA/55 falsi nei dati

Adattato per Y2K (Max) — CH340 a 2.000.000 baud.
"""

import serial
import can
import time
import signal
import sys
import os

# ============================================================
# CONFIGURAZIONE
# ============================================================
SERIAL_PORT = '/dev/ttyNMEA2000'
SERIAL_BAUD = 2000000
CAN_INTERFACE = 'vcan0'
FRAME_SIZE = 15          # Ogni frame è esattamente 15 byte
START_MARKER = 0xAA
END_MARKER = 0x55
MAX_BUFFER_SIZE = 15000  # Se il buffer supera questa soglia, si resetta
CLEAR_TIMEOUT = 10.0     # Se non si vede un frame valido per X secondi, resetta buffer
LOG_INTERVAL = 60        # Stampa statistiche ogni X secondi
VERBOSE = True           # Stampa ogni frame su stdout

# ============================================================
# STATISTICHE
# ============================================================
class Stats:
    def __init__(self):
        self.total_raw = 0       # byte letti dalla seriale
        self.frames_valid = 0    # frame N2K inoltrati a CAN
        self.frames_invalid = 0  # frame scartati (DLC errato, marker fuori posto, ecc.)
        self.frames_dropped = 0  # persi per buffer pieno
        self.sync_resets = 0     # buffer cancellati per perdita sync
        self.errors = 0          # eccezioni totali
        self._last_log = time.monotonic()
        self._start = time.monotonic()

    def log(self):
        uptime = time.monotonic() - self._start
        print(f"\n--- STATS (uptime {uptime:.0f}s) ---")
        print(f"  Frame validi:     {self.frames_valid}")
        print(f"  Frame invalidi:   {self.frames_invalid}")
        print(f"  Frame persi:      {self.frames_dropped}")
        print(f"  Reset sync:       {self.sync_resets}")
        print(f"  Errori:           {self.errors}")
        print(f"  Byte letti:       {self.total_raw}")
        print(f"  Velocità media:   {self.frames_valid / uptime:.1f} frame/s")
        print("-----------------------------\n")
        self._last_log = time.monotonic()

    def maybe_log(self):
        if time.monotonic() - self._last_log >= LOG_INTERVAL:
            self.log()


# ============================================================
# VALIDAZIONE FRAME
# ============================================================
def validate_frame(frame, stats):
    """Verifica che un frame da 15 byte sia un valido frame N2K."""
    if len(frame) != FRAME_SIZE:
        stats.frames_invalid += 1
        return False

    # Controllo marker di inizio/fine
    if frame[0] != START_MARKER or frame[14] != END_MARKER:
        stats.frames_invalid += 1
        return False

    # Controllo DLC (lower nibble del control byte)
    ctrl = frame[1]
    dlc = ctrl & 0x0F
    if dlc > 8:
        # DLC non valido per N2K
        stats.frames_invalid += 1
        if VERBOSE:
            print(f"  [VALID] Frame scartato: DLC={dlc} > 8")
        return False

    return True


def extract_can_message(frame):
    """Estrae un can.Message da un frame grezzo di 15 byte."""
    ctrl = frame[1]
    is_extended = bool(ctrl & 0x80)
    dlc = ctrl & 0x0F

    # CAN ID in little-endian (4 byte), mascherato a 29 bit
    can_id = int.from_bytes(frame[2:6], byteorder='little')
    can_id &= 0x1FFFFFFF

    # Payload (max 8 byte)
    data = frame[6:6 + dlc]

    return can.Message(
        arbitration_id=can_id,
        data=data,
        is_extended_id=is_extended,
        check=True  # lascia che python-can validi
    )


# ============================================================
# SCAN DEL BUFFER — CERCA IL PROSSIMO FRAME VALIDO
# ============================================================
def scan_buffer(buffer, stats):
    """
    Cerca frame validi nel buffer.
    Ritorna il buffer con i frame estratti rimossi.
    Molto più efficiente di pop(1) per iterazione.
    """
    consumed = 0
    while consumed < len(buffer):
        # Deve rimanere almeno FRAME_SIZE byte
        remaining = len(buffer) - consumed
        if remaining < FRAME_SIZE:
            break

        # Cerca lo START_MARKER
        if buffer[consumed] != START_MARKER:
            consumed += 1
            continue

        # Controlla se abbiamo un frame completo
        if buffer[consumed + FRAME_SIZE - 1] == END_MARKER:
            # Frame candidato — estrailo
            candidate = buffer[consumed:consumed + FRAME_SIZE]

            if validate_frame(candidate, stats):
                try:
                    msg = extract_can_message(candidate)
                    yield msg
                except Exception as e:
                    stats.frames_invalid += 1
                    if VERBOSE:
                        print(f"  [VALID] Errore estrazione: {e}")
            else:
                # Falso AA — salta questo byte e continua scan
                consumed += 1
                continue

            # Frame processato — salta avanti di FRAME_SIZE
            consumed += FRAME_SIZE
        else:
            # Trovato AA ma non abbiamo 55 a +14 — potrebbe essere:
            # 1) Frame incompleto (aspettiamo più dati)
            # 2) Falso AA (byte casuale nei dati)
            # Distinguiamo: se nei prossimi FRAME_SIZE-1 byte
            # c'è un altro AA valido, probabilmente è falso.
            # Per sicurezza: salta e cerca altro AA valido.
            # Cerca un AA valido più avanti (con 55 a +14)
            found_valid = False
            scan_pos = consumed + 1
            while scan_pos + FRAME_SIZE <= len(buffer):
                if (buffer[scan_pos] == START_MARKER and
                    buffer[scan_pos + FRAME_SIZE - 1] == END_MARKER):
                    # Trovato un vero frame più avanti
                    # Il primo AA era un falso positivo
                    if VERBOSE:
                        print(f"  [SYNC] Falso AA a offset {consumed}, "
                              f"trovato frame valido a {scan_pos}")
                    stats.frames_invalid += 1
                    found_valid = True
                    consumed = scan_pos
                    break
                scan_pos += 1

            if not found_valid:
                # Nessun frame valido trovato — teniamo il buffer
                # e aspettiamo più dati (break dal while principale)
                break

    # Rimuovi byte già processati
    if consumed > 0:
        del buffer[:consumed]

    return buffer


# ============================================================
# MAIN
# ============================================================
running = True

def signal_handler(sig, frame):
    global running
    print("\nRicevuto segnale di arresto. Chiusura in corso...")
    running = False


def main():
    global running
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    stats = Stats()
    buffer = bytearray()
    last_valid_frame = time.monotonic()

    print(f"\n=== N2K Bridge v2.0 ===")
    print(f"Porta:     {SERIAL_PORT} @ {SERIAL_BAUD} baud")
    print(f"CAN:       {CAN_INTERFACE}")
    print(f"Verbose:   {VERBOSE}")
    print(f"========================\n")

    # Apri porte
    try:
        ser = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.1)
        print(f"[OK] Seriale aperta: {ser.portstr}")
    except Exception as e:
        print(f"[ERRORE] Impossibile aprire {SERIAL_PORT}: {e}")
        sys.exit(1)

    try:
        bus = can.interface.Bus(CAN_INTERFACE, bustype='socketcan')
        print(f"[OK] CAN bus aperto: {CAN_INTERFACE}")
    except Exception as e:
        print(f"[ERRORE] Impossibile aprire {CAN_INTERFACE}: {e}")
        ser.close()
        sys.exit(1)

    print("\nBridge avviato. In attesa dati...\n")

    # Loop principale
    while running:
        try:
            # 1. Leggi dati dalla seriale
            waiting = ser.in_waiting
            if waiting > 0:
                data = ser.read(waiting)
                buffer.extend(data)
                stats.total_raw += len(data)

                # Protezione buffer overflow
                if len(buffer) > MAX_BUFFER_SIZE:
                    print(f"[WARN] Buffer overflow ({len(buffer)} byte) — reset")
                    buffer.clear()
                    stats.sync_resets += 1
                    stats.frames_dropped += 1

                # 2. Processa frame completi nel buffer
                for msg in scan_buffer(buffer, stats):
                    bus.send(msg)
                    stats.frames_valid += 1

                    if time.monotonic() - last_valid_frame > CLEAR_TIMEOUT:
                        print(f"[SYNC] Recuperata sync dopo {CLEAR_TIMEOUT}s di silenzio")
                    last_valid_frame = time.monotonic()

                    if VERBOSE and stats.frames_valid <= 10:
                        pgn = (msg.arbitration_id >> 8) & 0x3FFFF
                        print(f"  [TX] ID=0x{msg.arbitration_id:08X} "
                              f"PGN=0x{pgn:04X} "
                              f"data={msg.data.hex(' ')}")

                # 3. Timeout sync: se non riceviamo frame validi da troppo tempo
                if (len(buffer) > 0 and
                    time.monotonic() - last_valid_frame > CLEAR_TIMEOUT and
                    running):
                    print(f"[SYNC] Timeout — nessun frame valido per {CLEAR_TIMEOUT}s. "
                          f"Buffer={len(buffer)} byte. Reset.")
                    if VERBOSE and len(buffer) > 0:
                        print(f"  Buffer dump (max 60 byte): {buffer[:60].hex(' ')}")
                    buffer.clear()
                    stats.sync_resets += 1

                # 4. Log periodico
                stats.maybe_log()

            else:
                # Nessun dato — breve pausa per tenere CPU bassa
                time.sleep(0.001)

        except serial.SerialException as e:
            stats.errors += 1
            print(f"[ERRORE] Seriale: {e}")
            time.sleep(2)
        except can.CanError as e:
            stats.errors += 1
            print(f"[ERRORE] CAN: {e}")
            time.sleep(1)
        except Exception as e:
            stats.errors += 1
            if VERBOSE:
                print(f"[ERRORE] Generico: {e}", file=sys.stderr)
            time.sleep(2)

    # Chiusura pulita
    print("\nChiusura bridge...")
    stats.log()
    ser.close()
    bus.shutdown()
    print("Bridge fermato.")


# ============================================================
if __name__ == "__main__":
    main()
