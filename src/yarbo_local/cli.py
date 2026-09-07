"""Command line entry point: ``yarbo-local sniff|probe|discover|dump``."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from . import __version__, capture, discover, dump, probe


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yarbo-local",
        description="Phase 0 tooling for the Yarbo local MQTT protocol. LAN only, no cloud.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sniff", help="capture every topic from a broker to JSONL")
    p.add_argument("host")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--out", type=Path, help="JSONL file to append to")
    p.add_argument("--duration", type=float, help="stop after N seconds (default: until Ctrl-C)")
    p.add_argument("--filter", help="MQTT subscription filter (default: snowbot/+/#)")
    p.add_argument("--redact", action="store_true", help="redact serial, MAC, IP, coordinates")
    p.add_argument("--stats-every", type=float, default=15.0)

    p = sub.add_parser("probe", help="send one allowlisted command and show what comes back")
    p.add_argument("host")
    p.add_argument("command", choices=sorted(probe.PHASE0_COMMANDS))
    p.add_argument("--serial", help="robot serial (learned from traffic when omitted)")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--payload", help="JSON object overriding the default payload")
    p.add_argument("--encoding", choices=["auto", "zlib", "json"], default="auto")
    p.add_argument("--settle", type=float, default=3.0, help="seconds to observe before sending")
    p.add_argument("--timeout", type=float, default=8.0, help="seconds to collect after sending")
    p.add_argument("--out", type=Path, help="append the whole probe window to this JSONL")

    p = sub.add_parser("discover", help="scan hosts or a CIDR for brokers carrying snowbot traffic")
    p.add_argument("hosts", help="host, comma list, or CIDR such as 192.168.40.0/24")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--wait", type=float, default=6.0, help="seconds to listen per open host")
    p.add_argument("--connect-timeout", type=float, default=0.6)

    p = sub.add_parser("dump", help="summarise a JSONL capture")
    p.add_argument("file", type=Path)
    p.add_argument("--keys", action="store_true", help="print the DeviceMSG key inventory")
    p.add_argument("--app", action="store_true", help="print app-side commands observed")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.cmd == "sniff":
            stats = asyncio.run(
                capture.sniff(
                    args.host,
                    args.port,
                    out=args.out,
                    duration=args.duration,
                    topic_filter=args.filter,
                    redact=args.redact,
                    stats_every=args.stats_every,
                )
            )
            print(stats.render(), file=sys.stderr)
        elif args.cmd == "probe":
            payload = json.loads(args.payload) if args.payload else None
            result = asyncio.run(
                probe.probe(
                    args.host,
                    args.command,
                    serial=args.serial,
                    port=args.port,
                    payload=payload,
                    encoding=args.encoding,
                    settle=args.settle,
                    timeout=args.timeout,
                    out=args.out,
                )
            )
            print(result.render())
        elif args.cmd == "discover":
            hits = asyncio.run(
                discover.discover(
                    discover.expand(args.hosts),
                    port=args.port,
                    wait=args.wait,
                    connect_timeout=args.connect_timeout,
                )
            )
            if not hits:
                print("no hosts with an open MQTT port")
            for hit in hits:
                print(hit.render())
        elif args.cmd == "dump":
            print(dump.summarise(args.file).render(keys=args.keys, app=args.app))
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, OSError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
