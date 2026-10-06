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
  -v "$PWD/data":/data \
  plexamp-avr
```

The installer uses only Python's standard library. Configure `plexamp_host`, `plexamp_port`, `avr_host`, `avr_input`, `off_timer_seconds`, and optionally `preset_volume`. Volume values are Denon values between 0 and 98; leave it empty to skip volume changes. The Plexamp defaults are `localhost:32500`.

Web UI/API settings: `web_enabled` (default `true`), `web_host` (default `0.0.0.0`),
`web_port` (default `8080`), `avr_inputs`, a comma-separated list of inputs
offered in the web UI (defaults to common Denon sources), and optional
`avr_input_aliases`, a comma-separated list of `INPUT=Label` pairs for display
only. For example, `MPLAY=Apple TV` displays `MPLAY (Apple TV)` in the input
pickers; the AVR command and webhook value remain `MPLAY`.

Settings made in the web UI (currently webhooks) are saved in a config store
directory, `data_dir` (default `/data`), as `webhooks.json`. The Docker image
declares `/data` as a volume; mount a local directory there (as above, or
`./data` in `docker-compose.yml`) to keep webhooks across container re-creation
and to back them up or edit them on the host. A new `webhooks.json` is created
with mode `0600` because webhook URLs may contain tokens; if you change its mode,
it is preserved on later saves.

## Web UI

Open `http://<host>:8080/` on a phone or desktop. Tabs Z1, Z2 and Z3 select the
zone; each zone has power, input, volume (slider and −/+) and mute controls.
State updates arrive in realtime over a WebSocket.

The **Webhooks** tab lists the configured webhooks and lets you add, edit,
**duplicate** (opens a pre-filled copy, handy for webhooks with similar URLs)
and delete them.

On iPhone or iPad, open the web UI in Safari, tap **Share**, then **Add to Home
Screen**. The app opens without Safari chrome and has a dedicated home-screen
icon. Service-worker caching requires a secure context; use HTTPS for offline
app-shell support. On plain HTTP, the controls still require a live connection
to the server and AVR.

## Webhooks

Each webhook maps an AVR event to an HTTP call:

- **Zone**: `z1`, `z2` or `z3`.
- **Event** and **value**: `power` (`on`/`off`), `mute` (`on`/`off`) or
  `input` (a Denon source name such as `CD`, or empty for any input change).
- **Method**: `GET`, `PUT` or `POST`, with an absolute `http://` or `https://`
  **URL** and an optional **body** (sent for `PUT`/`POST` only, as
  `application/json` when it is valid JSON, otherwise `text/plain`).
- **Headers**: optional JSON object of HTTP header names and string values,
  for example `{"Authorization": "Bearer token"}`. Leave it empty for no
  custom headers. Existing webhooks without this field continue to work.
- **Enabled**: disabled webhooks are kept but not called.

Webhooks fire when the AVR reports a change, whether it was made through this
service, the web UI or on the AVR itself (e.g. `Z1 power off`, `Z2 input CD`).
The state learned when first connecting to the AVR does not trigger webhooks.
Calls are made in the background, one at a time, using `request_timeout_seconds`.
Each call is logged at info level with its response code, for example:

```
INFO Webhook 'Lights off' (Z1 power off): POST http://192.168.1.5/api/scene -> 200
```

Connection failures are logged as warnings.

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
| `GET` | `/api/webhooks` | `{"webhooks": [...]}` |
| `POST` | `/api/webhooks` | Create a webhook (`201`) |
| `GET` | `/api/webhooks/{id}` | A single webhook |
| `PUT` | `/api/webhooks/{id}` | Replace a webhook |
| `DELETE` | `/api/webhooks/{id}` | Delete a webhook |
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

Webhook shape (`name`, `enabled`, `body` and `headers` are optional when creating):

```json
{"id": "6b977e6c58cf4d0d85782cc1b26a4bf4", "name": "Z2 CD", "enabled": true, "zone": "z2",
 "event": "input", "value": "CD", "method": "POST", "url": "http://192.168.1.5/hook", "body": "{\"on\": true}",
 "headers": {"Authorization": "Bearer token"}}
```

Because webhooks make the service send HTTP requests to arbitrary URLs, only
expose the API on a trusted network.

The resulting state change is reported by the AVR and published on the
WebSocket and in `/api/status`. Errors return JSON `{"error": "..."}` with
`400` (invalid value), `404` (unknown zone or path), `405` (wrong method),
`411` (missing `Content-Length`), `413` (body larger than 4 KiB, 64 KiB for
webhooks), `415` (not JSON), `426` (`/api/ws` without a WebSocket upgrade),
`500` (webhooks could not be saved to the config store) or `503` (AVR not
connected).

### WebSocket

Connect to `ws://<host>:8080/api/ws`. The server immediately sends the current
status and then a message whenever the state or AVR connection changes,
including changes made on the AVR itself or by another client:

```json
{"type": "status", "connected": true, "zones": {"z1": {...}, "z2": {...}, "z3": {...}}}
```

Messages sent by the client are ignored; the server sends periodic pings.
Browser connections whose `Origin` does not match the `Host` header are rejected
with `403`, so a reverse proxy must preserve the `Host` header.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The Docker end-to-end tests run the image's default entrypoint against local
mock Plexamp HTTP and Denon AVR TCP servers. They verify playback powers on the
AVR, selects the configured input and volume, and idle playback triggers standby;
that the telnet connection is persistent; the HTTP API commands for all zones;
WebSocket status updates; webhooks saved to a mounted config store and called on
AVR events; and that the website loads.
Run it on Linux with Docker available (the test uses host networking):

```sh
docker build -t plexamp-avr:e2e .
python3 -m unittest discover -s e2e -v
```

GitHub Actions runs both the unit tests and the Docker end-to-end test on pushes
and pull requests.
