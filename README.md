# Plexamp AVR

A small systemd service that long-polls a local Plexamp instance and controls a Denon AVR. Playback powers the AVR on; idle playback starts a configurable standby timer. The AVR is only put into standby when its current input exactly matches the configured input.

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

### Idle timer webhooks

Set `idle_timer_start_webhook_url` and/or `idle_timer_stop_webhook_url` to have the service call an external URL when the idle timer starts and stops. `webhook_method` selects the HTTP method (`GET`, `POST`, `PUT` or `PATCH`; default `POST`). Calls are made in the background, use `request_timeout_seconds`, and failures are only logged. For non-`GET` methods a JSON body is sent:

```json
{"event": "idle_timer_started", "state": "paused", "timeout_seconds": 900, "timestamp": 1760000000.0}
{"event": "idle_timer_stopped", "state": "playing", "reason": "playback_resumed", "timestamp": 1760000000.0}
```

The stop `reason` is `playback_resumed` when playback starts again before the timer fires, or `expired` when the timer elapses.

## Development

```sh
python3 -m unittest discover -s tests -v
```
