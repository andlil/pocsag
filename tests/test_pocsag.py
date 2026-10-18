"""Tests for the decoder and demodulator, using the built-in transmitter.

    python3 -m unittest discover -s tests -v
"""

import random
import unittest

import numpy as np

from app.demod import OFFSET_HZ, FskDemodulator
from app.encoder import encode, message_codewords, modulate
from app.pocsag import IDLE, SYNC, Decoder, correct, encode_codeword

PAGES = [
    (1234567, 3, "Larm: Brand i byggnad, Storgatan 12", True),
    (8, 0, "0701-234 567", False),
    (2000003, 1, "", False),                              # tone only
    (42, 3, "R[ddningstj{nst \\vning kl 19", True),        # Swedish chars (ISO 646-SE)
]


def decode_bits(bits):
    out = []
    d = Decoder(out.append)
    d.feed(bits)
    d.flush()
    return out


def radio(bits, fs=256_000, snr_db=None, offset=0.0, invert=False, seed=1):
    """What the SDR would hear, tuned OFFSET_HZ below the channel."""
    iq = modulate(bits, fs, offset=OFFSET_HZ + offset, invert=invert)
    iq = np.concatenate((np.zeros(fs // 5, np.complex64), iq, np.zeros(fs // 5, np.complex64)))
    rng = np.random.default_rng(seed)
    noise_amp = 10 ** (-(snr_db if snr_db is not None else 60) / 20)
    # SNR defined in a 12.5 kHz channel
    noise_amp *= np.sqrt(fs / 12_500)
    # float32 throughout: at 1 MS/s these arrays are big for a Pi 3.
    k = np.float32(noise_amp / np.sqrt(2))
    iq.real += rng.standard_normal(len(iq), np.float32) * k
    iq.imag += rng.standard_normal(len(iq), np.float32) * k
    return iq


def demod_decode(iq, fs=256_000, block=25_600):
    out = []
    dem = FskDemodulator(fs)
    dec = Decoder(out.append)
    for k in range(0, len(iq), block):
        dec.feed(dem.process(iq[k:k + block]))
    dec.flush()
    return out, dem


def check_pages(test, got, pages=PAGES):
    test.assertEqual([(m["ric"], m["func"]) for m in got], [(p[0], p[1]) for p in pages])
    for m, (_, _, text, alpha) in zip(got, pages):
        if not text and not alpha:
            test.assertEqual(m["type"], "tone")
        else:
            test.assertEqual(m["type"], "alpha" if alpha else "numeric")
            test.assertEqual(m["text"], text)


class CodewordTests(unittest.TestCase):
    def test_known_codewords_are_valid(self):
        for cw in (SYNC, IDLE):
            self.assertEqual(correct(cw), (cw, 0))
        self.assertEqual(encode_codeword(IDLE >> 11), IDLE)

    def test_corrects_up_to_two_errors(self):
        rnd = random.Random(3)
        for _ in range(300):
            cw = encode_codeword(rnd.getrandbits(21))
            bits = rnd.sample(range(32), rnd.choice((1, 2)))
            bad = cw
            for b in bits:
                bad ^= 1 << b
            self.assertEqual(correct(bad), (cw, len(bits)))

    def test_rejects_most_three_bit_errors(self):
        rnd = random.Random(4)
        rejected = 0
        for _ in range(300):
            cw = encode_codeword(rnd.getrandbits(21))
            for b in rnd.sample(range(32), 3):
                cw ^= 1 << b
            rejected += correct(cw)[0] is None
        self.assertEqual(rejected, 300)   # parity catches every odd error count


class DecoderTests(unittest.TestCase):
    def test_clean_bits(self):
        check_pages(self, decode_bits(encode(PAGES)))

    def test_inverted_bits(self):
        check_pages(self, decode_bits([1 - b for b in encode(PAGES)]))

    def test_bit_errors_are_corrected(self):
        bits = encode(PAGES)
        rnd = random.Random(5)
        # Two errors in every codeword after the preamble
        for start in range(576, len(bits), 32):
            for k in rnd.sample(range(32), 2):
                bits[start + k] ^= 1
        got = decode_bits(bits)
        check_pages(self, got)
        self.assertGreater(sum(m["corrected"] for m in got), 0)

    def test_message_spanning_batches(self):
        long = ("Lang text " * 12).strip()
        self.assertGreater(len(message_codewords(long)), 16)
        check_pages(self, decode_bits(encode([(77, 3, long, True)])), [(77, 3, long, True)])

    def test_noise_gives_nothing(self):
        rnd = random.Random(6)
        self.assertEqual(decode_bits([rnd.getrandbits(1) for _ in range(200_000)]), [])


class DemodulatorTests(unittest.TestCase):
    def test_strong_signal(self):
        got, _ = demod_decode(radio(encode(PAGES)))
        check_pages(self, got)

    def test_frequency_error_and_polarity(self):
        got, dem = demod_decode(radio(encode(PAGES), offset=2_000, invert=True))
        check_pages(self, got)

    def test_weak_signal(self):
        got, _ = demod_decode(radio(encode(PAGES), snr_db=12))
        check_pages(self, got)

    def test_other_sample_rates(self):
        pages = PAGES[:1]                   # short: keeps memory use low
        for fs in (384_000, 768_000, 1_024_000):
            with self.subTest(fs=fs):
                got, _ = demod_decode(radio(encode(pages), fs=fs), fs=fs, block=fs // 10)
                check_pages(self, got, pages)


if __name__ == "__main__":
    unittest.main()
