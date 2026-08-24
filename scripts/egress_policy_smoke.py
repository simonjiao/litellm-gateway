from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request


def _request(url: str, *, proxy: str | None, timeout: float) -> tuple[str, int | None]:
    handler = urllib.request.ProxyHandler({"https": proxy} if proxy else {})
    opener = urllib.request.build_opener(handler)
    request = urllib.request.Request(url, headers={"User-Agent": "egress-policy-smoke/1"})
    try:
        with opener.open(request, timeout=timeout) as response:
            return "response", response.status
    except urllib.error.HTTPError as exc:
        if exc.headers.get("X-Squid-Error", "").startswith("ERR_ACCESS_DENIED"):
            return "proxy-denied", exc.code
        return "response", exc.code
    except urllib.error.URLError as exc:
        message = str(exc.reason)
        if "Tunnel connection failed: 403" in message:
            return "proxy-denied", 403
        return f"network-error: {message}", None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--allowed-url", action="append", required=True)
    parser.add_argument("--denied-url", required=True)
    parser.add_argument("--direct-url", required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    failures: list[str] = []
    for url in args.allowed_url:
        outcome, status = _request(url, proxy=args.proxy, timeout=args.timeout)
        print(f"allowed {url}: {outcome} status={status}")
        if outcome != "response":
            failures.append(f"allowed URL failed: {url} ({outcome})")

    outcome, status = _request(args.denied_url, proxy=args.proxy, timeout=args.timeout)
    print(f"denied {args.denied_url}: {outcome} status={status}")
    if outcome != "proxy-denied":
        failures.append(f"denied URL was not rejected by proxy: {args.denied_url}")

    outcome, status = _request(args.direct_url, proxy=None, timeout=args.timeout)
    print(f"direct {args.direct_url}: {outcome} status={status}")
    if outcome == "response":
        failures.append(f"direct internet access unexpectedly succeeded: {args.direct_url}")

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
