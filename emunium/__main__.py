"""Command line for extension builds, Firefox policy setup and extension id.

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
from emunium._browser.firefox import write_firefox_policy
from emunium.errors import LaunchError


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
    build = actions.add_parser("build", help="build the attach extension")
    build.add_argument("--out", required=True, help="output directory")
    build.add_argument(
        "--target",
        choices=("chromium", "firefox"),
        default="chromium",
        help="extension target (default: chromium)",
    )
    build.add_argument(
        "--port",
        type=_port,
        default=None,
        help="hub port (default: the stored one, 47800)",
    )
    build.add_argument(
        "--firefox-path",
        help="Firefox executable; with --target firefox, register the signed release XPI in Enterprise Policies",
    )
    build.add_argument(
        "--firefox-xpi",
        help="signed local XPI path or HTTPS URL (default: EMUNIUM_FIREFOX_XPI or this emunium version's release)",
    )
    actions.add_parser("id", help="print the stable extension id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "build":
            if args.firefox_xpi is not None and args.firefox_path is None:
                raise ValueError("--firefox-xpi requires --firefox-path")
            if args.firefox_path is not None and args.target != "firefox":
                raise ValueError("--firefox-path requires --target firefox")
            result = build_extension(args.out, port=args.port, target=args.target)
            print("extension id: {}".format(result["extension_id"]))
            print("hub url:      {}".format(result["hub_url"]))
            print("folder:       {}".format(result["unpacked_dir"]))
            if args.target == "firefox":
                print("xpi:          {}".format(result["xpi_path"]))
                if args.firefox_path is not None:
                    policy_path = write_firefox_policy(
                        args.firefox_path, args.firefox_xpi
                    )
                    print(f"policy:       {policy_path}")
            else:
                print("zip:          {}".format(result["zip_path"]))
                print("crx:          {}".format(result["crx_path"]))
        else:
            print(load_or_create_state()["extension_id"])
    except (ImportError, ExtensionBuildError, LaunchError, ValueError, OSError) as exc:
        print(f"emunium: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
