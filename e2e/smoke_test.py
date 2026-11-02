"""Opt-in smoke test for a real Plexamp AVR API and Denon receiver."""
from __future__ import annotations

import argparse
import json
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

TEST_VOLUME = 50.0
RESTORE_PROBE_VOLUME = 48.0
ALLOWED_TEST_INPUTS = {"CD", "AUX", "AUX1"}


class CallbackHandler(BaseHTTPRequestHandler):
    def _record(self) -> None:
        self.server.record(self.path)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_PUT = do_POST = _record

    def log_message(self, _format: str, *args: object) -> None:
        pass


class CallbackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        super().__init__(("0.0.0.0", 0), CallbackHandler)
        self._condition = threading.Condition()
        self._events: list[str] = []

    def record(self, path: str) -> None:
        with self._condition:
            self._events.append(path)
            self._condition.notify_all()

    def mark(self) -> int:
        with self._condition:
            return len(self._events)

    def wait_for(self, path: str, since: int, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while path not in self._events[since:]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


class SmokeTest:
    def __init__(self, base_url: str, inputs: list[str], callback_host: str | None):
        self.base_url = base_url.rstrip("/")
        self.inputs = inputs
        self.callback_host = callback_host or self._route_address()
        self.run_id = uuid.uuid4().hex[:12]
        self.callback = CallbackServer()
        self.callback_thread = threading.Thread(target=self.callback.serve_forever, daemon=True)
        self.callback_thread.start()
        self.webhook_ids: list[str] = []
        self.original: dict | None = None
        self.failures: list[str] = []
        self._closed = False

    def _route_address(self) -> str:
        parts = urlsplit(self.base_url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((parts.hostname, port))
            return probe.getsockname()[0]

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode()
        request = Request(f"{self.base_url}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except HTTPError as err:
            payload = err.read()
            try:
                detail = json.loads(payload)
            except ValueError:
                detail = payload.decode(errors="replace")
            raise RuntimeError(f"{method} {path} returned HTTP {err.code}: {detail}") from err
        except URLError as err:
            raise RuntimeError(f"Could not reach {self.base_url}: {err.reason}") from err

    def zone(self) -> dict:
        return self.request("GET", "/api/zones/z1")[1]

    def command(self, action: str, field: str, value: object) -> None:
        status, response = self.request("POST", f"/api/zones/z1/{action}", {field: value})
        if status != 202:
            raise RuntimeError(f"{action} command returned HTTP {status}: {response}")
        print(f"  sent {response['command']}")

    def wait_for_zone(self, field: str, value: object, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.zone()
            if state.get(field) == value:
                return state
            time.sleep(0.2)
        raise RuntimeError(f"Timed out waiting for z1 {field}={value!r}; last status: {state}")

    def select_input(self, input_name: str, settle: bool = True, timeout: float = 30.0) -> dict:
        self.command("input", "input", input_name)
        state = self.wait_for_zone("input", input_name, timeout)
        if settle:
            time.sleep(5.5)
            state = self.zone()
            if state.get("input") != input_name:
                raise RuntimeError(f"Input changed during volume restore wait: {state}")
        return state

    def set_volume(self, volume: float) -> None:
        if volume > TEST_VOLUME:
            raise ValueError(f"smoke-test volume must not exceed {TEST_VOLUME:g}")
        self.command("volume", "volume", volume)
        state = self.wait_for_zone("volume", volume)
        if state.get("volume") != volume:
            raise RuntimeError(f"Volume command did not take effect: {state}")

    def add_webhook(self, event: str, value: str, path: str) -> None:
        url = f"http://{self.callback_host}:{self.callback.server_port}/{self.run_id}/{path}"
        status, webhook = self.request("POST", "/api/webhooks", {
            "name": f"Smoke test {self.run_id} {event} {value}",
            "zone": "z1",
            "event": event,
            "value": value,
            "method": "POST",
            "url": url,
        })
        if status != 201:
            raise RuntimeError(f"Could not add temporary webhook: HTTP {status}: {webhook}")
        self.webhook_ids.append(webhook["id"])

    def register_webhooks(self) -> None:
        for value in self.inputs:
            self.add_webhook("input", value, f"input/{value.lower()}")
        self.add_webhook("power", "on", "power/on")
        self.add_webhook("power", "off", "power/off")

    def expect_callback(self, event_path: str, since: int) -> None:
        path = f"/{self.run_id}/{event_path}"
        if not self.callback.wait_for(path, since):
            raise RuntimeError(
                f"Webhook callback {event_path!r} was not received. "
                "Check service-to-client routing/firewall and webhook logs."
            )

    def run_webhook_checks(self) -> None:
        assert self.original is not None
        initial_power = self.original["power"]
        if initial_power == "on":
            marker = self.callback.mark()
            self.command("power", "power", "off")
            self.wait_for_zone("power", "off")
            self.expect_callback("power/off", marker)
            print("  PASS power-off webhook")

        print("Checking input command while the AVR is off...")
        marker = self.callback.mark()
        self.command("input", "input", "cd")
        self.expect_callback("input/cd", marker)
        state_after_cd = self.zone()
        print(
            "  PASS CD input-command webhook while off; "
            f"receiver then reported power={state_after_cd['power']}, input={state_after_cd['input']}"
        )

        power_on_path = f"/{self.run_id}/power/on"
        powered_by_input = self.callback.wait_for(power_on_path, marker, timeout=5.0)
        state_after_cd = self.zone()
        if powered_by_input or state_after_cd["power"] == "on":
            if not powered_by_input:
                self.expect_callback("power/on", marker)
            self.wait_for_zone("power", "on")
        else:
            marker = self.callback.mark()
            self.command("power", "power", "on")
            self.wait_for_zone("power", "on")
            self.expect_callback("power/on", marker)
        print("  PASS power-on webhook")
        try:
            self.set_volume(TEST_VOLUME)
        except RuntimeError as err:
            self.failures.append(f"Could not set safe test volume {TEST_VOLUME:g}: {err}")
            print(f"  FAIL safe-volume command/read-back: {err}")

        for input_name in self.inputs:
            marker = self.callback.mark()
            self.command("input", "input", input_name)
            self.expect_callback(f"input/{input_name.lower()}", marker)
            try:
                self.wait_for_zone("input", input_name, timeout=30.0)
            except RuntimeError as err:
                self.failures.append(f"{input_name} did not become the reported input while on: {err}")
                print(f"  FAIL receiver did not settle on {input_name} after the API command")
            else:
                print(f"  PASS input webhook and receiver input {input_name} while AVR is on")
                try:
                    self.set_volume(TEST_VOLUME)
                except RuntimeError as err:
                    failure = f"Could not set safe test volume on {input_name}: {err}"
                    self.failures.append(failure)
                    print(f"  FAIL {failure}")

    def run_volume_checks(self) -> None:
        print(f"Setting test volumes to {TEST_VOLUME:g} or lower, then checking saved-volume restore...")
        for input_name in self.inputs:
            try:
                self.select_input(input_name, settle=False, timeout=30.0)
                self.set_volume(TEST_VOLUME)
            except RuntimeError as err:
                failure = f"{input_name} could not be selected for volume check: {err}"
                if failure not in self.failures:
                    self.failures.append(failure)
                print(f"  FAIL cannot prepare {input_name} volume")

        if len(self.inputs) < 2:
            self.failures.append("Fewer than two inputs were selectable; saved-volume round-trip was not tested")
            return

        for index, input_name in enumerate(self.inputs):
            detour = self.inputs[(index + 1) % len(self.inputs)]
            try:
                self.select_input(input_name, settle=False)
                self.set_volume(RESTORE_PROBE_VOLUME)
                self.select_input(detour)
                state = self.select_input(input_name)
            except RuntimeError as err:
                self.failures.append(f"Could not complete saved-volume round-trip for {input_name}: {err}")
                print(f"  FAIL volume round-trip for {input_name}")
                continue
            if state.get("volume") != RESTORE_PROBE_VOLUME:
                self.failures.append(
                    f"Saved volume failed for {input_name}: expected {RESTORE_PROBE_VOLUME}, "
                    f"observed {state.get('volume')}"
                )
                print(
                    f"  FAIL {input_name} volume expected {RESTORE_PROBE_VOLUME}, "
                    f"observed {state.get('volume')}"
                )
            else:
                print(f"  PASS {input_name} volume restored to {RESTORE_PROBE_VOLUME}")

    def turn_off(self) -> None:
        try:
            if self.zone().get("power") == "off":
                print("AVR is already off; leaving it off")
                return
            marker = self.callback.mark()
            self.command("power", "power", "off")
            self.wait_for_zone("power", "off", timeout=30.0)
            if self.webhook_ids:
                self.expect_callback("power/off", marker)
                print("  PASS power-off webhook; leaving AVR off")
        except Exception as err:
            self.failures.append(f"Could not leave AVR off: {err}")
            print(f"WARNING: Could not confirm AVR standby: {err}")

    def delete_webhooks(self) -> None:
        for webhook_id in reversed(self.webhook_ids):
            try:
                self.request("DELETE", f"/api/webhooks/{webhook_id}")
            except Exception as err:
                print(f"WARNING: Could not remove temporary webhook {webhook_id}: {err}")
        self.webhook_ids.clear()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.turn_off()
        self.delete_webhooks()
        self.callback.shutdown()
        self.callback.server_close()
        self.callback_thread.join(timeout=5)

    def run(self) -> None:
        status, state = self.request("GET", "/api/status")
        if status != 200 or not state.get("connected"):
            raise RuntimeError(f"AVR is not connected: {state}")
        self.original = state["zones"]["z1"]
        if self.original.get("power") not in {"on", "off"}:
            raise RuntimeError(f"Unknown initial AVR power state: {self.original}")
        print(f"Original z1 state: {self.original}")
        print(f"Testing inputs: {', '.join(self.inputs)}")
        print(f"Callback address: {self.callback_host}:{self.callback.server_port}")
        try:
            self.register_webhooks()
            self.run_webhook_checks()
            self.run_volume_checks()
        finally:
            self.close()
        if self.failures:
            raise RuntimeError("\n".join(self.failures))
        print("PASS: input webhooks and saved-volume round-trips work; AVR is off.")


def resolve_inputs(requested: list[str], available: list[str]) -> list[str]:
    resolved: list[str] = []
    for name in requested:
        if name.upper() not in ALLOWED_TEST_INPUTS:
            raise RuntimeError(
                f"Only CD and AUX are permitted for smoke testing, not {name!r}"
            )
        match = next((item for item in available if item.upper() == name.upper()), None)
        if match is None and name.upper() == "AUX":
            match = next((item for item in available if item.upper().startswith("AUX")), None)
        if match is None:
            raise RuntimeError(
                f"Input {name!r} is not in the API's configured inputs: {', '.join(available)}"
            )
        if match not in resolved:
            resolved.append(match)
        if match.upper() != name.upper():
            print(f"Using configured input {match} for requested {name}")
    if len(resolved) < 2:
        raise RuntimeError("At least two distinct inputs are needed for volume restore checks")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run real-system input-webhook and saved-volume smoke checks."
    )
    parser.add_argument("url", help="base API URL, for example http://vault:8090")
    parser.add_argument(
        "--inputs", default="CD,AUX",
        help="CD and/or AUX to test (default: both; AUX maps to the first configured AUX input)",
    )
    parser.add_argument(
        "--callback-host",
        help="address the AVR service can reach on this machine (auto-detected by default)",
    )
    parser.add_argument(
        "--confirm", action="store_true",
        help="required: test commands change AVR inputs, power and volume; AVR is left off",
    )
    args = parser.parse_args()
    if not args.confirm:
        parser.error("this test controls a real AVR; review the effects and pass --confirm")
    parts = urlsplit(args.url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path not in {"", "/"}:
        parser.error("url must be an absolute http:// or https:// base URL without a path")

    smoke: SmokeTest | None = None
    try:
        requested = [item.strip() for item in args.inputs.split(",") if item.strip()]
        smoke = SmokeTest(args.url, requested, args.callback_host)
        available = smoke.request("GET", "/api/inputs")[1].get("inputs", [])
        smoke.inputs = resolve_inputs(requested, available)
        smoke.run()
    except (OSError, RuntimeError, ValueError, KeyError) as err:
        print(f"FAIL: {err}")
        if smoke is not None:
            smoke.close()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
