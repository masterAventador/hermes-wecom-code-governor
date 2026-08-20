from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from hermes_wecom_code_governor.execution import MAX_OUTPUT_CHARS
from hermes_wecom_code_governor.policy import Project
from hermes_wecom_code_governor.project_job import (
    CodexSandboxExecutor,
    JobExecutionRequest,
    JobExecutionResult,
    ProjectJobRunner,
    SeatbeltGuiExecutor,
    TrustedExecutor,
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test Bot")
    git(repo, "config", "user.email", "bot@example.test")
    (repo / "README.md").write_text("original\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    return repo


def project(repo: Path, **changes: object) -> Project:
    values: dict[str, object] = {
        "project_id": "demo",
        "display_name": "Demo",
        "path": repo,
        "base_branch": "main",
        "job_allowed_commands": (("./build-artifact",),),
        "job_artifact_globs": ("release/*.bin",),
        "job_timeout_seconds": 120,
    }
    values.update(changes)
    return Project(**values)  # type: ignore[arg-type]


@dataclass
class FakeExecutor:
    result: JobExecutionResult = JobExecutionResult(0, "built", "")

    def __post_init__(self) -> None:
        self.requests: list[JobExecutionRequest] = []

    def run(self, request: JobExecutionRequest) -> JobExecutionResult:
        self.requests.append(request)
        assert git(request.cwd, "branch", "--show-current") == ""
        assert (request.cwd / "README.md").read_text(encoding="utf-8") == "original\n"
        (request.cwd / "README.md").unlink()
        release = request.cwd / "release"
        release.mkdir()
        (release / "demo.bin").write_bytes(b"artifact")
        return self.result


def test_job_runs_in_detached_worktree_stages_artifact_and_removes_workspace(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    executor = FakeExecutor()
    runtime_root = tmp_path / "runtime"
    runner = ProjectJobRunner(runtime_root, executor=executor)
    original_commit = git(repo, "rev-parse", "main")

    result = runner.run(
        project(repo),
        job_id="0818-build-package--message1",
        argv=("./build-artifact",),
        artifact_globs=("release/*.bin",),
    )

    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.output == "built"
    assert result.base_commit == original_commit
    assert len(result.artifacts) == 1
    assert result.artifacts[0].read_bytes() == b"artifact"
    assert result.artifacts[0].is_relative_to(runtime_root / "artifacts")
    assert not (runtime_root / "jobs" / "demo" / "0818-build-package--message1").exists()
    assert (repo / "README.md").read_text(encoding="utf-8") == "original\n"
    assert git(repo, "rev-parse", "main") == original_commit
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_gui_marked_commands_run_on_the_gui_executor_only(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    default_executor = FakeExecutor()
    gui_executor = FakeExecutor()
    runner = ProjectJobRunner(
        tmp_path / "runtime",
        executor=default_executor,
        gui_executor=gui_executor,
    )
    configured = project(repo, job_gui_commands=(("./build-artifact",),))

    result = runner.run(configured, job_id="0818-gui", argv=("./build-artifact",))

    assert result.status == "completed"
    assert len(gui_executor.requests) == 1
    assert default_executor.requests == []


def test_seatbelt_gui_executor_confines_writes_and_network_but_allows_gui(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    for path in (workspace, home, temporary):
        path.mkdir()
    secret_dir = tmp_path / "hermes-home"
    executor = SeatbeltGuiExecutor(denied_read_paths=(secret_dir,))
    request = JobExecutionRequest(
        cwd=workspace,
        home=home,
        temporary=temporary,
        argv=("npm", "run", "qa:screenshot"),
        timeout_seconds=120,
    )

    command = executor.build_command(request)

    assert command[0].endswith("sandbox-exec")
    profile = command[command.index("-p") + 1]
    assert "(allow default)" in profile
    assert "(deny network*)" in profile
    assert '(allow network-bind network-inbound (local ip "*:*"))' in profile
    assert '(allow network-outbound (remote ip "localhost:*"))' in profile
    assert "(deny file-write*)" in profile
    for writable in (workspace, home, temporary):
        assert f'(allow file-write* (subpath "{writable.resolve()}"))' in profile
    for denied in (
        Path.home() / ".ssh",
        Path.home() / ".hermes",
        # 签名/公证密钥目录：p12、p12 密码、公证 p8 都不能被沙箱任务读到。
        Path.home() / ".vpp-signing",
        Path.home() / ".appstoreconnect",
        Path.home() / ".at-tools-credentials",
        secret_dir,
    ):
        assert f'(deny file-read* (subpath "{denied.resolve()}"))' in profile
    assert command[-3:] == ("npm", "run", "qa:screenshot")

    environment = executor.build_environment(request, {"PATH": "/usr/bin:/bin"})
    assert environment["ELECTRON_DISABLE_SANDBOX"] == "1"
    assert environment["HOME"] == str(home.resolve())


def test_trusted_executor_matches_the_user_terminal_environment(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    for path in (workspace, home, temporary):
        path.mkdir()
    request = JobExecutionRequest(
        cwd=workspace,
        home=home,
        temporary=temporary,
        argv=("/bin/sh", "-c", "echo HOME=$HOME"),
        timeout_seconds=60,
        environment=(("CUSTOM_FLAG", "on"),),
    )

    outcome = TrustedExecutor().run(request)

    # 受信档与用户终端同构：保留真实 HOME（登录钥匙串签名依赖它——
    # Security 框架在非真实 HOME 下判定不出有效签名身份），不做沙箱式隔离。
    assert outcome.exit_code == 0, outcome.stderr
    assert f"HOME={Path.home()}" in outcome.stdout

    environment = TrustedExecutor.build_environment(
        request,
        {
            "PATH": "/usr/bin:/bin",
            "HOME": str(Path.home()),
            "TMPDIR": "/var/folders/xx/T/",
            "WECOM_SECRET": "leak-me",
        },
    )
    assert environment["HOME"] == str(Path.home())
    assert environment["TMPDIR"] == "/var/folders/xx/T/"
    assert environment["CUSTOM_FLAG"] == "on"
    # 网关侧密钥等白名单外变量仍不透传。
    assert "WECOM_SECRET" not in environment


def test_trusted_marked_commands_run_on_the_trusted_executor(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    default_executor = FakeExecutor()
    gui_executor = FakeExecutor()
    trusted_executor = FakeExecutor()
    runner = ProjectJobRunner(
        tmp_path / "runtime",
        executor=default_executor,
        gui_executor=gui_executor,
        trusted_executor=trusted_executor,
    )
    configured = project(repo, job_trusted_commands=(("./build-artifact",),))

    result = runner.run(configured, job_id="0820-trusted", argv=("./build-artifact",))

    assert result.status == "completed"
    assert len(trusted_executor.requests) == 1
    assert default_executor.requests == []
    assert gui_executor.requests == []


def test_runner_defaults_the_trusted_executor_to_no_sandbox(tmp_path: Path) -> None:
    runner = ProjectJobRunner(tmp_path / "runtime", codex_binary=Path("/usr/bin/true"))

    # 出厂装配门禁：受信档必须是 TrustedExecutor（钥匙串签名与 seatbelt 互斥），
    # GUI 档必须仍是 seatbelt 收紧档。
    assert isinstance(runner._trusted_executor, TrustedExecutor)
    assert isinstance(runner._gui_executor, SeatbeltGuiExecutor)


def test_trusted_environment_and_seeds_reach_only_trusted_commands(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    secret = tmp_path / "p12-password"
    secret.write_text("sign-pass\n", encoding="utf-8")
    p12_source = tmp_path / "developer-id.p12"
    p12_source.write_bytes(b"p12-bytes")

    @dataclass
    class HomeSnapshotExecutor:
        def __post_init__(self) -> None:
            self.requests: list[JobExecutionRequest] = []
            self.saw_p12 = False

        def run(self, request: JobExecutionRequest) -> JobExecutionResult:
            self.requests.append(request)
            self.saw_p12 = (request.home / ".vpp-signing/developer-id.p12").exists()
            return JobExecutionResult(0, "ok", "")

    default_executor = HomeSnapshotExecutor()
    trusted_executor = HomeSnapshotExecutor()
    runner = ProjectJobRunner(
        tmp_path / "runtime",
        executor=default_executor,
        gui_executor=FakeExecutor(),
        trusted_executor=trusted_executor,
    )
    configured = project(
        repo,
        job_allowed_commands=(("./build-artifact",), ("./package-mac",)),
        job_trusted_commands=(("./package-mac",),),
        job_artifact_globs=(),
        job_environment=(("SHARED_FLAG", "on"),),
        job_trusted_environment=(("CSC_KEY_PASSWORD", f"${{SECRET_FILE:{secret}}}"),),
        job_trusted_home_seeds=((p12_source, Path(".vpp-signing/developer-id.p12")),),
    )

    runner.run(configured, job_id="0820-plain", argv=("./build-artifact",))
    runner.run(configured, job_id="0820-mac", argv=("./package-mac",))

    # 非受信命令：拿不到签名材料——环境无密钥、隔离 HOME 无 p12。
    plain_env = dict(default_executor.requests[0].environment)
    assert plain_env == {"SHARED_FLAG": "on"}
    assert default_executor.saw_p12 is False
    # 受信命令：签名材料齐备。
    trusted_env = dict(trusted_executor.requests[0].environment)
    assert trusted_env["SHARED_FLAG"] == "on"
    assert trusted_env["CSC_KEY_PASSWORD"] == "sign-pass"
    assert trusted_executor.saw_p12 is True


def test_secret_values_are_masked_in_job_output(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    secret = tmp_path / "p12-password"
    secret.write_text("sign-pass-9f3\n", encoding="utf-8")

    @dataclass
    class LeakyExecutor:
        def run(self, request: JobExecutionRequest) -> JobExecutionResult:
            # 密钥可能从任一通道回显——codesign/notarytool 报错、set -x 走 stderr。
            return JobExecutionResult(
                1,
                "keychain unlock -p sign-pass-9f3 failed",
                "codesign: ambient credential sign-pass-9f3 rejected",
            )

    runner = ProjectJobRunner(
        tmp_path / "runtime",
        executor=FakeExecutor(),
        gui_executor=FakeExecutor(),
        trusted_executor=LeakyExecutor(),
    )
    configured = project(
        repo,
        job_allowed_commands=(("./package-mac",),),
        job_trusted_commands=(("./package-mac",),),
        job_artifact_globs=(),
        job_trusted_environment=(("CSC_KEY_PASSWORD", f"${{SECRET_FILE:{secret}}}"),),
    )

    result = runner.run(configured, job_id="0820-leak", argv=("./package-mac",))

    # 密钥值在回传输出里必须被脱敏——它会经模型转发进企微群，stdout/stderr 皆然。
    assert "sign-pass-9f3" not in result.output
    assert result.output.count("***") >= 2


def test_secret_masking_happens_before_output_truncation(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    secret_value = "S3cret-P12-Password-Value"
    secret = tmp_path / "p12-password"
    secret.write_text(secret_value + "\n", encoding="utf-8")

    @dataclass
    class BoundaryLeakExecutor:
        def run(self, request: JobExecutionRequest) -> JobExecutionResult:
            # 让密钥恰好横跨截断切点（total - MAX_OUTPUT_CHARS 落在密钥中间）：
            # 尾部 padding 必须小于 MAX_OUTPUT_CHARS，否则密钥整段被丢弃、
            # 新旧顺序都测不出差别。若先截断后脱敏，尾部会残留明文后缀。
            head = "x" * (MAX_OUTPUT_CHARS - 10)
            tail = "y" * (MAX_OUTPUT_CHARS - 10)
            stdout = head + secret_value + tail
            cut = len(stdout) - MAX_OUTPUT_CHARS
            assert len(head) < cut < len(head) + len(secret_value)
            return JobExecutionResult(1, stdout, "")

    runner = ProjectJobRunner(
        tmp_path / "runtime",
        executor=FakeExecutor(),
        gui_executor=FakeExecutor(),
        trusted_executor=BoundaryLeakExecutor(),
    )
    configured = project(
        repo,
        job_allowed_commands=(("./package-mac",),),
        job_trusted_commands=(("./package-mac",),),
        job_artifact_globs=(),
        job_trusted_environment=(("CSC_KEY_PASSWORD", f"${{SECRET_FILE:{secret}}}"),),
    )

    result = runner.run(configured, job_id="0820-boundary", argv=("./package-mac",))

    # 密钥的任何一段都不允许残留——脱敏必须发生在截断之前。
    for length in range(6, len(secret_value) + 1):
        assert secret_value[-length:] not in result.output


def test_project_environment_reaches_executor_with_job_home_resolved(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    executor = FakeExecutor()
    runtime_root = tmp_path / "runtime"
    runner = ProjectJobRunner(runtime_root, executor=executor, gui_executor=FakeExecutor())
    configured = project(
        repo,
        job_environment=(("VPP_QA_USER_DATA", "${JOB_HOME}/Library/App Support/vpp"),),
    )

    runner.run(configured, job_id="0818-env", argv=("./build-artifact",))

    request = executor.requests[0]
    expected_home = runtime_root / "jobs" / "demo" / "0818-env" / "home"
    assert request.environment == (
        ("VPP_QA_USER_DATA", f"{expected_home.resolve()}/Library/App Support/vpp"),
    )


def test_secret_file_environment_values_are_read_from_the_local_file(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    executor = FakeExecutor()
    runner = ProjectJobRunner(tmp_path / "runtime", executor=executor)
    secret = tmp_path / "p12-password"
    secret.write_text("s3cret-value\n", encoding="utf-8")
    configured = project(
        repo,
        job_environment=(("CSC_KEY_PASSWORD", f"${{SECRET_FILE:{secret}}}"),),
    )

    runner.run(configured, job_id="0820-secret", argv=("./build-artifact",))

    # 密钥值在任务启动时从本机文件读出（去掉尾部换行），不经过入库配置。
    assert executor.requests[0].environment == (("CSC_KEY_PASSWORD", "s3cret-value"),)


def test_missing_secret_file_fails_the_job_loudly(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    runner = ProjectJobRunner(tmp_path / "runtime", executor=FakeExecutor())
    configured = project(
        repo,
        job_environment=(("CSC_KEY_PASSWORD", f"${{SECRET_FILE:{tmp_path / 'absent-file'}}}"),),
    )

    # 密钥文件缺失必须显式失败，不能静默注入空值让签名环节晚点才炸。
    with pytest.raises(FileNotFoundError):
        runner.run(configured, job_id="0820-nosecret", argv=("./build-artifact",))


def test_request_environment_cannot_override_managed_isolation_keys(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    for path in (workspace, home, temporary):
        path.mkdir()
    request = JobExecutionRequest(
        cwd=workspace,
        home=home,
        temporary=temporary,
        argv=("npm", "test"),
        timeout_seconds=60,
        environment=(("CUSTOM_FLAG", "on"), ("HOME", "/evil")),
    )

    environment = CodexSandboxExecutor.build_environment(request, {"PATH": "/usr/bin:/bin"})

    assert environment["CUSTOM_FLAG"] == "on"
    assert environment["HOME"] == str(home.resolve())


@pytest.mark.parametrize(
    "argv",
    (
        ("npm", "run", "unknown"),
        ("sh", "-c", "rm -rf ."),
        ("./build-artifact", "--unexpected"),
    ),
)
def test_job_rejects_commands_outside_exact_admin_patterns(
    tmp_path: Path,
    argv: tuple[str, ...],
) -> None:
    repo = create_repo(tmp_path)
    runner = ProjectJobRunner(tmp_path / "runtime", executor=FakeExecutor())

    with pytest.raises(PermissionError, match="command is not allowed"):
        runner.run(project(repo), job_id="0818-denied", argv=argv)

    assert not (tmp_path / "runtime" / "jobs").exists()


def test_one_token_wildcard_does_not_allow_extra_arguments(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    executor = FakeExecutor()
    runner = ProjectJobRunner(tmp_path / "runtime", executor=executor)
    configured = project(
        repo,
        job_allowed_commands=(("npm", "run", "*"),),
        job_artifact_globs=(),
    )

    runner.run(configured, job_id="0818-test", argv=("npm", "run", "test"))
    with pytest.raises(PermissionError, match="command is not allowed"):
        runner.run(
            configured,
            job_id="0818-injected",
            argv=("npm", "run", "test", "--", "--runInBand"),
        )


def test_job_rejects_unapproved_artifact_globs_before_execution(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    runner = ProjectJobRunner(tmp_path / "runtime", executor=FakeExecutor())

    with pytest.raises(PermissionError, match="artifact glob is not allowed"):
        runner.run(
            project(repo),
            job_id="0818-exfiltrate",
            argv=("./build-artifact",),
            artifact_globs=("../*.txt",),
        )


@dataclass
class NoArtifactExecutor:
    result: JobExecutionResult = JobExecutionResult(0, "tests passed", "")

    def run(self, request: JobExecutionRequest) -> JobExecutionResult:
        return self.result


def test_fallback_globs_tolerate_a_command_that_produces_no_artifacts(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    runner = ProjectJobRunner(tmp_path / "runtime", executor=NoArtifactExecutor())

    # 回退来的 glob（require_artifacts=False）：命令成功但没产出时空手返回，不报错。
    lenient = runner.run(
        project(repo),
        job_id="0820-test-lenient",
        argv=("./build-artifact",),
        artifact_globs=("release/*.bin",),
        require_artifacts=False,
    )
    assert lenient.status == "completed"
    assert lenient.output == "tests passed"
    assert lenient.artifacts == ()
    assert lenient.staging_root is None

    # 显式请求的 glob 仍严格要求产出。
    with pytest.raises(FileNotFoundError, match="was not produced"):
        runner.run(
            project(repo),
            job_id="0820-test-strict",
            argv=("./build-artifact",),
            artifact_globs=("release/*.bin",),
        )


def test_public_validate_rejects_bad_globs_and_names_the_allowlist(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    runner = ProjectJobRunner(tmp_path / "runtime", executor=FakeExecutor())

    # 报错必须列出允许的 glob，让外层模型一次改对而不是盲试。
    with pytest.raises(PermissionError, match=r"release/\*\.bin"):
        runner.validate(
            project(repo),
            job_id="0820-mac",
            argv=("./build-artifact",),
            artifact_globs=("dist/*.dmg",),
        )


def test_failed_job_cleans_worktree_and_does_not_stage_artifacts(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    executor = FakeExecutor(JobExecutionResult(3, "", "build failed"))
    runtime_root = tmp_path / "runtime"
    runner = ProjectJobRunner(runtime_root, executor=executor)

    result = runner.run(
        project(repo),
        job_id="0818-failed",
        argv=("./build-artifact",),
        artifact_globs=("release/*.bin",),
    )

    assert result.status == "failed"
    assert result.exit_code == 3
    assert result.output == "build failed"
    assert result.artifacts == ()
    assert not (runtime_root / "jobs" / "demo" / "0818-failed").exists()
    assert (repo / "README.md").exists()


def test_executor_uses_minimal_read_access_exact_write_roots_and_sanitized_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "workspace"
    home = tmp_path / "home"
    temporary = tmp_path / "tmp"
    for path in (worktree, home, temporary):
        path.mkdir()
    request = JobExecutionRequest(
        cwd=worktree,
        home=home,
        temporary=temporary,
        argv=("npm", "test"),
        timeout_seconds=60,
        readable_paths=(Path("/opt/homebrew"),),
        unix_sockets=(Path("/private/var/run/mDNSResponder"),),
    )
    monkeypatch.setenv("WECOM_SECRET", "must-not-leak")
    monkeypatch.setenv("COS_SECRET_KEY", "must-not-leak")
    monkeypatch.setenv(
        "PATH",
        f"{Path.home()}/fvm/default/bin:/opt/homebrew/bin:/usr/bin:/bin",
    )
    executor = CodexSandboxExecutor(Path("/opt/homebrew/bin/codex"))

    environment = executor.build_environment(request, os.environ)
    state = executor.build_sandbox_state(request)
    command = executor.build_command(request)

    assert "WECOM_SECRET" not in environment
    assert "COS_SECRET_KEY" not in environment
    assert environment["HOME"] == str(home)
    assert environment["TMPDIR"] == str(temporary)
    # 沙箱拒读真实 HOME 下的目录；PATH 里指向 HOME 的条目会让 execvp 型
    # 查找（如 npm 找 sh）拿到 EPERM 直接失败，必须在进沙箱前剔除。
    assert environment["PATH"] == "/opt/homebrew/bin:/usr/bin:/bin"
    assert state["sandboxPolicy"] == {"type": "read-only"}
    entries = state["permissionProfile"]["file_system"]["entries"]
    assert entries[0] == {
        "path": {"type": "special", "value": {"kind": "minimal"}},
        "access": "read",
    }
    assert {
        "path": {"type": "path", "path": str(worktree.resolve())},
        "access": "write",
    } in entries
    assert {
        "path": {"type": "path", "path": str(home.resolve())},
        "access": "write",
    } in entries
    assert {
        "path": {"type": "path", "path": "/opt/homebrew"},
        "access": "read",
    } in entries
    assert state["permissionProfile"]["network"] == "restricted"
    assert command[0] == str(Path("/opt/homebrew/bin/codex").resolve())
    assert command[1:3] == ("sandbox", "--log-denials")
    assert command[3:5] == (
        "--allow-unix-socket",
        "/private/var/run/mDNSResponder",
    )
    assert command[-3:] == ("--", "npm", "test")


def test_project_dependencies_and_build_caches_are_copied_into_the_isolated_job(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-m", "ignore dependencies")
    dependency = repo / "node_modules" / "demo-package" / "index.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("original dependency\n", encoding="utf-8")
    cache_source = tmp_path / "electron-cache"
    cache_source.mkdir()
    (cache_source / "electron.zip").write_bytes(b"cached runtime")
    external_cache_target = tmp_path / "external-cache-target"
    external_cache_target.mkdir()
    (external_cache_target / "outside.txt").write_text("outside\n", encoding="utf-8")
    (cache_source / "root-link").symlink_to(external_cache_target, target_is_directory=True)

    class SeedAwareExecutor:
        def run(self, request: JobExecutionRequest) -> JobExecutionResult:
            isolated_dependency = request.cwd / "node_modules" / "demo-package" / "index.js"
            isolated_cache = request.home / "Library" / "Caches" / "electron" / "electron.zip"
            isolated_link = request.home / "Library" / "Caches" / "electron" / "root-link"
            assert isolated_dependency.read_text(encoding="utf-8") == "original dependency\n"
            assert isolated_cache.read_bytes() == b"cached runtime"
            assert isolated_link.is_symlink()
            assert os.readlink(isolated_link) == str(external_cache_target)
            isolated_dependency.write_text("job-only change\n", encoding="utf-8")
            isolated_cache.write_bytes(b"job-only cache change")
            return JobExecutionResult(0, "ok", "")

    runner = ProjectJobRunner(tmp_path / "runtime", executor=SeedAwareExecutor())
    configured = project(
        repo,
        seed_paths=("node_modules",),
        job_home_seeds=((cache_source, Path("Library/Caches/electron")),),
        job_artifact_globs=(),
    )

    result = runner.run(configured, job_id="0818-seeds", argv=("./build-artifact",))

    assert result.status == "completed"
    assert dependency.read_text(encoding="utf-8") == "original dependency\n"
    assert (cache_source / "electron.zip").read_bytes() == b"cached runtime"


def test_project_seed_path_cannot_escape_the_repository(tmp_path: Path) -> None:
    repo = create_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    runner = ProjectJobRunner(tmp_path / "runtime", executor=FakeExecutor())

    with pytest.raises(PermissionError, match="seed path"):
        runner.run(
            project(repo, seed_paths=("../outside",)),
            job_id="0818-seed-escape",
            argv=("./build-artifact",),
        )


def test_runner_requires_a_real_codex_executable_when_no_executor_is_injected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hermes_wecom_code_governor.project_job.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="codex executable"):
        ProjectJobRunner(tmp_path / "runtime")


def test_job_output_keeps_the_useful_tail_without_flooding_the_outer_agent(
    tmp_path: Path,
) -> None:
    repo = create_repo(tmp_path)
    executor = FakeExecutor(JobExecutionResult(1, "x" * 20_000, "final failure"))
    runner = ProjectJobRunner(tmp_path / "runtime", executor=executor)

    result = runner.run(
        project(repo, job_artifact_globs=()),
        job_id="0818-capped-output",
        argv=("./build-artifact",),
    )

    assert len(result.output) <= 12_100
    assert result.output.startswith("[前面 ")
    assert result.output.endswith("final failure")
