#!/usr/bin/env python3
"""
Extract the NWN AOL title theme from the unpacked GAME.EXE binary
and produce a Linux `beep` command script.

The music engine simulates 3-voice polyphony on the PC speaker by using
short envelope durations: bass sounds for ~34ms, harmony for ~68ms,
then the melody sustains. Priority: v0 > v1 > v2.
"""
import struct
import sys
import os

with open('/tmp/nwn_unpacked.bin', 'rb') as f:
    DATA = f.read()

CS_BASE = 0x19C0
PIT_FREQ = 1193182
TICK_DIVISOR = 0x13B1
TICK_HZ = PIT_FREQ / TICK_DIVISOR
TICK_MS = 1000.0 / TICK_HZ

note_names = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

def cs_byte(off):
    return DATA[CS_BASE + off]

def cs_word(off):
    return struct.unpack_from('<H', DATA, CS_BASE + off)[0]

def cs_sword(off):
    v = cs_word(off)
    return v if v < 32768 else v - 65536

FREQ_TABLE = [cs_word(0x2C9 + i * 2) for i in range(12)]
DUR_TABLE = [cs_byte(0x12A + i) for i in range(32)]


def find_note_name(freq):
    if freq <= 0:
        return "---"
    best = "?"
    best_diff = 99999
    for oct in range(0, 10):
        for ni, name in enumerate(note_names):
            div = FREQ_TABLE[ni] >> oct
            if div <= 0:
                continue
            nf = PIT_FREQ / div
            diff = abs(nf - freq)
            if diff < best_diff:
                best_diff = diff
                best = f"{name}{oct}"
    return best


def pitch_to_pit_div(pitch, transpose):
    """Convert a pitch value + transpose to PIT divisor."""
    actual = pitch + transpose
    octave = 0
    while actual >= 12:
        actual -= 12
        octave += 1
    while actual < 0:
        actual += 12
        octave -= 1
    if 0 <= actual < 12 and octave >= 0:
        return FREQ_TABLE[actual] >> octave
    return 0


class VoiceState:
    """Runtime state for one voice."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.pit_div = 0        # Current PIT divisor (frequency)
        self.duration = 0       # Note duration countdown
        self.volume = 0         # Speaker enable (0 = off)
        self.vol_slide = 0      # Volume slide per tick
        self.attack = 0         # Attack time parameter
        self.transpose = 0      # Pitch transpose
        self.env_ptr = 0        # Envelope table pointer
        self.env_offset = 0     # Current position in envelope
        self.env_trigger = 0    # Ticks until next envelope step
        self.decay_timer = 0    # Ticks until decay phase
        self.freq_slide = 0     # Frequency slide per tick
        self.freq_base = 0      # Base frequency


class TickSimulator:
    """Full tick-level simulation of the music engine."""

    def __init__(self):
        self.voices = [VoiceState() for _ in range(4)]
        self.seq_ptr = [0] * 4       # Bytecode position per sequencer voice
        self.seq_dur = [0] * 4       # Sequencer duration countdown
        self.seq_tempo = [0] * 4     # Tempo per sequencer voice
        self.seq_active = [False] * 4
        self.call_stack = 0
        self.selected_voice = 0
        self.loop_counters = {}

    def load_song(self, song_num):
        for i in range(4):
            ptr = cs_word(0x47B + song_num * 8 + i * 2)
            self.voices[i].reset()
            self.seq_ptr[i] = ptr if ptr != 0 else 0
            self.seq_dur[i] = 1 if ptr != 0 else 0
            self.seq_active[i] = ptr != 0

    def process_envelope(self, v):
        """Process envelope table for a voice."""
        if v.env_ptr == 0:
            return
        max_iter = 20
        while max_iter > 0:
            max_iter -= 1
            addr = v.env_ptr + v.env_offset
            val = cs_word(addr)
            time = cs_word(addr + 2)
            sval = val if val < 32768 else val - 65536

            if time == 0xFFFF:
                # Immediate set volume
                v.volume = val
                if val == 0:
                    v.vol_slide = 0
                v.env_offset += 4
                continue
            else:
                # Gradual slide
                v.vol_slide = sval
                v.env_trigger = time
                v.env_offset += 4
                return

    def tick_voice(self, vi):
        """Per-tick processing for one voice."""
        v = self.voices[vi]
        if v.duration == 0:
            return

        # Volume slide
        new_vol = v.volume + v.vol_slide
        v.volume = max(0, min(0xFFFF, new_vol))

        # Frequency slide
        v.freq_base = (v.freq_base + v.freq_slide) & 0xFFFF
        v.pit_div = v.freq_base  # simplified (no vibrato for beep output)

        # Envelope trigger
        if v.env_trigger > 0:
            v.env_trigger -= 1
            if v.env_trigger == 0:
                self.process_envelope(v)

        # Decay timer
        if v.decay_timer > 0:
            v.decay_timer -= 1
            if v.decay_timer == 0:
                # Switch to decay phase of envelope
                v.env_offset = 0x10
                v.env_trigger = 1

        # Duration countdown
        v.duration -= 1

    def process_bytecode(self, si):
        """Process bytecode for sequencer voice si until next note group."""
        di = self.seq_ptr[si]
        if di == 0:
            return

        max_iter = 500
        while max_iter > 0:
            max_iter -= 1
            b = cs_byte(di)
            di += 1

            if b < 0xFA:
                # Note command
                target_idx = (b >> 5) & 7
                dur_idx = b & 0x1F
                tempo = self.seq_tempo[si]
                if tempo == 0:
                    tempo = 1
                duration = (tempo * DUR_TABLE[dur_idx]) & 0xFFFF

                pitch_byte = cs_byte(di)
                di += 1
                pitch = pitch_byte & 0x7F
                last = (pitch_byte & 0x80) != 0

                if target_idx < 4 and pitch != 0x7F:
                    tv = self.voices[target_idx]
                    pit_div = pitch_to_pit_div(pitch, tv.transpose)
                    tv.pit_div = pit_div
                    tv.freq_base = pit_div
                    tv.duration = duration
                    tv.decay_timer = max(0, duration - tv.attack)
                    tv.env_offset = 0
                    tv.env_trigger = 1
                    tv.vol_slide = 0

                if last:
                    self.seq_dur[si] = duration
                    self.seq_ptr[si] = di
                    return
                continue

            if b == 0xFA:
                voice_off = cs_word(di); di += 2
                idx = voice_off // 0x30
                if idx < 4:
                    self.voices[idx].reset()
            elif b == 0xFB:
                di = self.call_stack
            elif b == 0xFC:
                target = cs_word(di); di += 2
                self.call_stack = di
                di = target
            elif b == 0xFD:
                voice_off = cs_word(di); di += 2
                idx = voice_off // 0x30
                if idx < 4:
                    self.selected_voice = idx
            elif b == 0xFE:
                counter_off = cs_byte(di); di += 1
                target = cs_word(di); di += 2
                key = (target, counter_off, si)
                if key not in self.loop_counters:
                    self.loop_counters[key] = 0
                if self.loop_counters[key] < 300:
                    self.loop_counters[key] += 1
                    di = target
            elif b == 0xFF:
                param_idx = cs_byte(di); di += 1
                param_val = cs_word(di); di += 2
                sval = param_val if param_val < 32768 else param_val - 65536
                sv = self.selected_voice
                tv = self.voices[sv]

                if param_idx == 0x00:
                    tv.duration = param_val
                    if param_val == 0:
                        self.seq_ptr[si] = di
                        return
                elif param_idx == 0x04:
                    tv.freq_base = param_val
                    tv.pit_div = param_val
                elif param_idx == 0x06:
                    tv.freq_slide = sval
                elif param_idx == 0x0A:
                    tv.volume = param_val
                elif param_idx == 0x0C:
                    tv.vol_slide = sval
                elif param_idx == 0x0E:
                    self.seq_tempo[sv] = param_val
                elif param_idx == 0x10:
                    tv.attack = param_val
                elif param_idx == 0x12:
                    tv.transpose = sval
                elif param_idx == 0x16:
                    tv.env_ptr = param_val
                elif param_idx == 0x18:
                    tv.env_offset = param_val
                elif param_idx == 0x1A:
                    tv.env_trigger = param_val

        self.seq_ptr[si] = di

    def tick(self):
        """One engine tick. Returns (pit_divisor, speaker_on)."""
        # Process all sequencer voices
        for si in range(4):
            if not self.seq_active[si] or self.seq_ptr[si] == 0:
                continue
            # Tick all voices (even non-sequencer ones get ticked)
            self.tick_voice(si)
            # Sequencer duration countdown
            if self.seq_dur[si] > 0:
                self.seq_dur[si] -= 1
                if self.seq_dur[si] == 0:
                    self.process_bytecode(si)

        # Also tick voices that aren't sequencers (target voices)
        for vi in range(4):
            if self.seq_active[vi]:
                continue
            if self.voices[vi].duration > 0:
                self.tick_voice(vi)

        # Find speaker output: first voice with volume > 0 and duration > 0
        for vi in range(4):
            v = self.voices[vi]
            if v.volume > 0 and v.duration > 0 and (v.volume & 3) != 0:
                return v.pit_div, True, vi

        return 0, False, -1


def simulate_song(song_num, max_ticks=500000, voice_filter=None):
    """Simulate song and return (freq_hz, duration_ms) events.
    voice_filter: None = normal speaker priority, or set of voice indices
                  to track directly (ignoring priority, reading their state).
    """
    sim = TickSimulator()
    sim.load_song(song_num)

    events = []
    current_div = 0
    current_ticks = 0
    idle_ticks = 0
    total_ticks = 0

    for _ in range(max_ticks):
        pit_div, on, voice_idx = sim.tick()
        total_ticks += 1

        if voice_filter is not None:
            # Direct voice state tracking (not speaker priority)
            # Find the first voice in filter set that has volume > 0
            pit_div = 0
            on = False
            for vi in sorted(voice_filter):
                v = sim.voices[vi]
                if v.volume > 0 and v.duration > 0 and (v.volume & 3) != 0:
                    pit_div = v.pit_div
                    on = True
                    break

        if not on:
            pit_div = 0
            idle_ticks += 1
        else:
            idle_ticks = 0

        if pit_div == current_div:
            current_ticks += 1
        else:
            if current_ticks > 0:
                freq = round(PIT_FREQ / current_div, 1) if current_div > 0 else 0
                events.append((freq, current_ticks * TICK_MS))
            current_div = pit_div
            current_ticks = 1

        # Check if all done
        all_done = all(
            sim.seq_ptr[i] == 0 and sim.seq_dur[i] == 0
            for i in range(4) if sim.seq_active[i]
        )
        if all_done and total_ticks > 50:
            break
        if idle_ticks > 10000:
            break

    if current_ticks > 0:
        freq = round(PIT_FREQ / current_div, 1) if current_div > 0 else 0
        events.append((freq, current_ticks * TICK_MS))

    return events


def merge_events(events, min_dur_ms=3):
    """Merge consecutive same-frequency events and filter tiny ones."""
    merged = []
    for freq, dur in events:
        if dur < min_dur_ms:
            # Absorb into previous event
            if merged:
                merged[-1] = (merged[-1][0], merged[-1][1] + dur)
            continue
        if merged and abs(merged[-1][0] - freq) < 1.0:
            merged[-1] = (merged[-1][0], merged[-1][1] + dur)
        else:
            merged.append((freq, dur))

    # Second pass: merge same-pitch notes separated only by tiny silence
    cleaned = []
    i = 0
    while i < len(merged):
        freq, dur = merged[i]
        # Look ahead: if this note is followed by a short silence then same note,
        # merge them together (common artifact of voice priority switching)
        while (i + 2 < len(merged)
               and merged[i+1][0] == 0 and merged[i+1][1] < 30
               and abs(merged[i+2][0] - freq) < 1.0 and freq > 0):
            dur += merged[i+1][1] + merged[i+2][1]
            i += 2
        cleaned.append((freq, dur))
        i += 1
    return cleaned


def write_beep_script(events, filename, song_label="NWN Theme"):
    """Write a beep command script."""
    # Strip leading/trailing silence
    while events and events[0][0] == 0:
        events.pop(0)
    while events and events[-1][0] == 0:
        events.pop()

    parts = []
    for freq, dur in events:
        dur_int = max(1, round(dur))
        if freq >= 20:
            parts.append(f"-f {freq:.0f} -l {dur_int}")
        else:
            # Silent pause
            parts.append(f"-f 1 -l 1 -D {dur_int}")

    with open(filename, 'w') as f:
        f.write("#!/bin/bash\n")
        f.write(f"# {song_label} - PC Speaker Emulation\n")
        f.write("# Extracted from NWN AOL Offline GAME.EXE\n")
        f.write("# Requires: sudo apt install beep; sudo modprobe pcspkr\n\n")
        f.write("beep \\\n")
        for i, part in enumerate(parts):
            if i < len(parts) - 1:
                f.write(f"  {part} -n \\\n")
            else:
                f.write(f"  {part}\n")

    os.chmod(filename, 0o755)
    return parts


def write_notes_file(events, filename):
    """Write readable note list."""
    with open(filename, 'w') as f:
        f.write(f"{'Note':>6s} {'Freq Hz':>10s} {'Dur ms':>10s}\n")
        f.write("-" * 30 + "\n")
        total = 0
        for freq, dur in events:
            nn = find_note_name(freq)
            f.write(f"{nn:>6s} {freq:10.1f} {dur:10.1f}\n")
            total += dur
        f.write(f"\nTotal: {total/1000:.1f}s\n")


# Main
print(f"Tick rate: {TICK_HZ:.1f} Hz ({TICK_MS:.3f} ms/tick)")
print(f"Envelope: bass=8 ticks(34ms), harmony=16 ticks(68ms), melody=sustain\n")

BASE = "/home/kn/nwn-theme"

for song_num in [12, 13, 14, 15]:
    ptr = cs_word(0x47B + song_num * 8)
    if ptr == 0:
        continue

    print(f"Song {song_num} (data at CS:0x{ptr:04X}):")

    # --- Full interleaved version (faithful to original speaker output) ---
    raw_full = simulate_song(song_num)
    full = merge_events(raw_full)
    n_full = sum(1 for f, _ in full if f > 0)
    t_full = sum(d for _, d in full) / 1000
    write_beep_script(full, f"{BASE}/song_{song_num}_full.sh",
                      f"NWN AOL Song {song_num} (full interleaved)")
    write_notes_file(full, f"{BASE}/song_{song_num}_full_notes.txt")
    print(f"  full:    {n_full:4d} notes, {t_full:.1f}s -> song_{song_num}_full.sh")

    # --- Melody only (voice 2 = the sustaining melody line) ---
    raw_mel = simulate_song(song_num, voice_filter={2})
    mel = merge_events(raw_mel, min_dur_ms=10)
    n_mel = sum(1 for f, _ in mel if f > 0)
    t_mel = sum(d for _, d in mel) / 1000
    write_beep_script(mel, f"{BASE}/song_{song_num}_melody.sh",
                      f"NWN AOL Song {song_num} (melody only)")
    write_notes_file(mel, f"{BASE}/song_{song_num}_melody_notes.txt")
    print(f"  melody:  {n_mel:4d} notes, {t_mel:.1f}s -> song_{song_num}_melody.sh")

    # --- Bass+harmony only (voices 0+1, the rhythmic accompaniment) ---
    raw_bh = simulate_song(song_num, voice_filter={0, 1})
    bh = merge_events(raw_bh, min_dur_ms=10)
    n_bh = sum(1 for f, _ in bh if f > 0)
    t_bh = sum(d for _, d in bh) / 1000
    write_beep_script(bh, f"{BASE}/song_{song_num}_bass.sh",
                      f"NWN AOL Song {song_num} (bass+harmony)")
    write_notes_file(bh, f"{BASE}/song_{song_num}_bass_notes.txt")
    print(f"  bass:    {n_bh:4d} notes, {t_bh:.1f}s -> song_{song_num}_bass.sh")

    # Print sample of melody
    mel_notes = [(f, d) for f, d in mel if f > 0]
    if mel_notes:
        print(f"  melody preview:")
        for f, d in mel_notes[:10]:
            nn = find_note_name(f)
            print(f"    {nn:5s} {f:8.1f} Hz  {d:8.1f} ms")
        if len(mel_notes) > 10:
            print(f"    ... ({len(mel_notes) - 10} more)")
    print()
