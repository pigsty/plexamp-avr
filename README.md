# Plexamp AVR

A small systemd service that long-polls a local Plexamp instance and controls a Denon AVR. Playback powers the AVR on; idle playback starts a configurable standby timer. The AVR is only put into standby when its current input exactly matches the configured input.

The service keeps a single persistent telnet connection to the AVR (reconnecting
automatically if it drops) and tracks the power, input, volume and mute state of
zones 1–3 from the events the AVR sends. A mobile-first web UI and an HTTP API
expose basic controls for each zone.

## Install

On the Linux host running Plexamp and able to reach the AVR:

```sh
docker compose up -d --build
```

or

```sh
docker build -t plexamp-avr .
docker run -d \
  --name plexamp-avr \
  --network host \
  --restart unless-stopped \
  -v /etc/plexamp-avr.conf:/etc/plexamp-avr.conf:ro \
  plexamp-avr
```

The installer uses only Python's standard library. Configure `plexamp_host`, `plexamp_port`, `avr_host`, `avr_input`, `off_timer_seconds`, and optionally `preset_volume`. Volume values are Denon values between 0 and 98; leave it empty to skip volume changes. The Plexamp defaults are `localhost:32500`.

Web UI/API settings: `web_enabled` (default `true`), `web_host` (default `0.0.0.0`),
`web_port` (default `8080`) and `avr_inputs`, a comma-separated list of inputs
offered in the web UI (defaults to common Denon sources).

## Web UI

Open `http://<host>:8080/` on a phone or desktop. Tabs Z1, Z2 and Z3 select the
zone; each zone has power, input, volume (slider and −/+) and mute controls.
State updates arrive in realtime over a WebSocket.

## API

The API is served under `/api` on the web port. **There is no authentication**;
only expose it on a trusted network. Zones are `z1` (main zone), `z2` and `z3`
(`1`, `2`, `3` and upper-case forms are also accepted). Request bodies must be
JSON with `Content-Type: application/json`.

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/status` | Connection status and state of all zones |
| `GET` | `/api/zones/{zone}` | State of a single zone |
| `GET` | `/api/inputs` | Inputs offered by the web UI |
| `POST` | `/api/zones/{zone}/power` | `{"power": "on"}` or `{"power": "off"}` |
| `POST` | `/api/zones/{zone}/input` | `{"input": "CD"}` (Denon source name; `SOURCE` follows the main zone in Z2/Z3) |
| `POST` | `/api/zones/{zone}/volume` | `{"volume": 45.5}` (0–98, half steps on Z1, whole steps on Z2/Z3) or `{"volume": "up"}` / `{"volume": "down"}` |
| `POST` | `/api/zones/{zone}/mute` | `{"muted": true}` or `{"muted": false}` |
| `GET` | `/api/ws` | WebSocket status stream |

Status shape (`volume` uses Denon's 0–98 scale, where 80 is 0 dB; fields are
`null` until the AVR has reported them):

```json
{
  "connected": true,
  "zones": {
    "z1": {"power": "on", "input": "MPLAY", "volume": 45.5, "muted": false},
    "z2": {"power": "off", "input": "SOURCE", "volume": 30.0, "muted": false},
    "z3": {"power": "off", "input": "TUNER", "volume": 25.0, "muted": true}
  }
}
```

`GET /api/zones/{zone}` returns `{"zone": "z1", "connected": true, "power": ..., "input": ..., "volume": ..., "muted": ...}`.

Commands are sent to the AVR and acknowledged with `202 Accepted`:

```sh
curl -X POST -H 'Content-Type: application/json' -d '{"power": "on"}' http://localhost:8080/api/zones/z2/power
# {"zone": "z2", "command": "Z2ON"}
```

The resulting state change is reported by the AVR and published on the
WebSocket and in `/api/status`. Errors return JSON `{"error": "..."}` with
`400` (invalid value), `404` (unknown zone or path), `405` (wrong method),
`415` (not JSON) or `503` (AVR not connected).

### WebSocket

Connect to `ws://<host>:8080/api/ws`. The server immediately sends the current
status and then a message whenever the state or AVR connection changes,
including changes made on the AVR itself or by another client:

```json
{"type": "status", "connected": true, "zones": {"z1": {...}, "z2": {...}, "z3": {...}}}
```

Messages sent by the client are ignored; the server sends periodic pings.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The Docker end-to-end tests run the image's default entrypoint against local
mock Plexamp HTTP and Denon AVR TCP servers. They verify playback powers on the
AVR, selects the configured input and volume, and idle playback triggers standby;
that the telnet connection is persistent; the HTTP API commands for all zones;
WebSocket status updates; and that the website loads.
Run it on Linux with Docker available (the test uses host networking):

```sh
docker build -t plexamp-avr:e2e .
python3 -m unittest discover -s e2e -v
```

GitHub Actions runs both the unit tests and the Docker end-to-end test on pushes
and pull requests.
