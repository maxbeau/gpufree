"""
CLI for gpufree.cn — see ``gpufree --help`` for available commands.
"""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from gpufree import __version__
from gpufree.client import DEFAULT_TOKEN_FILE, GPUFeeClient


def _print_json(obj: object) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


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
    sub.add_parser("instances", help="List Jupyter/container instances")
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

    if args.command in {"start", "stop"}:
        try:
            options = {"wait": args.wait, "wait_timeout": args.wait_timeout}
            if args.command == "start":
                result = client.start_instance_by_selector(args.selector, start_mode=args.mode, **options)
            else:
                result = client.stop_instance_by_selector(args.selector, **options)
            print(f"{args.command} requested for {args.selector or 'the sole instance'}")
            _print_json(result)
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
