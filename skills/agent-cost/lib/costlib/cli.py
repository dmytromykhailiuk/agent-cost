import argparse
import importlib
import json
import sys

from . import core


def _load(name):
    try:
        return importlib.import_module(f"costlib.{name}")
    except ModuleNotFoundError as exc:
        if exc.name == f"costlib.{name}":
            return None
        raise


def _not_implemented(command):
    print(f"agent-cost {command}: not implemented yet", file=sys.stderr)
    return 2


def _stdin_text():
    try:
        return sys.stdin.read()
    except Exception:
        return ""


def _hook(args):
    try:
        module = _load("hooks")
        if module is not None:
            module.run(args.event, _stdin_text())
    except BaseException:
        pass
    return 0


def _statusline(args):
    try:
        module = _load("statusline")
        if module is None:
            return 0
        out = module.run(_stdin_text(), args.json)
        if out:
            print(out)
    except BaseException:
        pass
    return 0


def _dispatch(module_name, func_name):
    def run(args):
        module = _load(module_name)
        if module is None or not hasattr(module, func_name):
            return _not_implemented(args.command)
        return module.__dict__[func_name](args) or 0

    return run


def _version(args):
    version = core.plugin_version()
    if args.json:
        print(json.dumps({"name": "agent-cost", "version": version, "contract": 1}))
    else:
        print(version)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="agent-cost", description="Cost and time tracking for agent sessions.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("hook", help="Run a Claude Code hook handler.")
    p.add_argument("event", choices=["session-start", "prompt", "stop", "subagent-stop", "session-end"])
    p.set_defaults(func=_hook)

    p = sub.add_parser("statusline", help="Print the status-line segment for the stdin payload.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_statusline)

    p = sub.add_parser("report", help="Generate the HTML report and open it.")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--session")
    group.add_argument("--latest", action="store_true")
    group.add_argument("--all", action="store_true")
    p.add_argument("--out")
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=_dispatch("report", "cmd_report"))

    p = sub.add_parser("serve", help="Run the live report page on a local server and print its URL.")
    p.add_argument("--port", type=int)
    p.add_argument("--session")
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--foreground", action="store_true")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--stop", action="store_true")
    group.add_argument("--status", action="store_true")
    p.set_defaults(func=_dispatch("server", "cmd_serve"))

    p = sub.add_parser("list", help="List recorded sessions.")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="Include roots outside visible_roots.")
    p.set_defaults(func=_dispatch("report", "cmd_list"))

    p = sub.add_parser("show", help="Show a session's agent tree.")
    p.add_argument("sid", nargs="?")
    p.add_argument("--latest", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_dispatch("report", "cmd_show"))

    p = sub.add_parser("export", help="Print a session's own cost and time and its descendants' as JSON (read-only).")
    p.add_argument("--session", required=True)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_dispatch("export", "cmd_export"))

    p = sub.add_parser("scan", help="Backfill sessions from the transcript history.")
    p.add_argument("--since", default="30d")
    p.add_argument("--config-dir", action="append", default=[])
    p.set_defaults(func=_dispatch("scan", "cmd_scan"))

    p = sub.add_parser("doctor", help="Check the installation.")
    p.add_argument("--dashboard")
    p.set_defaults(func=_dispatch("setup", "cmd_doctor"))

    p = sub.add_parser("setup", help="Show or apply the status-line patch.")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--dashboard")
    p.set_defaults(func=_dispatch("setup", "cmd_setup"))

    p = sub.add_parser("version", help="Print the plugin version.")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_version)
    return parser


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        if len(sys.argv) > 1 and sys.argv[1] in ("hook", "statusline"):
            return 0
        return code
    return args.func(args)
