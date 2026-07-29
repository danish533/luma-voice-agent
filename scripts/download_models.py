"""Fetch the local model weights so the first call does not pay for them.

Silero VAD and the turn-detector run on the worker, not in the cloud. Importing
the plugins registers them; `download_files()` then pulls their weights.

`livekit.agents download-files` would also work, but only for plugins that have
already been imported -- and the voice plugins are imported lazily inside
`runtime.build_runtime`, so it would silently miss the turn detector.
"""

from __future__ import annotations

from livekit.agents import Plugin
from livekit.plugins import silero  # noqa: F401  (registers the VAD plugin)
from livekit.plugins.turn_detector import english  # noqa: F401  (registers the EOU model)


def main() -> None:
    plugins = Plugin.registered_plugins
    if not plugins:
        raise SystemExit("no plugins registered; check the imports above")
    for plugin in plugins:
        print(f"downloading files for {plugin.title}...", flush=True)
        plugin.download_files()
    print(f"done ({len(plugins)} plugins)")


if __name__ == "__main__":
    main()
