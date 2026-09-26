# AP-09 USB protocol notes

Device: Ammoon AP-09 nano looper, OEM chip by Rowin. USB ID `0416:5555`.

## Transport

- USB class Audio / MIDIStreaming (interface 1, `iInterface` = "DFU-Midi").
- Bulk endpoints: `0x81` IN, `0x02` OUT, 64-byte max packet.
- Despite being a MIDIStreaming interface, the host must talk to it at the
  **USB-MIDI Event Packet** level (4-byte packets: 1 header byte
  `(cable<<4)|CIN`, 3 data bytes), not raw bulk bytes. Sending a bare SysEx
  byte stream directly (no USB-MIDI framing) times out with no reply.
- Only cable number 0 is used. Only SysEx (`F0`...`F7`) has been observed;
  CINs used: `0x4` (start/continue, 3 bytes), `0x5`/`0x6`/`0x7` (end with
  1/2/3 bytes).
- Confirmed empirically: wrapping/unwrapping via `encode_usb_midi` /
  `decode_usb_midi` in `ap09.py` round-trips correctly against real hardware.

## Known command

Manufacturer ID bytes: `00 32 45` (Rowin).

```
> F0 00 32 45 00 00 00 40 7F F7
< F0 00 32 45 58 01 00 40 30 62 46 11 2b 66 6c 19 34 61 44 0d 23 56 4c 59
  33 68 02 00 28 20 62 04 0a 15 2c 5c 40 11 23 01 F7
```

Source: reported in
[reinerh/loopertrx#6](https://github.com/reinerh/loopertrx/issues/6),
reproduced in this repo via `python3 ap09.py probe "F0 00 32 45 00 00 00 40 7f F7"`.

Meaning of the command/response bytes is **not yet decoded**. Response length
(41 bytes) and content likely depend on looper state (whether a loop is
recorded, its length, etc.) — needs testing with pedal in different states
(empty vs. holding a recording of known length) to isolate which bytes are a
size/status field.

## Not yet known

- How to query recorded-audio size specifically (as opposed to whatever this
  status/handshake command returns).
- How to request/receive audio data in chunks (equivalent of `loopertrx.py`'s
  `get_data`/`send_data` for the mass-storage protocol).
- Whether audio is SysEx-encoded 7-bit-safe (typical MIDI SysEx payloads
  avoid bytes >= 0x80 mid-message, often via 7-to-8 bit packing) — this
  matters a lot for how a 24-bit/48kHz WAV payload would be transferred.
- Any handshake/session bytes (tags, sequence numbers) analogous to the
  mass-storage protocol's random `tag` field.

## How to keep reverse-engineering this

- Use `python3 ap09.py probe "<hex sysex>"` to try candidate commands and
  inspect raw replies.
- Cross-check against the official Windows tool by capturing USB traffic
  (e.g. Wireshark + usbmon on a Linux host, or USBPcap on Windows) while
  using the real transfer software, to see the actual command set for
  querying size and streaming data.
- `amidi -p hw:X,0,0 -S "<hex>" -r out.syx -t <seconds>` is a working manual
  alternative for sending/receiving one SysEx message via ALSA rawmidi,
  useful to cross check `ap09.py` behavior (find the right `hw:X,0,0` via
  `amidi -l`).
