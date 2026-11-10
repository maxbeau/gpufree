"""
CLI for gpufree.cn — see ``gpufree --help`` for available commands.
"""

import argparse
import json
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from gpufree import __version__
from gpufree.client import (
    DEFAULT_TOKEN_FILE, GPUFeeClient, PlatformError,
    _format_duration, _yuan, status_text,
)


def _print_json(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _control_output(value):
    """Control confirmations must not expose credentials in a waited instance."""
    if isinstance(value, dict):
        return {key: _control_output(item) for key, item in value.items()
                if key != "ssh_password" and "token" not in key.casefold()}
    if isinstance(value, list):
        return [_control_output(item) for item in value]
    return value


def _print_instances(items) -> None:
    # Deliberate allowlist: never dump passwords or token-like fields in human output.
    for item in items:
        print(f"名称: {item.get('nick_name') or item.get('webide_instance_name', '?')}")
        for label, value in (
            ("uuid", GPUFeeClient._instance_identifiers(item)[1]),
            ("产品", item.get("product_name", "?")),
            ("机房", item.get("data_center_name", "?")),
            ("状态", status_text(item.get("status"))),
            ("start_mode", item.get("start_mode", "?")),
            ("计费", "按量付费" if str(item.get("charge_type")) == "0" else item.get("charge_type", "?")),
            ("ssh_command", item.get("ssh_command", "")),
        ):
            print(f"  {label}: {value}")
        disk = item.get("local_disk_info")
        if isinstance(disk, dict) and "root_fs_used_rate" in disk:
            print(f"  根盘使用率: {disk['root_fs_used_rate']}")
        release = item.get("release_time")
        if release:
            suffix = ""
            try:
                expires = datetime.fromisoformat(release.replace("Z", "+00:00"))
                remaining = (expires - datetime.now(expires.tzinfo)).total_seconds()
                suffix = "已过期" if remaining < 0 else "剩 " + _format_duration(int(remaining))
            except (ValueError, TypeError):
                pass
            print(f"  release_time: {release}" + (f" ({suffix})" if suffix else ""))
        print()


def _start_with_stock(client, args):
    options = {"wait": args.wait, "wait_timeout": args.wait_timeout}
    if args.mode == "nogpu" or not (args.when_available or args.fallback):
        if args.mode == "nogpu" and (args.when_available or args.fallback):
            print("nogpu 模式：忽略 --when-available 和 --fallback")
        return client.start_instance_by_selector(args.selector, start_mode=args.mode, **options)
    item = client.resolve_instance(args.selector)
    available = (client.wait_for_gpu_stock(item, args.wait_timeout) if args.when_available
                 else client.gpu_stock_for_instance(item) > 0)
    mode = "gpu"
    if not available:
        if not args.fallback:
            raise RuntimeError("Timed out waiting for GPU stock")
        mode = "nogpu"
    try:
        response = client.start_instance(*client._instance_identifiers(item), start_mode=mode)
    except PlatformError as exc:
        if str(exc.code) != "53523" or not args.fallback or mode != "gpu":
            raise
        mode = "nogpu"
        response = client.start_instance(*client._instance_identifiers(item), start_mode=mode)
    if mode == "nogpu":
        print("已降级为无卡模式 (nogpu)")
    if args.wait:
        return {"response": response, "instance": client._wait_for_instance(item, 3, args.wait_timeout)}
    return response


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="gpufree",
        description="GPUFee.cn (算力自由) API Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  gpufree status           Show overall account status
  gpufree balance          Show balance overview
  gpufree instances        List running GPU instances
  gpufree products         List available products""",
    )
    parser.add_argument(
        "--token-file", type=Path, default=DEFAULT_TOKEN_FILE,
        help=f"Path to token JSON (default: {DEFAULT_TOKEN_FILE})",
    )
    parser.add_argument(
        "--client-id", type=str, default=None,
        help="Override OAuth client_id (extracted from stored tokens by default)",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    sub = parser.add_subparsers(dest="command", help="Sub-command")

    # Status
    sub.add_parser("status", help="Overall status: balance + instances + user")

    # Balance
    sub.add_parser("balance", help="Balance overview")
    sub.add_parser("balance-detail", help="Raw balance detail")
    sub.add_parser("balance-history", help="Balance change history")
    sub.add_parser("orders", help="Billing order records")
    sub.add_parser("transactions", help="Transaction records")
    sub.add_parser("daily-bills", help="Daily bill records")
    sub.add_parser("recharge-state", help="Recharge order state")
    sub.add_parser("vouchers", help="Available vouchers/coupons")

    # Instances
    sub.add_parser("instances", help="List Jupyter/container instances").add_argument("--json", action="store_true")
    sub.add_parser("instance-tags", help="Instance status tags")
    sub.add_parser("instance-ide", help="Instance IDE resource info")
    sub.add_parser("instance-left-copy", help="Remaining instance copy count")

    for action in ("start", "stop"):
        command = sub.add_parser(action, help=f"Manually {action} an instance")
        command.add_argument("selector", nargs="?", help="Instance ID, UUID, or exact name")
        command.add_argument("--wait", action="store_true")
        command.add_argument("--wait-timeout", type=int, default=120)
        if action == "start":
            command.add_argument("--mode", choices=("gpu", "nogpu"), default="gpu")
            command.add_argument("--when-available", action="store_true",
                                 help="Wait for GPU stock every 30s, bounded by --wait-timeout")
            command.add_argument("--fallback", choices=("nogpu",),
                                 help="Fall back once to nogpu on no stock or control code 53523")

    create = sub.add_parser("create", help="Create an instance")
    create.add_argument("--sku", required=True)
    create.add_argument("--dc", type=int, required=True)
    create.add_argument("--image", type=int, required=True)
    create.add_argument("--count", type=int, default=1)
    create.add_argument("--mode", choices=("gpu", "nogpu"), default="gpu")
    create.add_argument("--charge-type", type=int, default=0)
    create.add_argument("--expand-disk-gb", type=int, default=0)
    create.add_argument("--duration-type", default="")
    create.add_argument("--duration-count", type=int, default=1)

    clone = sub.add_parser("clone", help="Clone a stopped instance (without data by default)")
    clone.add_argument("selector", nargs="?", help="Instance ID, UUID, or exact name")
    clone.add_argument("--name")
    clone.add_argument("--with-data", action="store_true")
    clone.add_argument("--expand-disk-gb", type=int, default=0)

    release = sub.add_parser("release", help="Release an instance after UUID confirmation")
    release.add_argument("selector", nargs="?", help="Instance ID, UUID, or exact name")
    release.add_argument("--confirm", help="First five UUID characters (case-insensitive), or full UUID")

    watch = sub.add_parser("watch", help="Watch instance status changes")
    watch.add_argument("selector", nargs="?")
    watch.add_argument("--until", choices=("running", "stopped"), default="running")
    watch.add_argument("--interval", type=float, default=10)
    watch.add_argument("--timeout", type=float, default=0)
    stock = sub.add_parser("stock", help="Show GPU stock")
    stock.add_argument("query", nargs="?")
    stock.add_argument("--available", action="store_true")
    stock.add_argument("--json", action="store_true")

    # Dashboard
    sub.add_parser("dashboard", help="Dashboard resource instances")
    sub.add_parser("dashboard-storage", help="Dashboard storage usage")

    # Products
    sub.add_parser("products", help="List all GPU products")
    sub.add_parser("hot", help="Featured/hot products")
    sub.add_parser("gpu-names", help="Available GPU model names")
    sub.add_parser("product-tags", help="Product category tags")
    sub.add_parser("sku-prices", help="Product SKU pricing")

    # User
    sub.add_parser("userinfo", help="User profile")
    sub.add_parser("messages", help="Unread message count")
    sub.add_parser("message-list", help="Message list")
    sub.add_parser("invitation", help="Invitation code")

    # Other
    sub.add_parser("ssh-keys", help="Registered SSH public keys")
    sub.add_parser("storage", help="File storage list")
    sub.add_parser("images", help="Public container images")
    sub.add_parser("refresh", help="Refresh session tokens now")
    sub.add_parser("client-id", help="Show the client_id currently in use")
    sub.add_parser("login", help="Interactive login — extract tokens from browser")
    sub.add_parser("check", help="Check token expiry status")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    if args.command == "login":
        return GPUFeeClient.login_interactive(args.token_file)

    try:
        client = GPUFeeClient(token_file=args.token_file, client_id=args.client_id)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # ---- Special commands ----
    if args.command == "refresh":
        try:
            client.refresh_token()
            print("Token refreshed successfully.")
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "check":
        _print_json(client.check_expiry())
        return 0

    if args.command == "client-id":
        print(f"client_id: {client.client_id}")
        print(f"source:     {client.client_id_source}")
        return 0

    if args.command in {"create", "clone", "release"}:
        try:
            if args.command == "create":
                result = client.create_instance(
                    product_sku_code=args.sku, data_center_id=args.dc, image_id=args.image,
                    req_amount=args.count, start_model=args.mode, charge_type=args.charge_type,
                    expand_data_disk_gb=args.expand_disk_gb,
                    duration_type=args.duration_type, duration_count=args.duration_count)
            elif args.command == "clone":
                result = client.clone_instance_by_selector(
                    args.selector, nickname=args.name, clone_data=args.with_data,
                    expand_data_disk_gb=args.expand_disk_gb)
            else:
                if not args.confirm:
                    raise ValueError("release requires --confirm with the first 5 UUID characters or full UUID")
                item = client.resolve_instance(args.selector)
                instance_id, instance_uuid = client._instance_identifiers(item)
                if args.confirm.casefold() not in {instance_uuid[:5].casefold(), instance_uuid.casefold()}:
                    raise ValueError("--confirm must match the first 5 UUID characters or full UUID")
                result = client.release_instance(instance_id, instance_uuid)
            _print_json(_control_output(result))
        except (RuntimeError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command in {"start", "stop"}:
        try:
            options = {"wait": args.wait, "wait_timeout": args.wait_timeout}
            if args.command == "start":
                result = _start_with_stock(client, args)
            else:
                result = client.stop_instance_by_selector(args.selector, **options)
            print(f"{args.command} requested for {args.selector or 'the sole instance'}")
            _print_json(_control_output(result))
        except (RuntimeError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command in {"instances", "stock", "watch"}:
        try:
            if args.command == "instances":
                response = client.list_instances()
                if args.json:
                    _print_json(response)
                else:
                    _print_instances((response.get("data") or {}).get("dataList") or [])
            elif args.command == "stock":
                products = client.stock_products(args.query, available=args.available)
                if args.json:
                    _print_json(products)
                else:
                    print("产品\t价格\t可用节点\t机房")
                    for product in products:
                        total, details = GPUFeeClient.product_stock(product)
                        locations = " ".join(f"dc{dc}:{left}" for dc, left in details if left > 0) or "0"
                        print(f"{product.get('product_name', '?')}\t¥{_yuan(product.get('current_price'))}/h\t{total}\t{locations}")
            else:
                client.watch_instance(args.selector, until=args.until, interval=args.interval,
                                      timeout=args.timeout, progress=lambda status: print(
                                          f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z} {status_text(status)}"))
        except (RuntimeError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    # ---- API commands ----
    commands: dict[str, Callable[[], dict]] = {
        "status": client.get_status,
        # Balance
        "balance": client.get_balance_overview,
        "balance-detail": client.get_balance,
        "balance-history": client.get_balance_history,
        "orders": client.get_orders,
        "transactions": client.get_transactions,
        "daily-bills": client.get_daily_bills,
        "recharge-state": client.get_recharge_state,
        "vouchers": client.get_vouchers,
        # Instances
        "instances": client.list_instances,
        "instance-tags": client.get_instance_status_tags,
        "instance-ide": client.get_instance_ide_res,
        "instance-left-copy": client.get_instance_left_copy,
        # Dashboard
        "dashboard": client.get_dashboard_instances,
        "dashboard-storage": client.get_dashboard_storage,
        # Products
        "products": client.list_products,
        "hot": client.get_hot_products,
        "gpu-names": client.list_gpu_names,
        "product-tags": client.get_product_tags,
        "sku-prices": client.get_sku_price,
        # User
        "userinfo": client.get_userinfo,
        "messages": client.get_unread_count,
        "message-list": client.get_messages,
        "invitation": client.get_invitation_code,
        # Other
        "ssh-keys": client.list_ssh_keys,
        "storage": client.list_file_storage,
        "images": client.list_public_images,
    }

    handler = commands.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    try:
        _print_json(handler())
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
