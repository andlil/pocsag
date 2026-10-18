"""2-FSK demodulator: complex samples from the SDR in, bits out.

The receiver is tuned OFFSET_HZ below the wanted frequency (keeps the
RTL-SDR's DC spike out of the channel). Per block:

  shift the channel to 0 Hz, low-pass and decimate to CHANNEL_RATE
  -> FM discriminator (instantaneous frequency in Hz)
  -> low-pass to the data bandwidth, decimate to DATA_RATE
  -> clock recovery and integrate-and-dump bit decisions

The bit threshold follows the average of recent '1' and '0' levels, which
also cancels any frequency error between transmitter and receiver; that
error is reported as offset_hz.
"""

import math

import numpy as np
from scipy import signal

OFFSET_HZ = 60_000
CHANNEL_RATE = 32_000
DATA_RATE = 16_000
CHANNEL_CUTOFF_HZ = 7_500   # +-4.5 kHz deviation, 1200 baud, a few kHz of error


class FskDemodulator:
    CLOCK_GAIN = 0.15       # how hard a bit transition pulls the bit clock
    LEVEL_RATE = 0.05       # how fast the '1' / '0' levels follow the signal

    def __init__(self, fs, baud=1200, offset_hz=OFFSET_HZ):
        if fs % CHANNEL_RATE:
            raise ValueError(f"sample rate {fs} is not a multiple of {CHANNEL_RATE}")
        self.fs = fs
        self.baud = baud
        self.d1 = fs // CHANNEL_RATE
        self.d2 = CHANNEL_RATE // DATA_RATE
        # Local oscillator, one period long; indexed by a running sample count.
        period = fs // math.gcd(int(offset_hz), fs)
        self._lo = np.exp(-2j * np.pi * offset_hz * np.arange(period) / fs).astype(np.complex64)
        self._n = 0
        self._sos1 = signal.butter(8, CHANNEL_CUTOFF_HZ, fs=fs, output="sos")
        self._zi1 = np.zeros((self._sos1.shape[0], 2), np.complex128)
        self._ph1 = 0
        self._sos2 = signal.butter(4, baud * 1.25, fs=CHANNEL_RATE, output="sos")
        self._zi2 = np.zeros((self._sos2.shape[0], 2))
        self._ph2 = 0
        self._prev = np.complex64(1)
        # Clock recovery / slicer state
        self._phase = 0.0
        self._inc = baud / DATA_RATE
        self._acc = 0.0
        self._cnt = 0
        self._last = 0.0
        self.hi = 2_000.0
        self.lo = -2_000.0
        self.level_db = -120.0

    @property
    def offset_hz(self):
        """Frequency error of the signal being received (Hz)."""
        return (self.hi + self.lo) / 2

    @property
    def deviation_hz(self):
        return (self.hi - self.lo) / 2

    def process(self, iq):
        """Feed samples at fs; returns the bits they completed."""
        n = len(iq)
        idx = (self._n + np.arange(n)) % len(self._lo)
        self._n = (self._n + n) % len(self._lo)
        x, self._zi1 = signal.sosfilt(self._sos1, iq * self._lo[idx], zi=self._zi1)
        x = x[self._ph1::self.d1]
        self._ph1 = (self._ph1 - n) % self.d1
        if not len(x):
            return []
        x = x.astype(np.complex64)
        power = float(np.mean(x.real ** 2 + x.imag ** 2))
        self.level_db = 10 * math.log10(max(power, 1e-12))

        z = np.concatenate(([self._prev], x))
        self._prev = x[-1]
        freq = np.angle(z[1:] * np.conj(z[:-1])) * (CHANNEL_RATE / (2 * np.pi))
        f, self._zi2 = signal.sosfilt(self._sos2, freq, zi=self._zi2)
        f = f[self._ph2::self.d2]
        self._ph2 = (self._ph2 - len(freq)) % self.d2
        return self._slice(f.tolist())

    def _slice(self, samples):
        bits = []
        phase, inc, gain = self._phase, self._inc, self.CLOCK_GAIN
        acc, cnt, last = self._acc, self._cnt, self._last
        hi, lo, rate = self.hi, self.lo, self.LEVEL_RATE
        thr = (hi + lo) / 2
        for v in samples:
            s = v - thr
            if (s > 0) != (last > 0):
                # Transitions belong on bit boundaries (phase 0): steer there.
                phase -= gain * (phase if phase < 0.5 else phase - 1.0)
            last = s
            acc += v
            cnt += 1
            phase += inc
            if phase >= 1.0:
                phase -= 1.0
                m = acc / cnt
                acc = 0.0
                cnt = 0
                # High frequency = 0 on most transmitters; the decoder copes
                # with the other polarity anyway.
                if m > thr:
                    hi += rate * (m - hi)
                    bits.append(0)
                else:
                    lo += rate * (m - lo)
                    bits.append(1)
                thr = (hi + lo) / 2
        self._phase, self._acc, self._cnt, self._last = phase, acc, cnt, last
        self.hi, self.lo = hi, lo
        return bits
