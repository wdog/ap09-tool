# ap09

Linux CLI tool to import/export loops from the **Ammoon AP-09 nano looper**
(USB ID `0416:5555`, OEM chip by Rowin, USB product string "DFU").

Status: **work in progress**. Transport, framing, and the memory-read command are
reverse-engineered and verified on real hardware. The memory layout (where the loop
audio sits, its length) and the write/upload command are not done yet.

- `ap09.py` — current tool (`list`, `probe`).
- `PROTOCOL.md` — older notes. Where it disagrees with this README, **this README is
  correct**. Its "Known command" section predates the 7-bit discovery and reads the
  bytes wrongly.

## Requirements

- Python 3 and `pyusb` (`apt install python3-usb`).
- Root access, or a udev rule for `0416:5555` (not set up yet).

```
sudo python3 ap09.py list
sudo python3 ap09.py probe "F0 00 32 45 00 00 00 40 7f F7"
```

## How this was reverse-engineered

The official Windows tool is "LooperSuite V1.7" (`Looper Software.exe`, a Qt5 app
using RtMidi / WinMM MIDI). The installer came from
`Looper+software+1.7.exe` (see rowinmusic.com). It installs into
`C:\Program Files (x86)\Looper software\`. It crashes under Wine (wow64), so the
protocol was taken from static disassembly (`objdump -d`) of `Looper Software.exe`.
Addresses below refer to that binary (image base 0x400000).

Useful strings in the binary: `CUSBConnect::flash_read failed`,
`get_upload_responds::Data type not 0x23!`, `get_upload_responds::Checksum error!`,
`Data%ul header checksum Error`, `%s address not follow the last flash`.

## Transport (verified)

- USB interface 1 (Audio/MIDIStreaming, "DFU-Midi"), bulk EP `0x02` OUT and
  `0x81` IN, 64-byte packets.
- The kernel's `snd-usb-audio` claims it. Either detach it (pyusb, as `ap09.py`
  does) or use ALSA rawmidi (`amidi -l` lists it as "DFU̒ MIDI 1").
- On raw USB, bytes go inside **USB-MIDI event packets**: 4 bytes each,
  `[CIN] [b0] [b1] [b2]`, cable 0. CIN 0x4 = SysEx start/continue (3 bytes);
  0x5/0x6/0x7 = SysEx end with 1/2/3 bytes. See `encode_usb_midi` /
  `decode_usb_midi` in `ap09.py`.

## SysEx framing (verified)

The wire message is `F0 <packed payload> F7`. **There is no manufacturer ID.**
The `00 32 45 …` seen in loopertrx issue #6 is just packed data.

Packing (encoder at `0x410a80`, decoder at `0x4108a0`) is a little-endian
bitstream cut into 7-bit groups:

```python
def enc(b):                       # 8-bit bytes -> 7-bit SysEx data bytes
    out, acc, n = [], 0, 0
    for x in b:
        acc |= x << n; n += 8
        while n >= 7: out.append(acc & 0x7f); acc >>= 7; n -= 7
    if n: out.append(acc & 0x7f)
    return bytes(out)

def dec(b):                       # 7-bit SysEx data bytes -> 8-bit bytes
    out, acc, n = [], 0, 0
    for x in b:
        acc |= x << n; n += 7
        if n >= 8: out.append(acc & 0xff); acc >>= 8; n -= 8
    return bytes(out)
```

## Packet format (unpacked payload)

```
off  size  field
0    2     header 00 59          (constant at 0x48ea78)
2    1     command
3    3     body length, little-endian 24-bit
6    N     body
6+N  1     checksum = (~sum(body)) & 0xff   (0xff when the body is empty)
```

Responses use the same layout. The command byte is echoed back.

### Command 0x11 — device info (verified)

Request `00 59 11 00 00 00 ff` (packed: `F0 00 32 45 00 00 00 40 7F F7`).
Built at `0x4189e0`. Response body (27 bytes) from our unit:

```
00 59 11 1b 00 00 | 61 62 63 64 65 66 67 68 61 62 63 64 65 66 67 68 01 00 05 12 13 14 15 16 17 18 19 | 05
                    "abcdefgh" "abcdefgh" ...                                                          checksum
```

Meaning is not decoded yet. The response is the same whether or not a loop is
recorded, so it is probably an ID or version block.

### Command 0x23 — memory read (verified)

Built in `0x418f80` (flash_read). Body is 8 bytes:

```
body[0]    area/flag   0 and 1 answer; 2 returns an error (00 59 00 01 00 00 01 fe)
body[1:5]  address     LE32
body[5:8]  length      LE24, the tool caps each chunk at 0x3f1 (1009) bytes
```

The response body echoes those 8 bytes, then returns `length` data bytes:

```
req : area=0 addr=0x2180 len=16
resp: 00 59 23 18 00 00 | 00 80 21 00 00 10 00 00 | 01 00 00 00 15 27 00 00 10 b5 82 b0 0c 46 58 b9 | b7
```

Area 0 at 0x2180 looks like MCU flash: `10 b5 82 b0` is Thumb code (push/sub sp),
so this may be firmware. Area 1 holds other data, possibly the SPI flash with
audio. **Which area and address range hold the loop is still TODO.**

### Old protocol (fallback in the tool, not used by this unit)

`0x4186a0` first tries cmd 0x11 (`0x4189e0`). If that works, it reads 16 bytes at
0x2180 with cmd 0x23 and parses three LE32 values into fields
`0x2c860/64/68`. These are probably the loop start, length and end, but that is
unconfirmed.

If 0x11 fails, it falls back to raw 4-byte commands `59 12 00 34` (ping, also in
`0x418b60`), `59 13..1e 00 00` (reads the three LE32 values one nibble at a time)
and `59 21 00 21`. This unit does not answer those (tested, they time out).

## Next steps

1. Parse the 16 bytes at 0x2180. The tool code at `0x418762`–`0x4187ef` assembles
   three LE32 values (`0x2c860`, `0x2c864`, `0x2c868`) from bytes 0–3, 4–7 and 8–11
   of that read. Here they are `0x00000001`, `0x00002715` and `0xb0 82 b5 10`.
   Compare reads from area 0 and area 1, with and without a recorded loop.
2. Find where the loop audio is and how long it is. Record loops of known lengths
   and diff reads of area 1. The tool streams audio with cmd 0x23 in 0x3f1-byte
   chunks, and the caller is at `0x40d7c6`.
3. Find the upload/write command. The strings `get_upload_responds` and "address
   not follow the last flash" point to a write path. Look for other command bytes
   stored after `movzwl 0x48ea78` (the `00 59` header load). Every packet builder
   starts that way, so `grep -n "0x48ea78" disasm` lists them all.
4. Audio format. loopertrx's sister devices use mono 24-bit 48 kHz PCM; this one is
   still unverified.

Quick test harness: `/tmp/t2.py` during development. Recreate it from the
`enc`/`dec`/packet code above plus `ap09.open_device()` / `ap09.send_sysex()`.
