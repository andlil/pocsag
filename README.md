# POCSAG

Receives and decodes POCSAG pager messages (1200 baud) with an RTL-SDR or
Airspy HF+ on a Raspberry Pi, and shows them in a web page. The frequency to
listen to is set from the page.

## Run

```sh
docker compose pull      # or: docker compose build
docker compose up -d
```

Open `http://<pi>:8081/`. Settings and received messages are kept in `./data/`
(`config.json`, `messages.jsonl`).

Only one program can use an SDR at a time. To run this next to
[sdr-scanner](https://github.com/andlil/sdr-scanner), give each its own
receiver with `SDR_DEVICE` in `docker-compose.override.yml`:

```yaml
services:
  pocsag:
    environment:
      SDR_DEVICE: "driver=rtlsdr,serial=00000001"
```

List connected receivers: `docker compose stop && docker compose run --rm pocsag python3 -m app.devices`.

Try it without hardware: `SDR_DEVICE: "driver=sim"` sends test pages now and then.

## Settings (environment)

| Variable     | Default | Meaning |
|--------------|---------|---------|
| `SDR_DEVICE` | `""`    | Receiver: first found, `driver=rtlsdr`, `driver=airspyhf`, `...,serial=...`, `driver=sim` |
| `SDR_GAIN`   | `auto`  | AGC, or a fixed gain in dB |
| `SDR_PPM`    | `0`     | RTL-SDR frequency correction. The page shows the signal's offset while receiving, which helps to set this |

## How it works

```
SDR (256 kS/s, tuned 60 kHz below the channel)
 -> shift, low-pass, decimate to 32 kHz          app/demod.py
 -> FM discriminator, low-pass, 16 kHz
 -> bit clock recovery, integrate-and-dump slicer
 -> sync word search (either polarity), batches   app/pocsag.py
 -> BCH(31,21) correction of up to 2 bit errors per codeword
 -> address / message codewords -> pages
 -> data/messages.jsonl and the web page          app/server.py
```

Alphanumeric or numeric is not signalled on air (the pager knows from its RIC),
so both readings are decoded and the more plausible one is shown; click a
message to see both. Swedish pagers send ÄÖÅäöå as `[\]{|}` (ISO 646-SE); the
character set setting chooses how these are shown. Stored text is always the
raw characters.

### API

- `GET /api/messages?limit=100` — latest messages, oldest first.
- WebSocket `/api/ws` — live messages, status and settings (see `app/server.py`).

Message fields: `id`, `time` (Unix seconds), `freq` (Hz), `ric`, `func` (0–3 =
A–D), `type` (`alpha`, `numeric`, `tone`), `text`, `alpha`, `numeric`,
`corrected` (bits fixed), `bad` (codewords lost).

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## Planned

- Choose which RICs to listen for.
- Handle duplicate messages (the same page sent repeatedly or from several transmitters).
- HTTP POST each message to an existing web page.
