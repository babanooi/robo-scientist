#!/usr/bin/env python3
"""Generate a self-contained evidence package for the software virtual route.

The package produced by this script is intentionally limited to the verified
pure-Python virtual workcell.  It runs one ``pose_offset`` campaign and then
repeats P0/P1 ten times under the same conditions.  No robot is connected and
Qwen is explicitly disabled so that the result is reproducible without a
credential.  The script copies only generated evidence; it never copies the
repository's ``.env`` or any source of API keys.

Example::

    python scripts/generate_virtual_submission.py \
        --output-dir /tmp/roboscientist-virtual-submission

The output directory must be new or empty.  A failed/partial package is not
silently overwritten, which prevents stale evidence from being mistaken for a
new run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
# Allow direct execution from a checkout (``python scripts/...``) without
# requiring a prior editable install.  The generated source snapshot uses the
# same layout, so this remains portable there as well.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
TASK_TEXT = "把红色方块放到右侧目标区域"
SCENARIO = "pose_offset"
MODE = "simulation"
REPEATS = 10
PACKAGE_VERSION = "0.1"

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    # Match non-empty credential values while allowing the explicit redacted
    # placeholders emitted by ``_sanitize_source_text``.  The value boundary
    # deliberately excludes quotes and escaped newlines so this pattern is
    # safe for both shell snippets and Python string fixtures.
    re.compile(
        r"(?i)(?:DASHSCOPE|OPENAI|QWEN|ROBO)_API_KEY\s*[:=]\s*['\"]?"
        r"(?!REDACTED(?:_[A-Z_]+)?\b)(?!<[^>]+>)[^'\"\s,}\\\n]{16,}"
    ),
    re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----"),
)

# A submission package must be independently inspectable.  Keep the snapshot
# deliberately small and explicit instead of copying the whole checkout (and
# accidentally including credentials, caches, local hardware archives, or
# unrelated development files).
_SOURCE_COPY_SPECS = (
    "roboscientist",
    "configs",
    "tests",
    # Compatibility modules are imported directly by the contract tests.
    # They remain inert unless a separately configured real-arm profile and
    # explicit safety gate are supplied, so including them does not change the
    # pure-software execution path.
    "armpi_backend.py",
    "armpi_final_wrapper_backend.py",
    "armpi_parameterized_runner.py",
    "pyproject.toml",
    "uv.lock",
    "LICENSE",
    "README.md",
    "scripts",
)

# These are the documents a reviewer needs to understand and reproduce the
# current software-only route.  Hardware manuals and private delivery
# archives intentionally stay outside this package.
_DOC_COPY_SPECS = {
    "docs/虚拟工作台运行说明.md": "docs/virtual_workbench_runbook.md",
    "docs/API.md": "docs/api.md",
    "docs/提交限制声明_无实机.md": "docs/limitations_no_physical_robot.md",
    "docs/当前路线决议_纯软件虚拟实验.md": "docs/route_decision.md",
    # Submission-facing material is included so the frozen package can be
    # reviewed and presented without reaching back into the checkout.
    "docs/提交演示脚本_无实机路线.md": "docs/demo_script_no_physical_robot.md",
    "docs/提交材料提纲_20页.md": "docs/submission_outline_20_pages.md",
    "docs/虚拟实验验收报告.md": "docs/virtual_experiment_acceptance_report.md",
    "docs/虚拟实验验收报告_模板.md": "docs/virtual_experiment_acceptance_template.md",
    "docs/提交包说明_无实机路线.md": "docs/submission_package_spec_no_physical_robot.md",
}

# The same selected runbooks are embedded in the source snapshot so that the
# copied generator can be run from ``submission/source`` without reaching back
# into the original checkout.
_SOURCE_DOC_COPY_SPECS = tuple(_DOC_COPY_SPECS.keys())

_FRONTEND_FILES = (
    "index.html",
    "app.js",
    "app.css",
)

_VIRTUAL_ARTIFACT_FILES = frozenset(
    {
        "scene.json",
        "trajectory.json",
        "evaluation.json",
        "execution_trace.json",
        "replay.json",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _safe_test_environment() -> dict[str, str]:
    """Return an environment in which tests cannot accidentally use a key."""

    env = dict(os.environ)
    for name in (
        "DASHSCOPE_API_KEY",
        "QWEN_API_KEY",
        "OPENAI_API_KEY",
        "ROBO_API_KEY",
    ):
        env.pop(name, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_check(
    command: Sequence[str],
    *,
    label: str,
    cwd: Path = REPO_ROOT,
    env: Optional[Mapping[str, str]] = None,
) -> tuple[int, str]:
    """Run a verification command and return an intentionally portable log."""

    result = subprocess.run(
        list(command),
        cwd=str(cwd),
        env=dict(env) if env is not None else _safe_test_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = result.stdout or ""
    # Logs are part of the submission package.  Do not leak local checkout or
    # temporary-data paths if a test prints one in a traceback.
    output = output.replace(str(REPO_ROOT), "<repo>")
    return result.returncode, f"$ {label}\n{output.rstrip()}\n[exit_code={result.returncode}]\n"


def _sanitize(value: Any, temporary_root: Path) -> Any:
    """Make generated JSON portable and redact credentials defensively."""

    if isinstance(value, Mapping):
        result: MutableMapping[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            # These keys should never occur in a deterministic run, but keep
            # the package safe if a future adapter adds one.
            if key_text.lower() in {
                "api_key",
                "authorization",
                "token",
                "secret",
                "password",
            }:
                continue
            result[key_text] = _sanitize(item, temporary_root)
        return dict(result)
    if isinstance(value, list):
        return [_sanitize(item, temporary_root) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item, temporary_root) for item in value]
    if isinstance(value, str):
        text = value.replace("\\", "/")
        root = temporary_root.resolve().as_posix().rstrip("/")
        if text == root:
            text = "evidence/data"
        elif text.startswith(root + "/"):
            text = "evidence/data/" + text[len(root) + 1 :]
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub("[REDACTED]", text)
        return text
    return value


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _copy_and_sanitize_json_tree(source: Path, destination: Path, temporary_root: Path) -> None:
    """Copy generated data and rewrite only path/secret values in JSON files."""

    shutil.copytree(source, destination)
    for path in destination.rglob("*.json"):
        try:
            payload = _read_json(path)
        except (OSError, json.JSONDecodeError):
            # All current generated JSON is valid.  Leave an unknown future
            # artifact untouched rather than deleting evidence.
            continue
        _json_dump(path, _sanitize(payload, temporary_root))


def _refresh_virtual_artifact_manifests(evidence_root: Path) -> int:
    """Re-hash inner virtual evidence after portable JSON rewriting.

    ``_copy_and_sanitize_json_tree`` deliberately rewrites JSON so absolute
    temporary paths and credential-shaped content cannot escape in the package.
    Virtual workcell artifacts include their own byte-level manifest, therefore
    those nested hashes must be regenerated after that rewrite.  The outer
    ``SHA256SUMS.txt`` is written later and covers these refreshed manifests.
    """

    artifact_root = evidence_root / "virtual_artifacts"
    if not artifact_root.is_dir():
        return 0

    refreshed = 0
    for manifest_path in sorted(artifact_root.glob("*/manifest.json")):
        payload = _read_json(manifest_path)
        if payload.get("format_version") != "virtual-evidence-v1":
            raise RuntimeError(
                f"unexpected virtual artifact manifest format: {manifest_path}"
            )
        files = payload.get("files")
        if not isinstance(files, Mapping) or not files:
            raise RuntimeError(
                f"virtual artifact manifest has no verifiable files: {manifest_path}"
            )
        if set(files) != _VIRTUAL_ARTIFACT_FILES:
            raise RuntimeError(
                f"virtual artifact manifest has unexpected file set: {manifest_path}"
            )

        refreshed_hashes: dict[str, str] = {}
        for name in sorted(files):
            if (
                not isinstance(name, str)
                or not name
                or "/" in name
                or "\\" in name
                or name in {".", ".."}
                or Path(name).name != name
            ):
                raise RuntimeError(
                    f"virtual artifact manifest contains invalid file name: {name!r}"
                )
            artifact_path = manifest_path.parent / name
            if not artifact_path.is_file():
                raise RuntimeError(
                    f"virtual artifact manifest references missing file: {artifact_path}"
                )
            refreshed_hashes[name] = _sha256(artifact_path)
        payload["files"] = refreshed_hashes
        _json_dump(manifest_path, payload)
        refreshed += 1
    return refreshed


def _verify_virtual_artifact_manifests(evidence_root: Path) -> int:
    """Fail closed unless every nested virtual manifest matches its payloads."""

    artifact_root = evidence_root / "virtual_artifacts"
    if not artifact_root.is_dir():
        return 0

    verified = 0
    for manifest_path in sorted(artifact_root.glob("*/manifest.json")):
        payload = _read_json(manifest_path)
        if payload.get("format_version") != "virtual-evidence-v1":
            raise RuntimeError(
                f"unexpected virtual artifact manifest format: {manifest_path}"
            )
        files = payload.get("files")
        if not isinstance(files, Mapping) or not files:
            raise RuntimeError(
                f"virtual artifact manifest has no verifiable files: {manifest_path}"
            )
        if set(files) != _VIRTUAL_ARTIFACT_FILES:
            raise RuntimeError(
                f"virtual artifact manifest has unexpected file set: {manifest_path}"
            )
        for name, expected in files.items():
            if (
                not isinstance(name, str)
                or not name
                or "/" in name
                or "\\" in name
                or name in {".", ".."}
                or Path(name).name != name
            ):
                raise RuntimeError(
                    f"virtual artifact manifest contains invalid file name: {name!r}"
                )
            artifact_path = manifest_path.parent / name
            if not artifact_path.is_file():
                raise RuntimeError(
                    f"virtual artifact manifest references missing file: {artifact_path}"
                )
            actual = _sha256(artifact_path)
            if not isinstance(expected, str) or actual != expected:
                raise RuntimeError(
                    f"virtual artifact digest mismatch: {artifact_path}"
                )
        verified += 1
    return verified


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_files(root: Path, *, exclude: Iterable[str] = ()) -> list[Path]:
    excluded = {str(item).replace("\\", "/") for item in exclude}
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _sanitize_source_text(text: str) -> str:
    """Redact local-only paths and credential-shaped fixtures in source/docs.

    The source snapshot is intended for review and offline reproduction, not
    for preserving a developer's local environment byte-for-byte.  Replacing
    fake credential fixtures consistently (rather than dropping whole files)
    keeps the snapshot readable while making the package-level secret scan
    fail closed.
    """

    text = text.replace(str(REPO_ROOT), "<repo>")
    # Remove other user-specific macOS paths that may have been copied into a
    # runbook or a traceback.  Generic /tmp paths are intentionally retained
    # because they are portable scratch locations used by the instructions.
    text = re.sub(r"/Users/[^\s`\"']+", "<local-path>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "REDACTED_TEST_SECRET", text)
    # Environment assignments in examples can contain a real value even when
    # the value is not key-shaped.  Redact only the value.  In particular, do
    # not consume an escaped ``\\n`` or closing quote in a Python fixture such
    # as ``"DASHSCOPE_API_KEY=sk-local-test\\n"``; doing so would make the
    # copied source syntactically invalid.
    assignment_pattern = re.compile(
        r"(?i)((?:export\s+)?(?:DASHSCOPE|OPENAI|QWEN|ROBO)_API_KEY\s*[:=]\s*)"
        r"(['\"]?)([^\s,}\'\"\\\n]+)"
    )
    text = assignment_pattern.sub(
        lambda match: (
            match.group(0)
            if match.group(3) == "sk-local-test"
            else f"{match.group(1)}{match.group(2)}REDACTED_ENV_VALUE"
        ),
        text,
    )
    text = re.sub(
        r"-----BEGIN [A-Z ]+ PRIVATE KEY-----.*?-----END [A-Z ]+ PRIVATE KEY-----",
        "[REDACTED_PRIVATE_KEY]",
        text,
        flags=re.DOTALL,
    )
    return text


def _copy_sanitized_file(source: Path, destination: Path) -> None:
    """Copy a text or binary file while sanitizing textual package content."""

    data = source.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_sanitize_source_text(text), encoding="utf-8")


def _copy_source_snapshot(output: Path) -> Path:
    """Copy the allow-listed source tree into ``output/source``."""

    source_root = output / "source"
    for spec in _SOURCE_COPY_SPECS:
        source = REPO_ROOT / spec
        if not source.exists():
            raise FileNotFoundError(f"required source snapshot path is missing: {source}")
        if source.is_dir():
            for path in sorted(source.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                if (
                    "__pycache__" in path.parts
                    or path.suffix == ".pyc"
                    or path.name.startswith(".")
                ):
                    continue
                relative = path.relative_to(REPO_ROOT)
                _copy_sanitized_file(path, source_root / relative)
        else:
            _copy_sanitized_file(source, source_root / spec)
    for spec in _SOURCE_DOC_COPY_SPECS:
        source = REPO_ROOT / spec
        if not source.is_file():
            raise FileNotFoundError(f"required source snapshot document is missing: {source}")
        _copy_sanitized_file(source, source_root / spec)
    return source_root


def _copy_documentation(output: Path) -> Path:
    """Copy the minimal runbook set under stable, ASCII-friendly names."""

    docs_root = output / "docs"
    for source_name, destination_name in _DOC_COPY_SPECS.items():
        source = REPO_ROOT / source_name
        if not source.is_file():
            raise FileNotFoundError(f"required documentation path is missing: {source}")
        _copy_sanitized_file(source, output / destination_name)
    return docs_root


def _copy_frontend(output: Path) -> Path:
    """Copy the static UI and add a portable local startup note."""

    frontend_root = output / "frontend"
    static_root = REPO_ROOT / "roboscientist" / "web" / "static"
    for filename in _FRONTEND_FILES:
        source = static_root / filename
        if not source.is_file():
            raise FileNotFoundError(f"required frontend asset is missing: {source}")
        _copy_sanitized_file(source, frontend_root / filename)
    (frontend_root / "entry_url.txt").write_text(
        "http://127.0.0.1:8001/\n", encoding="utf-8"
    )
    (frontend_root / "README.md").write_text(
        """# 本地前端入口

本目录包含可交互看板的静态资源。请在提交包根目录执行：

```sh
cd source
python -m roboscientist.web.server --host 127.0.0.1 --port 8001 --data-root ../runtime-data
```

然后打开 `entry_url.txt` 中的地址。页面默认运行纯 Python 虚拟工作单元；
它不连接实机，也不代表 Gazebo/MoveIt2 结果。
""",
        encoding="utf-8",
    )
    return frontend_root


def _write_submission_reports(
    output: Path,
    *,
    campaign: Mapping[str, Any],
    validation: Mapping[str, Any],
    run_summary: Mapping[str, Any],
) -> Path:
    """Write reviewer-facing reports populated from the frozen campaign."""

    report_root = output / "report"
    report_root.mkdir(parents=True, exist_ok=True)
    summary = validation.get("summary") or {}
    p0 = summary.get("p0") or {}
    p1 = summary.get("p1") or {}
    comparison = validation.get("comparison") or {}
    campaign_id = str(campaign.get("campaign_id", "unknown"))
    report = f"""# RoboScientist 软件虚拟实验验收报告

## 1. 验收范围

- 赛道：赛道一·方向 1B「科学实验任务规划与反馈迭代」
- 运行路线：纯 Python 确定性软件虚拟工作单元
- 数据来源：`simulation`
- 运行时：`roboscientist.virtual_workcell` / `python_deterministic`
- 物理机械臂：未连接（`physical_robot_connected=false`）
- Gazebo/MoveIt2：未使用
- Campaign：`{campaign_id}`

## 2. 可复验闭环

本次固定任务为“把红色方块放到右侧目标区域”，场景为
`pose_offset`。P0 使用基线 Skill，虚拟评价器注入可重复的定位残差；系统
根据结构化失败证据生成只改变 `grasp_offset` 参数族的候选 Skill，随后在
相同任务、场景、安全约束和评价器下执行 P1。

| 版本 | 样本数 | 任务完成率 | 位置误差均值 | 碰撞率 | 结论 |
|---|---:|---:|---:|---:|---|
| P0 | {p0.get('sample_count', 0)} | {p0.get('task_success_rate')} | {p0.get('position_error_mean_m')} m | {p0.get('collision_rate')} | 基线失败 |
| P1 candidate | {p1.get('sample_count', 0)} | {p1.get('task_success_rate')} | {p1.get('position_error_mean_m')} m | {p1.get('collision_rate')} | 候选改善 |

机器决策：`{comparison.get('decision')}`。P1 仍标记为 `candidate_only`，
因为本包只证明软件虚拟工作单元中的可重复改善，不把它晋升为真实机械臂
Skill，也不外推为物理路径最优。

## 3. 证据索引

- `campaign.json`：P0→反馈→P1 的完整活动记录
- `validation.json`：P0/P1 各 `{validation.get('repeats_per_version', 0)}` 次同条件验证
- `comparison.json`：机器可读的指标比较与边界字段
- `evidence/data/`：计划、结果、失败分析、轨迹、评价和 replay 工件
- `test_run.log`：生成包前的测试、编译和前端语法检查
- `SHA256SUMS.txt`：包内文件校验清单

## 4. 允许的结论

可以表述：系统能够在可复验的软件虚拟工作单元中完成“计划—执行—评价—
失败归因—单参数反馈—再验证”闭环，并保留可调用 API、交互式前端和完整
证据链。

不能表述：真实机械臂已经抓取、Gazebo/MoveIt2 已验证、真实抓取率提升、
路径达到物理最优，或 Qwen 已在本包中成功调用。本包的生成模式是
`use_qwen=false` 的离线确定性模式。

## 5. 复核命令

```sh
shasum -a 256 -c SHA256SUMS.txt
cd source
python -m unittest discover -s tests -p 'test_*.py' -q
python -m roboscientist.web.server --host 127.0.0.1 --port 8001 --data-root ../runtime-data
```
"""
    (report_root / "virtual_experiment_acceptance_report.md").write_text(
        report, encoding="utf-8"
    )
    (report_root / "technical_solution_outline.md").write_text(
        """# 技术方案摘要

任务输入 → 结构化计划 → 确定性安全门 → 虚拟执行器 → 独立评价器 →
失败归因 → 单参数候选 Skill → 同条件重复验证 → 晋升/拒绝决策。

Qwen 接口作为可选的研究计划与反馈解释层；它不能绕过安全检查、评价器
或版本约束。当前冻结包使用离线确定性规划，以保证第三方无需密钥即可复现。
""",
        encoding="utf-8",
    )
    (report_root / "demo_script.md").write_text(
        """# 5 分钟演示顺序

1. 启动 `source` 下的 Web 服务并打开 `http://127.0.0.1:8001/`。
2. 保持 Simulation，输入“把红色方块放到右侧目标区域”。
3. 点击“运行 P0 → P1 科研闭环”，观察 P0 失败、失败码和候选参数。
4. 观察 P1 在相同条件下完成，展开仿真遥测查看轨迹和评价。
5. 点击“重复验证 P0 / P1（10 次）”，展示完成率、误差和决策。
6. 明确说明：这是纯 Python 软件虚拟工作单元，不是实机或 Gazebo/MoveIt2。
""",
        encoding="utf-8",
    )
    return report_root


def _tree_sha256(root: Path) -> str:
    """Hash relative names and bytes so the snapshot digest is portable."""

    digest = hashlib.sha256()
    for path in _relative_files(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        data = path.read_bytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _write_checksums(root: Path) -> Path:
    checksum_path = root / "SHA256SUMS.txt"
    lines = [
        f"{_sha256(path)}  {path.relative_to(root).as_posix()}"
        for path in _relative_files(root, exclude=("SHA256SUMS.txt",))
    ]
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return checksum_path


def _git_metadata() -> dict[str, Any]:
    def git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                check=False,
            )
        except OSError:
            return "unknown"
        return result.stdout.strip() if result.returncode == 0 else "unknown"

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    return {
        "commit": commit,
        "short_commit": commit[:12] if commit != "unknown" else "unknown",
        "working_tree_dirty": bool(status) if status != "unknown" else None,
    }


def _default_output_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return REPO_ROOT / "outputs" / f"virtual_submission_{stamp}_{uuid.uuid4().hex[:8]}"


def _ensure_new_output(path: Path) -> Path:
    path = path.expanduser()
    if path.resolve() == REPO_ROOT:
        raise ValueError("output directory must not be the repository root")
    if path.exists() and any(path.iterdir()):
        raise FileExistsError(
            f"output directory is not empty: {path}; choose a new directory"
        )
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _assert_virtual_campaign(campaign: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the evidence contract before writing a submission package."""

    records = list(campaign.get("records") or [])
    if len(records) != 2:
        raise RuntimeError(f"expected campaign P0/P1 records, got {len(records)}")
    p0, p1 = records
    p0_result = p0.get("result") or {}
    p1_result = p1.get("result") or {}
    if p0_result.get("skill_version") != "p0":
        raise RuntimeError("campaign P0 does not use skill version p0")
    if p0_result.get("status") != "failed":
        raise RuntimeError("pose_offset campaign P0 must fail deterministically")
    if p1_result.get("status") != "succeeded":
        raise RuntimeError("pose_offset campaign P1 must succeed deterministically")
    for label, record in (("p0", p0), ("p1", p1)):
        result = record.get("result") or {}
        if result.get("data_source") != MODE:
            raise RuntimeError(f"{label} data_source is not simulation")
        if result.get("hardware_status") != "simulation_runtime_verified":
            raise RuntimeError(f"{label} runtime is not verified virtual workcell")
        simulation = result.get("simulation") or {}
        if simulation.get("physical_robot_connected") is not False:
            raise RuntimeError(f"{label} physical_robot_connected boundary is missing")
        if simulation.get("synthetic") is not True:
            raise RuntimeError(f"{label} synthetic boundary is missing")
    validation = campaign.get("validation")
    if not isinstance(validation, Mapping):
        raise RuntimeError("campaign did not return repeated validation")
    if validation.get("repeats_per_version") != REPEATS:
        raise RuntimeError("validation is not 10x10")
    summary = validation.get("summary") or {}
    p0_summary = summary.get("p0") or {}
    p1_summary = summary.get("p1") or {}
    if p0_summary.get("sample_count") != REPEATS or p1_summary.get("sample_count") != REPEATS:
        raise RuntimeError("validation sample counts are not 10 per version")
    comparison = validation.get("comparison") or {}
    if comparison.get("decision") != "promote_candidate_for_review":
        raise RuntimeError(
            "validation did not produce the expected candidate review decision: "
            + str(comparison.get("decision"))
        )
    validation_scene = validation.get("scene_id")
    if validation_scene != p0.get("plan", {}).get("scene_id"):
        raise RuntimeError("validation scene_id does not match campaign P0 scene_id")
    if validation_scene != p1.get("plan", {}).get("scene_id"):
        raise RuntimeError("validation scene_id does not match campaign P1 scene_id")
    return {
        "p0_experiment_id": p0_result.get("experiment_id"),
        "p1_experiment_id": p1_result.get("experiment_id"),
        "p0_validation_success_rate": p0_summary.get("task_success_rate"),
        "p1_validation_success_rate": p1_summary.get("task_success_rate"),
        "comparison_decision": comparison.get("decision"),
    }


def _append_log(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(text)


def generate_submission(output_dir: Path) -> Path:
    """Run the deterministic campaign and build a portable evidence package."""

    output = _ensure_new_output(output_dir)
    test_log = output / "test_run.log"
    test_log.write_text(
        "RoboScientist virtual submission generation\n"
        f"generated_at_utc={_utc_now()}\n"
        "scope=simulation/pose_offset, pure Python virtual workcell\n\n",
        encoding="utf-8",
    )

    checks: list[tuple[int, str]] = []
    code, log = _run_check(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-q"],
        label="python -m unittest discover -s tests -p 'test_*.py' -q",
    )
    checks.append((code, log))
    _append_log(test_log, log + "\n")

    code, log = _run_check(
        [sys.executable, "-m", "compileall", "-q", "roboscientist"],
        label="python -m compileall -q roboscientist",
    )
    checks.append((code, log))
    _append_log(test_log, log + "\n")

    node = shutil.which("node")
    if node:
        code, log = _run_check(
            [node, "--check", "roboscientist/web/static/app.js"],
            label="node --check roboscientist/web/static/app.js",
        )
        checks.append((code, log))
        _append_log(test_log, log + "\n")
    else:
        _append_log(test_log, "$ node --check roboscientist/web/static/app.js\n[skipped: node not found]\n\n")

    failed_checks = [code for code, _ in checks if code != 0]
    if failed_checks:
        _append_log(test_log, f"verification_status=failed\nfailed_commands={len(failed_checks)}\n")
        raise RuntimeError(f"verification failed; inspect {test_log}")

    with tempfile.TemporaryDirectory(prefix="roboscientist_virtual_submission_") as temporary:
        temporary_root = Path(temporary).resolve()
        # Imports happen after the checks so this script can still report a
        # useful test log if project dependencies are missing.
        from roboscientist.web.server import DemoApplication

        app = DemoApplication(temporary_root, virtual_simulation=True)
        campaign = app.run_validation(
            TASK_TEXT,
            SCENARIO,
            mode=MODE,
            repeats=REPEATS,
            use_qwen=False,
        )
        run_summary = _assert_virtual_campaign(campaign)

        # Build the reproducibility-facing parts of the package before the
        # manifest is written.  These are all generated from the same
        # application instance and campaign, so the API examples cannot drift
        # from the evidence copied below.
        source_root = _copy_source_snapshot(output)
        _copy_documentation(output)
        _copy_frontend(output)

        evidence_data = output / "evidence" / "data"
        _copy_and_sanitize_json_tree(temporary_root, evidence_data, temporary_root)
        virtual_artifact_manifest_count = _refresh_virtual_artifact_manifests(
            evidence_data
        )
        if virtual_artifact_manifest_count == 0:
            raise RuntimeError("no virtual artifact manifests were found to verify")
        if (
            _verify_virtual_artifact_manifests(evidence_data)
            != virtual_artifact_manifest_count
        ):
            raise RuntimeError("virtual artifact manifest verification count mismatch")

        sanitized_campaign = _sanitize(
            {key: value for key, value in campaign.items() if key != "validation"},
            temporary_root,
        )
        sanitized_validation = _sanitize(campaign["validation"], temporary_root)
        _json_dump(output / "campaign.json", sanitized_campaign)
        _json_dump(output / "validation.json", sanitized_validation)

        api_root = output / "api"
        _json_dump(
            api_root / "health.json",
            _sanitize(app.health(), temporary_root),
        )
        _json_dump(
            api_root / "config.json",
            _sanitize(app.config(), temporary_root),
        )
        _json_dump(
            api_root / "runtime_simulation.json",
            _sanitize(app.runtime(MODE), temporary_root),
        )
        _json_dump(
            api_root / "campaign_request.json",
            {
                "task_text": TASK_TEXT,
                "scenario": SCENARIO,
                "mode": MODE,
                "max_rounds": 2,
                "use_qwen": False,
                "auto_run_p1": True,
            },
        )
        _json_dump(api_root / "campaign_response.json", _sanitize(campaign, temporary_root))
        _json_dump(
            api_root / "validation_request.json",
            {
                "task_text": TASK_TEXT,
                "scenario": SCENARIO,
                "mode": MODE,
                "repeats": REPEATS,
                "use_qwen": False,
            },
        )
        _json_dump(api_root / "validation_response.json", sanitized_validation)
        (api_root / "README.md").write_text(
            """# API 示例

这些 JSON 是本包生成时由同一 `DemoApplication` 运行实例产生的脱敏响应示例。
它们对应本地服务的 `/api/health`、`/api/config`、`/api/runtime?mode=simulation`、
`/api/campaigns` 和 `/api/validation`。启动服务后可用 `curl` 重放请求文件中的
字段；响应中的实验 ID 是本次生成包的只读证据。

当前示例明确使用 `use_qwen=false`、`data_source=simulation`，不代表真实模型或
机械臂执行。
""",
            encoding="utf-8",
        )

        validation_summary = sanitized_validation.get("summary") or {}
        comparison = dict(sanitized_validation.get("comparison") or {})
        # Keep the reviewer-facing comparison self-contained.  The detailed
        # aggregates remain in ``validation.json``, but the top-level report
        # should not force a reader to reconstruct the two headline rates.
        p0_validation = validation_summary.get("p0") or {}
        p1_validation = validation_summary.get("p1") or {}
        comparison.update(
            {
                "format_version": "comparison-v1",
                "campaign_id": campaign.get("campaign_id"),
                "task_text": TASK_TEXT,
                "mode": MODE,
                "scenario": SCENARIO,
                "data_source": "simulation",
                "runtime_name": "roboscientist.virtual_workcell",
                "runtime_version": "1.0.0",
                "engine": "python_deterministic",
                "physical_robot_connected": False,
                "synthetic": True,
                "p0_experiment_id": run_summary["p0_experiment_id"],
                "p1_experiment_id": run_summary["p1_experiment_id"],
                "p0_validation_success_rate": p0_validation.get("task_success_rate"),
                "p1_validation_success_rate": p1_validation.get("task_success_rate"),
                "p0_validation_sample_count": p0_validation.get("sample_count"),
                "p1_validation_sample_count": p1_validation.get("sample_count"),
                "source_validation": "validation.json",
            }
        )
        _json_dump(output / "comparison.json", comparison)
        _write_submission_reports(
            output,
            campaign=campaign,
            validation=sanitized_validation,
            run_summary=run_summary,
        )

        campaign_id = str(campaign.get("campaign_id"))
        source_snapshot_sha256 = _tree_sha256(source_root)
        package_files = [
            "README.md",
            "limitations.md",
            "MANIFEST.json",
            "campaign.json",
            "validation.json",
            "comparison.json",
            "test_run.log",
            "SHA256SUMS.txt",
            "source/",
            "docs/",
            "api/",
            "frontend/",
            "report/",
            "evidence/data",
        ]
        manifest = {
            "package_version": PACKAGE_VERSION,
            "package_type": "software_virtual_submission",
            "project": "RoboScientist",
            "direction": "track1-direction1B",
            "generator": "scripts/generate_virtual_submission.py",
            "generated_at_utc": _utc_now(),
            "git": _git_metadata(),
            "source_snapshot": {
                "path": "source/",
                "sha256": source_snapshot_sha256,
                "allow_list": list(_SOURCE_COPY_SPECS) + list(_SOURCE_DOC_COPY_SPECS),
                "sanitized": True,
            },
            "source_snapshot_sha256": source_snapshot_sha256,
            "api_examples": {
                "path": "api/",
                "health": "api/health.json",
                "config": "api/config.json",
                "runtime": "api/runtime_simulation.json",
                "campaign_request": "api/campaign_request.json",
                "campaign_response": "api/campaign_response.json",
                "validation_request": "api/validation_request.json",
                "validation_response": "api/validation_response.json",
            },
            "frontend": {
                "path": "frontend/",
                "entry_url_file": "frontend/entry_url.txt",
                "startup_guide": "frontend/README.md",
                "static_assets": [f"frontend/{name}" for name in _FRONTEND_FILES],
            },
            "reports": {
                "path": "report/",
                "acceptance_report": "report/virtual_experiment_acceptance_report.md",
                "technical_solution_outline": "report/technical_solution_outline.md",
                "demo_script": "report/demo_script.md",
            },
            "evidence_policy": {
                "primary_data_source": "simulation",
                "runtime_status": "simulation_runtime_verified",
                "runtime_name": "roboscientist.virtual_workcell",
                "runtime_version": "1.0.0",
                "engine": "python_deterministic",
                "synthetic": True,
                "physical_robot_connected": False,
                "claims_scope": "reproducible_software_virtual_experiment_only",
                "gazebo_moveit2_used": False,
                "real_arm_used": False,
            },
            "evidence_integrity": {
                "package_checksum": "SHA256SUMS.txt",
                "virtual_artifact_manifest_count": virtual_artifact_manifest_count,
                "virtual_artifact_hashes_refreshed_after_sanitization": True,
            },
            "experiment": {
                "task_text": TASK_TEXT,
                "mode": MODE,
                "scenario": SCENARIO,
                "campaign_id": campaign_id,
                "use_qwen": False,
                "planning_mode": "deterministic_only",
                "auto_run_p1": True,
                "repeats_per_version": REPEATS,
            },
            "campaign": {
                "path": "campaign.json",
                "persisted_path": f"evidence/data/campaigns/{campaign_id}/campaign.json",
                "p0_experiment_id": run_summary["p0_experiment_id"],
                "p1_experiment_id": run_summary["p1_experiment_id"],
                "records": 2,
            },
            "validation": {
                "path": "validation.json",
                "comparison_path": "comparison.json",
                "persisted_path": f"evidence/data/campaigns/{campaign_id}/validation.json",
                "p0_sample_count": validation_summary.get("p0", {}).get("sample_count"),
                "p1_sample_count": validation_summary.get("p1", {}).get("sample_count"),
                "decision": (sanitized_validation.get("comparison") or {}).get("decision"),
            },
            "qwen": {
                "invocations_included": False,
                "api_key_included": False,
                "reason": "deterministic_only_for_offline_reproducibility",
            },
            "security": {
                "env_files_included": False,
                "api_keys_included": False,
                "credential_redaction": "enabled",
            },
            "package_files": package_files,
            "limitations_file": "limitations.md",
            "checksums_file": "SHA256SUMS.txt",
            "checksums_scope": "all package files except SHA256SUMS.txt",
        }
        _json_dump(output / "MANIFEST.json", manifest)

        readme = f"""# RoboScientist 纯 Python 虚拟实验提交包

本包由 `scripts/generate_virtual_submission.py` 生成，范围是赛道一·方向 1B 的**软件虚拟实验闭环**：任务输入 → 科学计划 → P0 基线 → 结构化失败归因 → 单参数候选 Skill → P1 验证。

## 重要边界

- `data_source=simulation`，运行时为 `roboscientist.virtual_workcell`，引擎为 `python_deterministic`。
- `synthetic=true`、`physical_robot_connected=false`；本次没有连接或操作机械臂。
- 本包没有使用 Gazebo/MoveIt2，也不能作为真实机械臂或物理动力学证据。
- Qwen 明确关闭（`use_qwen=false`），因此不包含 API Key，也不把确定性规划器冒充成模型调用。

## 复现

提交包包含经过脱敏的 `source/` 快照。无需依赖原始工作区，可在包根目录执行：

```sh
cd source
python -m pip install -e .
python scripts/generate_virtual_submission.py --output-dir /tmp/roboscientist-virtual-submission
```

脚本会运行全量 Python 测试、编译检查，并执行 `pose_offset` campaign 及每个版本 10 次的重复验证。输出目录必须为空或不存在。

## 前端与 API

```sh
cd source
python -m roboscientist.web.server --host 127.0.0.1 --port 8001 --data-root ../runtime-data
```

浏览器入口记录在 `frontend/entry_url.txt`。`api/` 目录中的 health、config、
campaign 和 validation JSON 是本包生成时的实际脱敏响应示例；它们使用
`use_qwen=false` 的离线确定性模式。

## 本次结果

| 项目 | 结果 |
|---|---|
| campaign | `{campaign_id}` |
| P0 | 失败（固定位置残差，触发反馈归因） |
| P1 | 成功（只改变允许的单一参数族） |
| P0/P1 重复验证 | 10 / 10 次 |
| P0 成功率 | `{run_summary['p0_validation_success_rate']}` |
| P1 成功率 | `{run_summary['p1_validation_success_rate']}` |
| 比较决策 | `{run_summary['comparison_decision']}` |

这些数值只描述当前确定性虚拟工作单元，不应外推为实机成功率。

## 文件

- `campaign.json`：P0→P1 活动和反馈决策。
- `validation.json`：10×10 同条件重复验证及聚合指标。
- `comparison.json`：便于评审读取的 P0/P1 比较结果。
- `evidence/data/`：原始 campaign、实验计划/结果/分析、Skill 和虚拟场景/轨迹/评价工件。
- `source/`：可独立复现的源码、测试、配置和生成脚本快照；摘要见 `MANIFEST.json` 的 `source_snapshot_sha256`。
- `api/`：实际健康检查、配置、运行时和闭环请求/响应示例。
- `frontend/`：静态前端资源、入口地址和本地启动说明。
- `report/`：由本次冻结 campaign 自动生成的验收报告、技术方案摘要和演示脚本。
- `docs/`：关键运行手册、API 说明、路线及证据边界。
- `MANIFEST.json`：运行时、证据等级和安全边界。
- `test_run.log`：生成前的测试、编译和前端语法检查日志。
- `SHA256SUMS.txt`：包内文件校验清单（不包含清单文件自身）。
- `limitations.md`：不可支持的结论和后续升级条件。

校验包内文件：

```sh
cd /tmp/roboscientist-virtual-submission
shasum -a 256 -c SHA256SUMS.txt
```
"""
        (output / "README.md").write_text(readme, encoding="utf-8")

        limitations = f"""# 当前证据边界

## 已证明

1. 固定红色方块场景可以由纯 Python 虚拟工作单元生成结构化场景、TCP/关节轨迹、碰撞/安全事件和评价结果。
2. 在 `pose_offset` 场景中，P0 的可重复位置残差被归因到 `grasp_offset` 参数族；P1 只调整该参数族。
3. P0 与 P1 在相同任务、场景、评价器和安全约束下各运行 {REPEATS} 次，结果保存在 `validation.json`。

## 未证明

- 没有真实机械臂连接、动作、传感器采集或实机成功率证据。
- 没有 Gazebo/MoveIt2 运行证据，不能声称完成物理动力学或 ROS 规划验证。
- 没有真实 Qwen 调用记录；本包的计划和反馈使用透明的 deterministic-only 路径。
- 虚拟模型中的成功率提升不能外推到其他场景、硬件或实际工作空间。

## 升级条件

若要升级为仿真或实机结论，必须替换/增加经过审计的运行时，并保留同一实验契约：输入任务、实际计划、运行日志、评价证据、轨迹、同条件比较、源码版本和校验清单。任何新增证据都必须标明真实 `data_source`，不得覆盖本包的 `synthetic` 边界。
"""
        (output / "limitations.md").write_text(limitations, encoding="utf-8")

        # Add the package-level generation summary only after all assertions
        # pass.  The log remains safe even when the script is run on a machine
        # with local model credentials configured.
        _append_log(
            test_log,
            "verification_status=passed\n"
            f"campaign_id={campaign_id}\n"
            f"p0_experiment_id={run_summary['p0_experiment_id']}\n"
            f"p1_experiment_id={run_summary['p1_experiment_id']}\n"
            f"validation_repeats_per_version={REPEATS}\n"
            f"validation_decision={run_summary['comparison_decision']}\n",
        )

        _write_checksums(output)
        _assert_no_secrets_or_env(output)

    return output


def _assert_no_secrets_or_env(root: Path) -> None:
    """Fail closed if a future change accidentally adds credentials."""

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name == ".env" or path.name.startswith(".env."):
            raise RuntimeError(f"refusing to include environment file: {path}")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in _SECRET_PATTERNS:
            if pattern.search(content):
                raise RuntimeError(f"credential-like content detected in {path}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a pure-Python virtual P0/P1 submission evidence package"
    )
    parser.add_argument(
        "--output-dir",
        "--output",
        "-o",
        type=Path,
        default=None,
        help="new or empty output directory (default: a timestamped directory under outputs/)",
    )
    args = parser.parse_args(argv)
    output = args.output_dir or _default_output_dir()
    try:
        package = generate_submission(output)
    except Exception as error:
        print(f"submission generation failed: {error}", file=sys.stderr)
        return 1
    print(f"submission_package={package}")
    print(f"manifest={package / 'MANIFEST.json'}")
    print(f"checksums={package / 'SHA256SUMS.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
