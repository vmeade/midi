#!/usr/bin/env python3
"""
miditxt.py - convert MIDI files to a plain-text format and back.
No dependencies (Python 3.7+ standard library only).

Usage (Windows):
    python miditxt.py song.mid            -> writes song.txt
    python miditxt.py song.txt            -> writes song.mid
    python miditxt.py a.mid b.mid c.txt   -> converts each by extension
    Or drag-and-drop files onto the script.

Text format (one event per line, everything hex except delta):
    MIDITXT 1
    format 1
    division 480
    track
    0 FF 51 07A120          # meta: FF <type> <payload hex>
    0 90 3C 64              # channel message: raw bytes   # note_on ch0 C4 vel100
    480 80 3C 40
    0 FF 2F                 # end of track
    track
    ...
    - 'delta' is ticks since previous event (decimal).
    - Anything after '#' is a comment and ignored when converting back.
    - Meta/sysex lengths are computed automatically, so you can edit freely.
    - Running status is expanded (musically identical, not byte-identical).
"""
import struct
import sys
from pathlib import Path

NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(n):
    return f"{NOTES[n % 12]}{n // 12 - 1}"


def read_vlq(data, pos):
    value = 0
    while True:
        b = data[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, pos


def write_vlq(value):
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


# ---------------------------------------------------------------- MIDI -> TXT

def comment_for(status, payload):
    if status == 0xFF:
        mtype, body = payload[0], payload[1:]
        if mtype in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07) and body:
            return "text: " + body.decode("latin-1", "replace")
        if mtype == 0x51 and len(body) == 3:
            return f"tempo {60_000_000 / int.from_bytes(body, 'big'):.2f} bpm"
        if mtype == 0x58 and len(body) >= 2:
            return f"time sig {body[0]}/{2 ** body[1]}"
        if mtype == 0x2F:
            return "end of track"
        return ""
    hi, ch = status & 0xF0, status & 0x0F
    if hi == 0x90:
        return f"note_on ch{ch} {note_name(payload[0])} vel{payload[1]}" if payload[1] else f"note_off ch{ch} {note_name(payload[0])}"
    if hi == 0x80:
        return f"note_off ch{ch} {note_name(payload[0])}"
    if hi == 0xB0:
        return f"control ch{ch} cc{payload[0]}={payload[1]}"
    if hi == 0xC0:
        return f"program ch{ch} {payload[0]}"
    if hi == 0xE0:
        return f"pitchbend ch{ch} {(payload[1] << 7 | payload[0]) - 8192}"
    return ""


def midi_to_text(data):
    if data[:4] != b"MThd":
        raise ValueError("Not a MIDI file (missing MThd)")
    hlen = struct.unpack(">I", data[4:8])[0]
    fmt, _ntrk, div = struct.unpack(">HHH", data[8:14])
    pos = 8 + hlen
    lines = ["MIDITXT 1", f"format {fmt}", f"division {div}"]

    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        clen = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        chunk = data[pos + 8:pos + 8 + clen]
        pos += 8 + clen
        if cid != b"MTrk":
            continue
        lines.append("track")
        p, running = 0, None
        while p < len(chunk):
            delta, p = read_vlq(chunk, p)
            b = chunk[p]
            if b & 0x80:
                status = b
                p += 1
            else:
                status = running  # running status
                if status is None:
                    raise ValueError("Bad running status")
            if status == 0xFF:
                mtype = chunk[p]
                ln, p = read_vlq(chunk, p + 1)
                body = chunk[p:p + ln]
                p += ln
                payload = bytes([mtype]) + body
                line = f"{delta} FF {mtype:02X}" + (f" {body.hex().upper()}" if body else "")
            elif status in (0xF0, 0xF7):
                ln, p = read_vlq(chunk, p)
                body = chunk[p:p + ln]
                p += ln
                payload = body
                line = f"{delta} {status:02X}" + (f" {body.hex().upper()}" if body else "")
                running = None
            else:
                n = 1 if (status & 0xF0) in (0xC0, 0xD0) else 2
                payload = chunk[p:p + n]
                p += n
                running = status
                line = f"{delta} {status:02X} " + " ".join(f"{x:02X}" for x in payload)
            c = comment_for(status, payload)
            lines.append(f"{line:<28}# {c}" if c else line)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- TXT -> MIDI

def text_to_midi(text):
    fmt, div, tracks, cur = 1, 480, [], None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tok = line.split()
        if tok[0] == "MIDITXT":
            continue
        if tok[0] == "format":
            fmt = int(tok[1])
        elif tok[0] == "division":
            div = int(tok[1])
        elif tok[0] == "track":
            cur = bytearray()
            tracks.append(cur)
        else:
            if cur is None:
                raise ValueError("Event before first 'track' line")
            cur += write_vlq(int(tok[0]))
            status = int(tok[1], 16)
            if status == 0xFF:
                mtype = int(tok[2], 16)
                body = bytes.fromhex("".join(tok[3:]))
                cur += bytes([0xFF, mtype]) + write_vlq(len(body)) + body
            elif status in (0xF0, 0xF7):
                body = bytes.fromhex("".join(tok[2:]))
                cur += bytes([status]) + write_vlq(len(body)) + body
            else:
                cur += bytes([status] + [int(x, 16) for x in tok[2:]])
    out = bytearray(b"MThd" + struct.pack(">IHHH", 6, fmt, len(tracks), div))
    for t in tracks:
        out += b"MTrk" + struct.pack(">I", len(t)) + bytes(t)
    return bytes(out)


# ------------------------------------------------------------------------ CLI

def convert(path):
    src = Path(path)
    ext = src.suffix.lower()
    if ext in (".mid", ".midi"):
        dst = src.with_suffix(".txt")
        dst.write_text(midi_to_text(src.read_bytes()), encoding="utf-8")
    elif ext == ".txt":
        dst = src.with_suffix(".mid")
        dst.write_bytes(text_to_midi(src.read_text(encoding="utf-8")))
    else:
        raise ValueError("expected .mid, .midi or .txt")
    print(f"{src.name} -> {dst.name}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        input("Press Enter to exit...")
        return
    for f in sys.argv[1:]:
        try:
            convert(f)
        except Exception as e:
            print(f"ERROR {f}: {e}")


if __name__ == "__main__":
    main()