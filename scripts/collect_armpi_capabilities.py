#!/usr/bin/env python3
"""Build a read-only inventory of ArmPi source and ROS 2 capabilities.

The collector never imports or executes robot-side Python or shell files. It
only copies an allow-list of files, parses source text, and queries ROS 2 graph
metadata with list commands.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


ARTIFACTS = {
    "run_final_dynamic_pick_place.sh": "baseline_wrapper",
    "final_registered_pick_place.py": "registered_pick_place_source",
    "apply_final_tcp.py": "tcp_transform_source",
    "final_dynamic_target.json": "dynamic_target_sample",
    "run_roboscientist_experiment.sh": "parameterized_wrapper",
    "move_to_pose.py": "legacy_move_to_pose",
    "robot_control.py": "legacy_robot_control",
    "home_control.py": "legacy_home_control",
}

REQUIRED_BASELINE_ROLES = {
    "baseline_wrapper",
    "registered_pick_place_source",
    "tcp_transform_source",
    "dynamic_target_sample",
}

STATUS_PATTERN = re.compile(r"STATUS:\s*([A-Za-z0-9_.-]+)")
OPTION_PATTERN = re.compile(r"(?<![A-Za-z0-9_])--[A-Za-z][A-Za-z0-9-]*")
TYPED_NAME_PATTERN = re.compile(r"^(?P<name>/\S+)\s+\[(?P<types>.*)\]$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _node_text(node: ast.AST) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        try:
            return ast.unparse(node)
        except Exception:
            return node.__class__.__name__


def _function_parameters(node: ast.AST) -> List[str]:
    arguments = node.args  # type: ignore[attr-defined]
    names = [argument.arg for argument in arguments.posonlyargs + arguments.args]
    if arguments.vararg:
        names.append(f"*{arguments.vararg.arg}")
    names.extend(argument.arg for argument in arguments.kwonlyargs)
    if arguments.kwarg:
        names.append(f"**{arguments.kwarg.arg}")
    return names


def _python_metadata(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as error:
        return {
            "syntax_valid": False,
            "syntax_error": f"line {error.lineno}: {error.msg}",
            "functions": [],
            "cli_arguments": [],
            "imports": [],
        }

    functions = []
    cli_arguments = []
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "line": node.lineno,
                "parameters": _function_parameters(node),
                "async": isinstance(node, ast.AsyncFunctionDef),
            })
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imports.update(
                f"{module}.{alias.name}".strip(".") for alias in node.names
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            flags = [
                value for value in (_node_text(item) for item in node.args)
                if isinstance(value, str)
            ]
            options = {keyword.arg: _node_text(keyword.value) for keyword in node.keywords}
            cli_arguments.append({"flags": flags, "options": options, "line": node.lineno})

    return {
        "syntax_valid": True,
        "functions": sorted(functions, key=lambda item: (item["line"], item["name"])),
        "cli_arguments": sorted(cli_arguments, key=lambda item: item["line"]),
        "imports": sorted(imports),
    }


def _shell_metadata(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    first_line = text.splitlines()[0] if text.splitlines() else ""
    return {
        "shebang": first_line if first_line.startswith("#!") else "",
        "options": sorted(set(OPTION_PATTERN.findall(text))),
        "status_tokens": sorted(set(STATUS_PATTERN.findall(text))),
        "mentions_check_mode": bool(re.search(r"(^|[^A-Za-z])check([^A-Za-z]|$)", text)),
        "mentions_pick_place_mode": "pick-place" in text,
    }


def _json_shape(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return {
            "type": "object",
            "fields": {str(key): _json_shape(item) for key, item in value.items()},
        }
    if isinstance(value, list):
        item_shapes = [_json_shape(item) for item in value[:3]]
        return {"type": "array", "length": len(value), "sample_item_shapes": item_shapes}
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    return {"type": type(value).__name__}


def _json_metadata(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {"valid": False, "error": str(error)}
    return {"valid": True, "shape": _json_shape(value)}


def _source_metadata(path: Path) -> Dict[str, Any]:
    if path.suffix == ".py":
        return {"kind": "python", **_python_metadata(path)}
    if path.suffix == ".sh":
        return {"kind": "shell", **_shell_metadata(path)}
    if path.suffix == ".json":
        return {"kind": "json", **_json_metadata(path)}
    return {"kind": "unknown"}


def _discover(root: Path, root_label: str) -> Iterable[Dict[str, Any]]:
    if not root.is_dir():
        return []
    root_resolved = root.resolve()
    found = []
    for path in sorted(root.rglob("*")):
        if path.name not in ARTIFACTS or not path.is_file():
            continue
        if path.is_symlink():
            found.append({
                "role": ARTIFACTS[path.name],
                "source_path": str(path),
                "root_label": root_label,
                "copyable": False,
                "problem": "symbolic links are not copied",
            })
            continue
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root_resolved)
        except ValueError:
            found.append({
                "role": ARTIFACTS[path.name],
                "source_path": str(path),
                "root_label": root_label,
                "copyable": False,
                "problem": "resolved path is outside the requested root",
            })
            continue
        found.append({
            "role": ARTIFACTS[path.name],
            "source_path": str(resolved),
            "root_label": root_label,
            "relative_path": str(relative),
            "copyable": True,
        })
    return found


def _command_result(command: Sequence[str], timeout_s: float = 10.0) -> Dict[str, Any]:
    try:
        completed = subprocess.run(
            list(command), capture_output=True, text=True, errors="replace",
            timeout=timeout_s, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"command": list(command), "available": False, "error": str(error)}
    return {
        "command": list(command),
        "available": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout_lines": completed.stdout.splitlines(),
        "stderr_lines": completed.stderr.splitlines(),
    }


def _parse_typed_names(lines: Sequence[str]) -> List[Dict[str, Any]]:
    values = []
    for line in lines:
        match = TYPED_NAME_PATTERN.match(line.strip())
        if match:
            values.append({
                "name": match.group("name"),
                "types": [item.strip() for item in match.group("types").split(",") if item.strip()],
            })
        elif line.strip():
            values.append({"name": line.strip(), "types": []})
    return values


def _collect_ros2() -> Dict[str, Any]:
    executable = shutil.which("ros2")
    if not executable:
        return {"available": False, "reason": "ros2 executable was not found"}
    commands = {
        "topics": [executable, "topic", "list", "-t"],
        "services": [executable, "service", "list", "-t"],
        "actions": [executable, "action", "list", "-t"],
        "nodes": [executable, "node", "list"],
    }
    results = {name: _command_result(command) for name, command in commands.items()}
    for name in ("topics", "services", "actions"):
        results[name]["interfaces"] = _parse_typed_names(results[name].get("stdout_lines", []))
    results["nodes"]["names"] = [
        line.strip() for line in results["nodes"].get("stdout_lines", []) if line.strip()
    ]
    return {"available": all(result.get("available") for result in results.values()), **results}


def _repo_commit() -> Optional[str]:
    repository = Path(__file__).resolve().parents[1]
    result = _command_result(["git", "-C", str(repository), "rev-parse", "HEAD"], 5.0)
    lines = result.get("stdout_lines", [])
    return lines[0].strip() if result.get("available") and lines else None


def collect_capabilities(
    task_dir: Path,
    legacy_script_dir: Path,
    output_dir: Path,
    include_ros2: bool = True,
) -> Dict[str, Any]:
    """Collect source and graph metadata without importing target code."""

    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output_dir}")
    discovered = list(_discover(task_dir, "armpi_tasks"))
    discovered.extend(_discover(legacy_script_dir, "my_armpi"))
    unique = []
    seen = set()
    for item in discovered:
        key = item["source_path"]
        if key not in seen:
            unique.append(item)
            seen.add(key)

    files_dir = output_dir / "files"
    files_dir.mkdir(parents=True)
    manifest_lines = []
    artifacts = []
    for item in unique:
        artifact = dict(item)
        if artifact.get("copyable"):
            source = Path(artifact["source_path"])
            destination = files_dir / artifact["root_label"] / artifact["relative_path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            digest = _sha256(destination)
            artifact.update({
                "copied_path": str(destination.relative_to(output_dir)),
                "size_bytes": destination.stat().st_size,
                "sha256": digest,
                "metadata": _source_metadata(destination),
            })
            manifest_lines.append(f"{digest}  {artifact['copied_path']}")
        artifacts.append(artifact)

    present_roles = {item["role"] for item in artifacts if item.get("copyable")}
    missing_baseline_roles = sorted(REQUIRED_BASELINE_ROLES - present_roles)
    report = {
        "schema_version": "armpi_capability_report_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "collector_commit": _repo_commit(),
        "safety": {
            "target_code_imported": False,
            "target_code_executed": False,
            "motion_commands_sent": False,
            "ros2_queries_are_graph_metadata_only": include_ros2,
        },
        "host": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "ros_distro": os.environ.get("ROS_DISTRO"),
            "ros_domain_id": os.environ.get("ROS_DOMAIN_ID"),
        },
        "roots": {
            "armpi_tasks": str(task_dir),
            "my_armpi": str(legacy_script_dir),
        },
        "artifacts": artifacts,
        "missing_baseline_roles": missing_baseline_roles,
        "baseline_source_inventory_complete": not missing_baseline_roles,
        "parameterized_wrapper_present": "parameterized_wrapper" in present_roles,
        "ros2": _collect_ros2() if include_ros2 else {
            "available": False, "reason": "collection skipped by operator",
        },
        "limitations": [
            "This inventory does not prove that any motion path succeeds on hardware.",
            "Source presence does not prove that candidate parameters are applied.",
            "ROS 2 graph presence does not prove message semantics or execution success.",
        ],
    }
    (output_dir / "manifest.sha256").write_text(
        "\n".join(sorted(manifest_lines)) + ("\n" if manifest_lines else ""),
        encoding="utf-8",
    )
    (output_dir / "capability_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return report


def _create_archive(output_dir: Path) -> Path:
    archive = output_dir.with_suffix(".tar.gz")
    if archive.exists():
        raise FileExistsError(f"refusing to overwrite existing archive: {archive}")
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(output_dir, arcname=output_dir.name, recursive=True)
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect ArmPi source contracts and ROS 2 graph metadata without motion"
    )
    parser.add_argument(
        "--task-dir",
        default=os.environ.get("ARMPI_TASK_DIR", "/home/ubuntu/armpi_tasks"),
        help="directory containing the verified red-cuboid scripts",
    )
    parser.add_argument(
        "--legacy-script-dir",
        default=os.environ.get("ARMPI_SCRIPT_DIR", "/home/ubuntu/my_armpi"),
        help="optional directory containing earlier high-level function modules",
    )
    parser.add_argument("--output-dir", help="new bundle directory; must not already exist")
    parser.add_argument("--skip-ros2", action="store_true", help="do not query ROS 2 graph metadata")
    parser.add_argument("--no-archive", action="store_true", help="leave only the bundle directory")
    parser.add_argument(
        "--strict", action="store_true",
        help="return exit code 2 when the four documented baseline artifacts are missing",
    )
    args = parser.parse_args()

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir) if args.output_dir else Path.cwd() / (
        f"armpi_capability_bundle_{timestamp}_{os.getpid()}"
    )
    try:
        report = collect_capabilities(
            Path(args.task_dir), Path(args.legacy_script_dir), output_dir,
            include_ros2=not args.skip_ros2,
        )
        archive = None if args.no_archive else _create_archive(output_dir)
    except (OSError, ValueError, tarfile.TarError) as error:
        print(f"RESULT: capability collection failed: {error}", file=sys.stderr)
        return 1

    print(json.dumps({
        "bundle_dir": str(output_dir),
        "archive": str(archive) if archive else None,
        "baseline_source_inventory_complete": report["baseline_source_inventory_complete"],
        "missing_baseline_roles": report["missing_baseline_roles"],
        "parameterized_wrapper_present": report["parameterized_wrapper_present"],
        "ros2_available": report["ros2"].get("available"),
    }, ensure_ascii=False, indent=2))
    if args.strict and report["missing_baseline_roles"]:
        print("RESULT: source inventory incomplete; return the bundle for software-side review")
        return 2
    print("RESULT: read-only capability bundle created; no motion was requested")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
