"""Console entry points and interactive local administration."""

import argparse
import getpass
import json
import os
import sqlite3
from pathlib import Path

from pwdlib import PasswordHash

from gpu_console import storage
from gpu_console.application import ssh_cli
from gpu_console.config import Config


def new_password_hash() -> str:
    password = getpass.getpass("新密码：")
    if not 12 <= len(password) <= 256:
        raise ValueError("密码须为 12 至 256 个字符")
    if password != getpass.getpass("再次输入密码："):
        raise ValueError("两次密码输入不一致")
    return PasswordHash.recommended().hash(password)


def init_admin(config: Config) -> None:
    storage.initialize(config.db)
    with storage.connect(config.db) as conn:
        if conn.execute("SELECT 1 FROM administrators").fetchone():
            raise ValueError("管理员已初始化")
    username = input("管理员用户名：").strip()
    if not 1 <= len(username) <= 64:
        raise ValueError("用户名须为 1 至 64 个字符")
    digest = new_password_hash()
    with storage.connect(config.db) as conn:
        try:
            conn.execute("INSERT INTO administrators VALUES (1,?,?)", (username, digest))
        except sqlite3.IntegrityError:
            raise ValueError("管理员已初始化") from None
    print("管理员已初始化")


def manage_mcp_token(config: Config, action: str, key_id: str | None) -> None:
    from gpu_console.mcp import auth
    from gpu_console.storage.maintenance import process_lock

    with process_lock(config.data_dir, ".maintenance.lock", shared=True):
        storage.initialize(config.db)
        with storage.connect(config.db) as conn, storage.transaction(conn):
            if action == "rotate":
                key = auth.get_key(conn, key_id)
                message = auth.issue(
                    conn, key_id=key_id, name=key["name"], manage=key["manage"], ssh=key["ssh"], days=90,
                )["token"]
            elif action == "revoke":
                auth.revoke(conn, key_id)
                message = "MCP 令牌已撤销"
            else:
                message = json.dumps(auth.list_keys(conn), ensure_ascii=False, indent=2)
    print(message)


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="gpu-admin")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="交互初始化唯一管理员")
    commands.add_parser("passwd", help="交互重设管理员密码，并注销现有会话")
    commands.add_parser("web", help="启动网页和管理 API")
    commands.add_parser("mcp", help="启动 MCP Streamable HTTP 服务")
    commands.add_parser("worker", help="启动后台采集、任务与通知")
    commands.add_parser("worker-health", help="检查后台心跳")
    commands.add_parser("backup", help="建立 SQLite 在线备份").add_argument("output", type=Path)
    commands.add_parser("verify-boot-default", help="登记从官方创建页核实的系统盘默认配置").add_argument("profile", type=Path)
    commands.add_parser("verify-cardless-profile", help="登记已实测的无卡档位及可选官方小时价").add_argument("profile", type=Path)
    commands.add_parser("restore", help="从本地备份恢复，进入只读对账状态").add_argument("source", type=Path)
    reconcile = commands.add_parser("restore-purchases", help="录入备份后已核实的额外订单次数")
    reconcile.add_argument("task_id")
    reconcile.add_argument("additional", type=int)
    commands.add_parser("confirm-restore", help="记录对账结果并恢复写入").add_argument("--note", required=True)
    mcp_token = commands.add_parser("mcp-token", help="管理独立的个人 MCP 令牌")
    token_actions = mcp_token.add_subparsers(dest="token_action", required=True)
    token_actions.add_parser("rotate", help="轮换指定 Key，有效期为 90 天").add_argument("--key-id", required=True)
    token_actions.add_parser("revoke", help="撤销指定 Key").add_argument("--key-id", required=True)
    token_actions.add_parser("status", help="列出各 Key 的脱敏状态")
    ssh_cli.register(commands.add_parser("ssh", help="从本机连接 GPU 实例 SSH"))
    args = parser.parse_args()
    try:
        config = Config.from_env()
        if args.command == "init":
            init_admin(config)
        elif args.command == "passwd":
            storage.initialize(config.db)
            digest = new_password_hash()
            with storage.connect(config.db) as conn, storage.transaction(conn):
                if not conn.execute("UPDATE administrators SET password_hash=? WHERE id=1", (digest,)).rowcount:
                    raise ValueError("请先初始化管理员")
                conn.execute("DELETE FROM sessions")
            print("管理员密码已更新")
        elif args.command == "ssh":
            raise SystemExit(ssh_cli.run(config, args))
        elif args.command in ("web", "mcp"):
            import uvicorn

            from gpu_console.api import create_app
            from gpu_console.mcp.server import create_mcp_app

            from gpu_console.storage.maintenance import process_lock

            with process_lock(config.data_dir, ".maintenance.lock", shared=True):
                app = create_app(config) if args.command == "web" else create_mcp_app(config)
                uvicorn.run(app, host="0.0.0.0", port=8080, access_log=False, proxy_headers=False)
        elif args.command in ("worker", "worker-health"):
            from gpu_console.worker.main import check_health, run_worker

            if args.command == "worker":
                run_worker(config)
            else:
                check_health(config)
        elif args.command == "mcp-token":
            manage_mcp_token(config, args.token_action, getattr(args, "key_id", None))
        elif args.command == "verify-boot-default":
            from gpu_console.application.purchase_options import record_boot_disk

            profile = json.loads(args.profile.read_text())
            if not isinstance(profile, dict):
                raise ValueError("系统盘核实记录必须是 JSON 对象")
            storage.initialize(config.db)
            with storage.connect(config.db) as conn, storage.transaction(conn):
                record_boot_disk(conn, profile)
            print("已登记该规格的官方系统盘默认配置")
        elif args.command == "verify-cardless-profile":
            from gpu_console.storage.cardless import record_profile

            profile = json.loads(args.profile.read_text())
            if not isinstance(profile, dict):
                raise ValueError("无卡核实记录必须是 JSON 对象")
            storage.initialize(config.db)
            with storage.connect(config.db) as conn, storage.transaction(conn):
                record_profile(conn, profile)
            print("已登记该可用区的无卡档位")
        else:
            from gpu_console.storage import maintenance

            if args.command == "backup":
                print(f"备份完成，UTC 秒时间 {maintenance.backup(config.db, args.output)}")
            elif args.command == "restore":
                backup_at, restored_at = maintenance.restore(config.db, args.source)
                print(f"已恢复备份，核对 UTC 时间范围 {backup_at} 至 {restored_at}")
            elif args.command == "restore-purchases":
                maintenance.restore_purchases(config.db, args.task_id, args.additional)
                print("已记录采购次数")
            elif args.command == "confirm-restore":
                maintenance.confirm_restore(config.db, args.note)
                print("已记录对账并解除恢复写入限制")
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f"{exc.args[0]}\n")


if __name__ == "__main__":
    main()
