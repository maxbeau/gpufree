"""
CLI for gpufree.cn — see ``gpufree --help`` for available commands.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from time import perf_counter
from datetime import datetime
from pathlib import Path

from gpufree import __version__
from gpufree.client import (
    DEFAULT_TOKEN_FILE, GPUFeeClient, PlatformError,
)
from gpufree.auth import AUTH_ACTIONS, classify, import_tokens, parse_token_input
from gpufree.envelope import error_details, make_envelope, project, redact
from gpufree.snapshot import normalize
from gpufree.operations import Journal, operation_lock, read_context, write_context, register_ttl
from gpufree.remote import SSHTransport
from gpufree.reconcile import ensure as reconcile_ensure, VerificationError


def _print_json(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _control_output(value):
    """Control confirmations must not expose credentials in a waited instance."""
    if isinstance(value, dict):
        return {key: _control_output(item) for key, item in value.items()
                if not any(part in key.casefold() for part in ("password", "token", "cookie", "secret"))}
    if isinstance(value, list):
        return [_control_output(item) for item in value]
    return value


def _redact_instance(value, *, unsafe=False):
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if "token" in key.casefold() or
                      (not unsafe and any(part in key.casefold() for part in ("password", "cookie", "secret")))
                      else _redact_instance(item, unsafe=unsafe))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_instance(item, unsafe=unsafe) for item in value]
    return value


def _add_output_flags(command_parser):
    command_parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    command_parser.add_argument("--jq", default=argparse.SUPPRESS)
    command_parser.add_argument("--format", default=argparse.SUPPRESS)
    command_parser.add_argument("-q", dest="quiet", action="store_true", default=argparse.SUPPRESS)


def _duration_seconds(value):
    import re
    match = re.fullmatch(r"(\d+)([smhd])", str(value))
    if not match:
        raise ValueError("duration must be a positive number followed by s, m, h, or d")
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]


def _selector(client, args):
    selector = getattr(args, "selector", None) or getattr(args, "instance", None)
    if selector is None:
        selector = read_context(_token_file(args)).get("instance")
    return selector


def _token_file(args):
    return Path(args.token_file).expanduser()


def _ensure_yes(args, prompt="Proceed with this platform operation? [y/N] "):
    if getattr(args, "dry_run", False):
        return False
    if not getattr(args, "yes", False) and not sys.stdin.isatty():
        raise ValueError("non-interactive write requires --yes (or use --dry-run)")
    if not getattr(args, "yes", False):
        answer = input(prompt)
        if answer.casefold() not in {"y", "yes"}:
            raise ValueError("operation cancelled")
    return True


def _items_from(response):
    data = response.get("data") if isinstance(response, dict) else None
    return data.get("dataList", []) if isinstance(data, dict) else []


def _within_since(event, seconds):
    value = event.get("at") or event.get("created_at") or event.get("create_time") or event.get("time")
    if value is None:
        return True
    try:
        if isinstance(value, (int, float)):
            stamp = float(value)
        else:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        return time.time() - stamp <= seconds
    except (ValueError, TypeError, OverflowError):
        return True


def _run_selftest(args, client, started):
    checks = []
    for name, method, validate in (
        ("userinfo", client.get_userinfo, lambda x: isinstance(x, dict) and
         ("sub" in x or "user_info" in x)),
        ("balance", client.get_balance, lambda x: isinstance(x.get("data"), dict)),
        ("instances", client.list_instances, lambda x: isinstance(x.get("data"), dict) and isinstance(x["data"].get("dataList"), list)),
        ("products", client.list_products, lambda x: isinstance(x.get("data"), list)),
    ):
        try:
            response = method()
            if not validate(response):
                checks.append({"name": name, "status": "drifted", "reason": "required fields or types changed"})
                continue
            if name == "instances":
                for item in response["data"]["dataList"]:
                    if not all(key in item for key in ("status", "start_mode")):
                        checks.append({"name": name, "status": "drifted", "reason": "instance status/start_mode missing"})
                        break
                    if not isinstance(item["status"], (str, int)) or not isinstance(item["start_mode"], str):
                        checks.append({"name": name, "status": "drifted", "reason": "instance status/start_mode type changed"})
                        break
                    if "ssh_command" not in item and "ssh_password" not in item:
                        checks.append({"name": name, "status": "drifted", "reason": "instance SSH fields missing"})
                        break
                else:
                    checks.append({"name": name, "status": "ok"})
            elif name == "products":
                invalid = [p for p in response["data"] if "current_price" not in p and "price" not in p]
                invalid.extend(p for p in response["data"] if any(k in p for k in ("current_price", "price")) and
                               not isinstance(p.get("current_price", p.get("price")), (int, float, str)))
                checks.append({"name": name, "status": "drifted" if invalid else "ok",
                               **({"reason": "price unit field missing or changed type; prices use cents"} if invalid else {})})
            else:
                checks.append({"name": name, "status": "ok"})
        except Exception as exc:
            checks.append({"name": name, "status": "down", "reason": str(exc)})
    drift = any(item["status"] == "drifted" for item in checks)
    data = {"checks": checks, "healthy": all(c["status"] == "ok" for c in checks)}
    _emit(args, "selftest", data, started)
    return 17 if drift else (0 if data["healthy"] else 14)


def _check_hourly_limit(client, selector, limit):
    if limit is None:
        return None
    item = client.resolve_instance(selector)
    products = client.list_products().get("data") or []
    product = next((p for p in products if p.get("product_name") == item.get("product_name")), None)
    price = product.get("current_price") if product else None
    estimate = float(price) * int(item.get("req_gpu_amount", item.get("req_amount", 1))) / 100 if price is not None else None
    if estimate is not None and estimate > limit:
        error = RuntimeError(f"estimated hourly cost ¥{estimate:g} exceeds local limit ¥{limit:g}")
        error.code = 42408
        raise error
    return estimate


def _run_safe_write(args, client, operation, started):
    selector = _selector(client, args)
    token_file = _token_file(args)
    journal = Journal(Path(token_file).with_name("journal.jsonl"))
    params = {k: v for k, v in vars(args).items() if k not in {"command", "instance_command"}}
    if operation in {"create", "clone"} and getattr(args, "idempotency_key", None):
        previous = journal.successful_key(operation, args.idempotency_key)
        if previous is not None:
            _emit(args, "instance." + operation, {"reused": True, "result": previous}, started)
            return 0
    if getattr(args, "dry_run", False):
        hourly = None
        if operation == "create":
            products = client.list_products().get("data") or []
            product = next((p for p in products if str(p.get("product_sku_code", p.get("sku_code", ""))) == str(args.sku)), None)
            price = product.get("current_price") if product else None
            if price is not None:
                hourly = float(price) * args.count / 100
        elif operation == "clone" and selector:
            try:
                item = client.resolve_instance(selector)
                products = client.list_products().get("data") or []
                product = next((p for p in products if p.get("product_name") == item.get("product_name")), None)
                if product and product.get("current_price") is not None:
                    hourly = float(product["current_price"]) * int(item.get("req_gpu_amount", 1)) / 100
            except Exception:
                pass
        data = {"operation": operation, "request": redact(params), "estimated_hourly_cost_yuan": None,
                "estimated_first_hour_cost_yuan": None}
        data["estimated_hourly_cost_yuan"] = hourly
        data["estimated_first_hour_cost_yuan"] = hourly
        if hourly is None:
            data["note"] = "cost unavailable from current product price data"
        _emit(args, "instance." + operation, data, started)
        return 0
    if operation in {"create", "clone"}:
        for pending in journal.open_intents(operation=operation):
            if time.time() - float(pending.get("epoch", 0)) > 900:
                continue
            identity = pending.get("identity") or {}
            matches = []
            for candidate in client._instance_items():
                if operation == "create":
                    candidate_uuid = str(GPUFeeClient._instance_identifiers(candidate)[1])
                    if (candidate_uuid not in identity.get("existing_uuids", []) and
                            str(candidate.get("product_sku_code")) == str(identity.get("sku")) and
                            str(candidate.get("data_center_id")) == str(identity.get("data_center_id"))):
                        matches.append(candidate)
                else:
                    name = candidate.get("nick_name") or candidate.get("webide_instance_name")
                    candidate_uuid = str(GPUFeeClient._instance_identifiers(candidate)[1])
                    if identity.get("name") and name == identity.get("name") and candidate_uuid not in identity.get("existing_uuids", []):
                        matches.append(candidate)
            if len(matches) == 1:
                recovered = {"reconciled": True, "instance": _control_output(matches[0])}
                journal.result(pending["id"], recovered)
                _emit(args, "instance." + operation, recovered, started,
                      warnings=[{"kind": "intent_reconciled", "detail": "matched a recent instance"}])
                return 0
            raise RuntimeError(f"unresolved prior {operation} intent {pending['id']}; refusing duplicate request")
    release_item = None
    if operation == "release":
        confirm = getattr(args, "confirm", None)
        if not confirm:
            raise ValueError("release requires --confirm matching the first 5 UUID characters or full UUID")
        release_item = client.resolve_instance(selector)
        _, resolved_uuid = client._instance_identifiers(release_item)
        if not confirm or confirm.casefold() not in {resolved_uuid[:5].casefold(), resolved_uuid.casefold()}:
            raise ValueError("release requires --confirm matching the first 5 UUID characters or full UUID")
    _ensure_yes(args, f"Proceed with {operation} on {selector or getattr(args, 'sku', 'new instance')}? Costs may apply; release is irreversible. [y/N] ")
    target = selector or "new-instance"
    with operation_lock(Path(token_file).parent, target, journal):
        identity = None
        if operation == "create":
            before = client._instance_items()
            identity = {"sku": args.sku, "data_center_id": args.dc,
                        "existing_uuids": [str(GPUFeeClient._instance_identifiers(item)[1]) for item in before]}
        elif operation == "clone":
            before = client._instance_items()
            source = None
            for candidate in before:
                ids = GPUFeeClient._instance_identifiers(candidate)
                names = (candidate.get("nick_name"), candidate.get("webide_instance_name"))
                if selector is None and len(before) == 1 or selector is not None and str(selector) in tuple(map(str, ids)) + tuple(n for n in names if n):
                    source = candidate
                    break
            source_uuid = GPUFeeClient._instance_identifiers(source)[1] if source else None
            identity = {"name": getattr(args, "name", None) or (f"{source.get('nick_name') or source_uuid} 副本" if source else None),
                        "existing_uuids": [str(GPUFeeClient._instance_identifiers(item)[1]) for item in before]}
        event = journal.intent(operation, target, params, getattr(args, "idempotency_key", None), identity=identity)
        try:
            if operation == "create":
                result = client.create_instance(product_sku_code=args.sku, data_center_id=args.dc,
                    image_id=args.image, req_amount=args.count, start_model=args.mode,
                    charge_type=args.charge_type, expand_data_disk_gb=getattr(args, "expand_disk_gb", 0),
                    duration_type=getattr(args, "duration_type", ""), duration_count=getattr(args, "duration_count", 1))
            elif operation == "clone":
                result = client.clone_instance_by_selector(selector, nickname=getattr(args, "name", None),
                    clone_data=getattr(args, "with_data", False),
                    expand_data_disk_gb=getattr(args, "expand_disk_gb", 0))
            else:
                instance_id, instance_uuid = client._instance_identifiers(release_item)
                result = client.release_instance(instance_id, instance_uuid)
            journal.result(event, result)
        except Exception as exc:
            journal.result(event, error=exc)
            raise
    if args.command in {"create", "clone", "release"} and not args.json and not args.jq and not args.format and not args.quiet:
        _print_json(_control_output(result))
    else:
        _emit(args, operation if args.command in {"create", "clone", "release"} else "instance." + operation,
              _control_output(result), started)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="gpufree",
        description="GPUFee.cn (算力自由) API Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  gpufree snapshot
  gpufree instance ensure running
  gpufree billing balance""",
    )
    parser.add_argument(
        "--token-file", type=Path, default=Path(os.environ.get("GPUFREE_TOKEN_FILE", DEFAULT_TOKEN_FILE)),
        help=f"Path to token JSON (default: {DEFAULT_TOKEN_FILE})",
    )
    parser.add_argument(
        "--client-id", type=str, default=None,
        help="Override OAuth client_id (extracted from stored tokens by default)",
    )
    parser.add_argument("--json", action="store_true", default=os.environ.get("GPUFREE_JSON") == "1", help="Force JSON output")
    parser.add_argument("--instance", default=os.environ.get("GPUFREE_INSTANCE"), help="Default instance selector")
    parser.add_argument("--jq", help="Select a value using a dot path")
    parser.add_argument("--format", help="Format fields using {dot.path} placeholders")
    parser.add_argument("-q", dest="quiet", action="store_true", help="Quiet key output")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    sub = parser.add_subparsers(dest="command", help="Sub-command")

    # Object-oriented command surface.
    instance = sub.add_parser("instance", help="Instance operations")
    instance_sub = instance.add_subparsers(dest="instance_command", required=True)
    inst_list = instance_sub.add_parser("list")
    _add_output_flags(inst_list)
    inst_get = instance_sub.add_parser("get")
    _add_output_flags(inst_get)
    inst_get.add_argument("selector", nargs="?")
    inst_describe = instance_sub.add_parser("describe")
    _add_output_flags(inst_describe)
    inst_describe.add_argument("selector", nargs="?")
    inst_use = instance_sub.add_parser("use")
    inst_use.add_argument("selector")
    ensure_parser = instance_sub.add_parser("ensure")
    _add_output_flags(ensure_parser)
    ensure_parser.add_argument("desired", choices=("running", "stopped"))
    ensure_parser.add_argument("selector", nargs="?")
    ensure_parser.add_argument("--mode", choices=("gpu", "nogpu"), default="gpu")
    ensure_parser.add_argument("--deadline", default="45m")
    ensure_parser.add_argument("--verify", default="")
    ensure_parser.add_argument("--allow-restart", action="store_true")
    ensure_parser.add_argument("--plan-only", action="store_true")
    ensure_parser.add_argument("--ttl")
    ensure_parser.add_argument("--max-hourly-cost", type=float)
    ensure_parser.add_argument("--via")
    ensure_parser.add_argument("--accept-new-hostkey", action="store_true")
    plan_parser = instance_sub.add_parser("plan")
    _add_output_flags(plan_parser)
    plan_parser.add_argument("desired", choices=("running", "stopped"))
    plan_parser.add_argument("selector", nargs="?")
    plan_parser.add_argument("--mode", choices=("gpu", "nogpu"), default="gpu")
    plan_parser.add_argument("--allow-restart", action="store_true")
    exec_parser = instance_sub.add_parser("exec")
    exec_parser.add_argument("selector", nargs="?")
    exec_parser.add_argument("--script", type=Path)
    exec_parser.add_argument("--timeout", type=float)
    exec_parser.add_argument("--via")
    exec_parser.add_argument("--accept-new-hostkey", action="store_true")
    exec_parser.add_argument("--stdin", action="store_true", help="Forward standard input")
    exec_parser.add_argument("command", nargs=argparse.REMAINDER)
    for remote_name in ("ssh", "tunnel"):
        remote = instance_sub.add_parser(remote_name)
        remote.add_argument("selector", nargs="?")
        remote.add_argument("--via")
        remote.add_argument("--accept-new-hostkey", action="store_true")
        if remote_name == "tunnel":
            remote.add_argument("-L", dest="forward", required=True)
    events = instance_sub.add_parser("events")
    _add_output_flags(events)
    events.add_argument("--since", default="24h")
    ttl = instance_sub.add_parser("create")
    ttl.add_argument("--sku", required=True); ttl.add_argument("--dc", type=int, required=True)
    ttl.add_argument("--image", type=int, required=True); ttl.add_argument("--count", type=int, default=1)
    ttl.add_argument("--mode", choices=("gpu", "nogpu"), default="gpu")
    ttl.add_argument("--charge-type", type=int, default=0); ttl.add_argument("--expand-disk-gb", type=int, default=0)
    ttl.add_argument("--dry-run", action="store_true"); ttl.add_argument("--yes", action="store_true")
    ttl.add_argument("--idempotency-key")
    for name in ("clone", "release"):
        write = instance_sub.add_parser(name)
        _add_output_flags(write)
        write.add_argument("selector", nargs="?")
        write.add_argument("--dry-run", action="store_true"); write.add_argument("--yes", action="store_true")
        if name == "clone":
            write.add_argument("--name"); write.add_argument("--with-data", action="store_true")
            write.add_argument("--idempotency-key")
        else:
            write.add_argument("--confirm")

    for group_name, names in (("billing", ("balance", "history", "orders")),
                              ("catalog", ("stock", "products", "images"))):
        group = sub.add_parser(group_name)
        group_sub = group.add_subparsers(dest=group_name + "_command", required=True)
        for name in names:
            command = group_sub.add_parser(name)
            _add_output_flags(command)
            if group_name == "catalog" and name == "stock":
                command.add_argument("query", nargs="?"); command.add_argument("--available", action="store_true")

    selftest = sub.add_parser("selftest", help="Check read-only API contracts")
    _add_output_flags(selftest)
    completion = sub.add_parser("completion")
    completion.add_argument("shell", choices=("bash", "zsh", "fish"))
    # Auth suite
    auth = sub.add_parser("auth", help="Authentication lifecycle")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    auth_status = auth_sub.add_parser("status", help="Classify local token state")
    _add_output_flags(auth_status)
    auth_status.add_argument("--probe", action="store_true", help="Attempt one refresh to diagnose token chain")
    auth_import = auth_sub.add_parser("import", help="Import browser cookies or token JSON")
    _add_output_flags(auth_import)
    source = auth_import.add_mutually_exclusive_group(required=True)
    source.add_argument("--stdin", action="store_true")
    source.add_argument("--file", type=Path)
    auth_import.add_argument("--no-verify", action="store_true")
    auth_keepalive = auth_sub.add_parser("keepalive", help="Refresh and check token state")
    _add_output_flags(auth_keepalive)
    auth_sub.add_parser("snippet", help="Print a browser cookie bookmarklet")

    snap = sub.add_parser("snapshot", help="Normalized account and instance state")
    _add_output_flags(snap)
    snap.add_argument("--warn-balance-below", type=float)
    snap.add_argument("--probe-instance")

    api = sub.add_parser("api", help="Call a platform API endpoint")
    _add_output_flags(api)
    api.add_argument("method", choices=("GET", "POST"))
    api.add_argument("path")
    api.add_argument("--body")

    capabilities = sub.add_parser("capabilities", help="List registered CLI capabilities")
    _add_output_flags(capabilities)
    schema = sub.add_parser("schema", help="Show data fields for a command")
    _add_output_flags(schema)
    schema.add_argument("command_name")
    version = sub.add_parser("version", help="Show CLI version and executable path")
    _add_output_flags(version)
    version.add_argument("--check", action="store_true")

    args = parser.parse_args()
    started = perf_counter()

    if not args.command:
        parser.print_help()
        return 1

    if args.command == "completion":
        names = "instance billing catalog auth snapshot api capabilities schema selftest completion version".split()
        if args.shell == "fish":
            print("complete -c gpufree -f -a '" + " ".join(names) + "' --long-option json --long-option token-file --long-option instance")
        else:
            print("_gpufree() { COMPREPLY=( $(compgen -W '" + " ".join(names) + "' -- \"${COMP_WORDS[COMP_CWORD]}\") ); }\ncomplete -F _gpufree gpufree" if args.shell == "bash" else "#compdef gpufree\n_arguments '1:command:(" + " ".join(names) + ")' '--json' '--token-file' '--instance'")
        return 0

    if args.command == "capabilities":
        _emit(args, "capabilities", _capabilities(), started)
        return 0
    if args.command == "schema":
        data = _schemas().get(args.command_name)
        if data is None:
            return _emit_error(args, "schema", ValueError(f"Unknown command schema: {args.command_name}"), started)
        _emit(args, "schema", data, started)
        return 0
    if args.command == "version":
        data = {"version": __version__, "executable": os.path.realpath(sys.argv[0]), "update": "not_checked"}
        if args.check:
            try:
                remote = subprocess.run(["git", "ls-remote", "origin", "refs/heads/main"], cwd=Path(__file__).resolve().parent.parent,
                                        capture_output=True, text=True, timeout=10, check=True).stdout.strip()
                data["update"] = "unknown" if not remote else ("current" if remote.split()[0] == _head_hash() else "available")
            except Exception:
                data["update"] = "unknown"
        _emit(args, "version", data, started)
        return 0
    if args.command == "auth" and args.auth_command == "snippet":
        print("javascript:(async()=>{await navigator.clipboard.writeText(document.cookie)})()")
        print("Open gpufree.cn, click the bookmarklet, then run gpufree auth import --stdin.", file=sys.stderr)
        return 0

    try:
        client = GPUFeeClient(token_file=args.token_file, client_id=args.client_id)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.command == "selftest":
        return _run_selftest(args, client, started)

    if args.command in {"instance", "billing", "catalog"}:
        group = args.command
        operation = getattr(args, group + "_command" if group != "instance" else "instance_command")
        selector = _selector(client, args)
        try:
            if group == "instance":
                if operation == "use":
                    item = client.resolve_instance(args.selector)
                    picked = GPUFeeClient._instance_identifiers(item)[1]
                    write_context(_token_file(args), picked)
                    print(json.dumps({"instance": picked}))
                    return 0
                if operation == "list":
                    items = _items_from(client.list_instances())
                    _emit(args, "instance.list", items, started)
                    return 0
                if operation in {"get", "describe"}:
                    item = client.resolve_instance(args.selector or selector)
                    data = {"instance": _redact_instance(item)}
                    if operation == "describe":
                        journal = Journal(_token_file(args).with_name("journal.jsonl"))
                        _, selected_uuid = GPUFeeClient._instance_identifiers(item)
                        history = journal.records()
                        data["recent_operations"] = [r for r in history if r.get("target") in {str(args.selector or selector), str(selected_uuid)}][-20:]
                        data["lease"] = next((r.get("lease") for r in reversed(journal.records())
                                              if r.get("type") == "lease" and r.get("target") in {str(args.selector or selector), str(selected_uuid)}), None)
                        products = client.list_products().get("data") or []
                        product = next((p for p in products if p.get("product_name") == item.get("product_name")), None)
                        if product and product.get("current_price") is not None:
                            data["estimated_hourly_cost_yuan"] = float(product["current_price"]) * int(item.get("req_gpu_amount", 1)) / 100
                        else:
                            data["estimated_hourly_cost_yuan"] = None
                        try:
                            data["billing_events"] = {"orders": client.get_orders().get("data"),
                                                       "transactions": client.get_transactions().get("data")}
                        except Exception:
                            data["billing_events"] = None
                    _emit(args, "instance." + operation, data, started)
                    return 0
                if operation in {"ensure", "plan"}:
                    if operation == "plan":
                        args.plan_only = True
                    if args.selector is None:
                        args.selector = selector
                    if not args.selector:
                        raise ValueError("instance selector is required; pass one or use `instance use`")
                    journal = Journal(_token_file(args).with_name("journal.jsonl"))
                    with operation_lock(_token_file(args).parent, args.selector, journal):
                        intent = None if args.plan_only else journal.intent(
                            "ensure", args.selector, {"desired": args.desired, "mode": args.mode})
                        try:
                            progress = lambda event: print(json.dumps(event, ensure_ascii=False), file=sys.stderr)
                            limit = getattr(args, "max_hourly_cost", None)
                            if not args.plan_only and args.desired == "running":
                                _check_hourly_limit(client, args.selector, limit)
                            verify_steps = [v for v in getattr(args, "verify", "").split(",") if v]
                            invalid_verify = set(verify_steps) - {"ssh", "gpu", "disk"}
                            if invalid_verify:
                                raise ValueError("unknown verify step(s): " + ", ".join(sorted(invalid_verify)))
                            transport = SSHTransport(_token_file(args).parent)
                            def verifier(item, checks):
                                observed = {}
                                if "ssh" in checks or "gpu" in checks or "disk" in checks:
                                    import socket
                                    host, port, _, _ = transport.endpoint(item)
                                    if not getattr(args, "via", None):
                                        try:
                                            with socket.create_connection((host, port), timeout=5):
                                                pass
                                            observed["tcp"] = "reachable"
                                        except OSError as exc:
                                            raise VerificationError("SSH TCP endpoint is unreachable", "verify_network_unreachable") from exc
                                    probe = "true"
                                    if "gpu" in checks: probe += " && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader"
                                    if "disk" in checks: probe += " && df -h /"
                                    response = transport.invoke(item, ["sh", "-lc", probe], timeout=20,
                                        via=getattr(args, "via", None),
                                        accept_new_hostkey=getattr(args, "accept_new_hostkey", False), capture=True)
                                    if response.returncode:
                                        raise VerificationError("SSH probe failed")
                                    observed["ssh"] = "ok"
                                    output = (getattr(response, "stdout", b"") or b"").decode(errors="replace")
                                    if "gpu" in checks:
                                        import re
                                        product = str(item.get("product_name", ""))
                                        model = re.search(r"(?:RTX\s*)?(4090|4080|3090|A100|H100|L40S?|A6000)", product, re.I)
                                        first_line = output.splitlines()[0] if output.splitlines() else ""
                                        if model and model.group(1).casefold() not in first_line.casefold():
                                            raise VerificationError("nvidia-smi GPU model does not match product")
                                        expected_gb = item.get("gpu_memory_gb") or item.get("gpu_mem_gb")
                                        if expected_gb is None:
                                            capacity = re.search(r"(\d+)\s*(?:GB|G)\b", product, re.I)
                                            expected_gb = int(capacity.group(1)) if capacity else None
                                        memory = re.search(r",\s*(\d+)\s*(?:MiB|MB)", first_line, re.I)
                                        if expected_gb is not None and memory and int(memory.group(1)) < float(expected_gb) * 900:
                                            raise VerificationError("nvidia-smi VRAM is below the product specification")
                                        observed["gpu"] = first_line
                                    if "disk" in checks:
                                        observed["disk"] = output.splitlines()[1:]
                                return observed
                            result = reconcile_ensure(client, args.selector, args.desired, mode=args.mode,
                                deadline=_duration_seconds(args.deadline) if hasattr(args, "deadline") else 2700,
                                verify=verify_steps,
                                allow_restart=args.allow_restart, plan_only=args.plan_only,
                                progress=progress, verifier=verifier)
                            if getattr(args, "ttl", None) and not args.plan_only:
                                try:
                                    lease = register_ttl(args.selector, _token_file(args), _duration_seconds(args.ttl))
                                    journal.append({"type": "lease", "target": args.selector, "lease": lease})
                                except Exception as exc:
                                    result.setdefault("warnings", []).append({"kind": "ttl_registration_failed", "message": str(exc)})
                            if intent:
                                journal.result(intent, result)
                        except KeyboardInterrupt:
                            try:
                                current = client.resolve_instance(args.selector)
                                platform_state = {"status": current.get("status"), "start_mode": current.get("start_mode")}
                            except Exception:
                                platform_state = {"status": "unknown"}
                            if intent:
                                journal.result(intent, error="cancelled")
                            print(json.dumps({"event": "cancelled", "platform_state": platform_state}), file=sys.stderr)
                            return 130
                        except Exception as exc:
                            if intent:
                                journal.result(intent, error=exc)
                            raise
                    _emit(args, "instance." + operation, result, started, warnings=result.get("warnings", []))
                    return 0
                if operation in {"exec", "ssh", "tunnel"}:
                    item = client.resolve_instance(args.selector or selector)
                    transport = SSHTransport(_token_file(args).parent)
                    if operation == "exec":
                        command = args.command
                        if command and command[0] == "--": command = command[1:]
                        if args.script:
                            command = ["sh", "-s"]
                            stdin = args.script.read_bytes()
                        else:
                            stdin = sys.stdin.buffer.read() if args.stdin or not sys.stdin.isatty() else None
                        if not command:
                            raise ValueError("instance exec requires -- <command...> or --script PATH")
                        return transport.invoke(item, command, via=args.via, timeout=args.timeout, stdin=stdin,
                                                accept_new_hostkey=args.accept_new_hostkey)
                    return transport.invoke(item, [], via=args.via, tty=operation == "ssh",
                                            accept_new_hostkey=args.accept_new_hostkey,
                                            forward=args.forward if operation == "tunnel" else None)
                if operation == "events":
                    journal = Journal(_token_file(args).with_name("journal.jsonl"))
                    data = [{"source": "local", **r} for r in journal.records()]
                    try:
                        for source, getter in (("orders", client.get_orders), ("transactions", client.get_transactions)):
                            payload = getter().get("data", [])
                            rows = payload if isinstance(payload, list) else payload.get("dataList", []) if isinstance(payload, dict) else []
                            data.extend({"source": "platform", "kind": source, "event": x} for x in rows)
                    except Exception:
                        pass
                    horizon = _duration_seconds(args.since)
                    data = [event for event in data if _within_since(event, horizon)]
                    data.sort(key=lambda x: x.get("at", ""))
                    _emit(args, "instance.events", data, started)
                    return 0
                if operation in {"create", "clone", "release"}:
                    return _run_safe_write(args, client, operation, started)
            elif group == "billing":
                handler = {"balance": client.get_balance_overview, "history": client.get_balance_history,
                           "orders": client.get_orders}[operation]
                _emit(args, "billing." + operation, handler(), started); return 0
            else:
                if operation == "stock":
                    value = client.stock_products(args.query, available=args.available)
                elif operation == "products": value = client.list_products()
                else: value = client.list_public_images()
                _emit(args, "catalog." + operation, value, started); return 0
        except KeyboardInterrupt:
            print(json.dumps({"event": "cancelled", "platform_operation": "may_still_be_in_progress"}), file=sys.stderr)
            return 130
        except Exception as exc:
            return _emit_error(args, args.command + "." + operation, exc, started)

    if args.command == "auth":
        return _run_auth(args, client, started)
    if args.command == "snapshot":
        try:
            raw = client.get_status()
            failures = [key for key in ("balance", "instances") if isinstance(raw.get(key), dict) and "error" in raw[key]]
            if failures:
                raise RuntimeError("Snapshot unavailable: " + ", ".join(failures))
            data, warnings = normalize(raw, classify(client), client.list_products().get("data") or [],
                                       warn_balance_below=args.warn_balance_below)
            if args.probe_instance:
                selected = client.resolve_instance(args.probe_instance)
                transport = SSHTransport(_token_file(args).parent)
                probe = transport.invoke(selected, ["sh", "-lc", "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader; df -P / /mnt/data"], capture=True, timeout=20)
                if probe.returncode:
                    raise VerificationError("snapshot remote probe failed")
                lines = (probe.stdout or b"").decode(errors="replace").splitlines()
                if lines:
                    try:
                        utilization = int(lines[0].strip().rstrip("% "))
                        data.setdefault("probes", {})["gpu_utilization_percent"] = utilization
                        if utilization <= 5:
                            warnings.append({"kind": "gpu_idle", "instance_uuid": GPUFeeClient._instance_identifiers(selected)[1],
                                             "utilization_percent": utilization})
                    except ValueError:
                        warnings.append({"kind": "gpu_probe_unknown", "message": "nvidia-smi returned an unrecognized utilization value"})
                if len(lines) > 1:
                    data.setdefault("probes", {})["disk"] = lines[1:]
                    if len(lines) > 2:
                        data.setdefault("probes", {})["data_disk"] = lines[2]
                    else:
                        warnings.append({"kind": "data_disk_probe_unavailable", "message": "data disk was not mounted or reported"})
            _atomic_snapshot(_token_file(args), data)
            _emit(args, "snapshot", data, started, warnings=warnings)
            return 0
        except Exception as exc:
            code, error = error_details(exc)
            stale = _read_snapshot(_token_file(args))
            if stale:
                payload, saved_at = stale
                age = max(0, int(time.time() - saved_at))
                _emit(args, "snapshot", payload, started, error=error, stale={"stale": True, "age_seconds": age})
            else:
                _emit(args, "snapshot", None, started, error=error)
            return code
    if args.command == "api":
        if not args.path.startswith("/"):
            return _emit_error(args, "api", ValueError("API path must begin with /"), started)
        try:
            body = json.loads(args.body) if args.body is not None else None
            result = client._request(args.method, args.path, body, json_body=True)
            if isinstance(result, dict) and result.get("code") is not None and str(result.get("code")) != "200":
                raise PlatformError(result.get("code"), result.get("msg", ""))
            _emit(args, "api", result, started)
            return 0
        except Exception as exc:
            return _emit_error(args, "api", exc, started)

    # ---- Special commands ----
    parser.print_help()
    return 2


def _capabilities():
    entries = [
        ("auth.status", False, "Classify local authentication state"),
        ("auth.import", True, "Import and optionally verify session tokens"),
        ("auth.keepalive", True, "Refresh and check session tokens"),
        ("auth.snippet", False, "Create a browser cookie bookmarklet"),
        ("snapshot", False, "Read normalized account and instance state"),
        ("api", False, "Call a platform API endpoint"),
        ("capabilities", False, "List registered CLI capabilities"),
        ("schema", False, "Show command data fields"),
        ("version", False, "Show version and executable path"),
        ("instance.list", False, "List normalized instance records"),
        ("instance.get", False, "Read one instance"), ("instance.describe", False, "Read instance details and local operation history"),
        ("instance.ensure", True, "Reconcile an instance to running or stopped"),
        ("instance.create", True, "Create an instance"), ("instance.clone", True, "Clone an instance"),
        ("instance.release", True, "Release an instance"),
        ("instance.exec", True, "Execute a command over password based SSH"),
        ("instance.ssh", True, "Open an interactive SSH session"),
        ("instance.tunnel", True, "Open an SSH port forward"),
        ("instance.events", False, "Merge local journal and platform billing events"),
        ("billing.balance", False, "Read balance"), ("billing.history", False, "Read billing history"),
        ("billing.orders", False, "Read billing orders"), ("catalog.stock", False, "Read GPU stock"),
        ("catalog.products", False, "Read products"), ("catalog.images", False, "Read images"),
        ("selftest", False, "Check read-only API contracts"), ("completion", False, "Generate shell completion"),
    ]
    return [{"name": name, "write": write, "purpose": purpose} for name, write, purpose in entries]


def _schemas():
    return {
        "auth.status": {"classification": list(AUTH_ACTIONS), "action": "string", "remaining_seconds": "number|null"},
        "auth.import": {"imported": "boolean", "verified": "boolean", "backup_created": "boolean"},
        "auth.keepalive": {"status": "string"},
        "snapshot": {"auth": "object", "balance_yuan": "number|null", "burn_yuan_per_hour": "number|null", "instances": "array"},
        "api": {"platform_response": "object"},
        "capabilities": {"name": "string", "write": "boolean", "purpose": "string"},
        "version": {"version": "string", "executable": "string", "update": "string"},
        "instance.ensure": {"state": "running|stopped", "instance": "object"},
        "instance.plan": {"plan": "array", "cost_change_yuan_per_hour": "number|null", "risks": "array"},
        "instance.exec": {"remote_exit_code": "integer"},
        "instance.describe": {"instance": "object", "recent_operations": "array", "lease": "object|null"},
        "instance.create": {"response": "object"}, "instance.clone": {"response": "object"},
        "instance.release": {"response": "object"},
        "instance.events": {"source": "local|platform", "event": "object"},
        "selftest": {"checks": "array", "healthy": "boolean"},
    }


def _head_hash():
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent.parent,
                          capture_output=True, text=True, timeout=5, check=True).stdout.strip()


def _emit(args, command, data, started, *, error=None, warnings=None, stale=None, unsafe=False):
    envelope = make_envelope(command, data, error=error, warnings=warnings, started=started,
                             cli_version=__version__, stale=stale)
    if unsafe:
        envelope["data"] = _redact_instance(data, unsafe=True)
    if args.jq:
        try:
            rendered = project(envelope, jq=args.jq)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(rendered, ensure_ascii=False) if not isinstance(rendered, str) else rendered)
    elif args.format:
        try:
            print(project(envelope, template=args.format))
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    elif args.quiet:
        rendered = project(envelope["data"], quiet=True)
        print(rendered)
    elif sys.stdout.isatty() and not args.json:
        print(f"{command}: {'ok' if envelope['ok'] else envelope['error']['kind']}")
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        _print_json(envelope)
    return 0


def _emit_error(args, command, exc, started):
    code, error = error_details(exc)
    _emit(args, command, None, started, error=error)
    return code


def _run_auth(args, client, started):
    operation = args.auth_command
    command = "auth." + operation
    try:
        if operation == "status":
            data = classify(client, probe=args.probe)
            _emit(args, command, data, started)
            return 0
        if operation == "import":
            raw = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
            tokens = parse_token_input(raw)
            data = import_tokens(client, tokens, verify=not args.no_verify)
            _emit(args, command, data, started)
            return 0
        if operation == "keepalive":
            client.refresh_token()
            client.check_expiry()
            return 0
    except Exception as exc:
        if operation == "keepalive":
            code, error = error_details(exc)
            print(json.dumps(make_envelope(command, None, error=error, started=started,
                                           cli_version=__version__), ensure_ascii=False), file=sys.stderr)
            return code
        return _emit_error(args, command, exc, started)
    return 0


def _atomic_snapshot(token_file, data):
    path = Path(token_file).with_name("last-snapshot.json")
    payload = {"saved_at": time.time(), "data": redact(data)}
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".gpufree-snapshot-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _read_snapshot(token_file):
    path = Path(token_file).with_name("last-snapshot.json")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value["data"], float(value["saved_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
