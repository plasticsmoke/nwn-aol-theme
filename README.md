# Neverwinter Nights (AOL) — PC Speaker Music Engine

Reverse-engineering the music engine from the offline DOS version of
**Neverwinter Nights** (the AOL game, not the BioWare one). The game ships
as a single `GAME.EXE` that plays polyphonic music through the PC speaker
at startup — a trick that sounds like it shouldn't be possible on hardware
that can only produce one tone at a time.

This repo contains the tools used to extract the music data and the
resulting `beep` scripts that replicate the songs on modern Linux.

---

## The Hardware Constraint

The IBM PC speaker is about as simple as sound hardware gets. It's a
single piezoelectric element wired to **Channel 2** of the Intel 8253
Programmable Interval Timer (PIT). You program a 16-bit divisor into the
PIT; it divides its 1.193182 MHz base clock by that value and toggles the
speaker at the resulting frequency. That's it — one square wave, one
frequency at a time, no volume control.

To make a tone:

1. Write `0xB6` to port `0x43` (PIT control: channel 2, square wave mode)
2. Write the 16-bit divisor to port `0x42` (low byte, then high byte)
3. Set bits 0–1 of port `0x61` to enable the speaker gate

To silence it, clear those bits on port `0x61`.

The frequency you hear is `1193182 / divisor` Hz. A divisor of 2280 gives
you middle C (~523 Hz). A divisor of 2711 gives you A4 (440 Hz).

## The Executable

`GAME.EXE` is a DOS MZ executable compressed with **Microsoft EXEPACK**.
The telltale sign is the string `Packed file is corrupt` buried in the
decompression stub at the end of the file. EXEPACK is a simple
backward-running RLE scheme:

- `0xB0`/`0xB1`: fill N bytes with a value
- `0xB2`/`0xB3`: copy N literal bytes
- Odd command byte = last command in stream

After unpacking, the image is ~62 KB of 16-bit real-mode code and data.

## The Music Engine

The game contains a self-contained music engine that lives in a single
code segment (~3 KB of code plus data tables). It supports two output
targets:

| Target | Voices | Hardware |
|--------|--------|----------|
| PC Speaker | 1 (time-multiplexed to fake 3) | PIT Channel 2 + port 0x61 |
| Tandy/PCjr | 3 real + 1 noise | SN76489 via port 0xC0 |

The engine detects which hardware is present and uses the appropriate
output path. The same song data drives both — three melodic voices are
encoded in every song. On Tandy hardware all three play simultaneously;
on the PC speaker, the engine uses an envelope trick (described below)
to multiplex them.

### Timer Interrupt

The engine hooks **INT 8** (the system timer interrupt) by reprogramming
PIT Channel 0 to fire at a higher rate:

| Parameter | Value |
|-----------|-------|
| PIT divisor | `0x13B1` (5041) |
| Tick rate | 1193182 / 5041 ≈ **236.7 Hz** |
| Tick period | **4.225 ms** |

The original 18.2 Hz BIOS tick is preserved by chaining to the old INT 8
handler every 13th tick (236.7 / 13 ≈ 18.2).

On each tick the engine:

1. Runs the **envelope generator** for each voice (volume slide, decay)
2. Runs the **sequencer** for each voice (reads bytecode when the current
   note's duration expires)
3. Applies **vibrato** (LFO modulating the PIT divisor)
4. Selects the **highest-priority active voice** and programs the speaker

### Bytecode Format

Each voice's music data is a stream of bytecodes interpreted by the
sequencer. There are two kinds of atoms:

#### Note Pairs

Every note is encoded as two bytes:

```
Byte 1: [VVV DDDDD]
         VVV   = target voice index (0–7), selects which voice to set
         DDDDD = duration table index (0–31)

Byte 2: [L PPPPPPP]
         L       = 1 if this is the last note in the current group
         PPPPPPP = pitch value (0–126), or 127 = rest
```

Multiple note pairs can be chained in a single group (all sharing the
same tick). The **last** pair has bit 7 set in its pitch byte. This is how
a single sequencer voice triggers notes on all three output voices
simultaneously.

The **duration** is computed as:

```
ticks = tempo × duration_table[DDDDD]
```

where `tempo` is a per-voice parameter (typically 6) and the duration
table encodes standard musical values:

| Index | Multiplier | Musical value |
|-------|-----------|---------------|
| 3 | 2 | 64th note |
| 5 | 3 | 32nd triplet |
| 6 | 4 | 32nd note |
| 8 | 6 | 16th triplet |
| 9 | 8 | 16th note |
| 11 | 12 | 8th triplet |
| 12 | 16 | 8th note |
| 14 | 24 | quarter triplet |
| 15 | 32 | quarter note |
| 17 | 48 | dotted quarter |
| 18 | 64 | half note |

With tempo=6, a quarter note (index 15) = 6 × 32 = 192 ticks = **811 ms**.

The **pitch** is converted to a PIT divisor using a 12-entry frequency
table (one octave of semitones at the lowest octave) and octave shifting:

```
octave = 0
while pitch >= 12:
    pitch -= 12
    octave += 1
pitch += transpose       // per-voice, typically -24 (down 2 octaves)
divisor = freq_table[pitch % 12] >> octave
```

The base frequency table:

| Note | PIT Divisor | Frequency |
|------|-------------|-----------|
| C  | 36484 | 32.7 Hz |
| C# | 34436 | 34.6 Hz |
| D  | 32503 | 36.7 Hz |
| D# | 30679 | 38.9 Hz |
| E  | 29007 | 41.1 Hz |
| F  | 27332 | 43.7 Hz |
| F# | 25798 | 46.3 Hz |
| G  | 24350 | 49.0 Hz |
| G# | 22983 | 51.9 Hz |
| A  | 21693 | 55.0 Hz |
| A# | 20476 | 58.3 Hz |
| B  | 19326 | 61.7 Hz |

These are the exact divisors for standard A=440 tuning at octave 0.
Higher octaves simply right-shift the divisor (halving it doubles the
frequency).

#### Commands (0xFA–0xFF)

| Byte | Name | Operands | Effect |
|------|------|----------|--------|
| `FA` | Init Voice | word: voice offset | Zero all fields of a voice |
| `FB` | Return | — | Return from subroutine |
| `FC` | Call | word: target address | Save position, jump to subroutine |
| `FD` | Select Voice | word: voice offset | Set target voice for `FF` commands |
| `FE` | Loop | byte: counter offset, word: target | Decrement counter, jump if nonzero |
| `FF` | Set Param | byte: field offset, word: value | Write value into voice struct field |

The `FC`/`FB` pair provides one level of subroutine nesting, used for
common initialization sequences (setting tempo, transpose, envelope
pointers, vibrato parameters for all three voices).

### The Polyphony Trick

This is the clever part. The PC speaker can only produce one frequency at
a time, but the engine makes it sound like three voices are playing. The
trick is **envelope-based time-division multiplexing**.

Each voice has an **envelope table** that controls its volume over time.
The envelopes are tuned so the voices take turns being audible:

```
Voice 0 (bass):     volume=3 for  8 ticks (34 ms), then volume=0
Voice 1 (harmony):  volume=3 for 16 ticks (68 ms), then volume=0
Voice 2 (melody):   volume=3 for (duration - 5) ticks, then volume=0
```

The speaker output routine picks the **first voice with nonzero volume**,
in priority order v0 → v1 → v2 → v3. So when a chord is struck:

```
Time (ms)   0          34         68                    ~duration
            |──bass────|──harmony──|──────melody──────────|
            ▼          ▼           ▼                      ▼
Speaker:    C2         D#3         G4                     silence
            (34ms)     (34ms)      (sustain)              (21ms gap)
```

The bass gets a quick 34 ms stab. As soon as its envelope decays to zero,
the harmony voice becomes the highest-priority active voice for another
34 ms. Then both lower voices are silent and the melody sustains for the
remainder of the note. The result is a rapid arpeggiation at the start of
each beat that the ear perceives as a chord, followed by the melody
singing over it.

This is not unique to NWN — it's a well-known trick from the DOS era,
used by games like Ultima, SSI's Gold Box series, and various Sierra
titles. But the implementation here is clean and the musical arrangements
take good advantage of it.

### Voice Structure

Each voice occupies 48 bytes (`0x30`) in a contiguous array. The engine
maintains 4 voice slots starting at `CS:0x0149`:

| Offset | Size | Field |
|--------|------|-------|
| +00 | word | Duration countdown (ticks remaining) |
| +02 | word | Bytecode pointer (current position in song data) |
| +04 | word | Base frequency (PIT divisor) |
| +06 | word | Frequency slide (added per tick) |
| +08 | word | Current frequency (base + vibrato mod) |
| +0A | word | Volume (bits 0–1 gate the speaker) |
| +0C | word | Volume slide (added per tick) |
| +0E | word | Tempo multiplier |
| +10 | word | Attack time (ticks before decay phase) |
| +12 | word | Transpose (signed, in semitones) |
| +14 | word | Decay timer countdown |
| +16 | word | Envelope table base pointer |
| +18 | word | Envelope table current offset |
| +1A | word | Envelope trigger countdown |
| +1C | word | Vibrato wavetable base pointer |
| +1E | word | Vibrato position |
| +20 | word | Vibrato rate (added to position per tick) |
| +22 | word | Vibrato depth (amplitude multiplier) |
| +24 | word | Vibrato period (position wraps here) |

### Envelope Table Format

Envelope tables are arrays of 4-byte entries:

```
[value: word] [time: word]
```

- If `time == 0xFFFF`: immediately set volume to `value`, process next
  entry without waiting.
- If `time == 0`: stop processing (hold current state).
- Otherwise: set `vol_slide = value`, wait `time` ticks, then process
  next entry.

Each voice has two envelope phases at different offsets in the same table:

- **Attack phase** (offset `0x00`): triggered when a note starts
- **Decay phase** (offset `0x10`): triggered when the decay timer
  (duration − attack) expires

### Song Table

The game contains **16 songs** indexed by number. Each entry in the song
table is 8 bytes — four 16-bit pointers to bytecode streams, one per
voice:

| Song | Use |
|------|-----|
| 0 | Silence |
| 1–11 | Gameplay music / sound effects |
| 12 | **Title theme** (longest, ~2.5 min melody) |
| 13 | Secondary theme |
| 14 | Alternate arrangement |
| 15 | Shorter piece |

Songs 12–15 all share a common initialization subroutine (`CS:0x146F`)
that configures the three voices with their respective envelopes,
transpose (-24 semitones), vibrato parameters, and tempo (6).

## Extraction Process

1. **Unpack EXEPACK**: custom Python unpacker (`unexepack.py`) that
   decompresses the RLE-packed code image and recovers the relocation
   table.

2. **Locate the music engine**: search for `OUT 0x42` / `OUT 0x43` /
   `IN 0x61` instruction sequences, then trace the INT 8 handler
   installation (`INT 21h AH=25h` for vector 8) to find the segment base.

3. **Calculate CS base**: the INT 8 handler is installed at `CS:0x222E`;
   its code is at image offset `0x3BEE`; therefore
   `CS_base = 0x3BEE - 0x222E = 0x19C0`.

4. **Read data tables**: frequency table at `CS:0x02C9`, duration table
   at `CS:0x012A`, song pointer table at `CS:0x047B`, envelope tables
   at `CS:0x14C9`/`0x14E1`/`0x14F9`.

5. **Simulate**: tick-level simulation of the sequencer, envelope
   generator, and voice priority selection. Output is a stream of
   `(frequency, duration)` pairs.

6. **Generate `beep` scripts**: convert the frequency/duration stream
   to Linux `beep` command arguments.

## Playing the Music

```bash
# Enable the PC speaker kernel module
sudo modprobe pcspkr

# Install beep
sudo apt install beep

# Play the title theme (full interleaved speaker output)
./nwn-aol-theme_full.sh

# Melody only (cleanest for beep)
./nwn-aol-theme_melody.sh

# Harmony line
./nwn-aol-theme_harmony.sh

# Bass line
./nwn-aol-theme_bass.sh
```

### Output Variants

For each song, four scripts are generated:

| Suffix | Content |
|--------|---------|
| `_full.sh` | All voices with speaker priority simulation. Includes the 34ms bass stabs and 68ms harmony hits. Faithful to the original but may sound rough through `beep` since the command can't switch as fast as the PIT hardware. |
| `_melody.sh` | Voice 2 only — the sustaining melody line. Cleanest output for `beep`. |
| `_harmony.sh` | Voice 1 only — the harmony accompaniment (68ms hits). |
| `_bass.sh` | Voice 0 only — the rhythmic bass line (34ms stabs). |

## Files

The original game binaries (`GAME.EXE`, `GAME.OVR`, `*.DAX`) are
not included in this repository. The tools and output below were
produced from them.

```
unexepack.py           EXEPACK decompression tool
extract_theme.py       Music engine simulator and beep script generator

nwn-aol-theme_full.sh           Title theme — full interleaved speaker output
nwn-aol-theme_melody.sh         Title theme — melody only
nwn-aol-theme_harmony.sh        Title theme — harmony only
nwn-aol-theme_bass.sh           Title theme — bass only
nwn-aol-theme_full_notes.txt    Note listing (full)
nwn-aol-theme_melody_notes.txt  Note listing (melody)
nwn-aol-theme_harmony_notes.txt Note listing (harmony)
nwn-aol-theme_bass_notes.txt    Note listing (bass)
```
