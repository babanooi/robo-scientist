"""Read-only replay of historical ArmPi experiment files.

The replay service is deliberately separate from :mod:`adapters`.  A replay
can describe what was recorded in the past, but it cannot preflight, move, or
stop a robot.  This distinction is important when a historical run is shown
in the web UI: a trajectory is evidence, not a live device connection.

The service accepts either an extracted run directory, a directory containing
run directories/archives, or a ``.tar``, ``.tar.gz``/``.tgz`` archive.  Archive
members are read in memory and are never extracted.  All public responses use
the same small envelope and explicitly identify the historical source and the
read-only boundary.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import os
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from roboscientist.hardware_bridge.evidence import joint_trajectory_metrics, strip_ansi


DATA_SOURCE = "historical_real_arm"
STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"
STATUS_INVALID_DATA = "invalid_data"

_ROOT_ENV = "ROBO_REPLAY_ROOT"
_MAX_POINTS_DEFAULT = 200
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar")
_JOINT_FILES = ("joint_states_session.csv", "joint_states.csv")
_LOG_FILES = (
    "grasp_events_session.log",
    "grasp_events.log",
    "shape_recognition.log",
)
_EVENT_FILE = "events.csv"
_CALIBRATION_FILE = "config/calibration.yaml"

_TARGET_RE = re.compile(
    r"FACTORY_FINAL_PICK_TARGET\s+"
    r"x=(?P<x>[+-]?\d+(?:\.\d+)?),\s*"
    r"y=(?P<y>[+-]?\d+(?:\.\d+)?),\s*"
    r"z=(?P<z>[+-]?\d+(?:\.\d+)?),\s*"
    r"shape=(?P<shape>[A-Za-z0-9_-]+),\s*"
    r"yaw=(?P<yaw>[+-]?\d+(?:\.\d+)?)"
)
_TIMESTAMP_RE = re.compile(r"\[(?P<seconds>\d+(?:\.\d+)?)\]")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ReplayDataError(ValueError):
    """A source exists but cannot be interpreted as a replay run."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


@dataclass(frozen=True)
class _RunSource:
    run_id: str
    kind: str
    path: Path
    prefix: str = ""


def _envelope(status: str, **fields: Any) -> Dict[str, Any]:
    """Build a response with an explicit no-motion contract."""

    result: Dict[str, Any] = {
        "status": status,
        "data_source": DATA_SOURCE,
        "read_only": True,
        "motion_requested": False,
    }
    result.update(fields)
    return result


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _safe_relative(path: Path, root: Path) -> bool:
    """Return whether *path* resolves inside *root* (Python 3.9 compatible)."""

    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_archive_name(name: str) -> bool:
    """Reject absolute, traversal, and link-like archive member names."""

    if not name or name.startswith("/") or "\\" in name:
        return False
    parts = PurePosixPath(name).parts
    return all(part not in ("", ".", "..") for part in parts)


def _archive_run_prefix(names: Sequence[str], fallback: str) -> str:
    files = [name.rstrip("/") for name in names if name and not name.endswith("/")]
    if not files:
        return ""
    first_components = {PurePosixPath(name).parts[0] for name in files}
    if len(first_components) == 1:
        candidate = next(iter(first_components))
        # A single top-level file is still a valid archive, but it should not
        # make the file itself disappear from the lookup namespace.
        if any(len(PurePosixPath(name).parts) > 1 for name in files):
            return candidate
    return fallback


class ReplayService:
    """Enumerate and inspect historical runs without any hardware side effect.

    Parameters
    ----------
    root:
        A run directory, a directory containing runs, or a tar archive.  When
        omitted, ``ROBO_REPLAY_ROOT`` is read at construction time.
    max_points:
        Maximum number of grouped trajectory points returned by ``get_run``.
        The source files and the calculated metrics are never truncated.
    """

    def __init__(
        self,
        root: Optional[Union[str, Path]] = None,
        max_points: int = _MAX_POINTS_DEFAULT,
    ) -> None:
        configured = root if root is not None else os.environ.get(_ROOT_ENV)
        self.root: Optional[Path] = Path(configured).expanduser() if configured else None
        if isinstance(max_points, bool) or not isinstance(max_points, int) or max_points <= 0:
            raise ValueError("max_points must be a positive integer")
        self.max_points = max_points

    @property
    def configured(self) -> bool:
        return self.root is not None

    def list_runs(self) -> Dict[str, Any]:
        """Return available run metadata, or a truthful unavailable response."""

        if self.root is None:
            return _envelope(
                STATUS_UNAVAILABLE,
                reason_code="REPLAY_SOURCE_NOT_FOUND",
                message="no historical grasp run is configured or available",
                runs=[],
            )
        try:
            sources = self._discover_sources()
        except ReplayDataError as error:
            status = (
                STATUS_UNAVAILABLE
                if error.reason_code == "REPLAY_SOURCE_NOT_FOUND"
                else STATUS_INVALID_DATA
            )
            return _envelope(
                status,
                reason_code=error.reason_code,
                message=error.message,
                runs=[],
            )
        if not sources:
            return _envelope(
                STATUS_UNAVAILABLE,
                reason_code="REPLAY_RUNS_NOT_FOUND",
                message="the configured replay source contains no run directory or archive",
                runs=[],
            )

        runs = []
        for source in sources:
            try:
                files = self._source_files(source)
            except ReplayDataError as error:
                runs.append(
                    {
                        "run_id": source.run_id,
                        "source_kind": source.kind,
                        "status": STATUS_INVALID_DATA,
                        "reason_code": error.reason_code,
                        "message": error.message,
                        "files": [],
                    }
                )
                continue
            joint_file = next((name for name in _JOINT_FILES if name in files), None)
            runs.append(
                {
                    "run_id": source.run_id,
                    "source_kind": source.kind,
                    "status": STATUS_AVAILABLE if joint_file else STATUS_INVALID_DATA,
                    "joint_state_file": joint_file,
                    "has_events": _EVENT_FILE in files,
                    "has_calibration": _CALIBRATION_FILE in files,
                    "files": files,
                }
            )
        return _envelope(STATUS_AVAILABLE, runs=runs, run_count=len(runs))

    # A descriptive alias for callers that prefer noun-style naming.
    available_runs = list_runs

    def get_run(self, run_id: str) -> Dict[str, Any]:
        """Load one run and return a bounded, evidence-only representation."""

        if not self._valid_run_id(run_id):
            return _envelope(
                STATUS_INVALID_DATA,
                run_id=str(run_id),
                reason_code="REPLAY_INVALID_RUN_ID",
                message="run_id must be a single safe path component",
            )
        if self.root is None:
            return _envelope(
                STATUS_UNAVAILABLE,
                run_id=run_id,
                reason_code="REPLAY_SOURCE_NOT_FOUND",
                message="no historical grasp run is configured or available",
            )

        try:
            source = self._find_source(run_id)
        except ReplayDataError as error:
            status = (
                STATUS_UNAVAILABLE
                if error.reason_code == "REPLAY_SOURCE_NOT_FOUND"
                else STATUS_INVALID_DATA
            )
            return _envelope(
                status,
                run_id=run_id,
                reason_code=error.reason_code,
                message=error.message,
            )
        if source is None:
            return _envelope(
                STATUS_UNAVAILABLE,
                run_id=run_id,
                reason_code="REPLAY_RUN_NOT_FOUND",
                message="the requested historical run does not exist",
            )

        try:
            return self._load_run(source)
        except ReplayDataError as error:
            return _envelope(
                STATUS_INVALID_DATA,
                run_id=run_id,
                reason_code=error.reason_code,
                message=error.message,
            )

    # Keep both names available for API integration and scripts.
    replay_run = get_run
    load_run = get_run

    def _valid_run_id(self, run_id: Any) -> bool:
        return isinstance(run_id, str) and bool(_RUN_ID_RE.fullmatch(run_id)) and run_id not in {".", ".."}

    def _discover_sources(self) -> List[_RunSource]:
        assert self.root is not None
        root = self.root
        if not root.exists():
            raise ReplayDataError(
                "REPLAY_SOURCE_NOT_FOUND",
                f"configured replay source does not exist: {root}",
            )
        try:
            root_resolved = root.resolve()
        except OSError as error:
            raise ReplayDataError("REPLAY_SOURCE_UNREADABLE", str(error)) from error

        if root.is_file():
            if not self._is_archive(root):
                raise ReplayDataError(
                    "REPLAY_SOURCE_UNSUPPORTED",
                    "replay root must be a run directory, directory of runs, or tar archive",
                )
            return [self._archive_source(root, root.stem)]

        # A directory that directly contains a joint-state file is itself one run.
        direct_files = {p.name for p in root.iterdir() if p.is_file()}
        if any(name in direct_files for name in _JOINT_FILES):
            return [_RunSource(root.name, "directory", root_resolved)]

        sources: List[_RunSource] = []
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if child.is_dir() and not child.is_symlink():
                # Ignore unrelated directories unless they look like a run;
                # retaining directories with no CSV lets list_runs explain the
                # missing evidence rather than silently hiding them.
                sources.append(_RunSource(child.name, "directory", child.resolve()))
            elif child.is_file() and self._is_archive(child):
                sources.append(self._archive_source(child, child.name))
        return sources

    def _archive_source(self, path: Path, fallback: str) -> _RunSource:
        names = self._validated_archive_names(path)
        prefix = _archive_run_prefix(names, fallback)
        run_id = prefix or fallback
        # Archive file names often end in .tar.gz; expose a stable, safe id.
        for suffix in _ARCHIVE_SUFFIXES:
            if run_id.endswith(suffix):
                run_id = run_id[: -len(suffix)]
                break
        if not self._valid_run_id(run_id):
            run_id = "archive-" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
        return _RunSource(run_id, "archive", path.resolve(), prefix)

    def _find_source(self, run_id: str) -> Optional[_RunSource]:
        sources = self._discover_sources()
        matches = [source for source in sources if source.run_id == run_id]
        if len(matches) > 1:
            raise ReplayDataError(
                "REPLAY_DUPLICATE_RUN_ID",
                f"multiple replay sources use run_id {run_id!r}",
            )
        return matches[0] if matches else None

    def _is_archive(self, path: Path) -> bool:
        lower = path.name.lower()
        return any(lower.endswith(suffix) for suffix in _ARCHIVE_SUFFIXES)

    def _validated_archive_names(self, path: Path) -> List[str]:
        try:
            with tarfile.open(path, mode="r:*") as archive:
                members = archive.getmembers()
        except (OSError, tarfile.TarError) as error:
            raise ReplayDataError("REPLAY_ARCHIVE_UNREADABLE", f"cannot read archive: {error}") from error
        names: List[str] = []
        seen = set()
        for member in members:
            if not _safe_archive_name(member.name):
                raise ReplayDataError(
                    "REPLAY_ARCHIVE_UNSAFE_PATH",
                    f"archive contains an unsafe member path: {member.name!r}",
                )
            if member.issym() or member.islnk():
                raise ReplayDataError(
                    "REPLAY_ARCHIVE_LINK_UNSAFE",
                    f"archive contains a link member: {member.name!r}",
                )
            if member.isfile():
                normalized = member.name.rstrip("/")
                if normalized in seen:
                    raise ReplayDataError(
                        "REPLAY_ARCHIVE_DUPLICATE_PATH",
                        f"archive contains duplicate member path: {normalized!r}",
                    )
                seen.add(normalized)
                names.append(normalized)
        return names

    def _source_files(self, source: _RunSource) -> List[str]:
        if source.kind == "directory":
            if not source.path.is_dir() or source.path.is_symlink():
                raise ReplayDataError("REPLAY_SOURCE_UNREADABLE", "run directory is not readable")
            files: List[str] = []
            root = source.path.resolve()
            for path in sorted(source.path.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                resolved = path.resolve()
                if not _safe_relative(resolved, root):
                    raise ReplayDataError(
                        "REPLAY_PATH_ESCAPE",
                        f"run file resolves outside the run directory: {path.name}",
                    )
                files.append(path.relative_to(source.path).as_posix())
            return files
        if source.kind == "archive":
            names = self._validated_archive_names(source.path)
            prefix = source.prefix.rstrip("/")
            result = []
            for name in names:
                if prefix:
                    marker = prefix + "/"
                    if name.startswith(marker):
                        result.append(name[len(marker) :])
                else:
                    result.append(name)
            return sorted(name for name in result if name)
        raise ReplayDataError("REPLAY_SOURCE_UNSUPPORTED", f"unknown source kind: {source.kind}")

    def _read_source_file(self, source: _RunSource, relative_name: str) -> bytes:
        if not relative_name or not _safe_archive_name(relative_name):
            raise ReplayDataError("REPLAY_PATH_ESCAPE", "unsafe replay file path")
        if source.kind == "directory":
            root = source.path.resolve()
            candidate = (source.path / Path(*PurePosixPath(relative_name).parts)).resolve()
            if not _safe_relative(candidate, root) or candidate.is_symlink() or not candidate.is_file():
                raise ReplayDataError(
                    "REPLAY_FILE_NOT_FOUND",
                    f"historical file is missing: {relative_name}",
                )
            try:
                return candidate.read_bytes()
            except OSError as error:
                raise ReplayDataError("REPLAY_FILE_UNREADABLE", str(error)) from error

        if source.kind == "archive":
            target = f"{source.prefix.rstrip('/')}/{relative_name}" if source.prefix else relative_name
            try:
                with tarfile.open(source.path, mode="r:*") as archive:
                    member = archive.getmember(target)
                    if not member.isfile() or member.issym() or member.islnk():
                        raise KeyError(target)
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise KeyError(target)
                    return stream.read()
            except KeyError:
                raise ReplayDataError("REPLAY_FILE_NOT_FOUND", f"historical file is missing: {relative_name}")
            except (OSError, tarfile.TarError) as error:
                raise ReplayDataError("REPLAY_FILE_UNREADABLE", str(error)) from error
        raise ReplayDataError("REPLAY_SOURCE_UNSUPPORTED", f"unknown source kind: {source.kind}")

    def _load_run(self, source: _RunSource) -> Dict[str, Any]:
        files = self._source_files(source)
        joint_file = next((name for name in _JOINT_FILES if name in files), None)
        if joint_file is None:
            raise ReplayDataError(
                "REPLAY_JOINT_FILE_NOT_FOUND",
                "historical run has no joint_states_session.csv or joint_states.csv",
            )
        joint_bytes = self._read_source_file(source, joint_file)
        rows, csv_info = self._parse_joint_csv(joint_bytes)
        metrics = self._metrics_for_source(source, joint_file, joint_bytes)
        trajectory = self._trajectory(rows, joint_file)

        metadata = self._session_metadata(source, files)
        events, event_info = self._events(source, files)
        targets, log_info = self._vision_targets(source, files)
        calibration = self._calibration(source, files)
        evidence_files = self._evidence_files(source, files, joint_file)
        media_refs = self._media_refs(files)

        limitations = [
            "历史轨迹仅用于只读回放，不代表当前机械臂已连接或正在运动。",
            "关节轨迹指标位于 joint space；没有连续 TCP/TF 记录时，不将关节总行程解释为末端路径长度。",
        ]
        if not events:
            limitations.append("没有可用的结构化事件结果，抓取/提起/放置成功率无法从此运行推断。")
        else:
            limitations.append("已保留原始事件 result 文本，但未绑定经过验证的评价器，不据此计算成功率。")
        if not targets:
            limitations.append("没有从视觉日志解析到最终目标记录。")
        if not calibration:
            limitations.append("没有随运行归档的标定文件。")

        return _envelope(
            STATUS_AVAILABLE,
            run_id=source.run_id,
            source_kind=source.kind,
            source_name=source.path.name,
            summary={
                "joint_state_file": joint_file,
                "joint_row_count": csv_info["valid_rows"],
                "invalid_joint_rows": csv_info["invalid_rows"],
                "joint_names": trajectory["joint_names"],
                "session": metadata,
                "event_count": len(events),
                "vision_target_count": len(targets),
            },
            metrics=metrics,
            trajectory=trajectory,
            events=events,
            vision_targets=targets,
            calibration=calibration,
            outcome={
                "status": STATUS_UNAVAILABLE,
                "reason_code": "REPLAY_OUTCOME_NOT_VERIFIED",
                "message": "replay exposes raw evidence only; no verified outcome evaluator is attached",
            },
            media_refs=media_refs,
            evidence={
                "files": evidence_files,
                "event_file_status": event_info,
                "vision_log_status": log_info,
                # Raw event rows are evidence for later inspection, not a
                # validated task outcome.  A replay has no evaluator context
                # and therefore must never expose a success-rate claim.
                "success_labels_available": False,
                "structured_event_rows": len(events),
                "media_refs_available": bool(media_refs),
            },
            limitations=limitations,
        )

    def _parse_joint_csv(self, content: bytes) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        try:
            text = content.decode("utf-8-sig", errors="replace")
        except (AttributeError, UnicodeError) as error:
            raise ReplayDataError("REPLAY_JOINT_CSV_INVALID", f"cannot decode joint CSV: {error}") from error
        reader = csv.DictReader(io.StringIO(text))
        required = {"joint_name", "position_rad", "wall_time_ns"}
        fields = set(reader.fieldnames or [])
        if not required.issubset(fields):
            missing = ", ".join(sorted(required - fields))
            raise ReplayDataError(
                "REPLAY_JOINT_SCHEMA_MISSING",
                f"joint-state CSV is missing required columns: {missing}",
            )

        rows: List[Dict[str, Any]] = []
        invalid_rows = 0
        for raw in reader:
            name = str(raw.get("joint_name", "")).strip()
            position = _finite(raw.get("position_rad"))
            try:
                wall_ns = int(str(raw.get("wall_time_ns", "")).strip())
            except (TypeError, ValueError):
                wall_ns = None
            if not name or position is None or wall_ns is None:
                invalid_rows += 1
                continue
            ros_sec = self._optional_int(raw.get("ros_sec"))
            ros_nsec = self._optional_int(raw.get("ros_nsec"))
            rows.append(
                {
                    "wall_time_ns": wall_ns,
                    "ros_sec": ros_sec,
                    "ros_nsec": ros_nsec,
                    "sample_id": str(raw.get("sample_id", "")).strip() or None,
                    "shape": str(raw.get("shape", "")).strip() or None,
                    "phase": str(raw.get("phase", "")).strip() or None,
                    "joint_name": name,
                    "position_rad": position,
                }
            )
        if not rows:
            raise ReplayDataError(
                "REPLAY_JOINT_DATA_EMPTY",
                "joint-state CSV contains no valid position samples",
            )
        return rows, {"valid_rows": len(rows), "invalid_rows": invalid_rows}

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        try:
            if value is None or str(value).strip() == "":
                return None
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _metrics_for_source(self, source: _RunSource, file_name: str, content: bytes) -> Dict[str, float]:
        # Reuse the canonical evidence calculator.  Archives are staged only
        # in a temporary file; the configured source is never modified.
        try:
            if source.kind == "directory":
                return joint_trajectory_metrics(source.path / file_name)
            with NamedTemporaryFile(prefix="roboscientist-replay-", suffix=".csv") as temp:
                temp.write(content)
                temp.flush()
                return joint_trajectory_metrics(Path(temp.name))
        except (OSError, ValueError) as error:
            raise ReplayDataError("REPLAY_JOINT_DATA_INVALID", str(error)) from error

    def _trajectory(self, rows: Sequence[Mapping[str, Any]], source_file: str) -> Dict[str, Any]:
        groups: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for row in rows:
            ros_sec = row.get("ros_sec")
            ros_nsec = row.get("ros_nsec")
            key: Tuple[Any, ...]
            if ros_sec is not None and ros_nsec is not None:
                key = ("ros", ros_sec, ros_nsec)
            else:
                key = ("wall", row["wall_time_ns"])
            group = groups.setdefault(
                key,
                {
                    "wall_time_ns": row["wall_time_ns"],
                    "sample_id": row.get("sample_id"),
                    "shape": row.get("shape"),
                    "phase": row.get("phase"),
                    "joints": {},
                },
            )
            group["wall_time_ns"] = min(group["wall_time_ns"], row["wall_time_ns"])
            group["joints"][row["joint_name"]] = row["position_rad"]

        ordered = sorted(groups.values(), key=lambda item: item["wall_time_ns"])
        if not ordered:
            raise ReplayDataError("REPLAY_JOINT_DATA_EMPTY", "no trajectory points could be grouped")
        selected = self._sample(ordered, self.max_points)
        first_ns = ordered[0]["wall_time_ns"]
        points = []
        for group in selected:
            point: Dict[str, Any] = {
                "wall_time_ns": group["wall_time_ns"],
                "time_from_start_s": (group["wall_time_ns"] - first_ns) / 1_000_000_000,
                "joints": dict(sorted(group["joints"].items())),
            }
            for key in ("sample_id", "shape", "phase"):
                if group.get(key) is not None:
                    point[key] = group[key]
            points.append(point)
        return {
            "source_file": source_file,
            "joint_names": sorted({str(row["joint_name"]) for row in rows}),
            "original_points": len(ordered),
            "returned_points": len(points),
            "sampled": len(ordered) > len(points),
            "points": points,
        }

    @staticmethod
    def _sample(items: Sequence[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
        if len(items) <= limit:
            return list(items)
        if limit == 1:
            return [items[0]]
        indexes = {
            round(index * (len(items) - 1) / (limit - 1))
            for index in range(limit)
        }
        return [items[index] for index in sorted(indexes)]

    def _session_metadata(self, source: _RunSource, files: Sequence[str]) -> Dict[str, Any]:
        result: Dict[str, Any] = {"start_time_ns": None, "end_time_ns": None, "duration_s": None}
        for field, filename in (("start_time_ns", "session_start_ns"), ("end_time_ns", "session_end_ns")):
            if filename not in files:
                continue
            raw = self._read_source_file(source, filename).decode("utf-8", errors="replace").strip()
            value = self._optional_int(raw)
            if value is not None:
                result[field] = value
        if result["start_time_ns"] is not None and result["end_time_ns"] is not None:
            result["duration_s"] = (result["end_time_ns"] - result["start_time_ns"]) / 1_000_000_000
        return result

    def _events(self, source: _RunSource, files: Sequence[str]) -> Tuple[List[Dict[str, Any]], str]:
        if _EVENT_FILE not in files:
            return [], "missing"
        content = self._read_source_file(source, _EVENT_FILE)
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig", errors="replace")))
        required = {"sample_id", "shape", "start_time_ns", "end_time_ns", "result"}
        fields = set(reader.fieldnames or [])
        if not fields:
            return [], "empty"
        if not required.issubset(fields):
            return [], "invalid_schema"
        events: List[Dict[str, Any]] = []
        for raw in reader:
            if not any(str(value or "").strip() for value in raw.values()):
                continue
            event: Dict[str, Any] = {
                "sample_id": str(raw.get("sample_id", "")).strip() or None,
                "shape": str(raw.get("shape", "")).strip() or None,
                "result": str(raw.get("result", "")).strip() or None,
            }
            for key in ("start_time_ns", "end_time_ns"):
                value = self._optional_int(raw.get(key))
                event[key] = value
            events.append(event)
        return events, "available" if events else "empty"

    def _vision_targets(self, source: _RunSource, files: Sequence[str]) -> Tuple[List[Dict[str, Any]], str]:
        # Prefer the session-scoped log; the broader log commonly repeats the
        # same target records and would otherwise double-count them.
        selected_logs = [name for name in _LOG_FILES if name in files]
        if "grasp_events_session.log" in selected_logs:
            selected_logs = ["grasp_events_session.log"]
        targets: List[Dict[str, Any]] = []
        for filename in selected_logs:
            text = strip_ansi(self._read_source_file(source, filename).decode("utf-8", errors="replace"))
            for line in text.splitlines():
                match = _TARGET_RE.search(line)
                if not match:
                    continue
                values = match.groupdict()
                timestamp = _TIMESTAMP_RE.search(line)
                target: Dict[str, Any] = {
                    "x": float(values["x"]),
                    "y": float(values["y"]),
                    "z": float(values["z"]),
                    "shape": values["shape"],
                    "yaw": float(values["yaw"]),
                    "source_file": filename,
                }
                if timestamp:
                    target["timestamp_ns"] = int(float(timestamp.group("seconds")) * 1_000_000_000)
                targets.append(target)
        return targets, "available" if targets else ("missing" if not selected_logs else "empty")

    def _calibration(self, source: _RunSource, files: Sequence[str]) -> Optional[Dict[str, Any]]:
        if _CALIBRATION_FILE not in files:
            return None
        content = self._read_source_file(source, _CALIBRATION_FILE)
        text = content.decode("utf-8", errors="replace")
        # Keep the original text as the evidence of record.  Parsing arbitrary
        # YAML here would add a dependency and could change the semantics of a
        # vendor calibration file; callers can parse it with their chosen tool.
        return {
            "file": _CALIBRATION_FILE,
            "format": "yaml",
            "sha256": hashlib.sha256(content).hexdigest(),
            "raw": text,
        }

    def _evidence_files(self, source: _RunSource, files: Sequence[str], joint_file: str) -> List[Dict[str, Any]]:
        names = [joint_file]
        for name in ("events.csv", "session_start_ns", "session_end_ns", _CALIBRATION_FILE, *_LOG_FILES):
            if name in files:
                names.append(name)
        # Preserve order while removing duplicates.
        unique_names = list(dict.fromkeys(names))
        result = []
        for name in unique_names:
            content = self._read_source_file(source, name)
            result.append({"path": name, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
        return result

    @staticmethod
    def _media_refs(files: Sequence[str]) -> List[Dict[str, str]]:
        """Return references to recorded media without opening or serving them."""

        image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
        video_exts = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
        refs: List[Dict[str, str]] = []
        for name in files:
            suffix = Path(name).suffix.lower()
            if suffix in image_exts:
                refs.append({"path": name, "kind": "image"})
            elif suffix in video_exts:
                refs.append({"path": name, "kind": "video"})
            elif suffix == ".db3":
                refs.append({"path": name, "kind": "rosbag"})
        return refs


__all__ = [
    "DATA_SOURCE",
    "ReplayDataError",
    "ReplayService",
    "STATUS_AVAILABLE",
    "STATUS_INVALID_DATA",
    "STATUS_UNAVAILABLE",
]
