"""Exercise the cleanup CLI with a simulated Docker daemon."""

import runpy
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / ".agents/skills/gpu-deployment/scripts/cleanup_test_image.py"
TEST_TAG = "gpu-console:test-abcdef1-run-23"
RELEASE_TAG = "gpu-console:0.3.0"
IMAGE_ID = "sha256:" + "a" * 64
OTHER_ID = "sha256:" + "b" * 64
SOCKET = "unix:///mock/docker.sock"


class FakeDocker:
    def __init__(self):
        self.tags = {TEST_TAG: IMAGE_ID}
        self.containers = {}
        self.removals = []
        self.endpoint = SOCKET
        self.fail_at = None
        self.retag_during_scan = False

    def run(self, argv, **kwargs):
        assert argv[0] == "docker"
        if argv[1:3] == ["context", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, self.endpoint, "")
        assert argv[1:3] == ["--host", SOCKET]
        args = argv[3:]
        command = tuple(args[:2])
        if self.fail_at == command:
            return subprocess.CompletedProcess(argv, 1, "", "模拟 Docker 错误")

        if command == ("image", "ls"):
            assert args[2:] == ["--quiet", TEST_TAG]
            output = self.tags.get(TEST_TAG, "")
        elif command == ("image", "inspect"):
            assert args[-1] == TEST_TAG
            current = self.tags[TEST_TAG]
            output = f"{current} {list(self.tags.values()).count(current)}"
        elif command == ("container", "ls"):
            assert "--all" in args and args[-1] == f"ancestor={IMAGE_ID}"
            if self.retag_during_scan:
                self.tags[TEST_TAG] = OTHER_ID
            output = "\n".join(self.containers)
        elif command == ("container", "inspect"):
            assert args[4:] == list(self.containers)
            output = "\n".join(f"{name} {IMAGE_ID} {ref}" for name, ref in self.containers.items())
        elif command == ("image", "rm"):
            assert args[2:] == ["--no-prune", TEST_TAG]
            self.removals.append(TEST_TAG)
            del self.tags[TEST_TAG]
            output = f"Untagged: {TEST_TAG}"
        else:
            pytest.fail(f"意外调用 {argv}")
        return subprocess.CompletedProcess(argv, 0, output, "")


@pytest.fixture
def docker(monkeypatch):
    engine = FakeDocker()
    monkeypatch.setattr(subprocess, "run", engine.run)
    monkeypatch.setenv("DOCKER_HOST", SOCKET)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    return engine


def invoke(monkeypatch, capsys, tag=TEST_TAG, image_id=IMAGE_ID):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--tag", tag, "--image-id", image_id])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(SCRIPT), run_name="__main__")
    output = capsys.readouterr()
    if exit_info.value.code:
        assert output.err and not output.out
    else:
        assert output.out and not output.err
    return exit_info.value.code


def test_exact_tag_cleanup_is_repeatable(docker, monkeypatch, capsys):
    docker.tags["other-project:test"] = OTHER_ID
    assert invoke(monkeypatch, capsys) == 0
    assert invoke(monkeypatch, capsys) == 0
    assert docker.removals == [TEST_TAG]
    assert docker.tags == {"other-project:test": OTHER_ID}


def test_shared_release_survives_removal_of_test_tag(docker, monkeypatch, capsys):
    docker.tags[RELEASE_TAG] = IMAGE_ID
    docker.containers = {"web": RELEASE_TAG, "worker": RELEASE_TAG}
    assert invoke(monkeypatch, capsys) == 0
    assert docker.tags == {RELEASE_TAG: IMAGE_ID}


@pytest.mark.parametrize("tag,image_id", [
    (RELEASE_TAG, IMAGE_ID),
    ("other-project:test-abcdef1-run-23", IMAGE_ID),
    ("gpu-console:test-*", IMAGE_ID),
    (TEST_TAG, "abcd1234"),
])
def test_rejects_inputs_outside_run(docker, monkeypatch, capsys, tag, image_id):
    assert invoke(monkeypatch, capsys, tag, image_id) != 0
    assert docker.tags == {TEST_TAG: IMAGE_ID}


@pytest.mark.parametrize("during_scan", [False, True])
def test_reused_tag_is_preserved(docker, monkeypatch, capsys, during_scan):
    if during_scan:
        docker.retag_during_scan = True
    else:
        docker.tags[TEST_TAG] = OTHER_ID
    assert invoke(monkeypatch, capsys) != 0
    assert docker.tags == {TEST_TAG: OTHER_ID}


@pytest.mark.parametrize("reference,has_alias", [
    (TEST_TAG, True),
    ("docker.io/library/" + TEST_TAG, True),
    (IMAGE_ID, False),
])
def test_container_using_test_reference_is_reported(docker, monkeypatch, capsys, reference, has_alias):
    if has_alias:
        docker.tags[RELEASE_TAG] = IMAGE_ID
    docker.containers["stopped-test-container"] = reference
    assert invoke(monkeypatch, capsys) != 0
    assert TEST_TAG in docker.tags


@pytest.mark.parametrize("command", [
    ("image", "ls"), ("image", "inspect"), ("container", "ls"), ("container", "inspect"), ("image", "rm"),
])
def test_docker_errors_are_failures(docker, monkeypatch, capsys, command):
    if command == ("container", "inspect"):
        docker.containers["test"] = TEST_TAG
    docker.fail_at = command
    assert invoke(monkeypatch, capsys) != 0
    assert TEST_TAG in docker.tags


def test_remote_context_takes_precedence_over_local_host(docker, monkeypatch, capsys):
    docker.endpoint = "ssh://remote"
    monkeypatch.setenv("DOCKER_CONTEXT", "remote")
    assert invoke(monkeypatch, capsys) != 0
    assert TEST_TAG in docker.tags
