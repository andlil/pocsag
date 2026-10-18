"""POCSAG transmitter, for the simulator and the tests.

encode() turns pages into the bit stream a paging transmitter sends;
modulate() turns bits into 2-FSK complex baseband like a real one.
"""

import numpy as np

from .pocsag import IDLE, NUMERIC_CHARS, SYNC, encode_codeword

PREAMBLE_BITS = 576


def address_codeword(ric, func):
    return encode_codeword(((ric >> 3) << 2) | (func & 3))


def message_codewords(text, alpha=True):
    bits = []
    if alpha:
        for ch in text + "\x04":
            v = ord(ch) & 0x7F
            bits.extend((v >> i) & 1 for i in range(7))
    else:
        for ch in text:
            v = NUMERIC_CHARS.index(ch)
            bits.extend((v >> i) & 1 for i in range(4))
    while len(bits) % 20:                   # pad with NULs / spaces
        if alpha:
            bits.append(0)
        else:
            bits.extend((0xC >> i) & 1 for i in range(4))
    out = []
    for k in range(0, len(bits), 20):
        v = 0
        for b in bits[k:k + 20]:
            v = (v << 1) | b
        out.append(encode_codeword((1 << 20) | v))
    return out


def encode(pages):
    """Bits for a transmission of pages: [(ric, func, text, alpha), ...]."""
    stream = []                             # codewords, 16 per batch

    def put(cw):
        stream.append(cw)

    for ric, func, text, alpha in pages:
        while len(stream) % 16 // 2 != ric % 8:
            put(IDLE)
        put(address_codeword(ric, func))
        if text or alpha:
            for cw in message_codewords(text, alpha):
                put(cw)
    while len(stream) % 16:
        put(IDLE)

    bits = [1, 0] * (PREAMBLE_BITS // 2)
    for i, cw in enumerate(stream):
        if i % 16 == 0:
            bits.extend((SYNC >> (31 - k)) & 1 for k in range(32))
        bits.extend((cw >> (31 - k)) & 1 for k in range(32))
    return bits


def modulate(bits, fs, baud=1200, deviation=4500, offset=0.0, invert=False):
    """2-FSK baseband: '1' is the lower frequency (as most transmitters)."""
    sps = fs / baud
    n = int(len(bits) * sps)
    idx = np.minimum((np.arange(n) / sps).astype(int), len(bits) - 1)
    sym = 1 - 2 * np.asarray(bits, np.float64)[idx]     # 1 -> -1, 0 -> +1
    if invert:
        sym = -sym
    # Soften the edges a little, as a transmitter's filter would.
    k = max(1, int(sps / 4))
    sym = np.convolve(sym, np.ones(k) / k, mode="same")
    phase = np.cumsum(2 * np.pi * (offset + deviation * sym) / fs)
    return np.exp(1j * phase).astype(np.complex64)
