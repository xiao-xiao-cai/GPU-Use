"""Run direct SSH connections from the trusted console host."""

import argparse
import subprocess
from pathlib import Path

from gpu_console.adapters.openssh import TMUX_SESSION, Master, close_master, runtime_dir, scope_key
from gpu_console.application.ssh_access import ID, resolve_target
from gpu_console.config import Config
from gpu_console.storage.maintenance import process_lock


def register(parser: argparse.ArgumentParser) -> None:
    operations = parser.add_subparsers(dest="ssh_operation", required=True)
    for name, description in (
        ("open", "建立或复用 SSH 连接"),
        ("exec", "运行一条 SSH 命令"),
        ("attach", "接入 GPU 实例上的 tmux 终端"),
        ("close", "关闭 SSH 连接"),
    ):
        operation = operations.add_parser(name, help=description)
        operation.add_argument("project_id")
        operation.add_argument("instance_id")
        if name == "exec":
            operation.add_argument("remote_command")


def run(config: Config, args: argparse.Namespace) -> int:
    scope = args.project_id, args.instance_id
    if not all(ID.fullmatch(value) for value in scope):
        raise ValueError("请输入有效的项目 ID 和实例 ID")
    directory = runtime_dir(Path.home() / ".cache" / "gpu-ssh")
    prefix = scope_key(*scope)
    operation = args.ssh_operation
    if operation == "exec" and (not args.remote_command or "\0" in args.remote_command):
        raise ValueError("请输入有效的 SSH 命令")
    with process_lock(directory, f"{prefix}.lock", blocking=True):
        target = None if operation == "close" else resolve_target(config, *scope)
        master = Master.for_target(target, directory) if target else None
        for path in directory.glob(f"{prefix}-*.sock"):
            if master is None or master.path != path:
                close_master(path)
        if master is None:
            print("SSH 连接已关闭")
            return 0
        master.open(config.data_dir)
    if operation == "open":
        print("SSH 已连接")
        return 0
    if operation == "attach":
        command = f"tmux new-session -A -s {TMUX_SESSION}"
    else:
        command = args.remote_command
    result = subprocess.run(master.command(command, tty=operation == "attach"), check=False)
    return result.returncode
