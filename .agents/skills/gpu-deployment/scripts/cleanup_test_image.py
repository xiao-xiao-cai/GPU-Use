"""Remove one GPU console test tag from the local Docker daemon."""

import argparse
import os
import re
import subprocess
import sys
from functools import partial


def docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Docker 命令失败，退出码 {result.returncode}")
    return result.stdout.strip()


def local_endpoint() -> str:
    context = os.environ.get("DOCKER_CONTEXT")
    host = os.environ.get("DOCKER_HOST")
    if context or not host:
        args = ["context", "inspect", "--format", '{{(index .Endpoints "docker").Host}}']
        if context:
            args.append(context)
        host = docker(*args)
    if not host.startswith("unix:///"):
        raise ValueError("清理仅适用于本机 Docker Unix socket，请核对发布使用的 Docker 上下文")
    return host


def cleanup(tag: str, image_id: str) -> str:
    pattern = r"gpu-console:test-[0-9a-f]{7,64}-[A-Za-z0-9][A-Za-z0-9_.-]*"
    if not re.fullmatch(pattern, tag) or len(tag.partition(":")[2]) > 128:
        raise ValueError("需要完整测试标签 gpu-console:test-<Git 修订>-<运行编号>")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("需要以 sha256: 开头的完整镜像 ID")

    local = partial(docker, "--host", local_endpoint())
    if not local("image", "ls", "--quiet", tag):
        return f"测试标签已不存在：{tag}"

    def inspect_tag() -> int:
        current_id, count = local("image", "inspect", "--format", "{{.Id}} {{len .RepoTags}}", tag).split()
        if current_id != image_id:
            raise ValueError(f"{tag} 现指向 {current_id}，与记录的 {image_id} 不同")
        return int(count)

    inspect_tag()
    containers = local("container", "ls", "--all", "--quiet", "--no-trunc", "--filter", f"ancestor={image_id}").split()
    rows = []
    if containers:
        rows = local("container", "inspect", "--format", "{{.Id}} {{.Image}} {{.Config.Image}}", *containers).splitlines()

    # Use the tag's current ownership and alias count after checking container references.
    tag_count = inspect_tag()
    for row in rows:
        container, used_id, used_ref = row.split()
        used_tag = used_ref.partition("@")[0].removeprefix("index.docker.io/")
        used_tag = used_tag.removeprefix("docker.io/").removeprefix("library/")
        if used_tag == tag or (used_id == image_id and tag_count <= 1):
            raise ValueError(f"容器 {container} 正在引用待清理的镜像（{used_ref}），请核对容器归属")

    local("image", "rm", "--no-prune", tag)
    if local("image", "ls", "--quiet", tag):
        raise RuntimeError(f"删除后仍可查到测试标签 {tag}，请核对是否有并发构建")
    return f"已清理测试标签 {tag}（{image_id}）"


def main() -> int:
    parser = argparse.ArgumentParser(description="按本轮记录的标签和完整 ID 清理本机 GPU 实验管理台测试镜像")
    parser.add_argument("--tag", required=True, help="本轮唯一的 gpu-console:test-<修订>-<运行编号>")
    parser.add_argument("--image-id", required=True, help="构建时记录的完整 sha256 镜像 ID")
    args = parser.parse_args()
    try:
        print(cleanup(args.tag, args.image_id))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"清理失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
