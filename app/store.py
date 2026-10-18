"""Settings and received messages, kept in the data directory.

config.json     settings changed from the web page (frequency, charset)
messages.jsonl  one received message per line, newest last
"""

import collections
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "freq": 169_800_000,
    "charset": "sv",            # "sv": [\]{|} are ÄÖÅäöå (ISO 646-SE), "ascii"
}
CHARSETS = ("sv", "ascii")
KEEP_MESSAGES = 2000            # in memory, and roughly in the file


class ConfigError(ValueError):
    pass


def _write_atomic(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class Config:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "config.json"
        self.values = dict(DEFAULT_CONFIG)
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.values.update({k: v for k, v in saved.items() if k in DEFAULT_CONFIG})
            except (OSError, ValueError) as e:
                log.warning("ignoring unreadable %s: %s", self.path, e)

    def __getitem__(self, key):
        return self.values[key]

    def update(self, changes, freq_ok=None):
        """Validate and save changes; freq_ok(hz) -> error text or None."""
        new = dict(self.values)
        for key, value in changes.items():
            if key == "freq":
                try:
                    hz = int(round(float(value)))
                except (TypeError, ValueError):
                    raise ConfigError("frequency must be a number (Hz)") from None
                problem = freq_ok(hz) if freq_ok else None
                if problem:
                    raise ConfigError(problem)
                new["freq"] = hz
            elif key == "charset":
                if value not in CHARSETS:
                    raise ConfigError(f"charset must be one of {', '.join(CHARSETS)}")
                new["charset"] = value
            else:
                raise ConfigError(f"unknown setting {key!r}")
        self.values = new
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(self.path, json.dumps(new, indent=2) + "\n")
        return new


class MessageLog:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "messages.jsonl"
        self.recent = collections.deque(maxlen=KEEP_MESSAGES)
        self.next_id = 1
        lines = 0
        if self.path.exists():
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    lines += 1
                    try:
                        self.recent.append(json.loads(line))
                    except ValueError:
                        pass
        if self.recent:
            self.next_id = max(m.get("id", 0) for m in self.recent) + 1
        if lines > 2 * KEEP_MESSAGES:
            self._rewrite()

    def _rewrite(self):
        _write_atomic(self.path, "".join(json.dumps(m, ensure_ascii=False) + "\n"
                                         for m in self.recent))
        self._appended = 0

    _appended = 0

    def add(self, msg):
        msg = {"id": self.next_id, **msg}
        self.next_id += 1
        self.recent.append(msg)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self._appended += 1
        if self._appended > KEEP_MESSAGES:
            self._rewrite()
        return msg

    def latest(self, limit=500):
        return list(self.recent)[-limit:]
