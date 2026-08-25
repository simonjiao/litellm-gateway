from __future__ import annotations

import argparse
import socket
import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeniedTarget:
    label: str
    host: str
    port: int


def denied_target(value: str) -> DeniedTarget:
    try:
        label, endpoint = value.split("=", 1)
        host, port_text = endpoint.rsplit(":", 1)
        port = int(port_text)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "target must use label=IPv4-or-hostname:port"
        ) from exc
    if not label or not host or not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            "target must use label=IPv4-or-hostname:port"
        )
    return DeniedTarget(label=label, host=host, port=port)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--denied-target", action="append", type=denied_target, required=True
    )
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()

    failures: list[str] = []
    for target in args.denied_target:
        try:
            with socket.create_connection(
                (target.host, target.port), timeout=args.timeout
            ):
                pass
        except OSError as exc:
            print(
                f"denied {target.label} {target.host}:{target.port}: {exc}",
                flush=True,
            )
            continue
        failures.append(
            f"denied target unexpectedly reachable: "
            f"{target.label} ({target.host}:{target.port})"
        )

    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
