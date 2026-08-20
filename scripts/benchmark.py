"""Benchmark harness for the Blender bridge.

Times the hot query paths against a LIVE Blender + Bonsai session with a
model loaded, so perf claims in release notes come from measurements, not
vibes. Run it before and after a perf-relevant change:

    uv run python scripts/benchmark.py
    uv run python scripts/benchmark.py --rounds 5 --limit 200

It requires a running bridge (Blender + add-on started) and prints one line
per benchmark with min/mean over the rounds. It never edits the model.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bonsai_mcp.blender_client import BlenderBridgeClient, BlenderBridgeError  # noqa: E402


def _timed(fn, rounds: int) -> list[float]:
    samples: list[float] = []
    for _ in range(rounds):
        started = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - started)
    return samples


def _report(name: str, samples: list[float], note: str = "") -> None:
    mean = statistics.mean(samples)
    print(
        f"{name:<34} min {min(samples) * 1000:8.1f} ms   "
        f"mean {mean * 1000:8.1f} ms   rounds {len(samples)}{note}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the Bonsai MCP bridge.")
    parser.add_argument("--rounds", type=int, default=3, help="Rounds per benchmark (default 3).")
    parser.add_argument("--limit", type=int, default=200, help="Page size for list queries.")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-call timeout seconds.")
    args = parser.parse_args()

    client = BlenderBridgeClient(timeout=args.timeout)
    try:
        return _run_benchmarks(client, args)
    finally:
        client.close()


def _run_benchmarks(client: BlenderBridgeClient, args: argparse.Namespace) -> int:
    try:
        info = client.ping()
    except BlenderBridgeError as exc:
        print(f"Bridge unreachable: {exc}", file=sys.stderr)
        return 1
    print(
        f"bridge ok: blender {info.get('blender_version')}, "
        f"addon {info.get('addon_version')}, ifc_loaded={info.get('ifc_loaded')}"
    )
    if not info.get("ifc_loaded"):
        print("Load an IFC model in Bonsai first; benchmarks need one.", file=sys.stderr)
        return 1

    rounds = max(1, args.rounds)

    _report("ping", _timed(lambda: client.send("ping"), rounds))
    _report(
        "get_scene_info (summary)",
        _timed(lambda: client.send("get_scene_info", {}), rounds),
    )
    _report(
        f"get_scene_info query=all limit={args.limit}",
        _timed(
            lambda: client.send(
                "get_scene_info", {"query": "all", "limit": args.limit, "offset": 0}
            ),
            rounds,
        ),
    )
    _report(
        f"list_elements limit={args.limit}",
        _timed(
            lambda: client.send("list_elements", {"limit": args.limit, "offset": 0}),
            rounds,
        ),
    )
    _report(
        "list_elements ifc_class=IfcWall",
        _timed(
            lambda: client.send(
                "list_elements", {"ifc_class": "IfcWall", "limit": args.limit}
            ),
            rounds,
        ),
    )
    _report(
        "get_spatial_structure",
        _timed(lambda: client.send("get_spatial_structure", {}), rounds),
    )
    _report(
        "get_quantities (defaults)",
        _timed(lambda: client.send("get_quantities", {}), rounds),
    )

    walls = client.send("list_elements", {"ifc_class": "IfcWall", "limit": 50})
    gids = [e.get("global_id") for e in walls.get("elements", []) if e.get("global_id")]
    if gids:
        _report(
            f"get_psets batch of {len(gids)}",
            _timed(lambda: client.send("get_psets", {"global_ids": gids, "names": []}), rounds),
        )
    else:
        print("get_psets batch: skipped (no walls with GlobalIds found)")

    _report(
        "screenshot 800px jpeg",
        _timed(
            lambda: client.send(
                "get_viewport_screenshot", {"max_size": 800, "format": "jpeg"}
            ),
            rounds,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
