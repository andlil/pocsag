"""Receiver thread: SDR -> demodulator -> decoder, on one frequency.

Runs in its own thread because reading the SDR blocks. Messages and status
go out through callbacks (called from this thread); set_frequency() may be
called from any thread.
"""

import logging
import queue
import threading
import time

from .demod import OFFSET_HZ, FskDemodulator
from .pocsag import Decoder
from .sources import SourceError

log = logging.getLogger(__name__)

BLOCK_S = 0.1
STATUS_EVERY_S = 0.5
RETRY_S = 5.0


class Receiver:
    def __init__(self, make_source, freq, on_message, on_status, baud=1200):
        self.make_source = make_source
        self.freq = freq
        self.on_message = on_message
        self.on_status = on_status
        self.baud = baud
        self.freq_ranges = []
        self._cmds = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self._src = None

    def begin(self):
        self._thread = threading.Thread(target=self._run, name="receiver", daemon=True)
        self._thread.start()

    def shutdown(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def set_frequency(self, freq):
        self._cmds.put(("freq", freq))

    def check_frequency(self, freq):
        """Error text if the receiver can't tune there, else None."""
        if not self.freq_ranges:
            return None                     # not open yet: can't tell
        lo = freq - OFFSET_HZ
        if any(a <= lo <= b for a, b in self.freq_ranges):
            return None
        return (f"{freq / 1e6:.4f} MHz is outside what this receiver covers ("
                + ", ".join(f"{a / 1e6:.0f}-{b / 1e6:.0f} MHz" for a, b in self.freq_ranges) + ")")

    # ---- thread --------------------------------------------------------------------

    def _status(self, **kw):
        self.on_status({"type": "status", "freq": self.freq, **kw})

    def _run(self):
        while not self._stop.is_set():
            src = self.make_source()
            try:
                self._status(state="starting")
                src.open()
                self.freq_ranges = src.freq_ranges
                self._listen(src)
            except SourceError as e:
                log.error("%s", e)
                self._status(state="error", error=str(e))
                self._stop.wait(RETRY_S)
            except Exception as e:      # keep the service alive whatever happens
                log.exception("receiver failed")
                self._status(state="error", error=f"receiver failed: {e}")
                self._stop.wait(RETRY_S)
            finally:
                try:
                    src.close()
                except Exception:
                    log.exception("closing the SDR failed")

    def _listen(self, src):
        fs = src.sample_rate
        block = int(fs * BLOCK_S)
        dem = dec = None
        stats = {"messages": 0}
        next_status = 0.0

        def got(msg):
            stats["messages"] += 1
            self.on_message({"time": round(time.time(), 3), "freq": self.freq, **msg})

        retune = True
        while not self._stop.is_set():
            try:
                while True:
                    kind, value = self._cmds.get_nowait()
                    if kind == "freq" and value != self.freq:
                        self.freq = value
                        retune = True
            except queue.Empty:
                pass
            if retune:
                if dec:
                    dec.flush()
                src.tune(self.freq - OFFSET_HZ)
                dem = FskDemodulator(fs, self.baud)
                dec = Decoder(got)
                retune = False
                log.info("listening on %.4f MHz (%s, %d S/s)", self.freq / 1e6, src.name, fs)

            dec.feed(dem.process(src.read(block)))

            now = time.monotonic()
            if now >= next_status:
                next_status = now + STATUS_EVERY_S
                self._status(
                    state="receiving" if dec.in_sync else "listening",
                    device=src.name, sample_rate=fs,
                    level_db=round(dem.level_db, 1),
                    offset_hz=round(dem.offset_hz) if dec.in_sync else None,
                    batches=dec.batches, corrected_bits=dec.corrected_bits,
                    bad_codewords=dec.bad_codewords, messages=stats["messages"],
                )
        if dec:
            dec.flush()
