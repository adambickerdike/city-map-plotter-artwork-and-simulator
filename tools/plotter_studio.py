#!/usr/bin/env python3
"""Run the local interactive SVG plot simulator and timing studio.

The browser UI can load another SVG without restarting.  Planning remains in
Python, using the same motion engine as ``plotsim.py`` and ``plotter_control.py``;
the browser only animates the resulting job.  The server binds to loopback by
default and never exposes hardware execution endpoints.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import secrets
import sys
import tempfile
import threading
import webbrowser
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_plotsim_viewer import TEMPLATE, _encode_plate, _json_for_html  # noqa: E402
from plotsim import Machine  # noqa: E402

MAX_SVG_BYTES = 64 * 1024 * 1024


def _payload(
    paths: list[Path], machine: Machine, tolerance: float, api_token: str
) -> dict:
    return {
        "machine": machine.as_dict(),
        "live": True,
        "api_token": api_token,
        "plates": [_encode_plate(path, machine, tolerance) for path in paths],
    }


def _page(payload: dict) -> bytes:
    title = (
        payload["plates"][0].get("title")
        if len(payload.get("plates", [])) == 1
        else "Plotter Studio"
    ) or "Plotter Studio"
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("__TITLE__", escape(str(title), quote=False))
    html = html.replace("__DATA__", _json_for_html(payload))
    return html.encode("utf-8")


def _query_float(
    query: dict[str, list[str]], key: str, fallback: float, *, minimum: float = 0.0
) -> float:
    if key not in query:
        return fallback
    try:
        value = float(query[key][-1])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number") from exc
    if not math.isfinite(value) or value <= minimum:
        relation = "non-negative" if minimum < 0 else "greater than zero"
        raise ValueError(f"{key} must be finite and {relation}")
    return value


def _machine_from_query(query: dict[str, list[str]], base: Machine) -> Machine:
    values = base.as_dict()
    mapping = {
        "down": "pen_down_speed_mm_s",
        "up": "pen_up_speed_mm_s",
        "accel": "acceleration_mm_s2",
        "lift": "pen_lift_s",
        "lower": "pen_lower_s",
        "change": "pen_change_s",
        "corner": "cornering_tolerance_mm",
        "flatness": "curve_flatness_mm",
        "uncertainty": "timing_uncertainty_fraction",
    }
    nonnegative = {"lift", "lower", "change", "uncertainty"}
    for query_key, field_name in mapping.items():
        values[field_name] = _query_float(
            query,
            query_key,
            float(values[field_name]),
            minimum=-1e-15 if query_key in nonnegative else 0.0,
        )
    if values["timing_uncertainty_fraction"] > 1:
        raise ValueError("uncertainty must not exceed 1.0")
    return Machine(**values)


def _handler(
    initial_page: bytes,
    base_machine: Machine,
    display_tolerance: float,
    api_token: str,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "CityMapPlotterStudio/1"

        def _send(
            self,
            status: HTTPStatus,
            body: bytes,
            content_type: str,
            *,
            compress: bool = False,
        ) -> None:
            use_gzip = compress and "gzip" in self.headers.get("Accept-Encoding", "")
            if use_gzip:
                body = gzip.compress(body, compresslevel=5)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; img-src data:; connect-src 'self'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            if use_gzip:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: HTTPStatus, message: str) -> None:
            body = json.dumps({"error": message}, separators=(",", ":")).encode()
            self._send(status, body, "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            if path == "/":
                self._send(
                    HTTPStatus.OK,
                    initial_page,
                    "text/html; charset=utf-8",
                    compress=True,
                )
                return
            if path == "/health":
                self._send(HTTPStatus.OK, b'{"ok":true}', "application/json")
                return
            self._error(HTTPStatus.NOT_FOUND, "not found")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path != "/api/plan":
                self._error(HTTPStatus.NOT_FOUND, "not found")
                return
            supplied_token = self.headers.get("X-Plotter-Token", "")
            if not secrets.compare_digest(supplied_token, api_token):
                self._error(HTTPStatus.FORBIDDEN, "invalid local studio token")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
                return
            if length <= 0 or length > MAX_SVG_BYTES:
                self._error(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    f"SVG must be between 1 byte and {MAX_SVG_BYTES} bytes",
                )
                return
            payload = self.rfile.read(length)
            filename = Path(self.headers.get("X-Filename", "uploaded.svg")).name
            if not filename.casefold().endswith(".svg"):
                filename += ".svg"
            try:
                query = parse_qs(parsed.query, keep_blank_values=False)
                machine = _machine_from_query(query, base_machine)
                with tempfile.TemporaryDirectory(
                    prefix="city-map-plotter-studio-"
                ) as directory:
                    path = Path(directory) / filename
                    path.write_bytes(payload)
                    plate = _encode_plate(path, machine, display_tolerance)
                response = json.dumps(
                    {
                        "machine": machine.as_dict(),
                        "live": True,
                        "api_token": api_token,
                        "plates": [plate],
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
            except (OSError, ValueError, SystemExit) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            self._send(
                HTTPStatus.OK,
                response,
                "application/json; charset=utf-8",
                compress=True,
            )

        def log_message(self, format: str, *args: object) -> None:
            print(f"studio: {format % args}", file=sys.stderr)

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("svg", nargs="+", type=Path)
    parser.add_argument("--machine-profile", type=Path)
    parser.add_argument("--display-tolerance", type=float, default=0.08)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8042)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a non-loopback bind (uploaded SVGs are processed locally)",
    )
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not args.allow_remote:
        parser.error("non-loopback --host requires --allow-remote")
    missing = [path for path in args.svg if not path.is_file()]
    if missing:
        parser.error("missing SVG(s): " + ", ".join(str(path) for path in missing))
    try:
        machine = (
            Machine.from_json(args.machine_profile)
            if args.machine_profile
            else Machine()
        )
        api_token = secrets.token_urlsafe(24)
        initial_payload = _payload(args.svg, machine, args.display_tolerance, api_token)
    except ValueError as exc:
        parser.error(str(exc))
    server = ThreadingHTTPServer(
        (args.host, args.port),
        _handler(_page(initial_payload), machine, args.display_tolerance, api_token),
    )
    address = f"http://{args.host}:{server.server_port}/"
    print(f"Plotter Studio: {address}")
    print("Ctrl-C stops the local viewer; hardware execution is not exposed here.")
    if not args.no_browser:
        threading.Timer(0.35, lambda: webbrowser.open(address)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nstopping Plotter Studio")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
