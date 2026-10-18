"""POCSAG protocol: codewords, BCH error correction and message decoding.

A transmission is a preamble (alternating bits) followed by batches. Each
batch is a sync codeword and 8 frames of 2 codewords. A pager with RIC r
listens only in frame r % 8, where its address codeword is sent; the message
codewords follow it and may run on into later frames and batches.

Codeword (32 bits, sent MSB first):
    bit 31      0 = address, 1 = message
    bits 30-11  address: 18 RIC bits + 2 function bits; message: 20 data bits
    bits 10-1   BCH(31,21) check bits
    bit 0       even parity over the whole codeword
"""

import logging

log = logging.getLogger(__name__)

SYNC = 0x7CD215D8
IDLE = 0x7A89C197
MASK = 0xFFFFFFFF
BCH_POLY = 0x769            # x^10 + x^9 + x^8 + x^6 + x^5 + x^3 + 1

SYNC_SEARCH_ERRORS = 2      # bit errors allowed when hunting for the first sync
SYNC_BATCH_ERRORS = 4       # ...and where the next batch's sync is expected
FALSE_SYNC_BAD_CW = 5       # a first batch with more bad codewords was noise

NUMERIC_CHARS = "0123456789*U -)("
# Control characters that end an alphanumeric message; the rest is padding.
ALPHA_END = "\x00\x03\x04\x17"


def _remainder(word31):
    """BCH remainder of a 31-bit word (codeword bits 31..1)."""
    r = word31
    for i in range(30, 9, -1):
        if r >> i & 1:
            r ^= BCH_POLY << (i - 10)
    return r


def _parity(x):
    return x.bit_count() & 1


def encode_codeword(data21):
    """Full 32-bit codeword for 21 information bits (flag + 20 bits)."""
    w = (data21 & 0x1FFFFF) << 10
    w |= _remainder(w)
    return (w << 1) | _parity(w)


def _syndrome_table():
    # BCH(31,21) has distance 5, so every 1- and 2-bit error pattern has its
    # own remainder.
    table = {}
    for i in range(31):
        table[_remainder(1 << i)] = 1 << i
        for j in range(i):
            table[_remainder((1 << i) | (1 << j))] = (1 << i) | (1 << j)
    return table


_SYNDROMES = _syndrome_table()


def correct(cw):
    """(corrected codeword, bits fixed), or (None, 0) if beyond repair."""
    rem = _remainder(cw >> 1)
    err = 0
    if rem:
        err = _SYNDROMES.get(rem)
        if err is None:
            return None, 0
        err <<= 1
    fixed = cw ^ err
    n = err.bit_count()
    if _parity(fixed):
        fixed ^= 1
        n += 1
    if n > 2:
        return None, 0
    return fixed, n


# ---- message text ------------------------------------------------------------------

def _bits(chunks):
    """Flatten 20-bit message chunks into transmitted bit order (None = lost)."""
    out = []
    for c in chunks:
        if c is None:
            out.extend([None] * 20)
        else:
            out.extend((c >> (19 - i)) & 1 for i in range(20))
    return out


def _groups(bits, size):
    """Characters of `size` bits, each sent least significant bit first."""
    for k in range(0, len(bits) - size + 1, size):
        g = bits[k:k + size]
        yield None if None in g else sum(b << i for i, b in enumerate(g))


def decode_alpha(chunks):
    out = []
    for v in _groups(_bits(chunks), 7):
        if v is None:
            out.append("�")
            continue
        ch = chr(v)
        if ch in ALPHA_END:
            break
        if ch == "\r":
            ch = "\n"
        elif v < 0x20 and ch not in "\n\t":
            ch = f"<{v:02X}>"
        elif v == 0x7F:
            continue
        out.append(ch)
    return "".join(out).rstrip()


def decode_numeric(chunks):
    return "".join("�" if v is None else NUMERIC_CHARS[v]
                   for v in _groups(_bits(chunks), 4)).rstrip()


def _printable(text):
    if not text:
        return 0.0
    ok = sum(1 for c in text if " " <= c <= "~" or c == "\n")
    return ok / len(text)


def guess_type(func, alpha, numeric):
    """'numeric' or 'alpha'. Pagers know from the RIC; we have to guess.

    Real digits look like random junk when read as 7-bit text and vice
    versa, so pick the reading that looks like what it claims to be,
    with the function bits (0 is usually numeric) as the tie-breaker.
    """
    digits = sum(1 for c in numeric if c in "0123456789 -")
    numeric_score = digits / len(numeric) if numeric else 0.0
    if numeric_score >= 0.9 and (func == 0 or _printable(alpha) < 0.8):
        return "numeric"
    return "alpha"


# ---- bit stream decoder ------------------------------------------------------------

class _Message:
    __slots__ = ("ric", "func", "chunks", "corrected", "bad")

    def __init__(self, ric, func, corrected):
        self.ric = ric
        self.func = func
        self.chunks = []
        self.corrected = corrected
        self.bad = 0


class Decoder:
    """Bits in, decoded pages out through on_message(dict)."""

    def __init__(self, on_message):
        self.on_message = on_message
        self.reg = 0
        self.in_sync = False
        self.inverted = False
        self.batches = 0            # batches decoded (statistics)
        self.corrected_bits = 0
        self.bad_codewords = 0
        self._nbits = 0
        self._want_sync = False
        self._first_batch = False
        self._batch = []
        self._msg = None

    def feed(self, bits):
        for b in bits:
            self.reg = ((self.reg << 1) | b) & MASK
            if not self.in_sync:
                d = (self.reg ^ SYNC).bit_count()
                if d <= SYNC_SEARCH_ERRORS:
                    self._lock(False)
                elif d >= 32 - SYNC_SEARCH_ERRORS:
                    self._lock(True)
                continue
            self._nbits += 1
            if self._nbits < 32:
                continue
            self._nbits = 0
            cw = self.reg ^ MASK if self.inverted else self.reg
            if self._want_sync:
                if (cw ^ SYNC).bit_count() <= SYNC_BATCH_ERRORS:
                    self._want_sync = False
                else:
                    self._lost()
                continue
            self._batch.append(cw)
            if len(self._batch) == 16:
                self._end_batch()

    def flush(self):
        """End of signal: emit any message still being received."""
        self._lost()

    def _lock(self, inverted):
        self.in_sync = True
        self.inverted = inverted
        self._nbits = 0
        self._want_sync = False
        self._first_batch = True
        self._batch = []

    def _lost(self):
        self._finish()
        self.in_sync = False
        self._batch = []

    def _end_batch(self):
        fixed = [correct(cw) for cw in self._batch]
        self._batch = []
        bad = sum(1 for cw, _ in fixed if cw is None)
        if self._first_batch and bad > FALSE_SYNC_BAD_CW:
            # Noise that happened to look like a sync word.
            self._lost()
            return
        self._first_batch = False
        self._want_sync = True
        self.batches += 1
        self.bad_codewords += bad
        for i, (cw, n) in enumerate(fixed):
            self.corrected_bits += n
            self._codeword(i // 2, cw, n)

    def _codeword(self, frame, cw, nfixed):
        msg = self._msg
        if cw is None:
            # Could have been anything, even a new address; keep the gap.
            if msg:
                msg.chunks.append(None)
                msg.bad += 1
            return
        if cw == IDLE:
            self._finish()
        elif not cw >> 31:
            self._finish()
            ric = (((cw >> 13) & 0x3FFFF) << 3) | frame
            self._msg = _Message(ric, (cw >> 11) & 3, nfixed)
        elif msg:
            msg.chunks.append((cw >> 11) & 0xFFFFF)
            msg.corrected += nfixed

    def _finish(self):
        msg, self._msg = self._msg, None
        if msg is None:
            return
        while msg.chunks and msg.chunks[-1] is None:   # lost tail: not part of it
            msg.chunks.pop()
            msg.bad -= 1
        if msg.chunks:
            alpha = decode_alpha(msg.chunks)
            numeric = decode_numeric(msg.chunks)
            kind = guess_type(msg.func, alpha, numeric)
            text = alpha if kind == "alpha" else numeric
        else:
            alpha = numeric = text = ""
            kind = "tone"
        self.on_message({
            "ric": msg.ric, "func": msg.func, "type": kind, "text": text,
            "alpha": alpha, "numeric": numeric,
            "corrected": msg.corrected, "bad": msg.bad,
        })
