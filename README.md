# ap09

CLI tool for Linux to talk to the **Ammoon AP-09 nano looper** (USB ID
`0416:5555`, Rowin OEM chip) over USB, aimed at importing/exporting recorded
loops. Written from scratch because the pedal's actual wire protocol
(USB-MIDI SysEx) is different from what the existing
[loopertrx](https://github.com/reinerh/loopertrx) tool implements (a
mass-storage-like protocol used by a different OEM chip/pedal family).

Status: early. `probe`/`list` work against real hardware. `rx`/`tx` for full
loop transfer are not implemented yet — the SysEx command set for
size/read/write hasn't been fully reverse-engineered. See `PROTOCOL.md`.

## Requirements

- Python 3, `pyusb` (`pip install pyusb` or `apt install python3-usb`).
- Root, or a udev rule granting your user access to `0416:5555` (none
  configured yet in this repo).

## Usage

```
sudo python3 ap09.py list
sudo python3 ap09.py probe "F0 00 32 45 00 00 00 40 7f F7"
```

`probe` sends a raw SysEx message (hex, must start `F0` and end `F7`) and
prints the raw reply — the main tool for continuing protocol
reverse-engineering.
