"""Vendor the LiveKit browser SDK for the ops console.

The console must not depend on a CDN at demo time -- a flaky network should
never break a recording -- and `ops/vendor/` is gitignored, so a fresh clone
does not have it. Both the Docker build and the local setup call this.

Standard library only, on purpose: it runs inside the image build before the
application's dependencies matter, and it has to work identically on Windows,
where neither `curl` nor `bash` can be assumed.
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

URL = "https://cdn.jsdelivr.net/npm/livekit-client@2/dist/livekit-client.umd.min.js"
DEST = Path(__file__).resolve().parents[1] / "ops" / "vendor" / "livekit-client.umd.min.js"


def main() -> None:
    DEST.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(URL, timeout=60) as response:
            payload = response.read()
    except OSError as exc:
        raise SystemExit(f"could not fetch the browser SDK from {URL}: {exc}")

    # A CDN error page is still a 200 with a body. Anything this far under the
    # real ~500 KB bundle is not the SDK, and writing it would fail later as a
    # syntax error in the browser, which is a much harder thing to diagnose.
    if len(payload) < 100_000:
        raise SystemExit(f"got {len(payload)} bytes from {URL}; that is not the SDK")

    DEST.write_bytes(payload)
    print(f"vendored {len(payload)} bytes -> {DEST.parent}")


if __name__ == "__main__":
    sys.exit(main())
