"""Self-check for build.py output: python3 test_build.py <m4a folder>"""
import math, sys
import numpy as np
from scipy.signal import butter, sosfiltfilt
sys.argv = [sys.argv[0], sys.argv[1]]
import build

SR = build.SR
NOTE_BAND = butter(4, 80, 'highpass', fs=SR, output='sos')


load = build.load_wav


BRIGHT = butter(4, 2000, 'highpass', fs=SR, output='sos')


def pluck_brightness(x):
    """2 kHz+ energy of the first 40 ms relative to the first 300 ms, in dB: what makes a pluck sound open."""
    on = int(np.argmax(np.abs(x) > 0.03 * np.abs(x).max()))
    return build.db(build.rms(sosfiltfilt(BRIGHT, x)[on:on + 1764])) - build.db(build.rms(x[on:on + 13230]))


def steps(z, a, b):
    """Largest 10 ms step of a 30 ms RMS envelope, in dB."""
    env = [build.db(build.rms(z[i:i + 1323])) for i in range(a, b - 1323, 441)]
    return max(abs(p - q) for p, q in zip(env, env[1:]))


worst_jump = worst_pitch = 0
darker = []
for s, k0 in build.STRINGS.items():
    for fret in range(20):
        name = f'{s}{fret}'
        # junction smoothness: envelope steps across the crossfade vs the recording's own steps before it
        x = build.trim(build.peak_norm(sosfiltfilt(build.HPF, build.decode(f'{build.SRC}/{name}.m4a'))))
        E, W = len(x) - int(0.04 * SR), int(0.12 * SR)
        y = build.extend_tail(x, build.f0(x))
        # measured above 80 Hz: below it only rumble the 30 Hz HPF lets through remains (<= -59 dBFS, inaudible there)
        xh, yh = sosfiltfilt(NOTE_BAND, x), sosfiltfilt(NOTE_BAND, y)
        worst_jump = max(worst_jump, steps(yh, E - W - 2205, E + 6615) - steps(xh, E - 22050, E - W))
        assert build.db(build.rms(y[-2205:]) / np.abs(y).max()) < -60, f'{name}: tail does not decay'
        # baked tuning
        f = load(f'Dombra-Studio/Samples/{name}.wav')
        cents = 1200 * math.log2(build.f0(f) / (440 * 2 ** ((k0 + fret - 69) / 12)))
        worst_pitch = max(worst_pitch, abs(cents))
        # processing must not dull the pluck (the user heard 6.8 dB of lost brightness as "muffled")
        darker.append(pluck_brightness(build.decode(f'{build.SRC}/{name}.m4a')) - pluck_brightness(f))
print(f"tail junction: worst step above the recording's own {worst_jump:+.1f} dB")
print(f'baked tuning: worst error {worst_pitch:.1f} cents')
print(f'pluck brightness vs the recording: worst {-max(darker):+.1f} dB, mean {-np.mean(darker):+.1f} dB')
assert worst_pitch < 5 and worst_jump < 2 and max(darker) < 1.0

# qagys: lower-string pitch present, plus the upper string a fourth below
for fret in (0, 9, 19):
    q = load(f'Dombra-Studio/Samples/q{fret}dn.wav')
    seg = q[int(0.1 * SR):int(0.9 * SR)]
    S = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), 1 << 18)); fr = np.fft.rfftfreq(1 << 18, 1 / SR)
    for midi in (53 + fret, 48 + fret):
        fk = 440 * 2 ** ((midi - 69) / 12); band = np.abs(fr - fk) < fk * 0.01
        assert S[band].max() > 0.05 * S.max(), f'q{fret}: missing {midi}'
print('qagys dyads: both strings present at a perfect fourth')

# drop-in note sets: u0 / d0 / d19 land on the named pitch, and the mp3 starts right at the pluck
worst_lead = 0
for folder, shift in build.NOTE_SETS.items():
    for name, midi in (('u0', 48), ('d0', 53), ('d19', 72), ('u15', 63)):
        x = build.decode(f'Dombra-Studio/{folder}/{name}.mp3')
        cents = 1200 * math.log2(build.f0(x) / (440 * 2 ** ((midi + shift / 100 - 69) / 12)))
        assert abs(cents) < 5, f'{folder}/{name}: {cents:+.1f} cents'
        worst_lead = max(worst_lead, np.argmax(np.abs(x) > 0.03 * np.abs(x).max()) / SR * 1000)
print(f'note sets: pitch ok, worst lead-in after mp3 decode {worst_lead:.0f} ms')
assert worst_lead < 15
print('ok')
