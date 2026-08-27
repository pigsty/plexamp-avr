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

## Development

```sh
python3 -m unittest discover -s tests -v
```
