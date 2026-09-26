# ap09 — Ammoon AP-09 nano looper on Linux

A command-line tool to copy the loop stored on an **Ammoon AP-09 nano looper**
(USB ID `0416:5555`, Rowin OEM chip) to your computer over USB.

| Feature | Status |
|---|---|
| Download the loop → WAV | ✅ works, verified bit-exact |
| Show device/loop info | ✅ works |
| Upload WAV/MP3 → pedal | ⚠️ not working yet (see [Upload status](#upload-status)) |

---

## 1. How to use it

### Requirements

```
sudo apt install python3-usb ffmpeg     # ffmpeg is only needed for upload conversion
```

Plug the pedal into the computer with its USB cable. It does not need to be switched to
any special mode.

### Show what is on the pedal

```
sudo python3 ap09.py info
```

```
device info: 61 62 63 64 ...
model id: 0x2715 (NANO LOOPER)
loop: 19.91 s, 2799858 bytes, 22 blocks (first 1661), index record #0x696 at +0xc000
```

### Download the loop

```
sudo python3 ap09.py download myloop.wav
```

It takes about 12 s for a 20 s loop. The file you get is exactly what the pedal stores:

- **WAV, mono, 24-bit signed PCM, 46875 Hz** (the pedal's native rate, 12 MHz / 256).

Every audio program opens it. To turn it into a more common format:

```
ffmpeg -i myloop.wav -ar 48000 myloop-48k.wav      # resample to 48 kHz
ffmpeg -i myloop.wav -b:a 320k myloop.mp3          # mp3
```

### Upload (not working yet)

```
sudo python3 ap09.py upload song.mp3 --experimental     # DON'T, see below
```

When it works, the input can be any format ffmpeg reads (wav, mp3, flac, …). It gets
converted automatically to mono, 24-bit, 46875 Hz. A WAV that is already in that
format goes up as is. The maximum length is about 10 minutes (650 NAND blocks).

### Running without sudo

Create `/etc/udev/rules.d/99-ap09.rules`:

```
SUBSYSTEM=="usb", ATTRS{idVendor}=="0416", ATTRS{idProduct}=="5555", MODE="0666"
```

Then run `sudo udevadm control --reload && sudo udevadm trigger` and replug the pedal.

### Debug commands

```
sudo python3 ap09.py dump 1 0xF780000 0x60      # raw memory read: area, address, length
sudo python3 ap09.py probe 11                   # raw command (hex) + optional hex body
```

`backup-original-loop.wav` is a copy of the loop that was on the pedal when this tool
was developed.

---

## 2. Upload status

Upload is implemented the same way as the official Windows tool (erase blocks, write pages,
append an index record), but on this pedal **the erase command (0x21) replies "OK" and
does not erase anything**. Tests:

- Erasing a block that holds data, then reading it back (same session, pages never read
  before, even after the 0x11 handshake) shows the old data is still there.
- Writing (0x22) works, but only on pages that are already `FF`. On used pages the new
  data comes out **ANDed** with the old, which is typical NAND behaviour when you program
  without erasing first.

So an upload only comes out clean if every chosen block is already erased (`FF`). The next
step is to understand why erase does nothing. Possible causes: a missing "unlock/begin"
command, a different address unit, erase available only in some device state, or a
different erase command in the newer firmware. The official tool also sends 0x24
(checksum) after every write. Candidate workaround: pick only blocks whose pages
all read as `FF`. That can be checked with 0x24, or by reading, which is slower.

What went wrong during development, and how it was fixed: a test upload replaced the
current loop with a corrupted tone. The original loop was never erased (erase does
nothing), so it was brought back by appending an index record that points at its old
blocks again (see `/tmp/restore.py` in the dev history, or `build_record()` +
`Looper.write()`). The restore was verified bit-exact against the backup. Writing index
records works.

---

## 3. How it works (technical)

### How it was reverse-engineered

1. `lsusb` shows `0416:5555 Winbond ... "DFU"`, manufacturer "Rowin". The name "DFU" is
   misleading: it is not a bootloader.
2. Issue [reinerh/loopertrx#6](https://github.com/reinerh/loopertrx/issues/6) showed that
   the device talks MIDI SysEx and gave one working message
   (`F0 00 32 45 00 00 00 40 7F F7`). The upstream `loopertrx` protocol (mass-storage
   style) does **not** apply to this chip.
3. The official Windows software, "LooperSuite V1.7" (`Looper+software+1.7.exe`,
   installer from rowinmusic.com), crashes under Wine. So the installed
   `Looper Software.exe` (Qt5 + RtMidi, 32-bit) was **disassembled statically with
   `objdump -d`**. Protocol functions were found from their strings
   (`CUSBConnect::flash_read failed`, `get_upload_responds::Checksum error!`), from the
   constant `0xF0`, and by following call chains.
4. Every hypothesis was tested live on the pedal with small Python scripts.

Addresses below are virtual addresses in `Looper Software.exe` (image base 0x400000,
file size 620544 bytes, dated 2019-02-20).

### Transport

- USB interface 1 (Audio / MIDIStreaming, string "DFU-Midi"), bulk endpoints `0x02` OUT
  and `0x81` IN, 64-byte packets. The kernel's `snd-usb-audio` claims it. The tool
  detaches it and gives it back at the end. (The same messages also work through ALSA:
  `amidi -p hw:X,0,0 -S "<hex>" -r out.syx`.)
- Bytes travel in **USB-MIDI event packets**: `[CIN][b0][b1][b2]` on cable 0. CIN 0x4 =
  SysEx start/continue; 0x5/0x6/0x7 = SysEx end with 1/2/3 bytes.
- A reply can span many bulk reads, so the tool reads until it sees `F7`.

### SysEx framing

`F0 <packed> F7`, with **no manufacturer ID**. The payload is packed into 7-bit bytes as
a little-endian bitstream (encoder `0x410a80`, decoder `0x4108a0`). See `pack7` /
`unpack7` in `ap09.py`. What looks like a Rowin ID (`00 32 45`) is only packed data.

### Packet format (after unpacking)

```
00 59 | cmd | len (LE24) | body[len] | checksum = ~sum(body) & 0xFF   (0xFF if body empty)
```

Header `00 59` is the constant at `0x48ea78`. Replies use the same format. Commands that
answer with a status use reply cmd `0x00` with a 1-byte body: `00` = OK, anything else =
error (for example `00 59 00 01 00 00 01 FE` is error 1).

| cmd | body | reply | where in the exe | status |
|---|---|---|---|---|
| `0x11` info | – | 27 bytes (`"abcdefgh" "abcdefgh" 01 00 05 12..19`), static | `0x4189e0` | ✅ |
| `0x23` read | `area:u8 addr:LE32 len:LE24` | echo of the 8-byte body, then `len` bytes | `0x418f80` | ✅ |
| `0x22` write | `area addr:LE32 len:LE24 data` (≤0x1400 per packet) | status | `0x418c60` | ✅ (programs only, needs FF) |
| `0x21` erase | `area addr:LE32` | status (OK) | `0x4191b0` / `0x418bc0` | ❌ no effect |
| `0x24` checksum | `area addr:LE32 len:LE32` | echo + `u16` sum of the bytes | `0x419220` / `0x418ea0` | ✅ |

Read chunks are capped at 0x3F1 bytes (the official tool's limit), writes at 0x1400.
Areas: **0** = MCU internal flash (firmware, settings; never write here), **1** = 256 MiB
NAND. Area 2 returns an error.

The official tool also has an older fallback protocol for other models: raw 4-byte
commands `59 12 00 34`, `59 13..1e 00 00`, `59 21 00 21` (`0x418b60`, `0x4186a0`). This
unit does not answer them.

### Device / model

Reading area 0 at `0x2180` (16 bytes) → bytes 4..7 = model id **0x2715 = "NANO LOOPER"**
(model table at `0x48e700`, entries of 0x54 bytes). The per-model NAND layout table is at
`0x48ea20` (11 × u32), copied into the song object by `0x408200`:

| field | value | meaning |
|---|---|---|
| base | 0 | NAND base offset |
| index block | 0x7BC (1980) | block that holds the loop index log |
| max blocks | 0x28A (650) | max blocks per loop, about 10 min |
| block size | 0x20000 | 128 KiB |
| page size | 0x800 | 2 KiB |
| pages/block | 0x40 | 64 |
| audio/page | 0x7FE | 2046 audio bytes per page; the last 2 bytes are spare (`00 00`) |
| rate | 0xB71B | 46875 Hz |
| channels | 1 | mono |
| bits | 0x18 | 24-bit signed LE |
| tail unit | 0xBA | 186 bytes (62 samples), length granularity; 11 per page |

### Loop index (NAND block 1980, address `0xF780000`)

This is a log of records. A new record goes every 2 pages (0x1000 bytes). The **newest**
record is the current loop. When the block is full, the official tool erases it and starts
again at page 0 (`0x407cfa`). Record (0xA40 bytes, built at `0x407c1e`, read at
`0x407ff0`):

```
0x00  "looper\0"
0x07  u8   written by the pedal (maybe a checksum); the PC tool leaves it 0xFF
0x08  u8   1 = valid loop, 0 = no loop (cleared)
0x09  u8   0xFF
0x0A  u16  sequence number (pedal counts up; PC tool writes 0)
0x0C  u16  A  \
0x0E  u8   B   }  units = A*704 + B*11 + C  (units of 186 bytes, minus one)
0x0F  u8   C  /   length_bytes = (A*64 + B)*2046 + (C+1)*186
0x10  u16[] NAND block numbers of the loop, in order (ceil(length / 130944) entries)
0xA38 "looper\0"  (trailer written by the PC tool)
```

### Audio layout

The loop is the concatenation of 2046-byte chunks. Chunk *k* of a block is stored at
`block*0x20000 + page*0x800`, where `page = k` in every block **except the first block of
the loop**. That one is rotated by one page: chunk 0 → page 63, chunk k → page k−1
(`0x408900` download, `0x408b54` upload). The data is raw 24-bit LE mono PCM at 46875 Hz.

### Upload algorithm of the official tool (for reference)

1. Pick blocks: start at a random block (`rand() % 1980`), go up and wrap around, and skip
   blocks used by existing songs (`0x406ca0`).
2. For each block: erase (0x21), then write each 2046-byte chunk into its page (0x22),
   padding the last chunk to a multiple of 186 bytes. Verify each write with 0x24
   (`0x418ea0`).
3. Build the record (0xFF-filled, fields above) and write it into the next free record slot
   of the index block. If there is no free slot, erase the index block first.

### Where to continue

- **Fix erase.** Capture what the pedal expects. Look at other `0x21` users in the exe
  (`grep -n "0x48ea78"` on the disassembly lists every packet builder: `0x407470`,
  `0x407d1a`, `0x408e16`, `0x4189ee`, `0x418cd8`, `0x41900f`, `0x4191b0`, `0x419220`) and
  check for a mode or unlock command sent before uploads.
- Meaning of the info (0x11) bytes and of record byte 0x07.
- Whether the pedal has to be power-cycled to see a new index record.

### Files

- `ap09.py` — the tool (transport, protocol, index parsing, download, upload).
- `PROTOCOL.md` — early notes, now **superseded by this README**.
- `backup-original-loop.wav` — backup of the loop that was on the pedal.
