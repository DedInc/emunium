"""Command line: ``python -m emunium extension build --out DIR [--port N]`` and ``extension id``.

Contract: ``docs/architecture/attach-mode.md`` section 1.
"""

from __future__ import annotations

import argparse
import sys

from emunium._attach.build import (
    ExtensionBuildError,
    build_extension,
    load_or_create_state,
)


def _port(text: str) -> int:
    try:
        port = int(text, 10)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"port must be an integer, got {text!r}"
        ) from None
    if not 0 < port <= 65535:
        raise argparse.ArgumentTypeError(f"port must be in 1..65535, got {port}")
    return port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m emunium")
    commands = parser.add_subparsers(dest="command", metavar="command")
    commands.required = True
    extension = commands.add_parser("extension", help="attach-mode extension builds")
    actions = extension.add_subparsers(dest="action", metavar="action")
    actions.required = True
    build = actions.add_parser("build", help="write the unpacked folder, zip and CRX3")
    build.add_argument("--out", required=True, help="output directory")
    build.add_argument(
        "--port",
        type=_port,
        default=None,
        help="hub port (default: the stored one, 47800)",
    )
    actions.add_parser("id", help="print the stable extension id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "build":
            result = build_extension(args.out, port=args.port)
            print("extension id: {}".format(result["extension_id"]))
            print("hub url:      {}".format(result["hub_url"]))
            print("folder:       {}".format(result["unpacked_dir"]))
            print("zip:          {}".format(result["zip_path"]))
            print("crx:          {}".format(result["crx_path"]))
        else:
            print(load_or_create_state()["extension_id"])
    except (ImportError, ExtensionBuildError, ValueError, OSError) as exc:
        print(f"emunium: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
