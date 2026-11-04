# SPDX-License-Identifier: GPL-2.0-or-later
"""Generated audio for the how-to videos: a soft pad-and-pluck bed and the
small sounds that go with clicks and key presses. Standard library only, so
there is nothing to install and no licence to worry about: the music is made
here, from sine waves, for each video.

    bed(path, seconds)             a 22.05 kHz stereo wav, seamless loop of 4 chords
    effects(path, seconds, clicks, keys, chimes)   a 44.1 kHz stereo wav, silent but for the sounds

The bed is cached by length (rounded up to the loop). python3 music.py out.wav 60
writes a bed to listen to.
"""
from __future__ import annotations

import array
import math
import random
import struct
import sys
import wave
from pathlib import Path

BPM = 70
BEAT = 60.0 / BPM
SR = 22050
# C maj7 - A min7 - F maj7 - G add6: warm, open, never resolves hard.
CHORDS = [[48, 55, 59, 64], [45, 52, 55, 60], [41, 48, 52, 57], [43, 50, 55, 59, 64]]
ARP = [0, 2, 1, 3, 2, 1, 3, 1]          # which chord note each beat plucks (an octave up)
CHORD_BEATS = 8


def _hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def _loop():
    n_chord = round(CHORD_BEATS * BEAT * SR)
    n = n_chord * len(CHORDS)
    left, right = [0.0] * n, [0.0] * n

    def add(buf, start, samples):
        for i, v in enumerate(samples):
            buf[(start + i) % n] += v         # wraps: the loop has no seam

    for ci, chord in enumerate(CHORDS):
        start = ci * n_chord
        length = n_chord + round(2.5 * SR)    # rings into the next chord
        atk, rel = round(1.6 * SR), round(2.2 * SR)
        for mi, midi in enumerate(chord):
            amp = 0.11 / math.sqrt(len(chord)) * (1.3 if mi == 0 else 1.0)
            for buf, detune in ((left, 0.35), (right, -0.35)):
                f = _hz(midi) + detune * (1 + mi * 0.2)
                w1, w2 = 2 * math.pi * f / SR, 2 * math.pi * 2 * f / SR
                out = []
                for i in range(length):
                    env = min(1.0, i / atk, (length - i) / rel)
                    out.append(env * env * amp * (math.sin(w1 * i) + 0.18 * math.sin(w2 * i)))
                add(buf, start, out)
        # plucks: one a beat, alternate sides, an octave above the chord
        for b in range(CHORD_BEATS):
            midi = chord[ARP[b] % len(chord)] + 24
            f = _hz(midi)
            plen = round(1.4 * SR)
            wv = [math.exp(-i / SR * 3.2) * 0.045 * (math.sin(2 * math.pi * f * i / SR)
                  + 0.3 * math.sin(2 * math.pi * 2.01 * f * i / SR) * math.exp(-i / SR * 6))
                  for i in range(plen)]
            at = start + round(b * BEAT * SR) + round(0.01 * SR)
            add(left if b % 2 == 0 else right, at, [v * 1.0 for v in wv])
            add(right if b % 2 == 0 else left, at, [v * 0.45 for v in wv])

    def polish(buf):
        # two passes round the loop so filter and echo state carry across the seam
        lp, a = 0.0, 1 - math.exp(-2 * math.pi * 1400 / SR)
        d1, d2 = round(0.331 * SR), round(0.517 * SR)
        e1, e2 = [0.0] * d1, [0.0] * d2
        out = [0.0] * n
        for p in range(2):
            for i in range(n):
                lp += a * (buf[i] - lp)
                j1, j2 = i % d1, i % d2
                echo = 0.30 * e1[j1] + 0.22 * e2[j2]
                e1[j1] = lp + 0.45 * e1[j1]
                e2[j2] = lp + 0.40 * e2[j2]
                if p:
                    out[i] = lp + echo * 0.55
        return out

    return polish(left), polish(right)


def _pcm(vals, peak):
    s = 32767 / peak
    return array.array("h", (max(-32767, min(32767, int(v * s))) for v in vals))


def bed(path, seconds, cache=None):
    """Write a wav of @p seconds of the bed, faded in and out."""
    cache = Path(cache or Path.home() / ".cache" / "qet-how-to-video" / "bed-loop.wav")
    if cache.exists():
        with wave.open(str(cache)) as w:
            raw = array.array("h", w.readframes(w.getnframes()))
        nl = len(raw) // 2
        L, R = raw[0::2], raw[1::2]
    else:
        left, right = _loop()
        peak = max(max(map(abs, left)), max(map(abs, right))) / 0.5     # loop peaks at -6 dBFS
        L, R = _pcm(left, peak), _pcm(right, peak)
        nl = len(L)
        cache.parent.mkdir(parents=True, exist_ok=True)
        inter = array.array("h", [0]) * (2 * nl)
        inter[0::2], inter[1::2] = L, R
        with wave.open(str(cache), "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes(inter.tobytes())
    total = round(seconds * SR)
    fin, fout = round(2.5 * SR), round(3.5 * SR)
    out = array.array("h", [0]) * (2 * total)
    for i in range(total):
        g = min(1.0, i / fin, (total - i) / fout)
        j = i % nl
        out[2 * i] = int(L[j] * g)
        out[2 * i + 1] = int(R[j] * g)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(out.tobytes())


# -- sound effects ----------------------------------------------------------
FX = 44100


def _click(rng, kind):
    """A soft mouse tick, or a slightly lower keyboard tock."""
    n = round(0.09 * FX)
    f = 1900 if kind == "click" else 1150
    lowpass, y = 0.0, 0.0
    out = []
    for i in range(n):
        t = i / FX
        noise = rng.uniform(-1, 1)
        lowpass += 0.35 * (noise - lowpass)
        y = (0.55 * lowpass * math.exp(-t * 140)
             + 0.30 * math.sin(2 * math.pi * f * t) * math.exp(-t * 110)
             + 0.35 * math.sin(2 * math.pi * 170 * t) * math.exp(-t * 70))
        out.append(y * (0.9 if kind == "click" else 0.65))
    return out


def _chime(f0):
    n = round(1.8 * FX)
    return [0.35 * math.exp(-i / FX * 2.4) * (math.sin(2 * math.pi * f0 * i / FX)
            + 0.25 * math.sin(2 * math.pi * 2 * f0 * i / FX) * math.exp(-i / FX * 4))
            for i in range(n)]


def effects(path, seconds, clicks=(), keys=(), chimes=()):
    """Times in seconds from the start of the video."""
    rng = random.Random(7)
    total = round(seconds * FX)
    buf = [0.0] * total

    def put(t, wave_, gain=1.0, pan=0.0):
        i0 = round(t * FX)
        for i, v in enumerate(wave_):
            if 0 <= i0 + i < total:
                buf[i0 + i] += v * gain

    for t in clicks:
        put(t, _click(rng, "click"), 0.5)
    for t in keys:
        put(t, _click(rng, "key"), 0.4)
    for t in chimes:
        put(t, _chime(659.25), 0.5)
        put(t + 0.22, _chime(987.77), 0.4)
    pcm = _pcm(buf, 1.0)
    st = array.array("h", [0]) * (2 * total)
    st[0::2], st[1::2] = pcm, pcm
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(FX)
        w.writeframes(st.tobytes())


if __name__ == "__main__":
    bed(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 60)
