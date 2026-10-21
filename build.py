"""Builds the Dombra Studio sample library (SFZ + DecentSampler) from the raw dombra recordings.

Usage: python3 build.py <folder with u0..u19.m4a, d0..d19.m4a, beat.mp3> [out_dir]
Needs ffmpeg, numpy and scipy.

Per note: 30 Hz high-pass (DC drift), peak -1 dBFS, head trim, additive tail extension,
tuning baked to A=440. Plus a "Qagys" preset of pre-rendered two-string strums.
"""
import json, math, os, subprocess, sys, wave
import numpy as np
from scipy.signal import butter, resample, sosfiltfilt

SRC = sys.argv[1]
OUT = sys.argv[2] if len(sys.argv) > 2 else 'Dombra-Studio'
SR = 44100
STRINGS = {'u': 48, 'd': 53}   # open string MIDI note: upper = C3, lower = F3 (measured, not D3/G3)
BEAT_KEY = 36                  # C2: body knock
LOW_EXT, HIGH_EXT = 3, 4       # extra keys stretched from the lowest/highest sample: A2..E5
# each note sits at its own -1 dBFS peak; only notes louder than this (RMS of the first 300 ms) are turned down.
# Boosting quiet notes would need a limiter on the pluck, which dulls the attack.
TARGET_RMS = -12.0
# No synthetic velocity layers: low-passed p/mf copies took 6.8 dB off the 2 kHz+ pluck and sounded muffled,
# so velocity only sets the volume. Real soft layers need real soft recordings.
# round robins replaying one file with a small detune (semitones) and level change (dB): kills the "machine gun" on repeats
RR = [(0.0, 0.0), (0.05, -0.6), (-0.05, -0.3)]
# drop-in note sets for players that load u0..u19 / d0..d19 / beat.mp3: folder -> transposition in cents
NOTE_SETS = {'Notes-D-G': 200, 'Notes-C-F': 0}
NAMES = 'C C# D D# E F F# G G# A A# B'.split()
# 82 % of the energy below 80 Hz sits under 20 Hz (DC drift), so 30 Hz removes it without touching the body thump
HPF = butter(4, 30, 'highpass', fs=SR, output='sos')


def decode(path):
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-ac', '1', '-ar', str(SR), '-f', 'f32le', '-'],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32).astype(np.float64)


def write_wav(path, x):
    assert np.abs(x).max() < 1, f'{path} would clip'
    pcm = (x * 2**23).astype(np.int32)
    b = np.stack([pcm & 255, (pcm >> 8) & 255, (pcm >> 16) & 255], 1).astype(np.uint8).tobytes()
    with wave.open(path, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(3); w.setframerate(SR); w.writeframes(b)


def load_wav(path):
    with wave.open(path) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), np.uint8).reshape(-1, 3).astype(np.int32)
    v = a[:, 0] | (a[:, 1] << 8) | (a[:, 2] << 16)
    return np.where(v >= 1 << 23, v - (1 << 24), v) / 2**23


def db(v):
    return 20 * math.log10(max(v, 1e-12))


def rms(x):
    return float(np.sqrt(np.mean(x ** 2)))


def peak_norm(x, peak_db=-1.0):
    # the AAC decode overshoots 0 dBFS by up to +1.9 dB, so scale instead of clipping
    return x * (10 ** (peak_db / 20) / np.abs(x).max())


def trim(x):
    on = int(np.argmax(np.abs(x) > 0.03 * np.abs(x).max()))
    x = x[max(0, on - int(0.005 * SR)):].copy()      # keep 5 ms before the pluck
    x[:int(0.002 * SR)] *= np.linspace(0, 1, int(0.002 * SR))
    return x


def f0(x):
    """Fundamental of the sustain (0.1-0.9 s after the pluck), harmonic product spectrum."""
    seg = x[int(0.1 * SR):int(0.9 * SR)]
    seg = seg * np.hanning(len(seg))
    n = 1 << 18
    spec, fr = np.abs(np.fft.rfft(seg, n)), np.fft.rfftfreq(n, 1 / SR)
    idx = np.where((fr > 100) & (fr < 600))[0]
    hps = spec[idx] * spec[np.minimum(2 * idx, len(spec) - 1)] * spec[np.minimum(3 * idx, len(spec) - 1)]
    i = idx[np.argmax(hps)]
    a, b, c = np.log(spec[i - 1:i + 2])
    return fr[i] + 0.5 * (a - c) / (a - 2 * b + c) * (fr[1] - fr[0])


def extend_tail(x, f, max_len=4.0, floor_db=-70):
    """The recordings stop at -26..-42 dB. Continue each harmonic with its own measured decay
    (least-squares fit on the last 120 ms) and crossfade into it, down to floor_db."""
    x = x[:len(x) - int(0.04 * SR)]                 # skip any fade/edit at the very end
    E, W, H = len(x), int(0.12 * SR), int(0.2 * SR)
    n = 1 << 16
    spec = np.abs(np.fft.rfft(x[E - 2 * H:] * np.hanning(2 * H), n))
    freqs, rates = [], []
    for k in range(1, int(6000 / f) + 1):
        i = int(round(k * f * n / SR)); lo, hi = i - int(0.04 * k * f * n / SR) - 2, i + int(0.04 * k * f * n / SR) + 3
        j = lo + int(np.argmax(spec[lo:hi]))
        a, b, c = np.log(spec[j - 1:j + 2] + 1e-12)
        fk = (j + 0.5 * (a - c) / (a - 2 * b + c)) * SR / n
        t = np.arange(H) / SR
        amp = [abs(np.sum(x[s:s + H] * np.hanning(H) * np.exp(-2j * np.pi * fk * t))) for s in (E - 2 * H, E - H)]
        rate = math.log(max(amp[1], 1e-12) / max(amp[0], 1e-12)) / (H / SR)
        freqs.append(fk); rates.append(min(-1.5, max(-25.0, rate)))
    t = np.arange(-W, 0) / SR
    cols = []
    for fk, r in zip(freqs, rates):
        e = np.exp(r * t)
        cols += [e * np.cos(2 * np.pi * fk * t), e * np.sin(2 * np.pi * fk * t)]
    coef = np.linalg.lstsq(np.stack(cols, 1), x[E - W:], rcond=None)[0]
    t = np.arange(-W, int(max_len * SR)) / SR
    synth = np.zeros(len(t))
    for i, (fk, r) in enumerate(zip(freqs, rates)):
        synth += np.exp(r * t) * (coef[2 * i] * np.cos(2 * np.pi * fk * t) + coef[2 * i + 1] * np.sin(2 * np.pi * fk * t))
    fade = np.linspace(0, 1, W)
    y = np.concatenate([x[:E - W], x[E - W:] * (1 - fade) + synth[:W] * fade, synth[W:]])
    # cut where a 50 ms window falls below floor_db re peak, then fade out
    pk, blk = np.abs(y).max(), int(0.05 * SR)
    end = len(y)
    for s in range(E, len(y) - blk, blk):
        if db(rms(y[s:s + blk]) / pk) < floor_db:
            end = s + blk; break
    y = y[:end]
    y[-blk:] *= np.linspace(1, 0, blk)
    return y


def retune(x, cents):
    return resample(x, int(round(len(x) / 2 ** (cents / 1200))))


def save_sample(name, x, out):
    """Writes Samples/<name>.wav at a -1 dBFS peak; returns the gain (<= 0 dB) that tames notes louder than TARGET_RMS."""
    x = peak_norm(x)
    write_wav(f'{out}/Samples/{name}.wav', x)
    return round(min(0.0, TARGET_RMS - db(rms(x[:int(0.3 * SR)]))), 1)


def build():
    os.makedirs(f'{OUT}/Samples', exist_ok=True)
    for old in os.listdir(f'{OUT}/Samples'):
        os.remove(f'{OUT}/Samples/{old}')
    notes, report = {}, []
    for s, open_key in STRINGS.items():
        for fret in range(20):
            name = f'{s}{fret}'
            raw = decode(f'{SRC}/{name}.m4a')
            x = trim(peak_norm(sosfiltfilt(HPF, raw)))
            key = open_key + fret       # frets are chromatic; pitch only decides the fine tuning
            f = f0(x)
            cents = round((key - (69 + 12 * math.log2(f / 440))) * 100)
            assert abs(cents) < 100, f'{name}: measured pitch is not near key {key}'
            tail_before = db(rms(x[-int(0.05 * SR):]) / np.abs(x).max())
            x = retune(extend_tail(x, f), cents)
            f = 440 * 2 ** ((key - 69) / 12)
            notes[name] = dict(name=name, key=key, x=x, f=f, gain=save_sample(name, x, OUT))
            report.append(dict(name=name, key=key, cents=cents, gain=notes[name]['gain'],
                               tail_before=round(tail_before, 1), length=round(len(x) / SR, 2)))
            print(f'{name:4} {NAMES[key % 12]}{key // 12 - 1:<3} tune {cents:+4d}c  gain {notes[name]["gain"]:+.1f} dB  '
                  f'tail {tail_before:6.1f} dB -> extended to {len(x) / SR:.2f}s')

    # Qagys: both strings at the same fret (a fourth apart), down strum = upper string first, up = lower first
    for fret in range(20):
        u, d = notes[f'u{fret}']['x'], notes[f'd{fret}']['x']
        for strum, first, second, gap, extra in (('dn', u, d, 0.014, 0), ('up', d, u, 0.010, -1.5)):
            g = int(gap * SR)
            y = np.zeros(max(len(first), len(second) + g))
            y[:len(first)] += first; y[g:g + len(second)] += second * 0.9
            name = f'q{fret}{strum}'
            notes[name] = dict(name=name, key=53 + fret, f=notes[f'd{fret}']['f'],
                               gain=save_sample(name, y, OUT) + extra)

    beat = decode(f'{SRC}/beat.mp3')
    write_wav(f'{OUT}/Samples/beat.wav', peak_norm(sosfiltfilt(HPF, beat)))

    keys = {}
    for n in notes.values():
        if n['name'][0] in 'ud':
            keys.setdefault(n['key'], []).append(n)
    assert sorted(keys) == list(range(48, 73)), 'keyboard must cover C3..C5 without gaps'
    single = {k: v for k, v in keys.items()}
    ranges = {k: (k, k) for k in keys}
    ranges[48], ranges[72] = (48 - LOW_EXT, 48), (72, 72 + HIGH_EXT)
    qagys = {53 + fret: [notes[f'q{fret}dn'], notes[f'q{fret}up']] for fret in range(20)}
    qranges = {k: (k, k) for k in qagys}
    write_sfz('Dombra.sfz', 'Kazakh dombra, single notes. Keys F3-G4 alternate between the two strings.', single, ranges)
    write_sfz('Dombra-Qagys.sfz', 'Qagys: both strings at one fret, down/up strums alternate. Melody = lower string.',
              qagys, qranges)
    write_dspreset('Dombra.dspreset', single, ranges)
    write_dspreset('Dombra-Qagys.dspreset', qagys, qranges)
    json.dump(report, open(f'{OUT}/build_report.json', 'w'), indent=1)
    write_note_sets(notes, peak_norm(sosfiltfilt(HPF, beat), -3))


def write_note_sets(notes, beat):
    """One loudness-matched mp3 per note under the original names, for apps that play u0..d19 directly."""
    level = {n: peak_norm(notes[n]['x']) * 10 ** (notes[n]['gain'] / 20) for n in notes if n[0] in 'ud'}
    for folder, cents in NOTE_SETS.items():
        os.makedirs(f'{OUT}/{folder}', exist_ok=True)
        for name, x in list(level.items()) + [('beat', beat)]:
            y = retune(x, cents) if cents and name != 'beat' else x
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'f64le', '-ar', str(SR), '-ac', '1', '-i', '-',
                            '-c:a', 'libmp3lame', '-b:a', '192k', f'{OUT}/{folder}/{name}.mp3'],
                           input=np.clip(y, -1, 1).astype('<f8').tobytes(), check=True)


def write_sfz(fname, title, keys, ranges):
    lines = [f'// Dombra Studio - {title}', '// A=440, tuning baked into the samples. Velocity sets the volume only.',
             '<control> default_path=Samples/', '',
             # pitch/amp_random: SFZ's own round-robin variation, same spread as RR in the DecentSampler presets
             '<global> ampeg_attack=0.001 ampeg_release=0.8 amp_veltrack=70 volume=-3 pitch_random=5 amp_random=0.6', '']
    for key, rs in sorted(keys.items()):
        lo, hi = ranges[key]
        for i, r in enumerate(rs):
            rand = f' lorand={i / len(rs):.2f} hirand={(i + 1) / len(rs):.2f}' if len(rs) > 1 else ''
            lines.append(f'<region> sample={r["name"]}.wav lokey={lo} hikey={hi} pitch_keycenter={key} '
                         f'volume={r["gain"]}{rand}')
    lines += ['', '// body knock (шанақ)',
              f'<region> sample=beat.wav key={BEAT_KEY} pitch_keytrack=0 loop_mode=one_shot']
    open(f'{OUT}/{fname}', 'w').write('\n'.join(lines) + '\n')


def write_dspreset(fname, keys, ranges):
    def zones(key, rs):
        """Every source sample x every round robin for one key."""
        lo, hi = ranges[key]
        return [f'      <sample path="Samples/{r["name"]}.wav" rootNote="{key}" loNote="{lo}" hiNote="{hi}" '
                f'tuning="{dt}" volume="{r["gain"] + dv:.1f}dB" seqPosition="{i * len(RR) + j + 1}"/>'
                for i, r in enumerate(rs) for j, (dt, dv) in enumerate(RR)]
    single = [z for k, rs in sorted(keys.items()) if len(rs) == 1 for z in zones(k, rs)]
    both = [z for k, rs in sorted(keys.items()) if len(rs) > 1 for z in zones(k, rs)]
    knob = ('      <labeled-knob x="{x}" y="60" width="90" label="{label}" type="float" minValue="{lo}" maxValue="{hi}" '
            'value="{v}" textColor="FFF2E6D0" trackForegroundColor="FFC9A35C">\n'
            '        <binding type="{t}" level="instrument" position="{pos}" parameter="{p}"/>\n'
            '      </labeled-knob>')
    knobs = [('Volume', 0, 1, 0.5, 'amp', 0, 'AMP_VOLUME'), ('Attack', 0.001, 1, 0.001, 'amp', 0, 'ENV_ATTACK'),
             ('Release', 0.05, 4, 0.8, 'amp', 0, 'ENV_RELEASE'), ('Tone', 300, 22000, 22000, 'effect', 0, 'FX_FILTER_FREQUENCY'),
             ('Reverb', 0, 1, 0.15, 'effect', 1, 'FX_REVERB_WET_LEVEL'), ('Room', 0, 1, 0.5, 'effect', 1, 'FX_REVERB_ROOM_SIZE')]
    ui = '\n'.join(knob.format(x=20 + 100 * i, label=l, lo=lo, hi=hi, v=v, t=t, pos=pos, p=p)
                   for i, (l, lo, hi, v, t, pos, p) in enumerate(knobs))
    groups = [g for g in [('Single string', len(RR), single), ('Alternating', 2 * len(RR), both)] if g[2]]
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<DecentSampler minVersion="1.0.0">
  <ui width="812" height="375" bgColor="FF2B1D14">
    <tab name="main">
      <label x="20" y="10" width="600" height="30" text="DOMBRA STUDIO{' · ҚАҒЫС' if 'Qagys' in fname else ''}" textColor="FFF2E6D0" textSize="24"/>
{ui}
    </tab>
  </ui>
  <groups attack="0.001" decay="0" sustain="1" release="0.8" ampVelTrack="0.7">
''' + ''.join(f'''    <group name="{n}" seqMode="random" seqLength="{length}">
{chr(10).join(s)}
    </group>
''' for n, length, s in groups) + f'''    <group name="Body knock" seqMode="always">
      <sample path="Samples/beat.wav" rootNote="{BEAT_KEY}" loNote="{BEAT_KEY}" hiNote="{BEAT_KEY}" pitchKeyTrack="0" trigger="attack" loopEnabled="false"/>
    </group>
  </groups>
  <effects>
    <effect type="lowpass" frequency="22000"/>
    <effect type="reverb" roomSize="0.5" damping="0.4" wetLevel="0.15"/>
  </effects>
</DecentSampler>
'''
    open(f'{OUT}/{fname}', 'w').write(xml)


if __name__ == '__main__':
    build()
