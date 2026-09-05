"""Read-only validation for real-arm P0/P1 evidence delivery packages.

The hardware team normally delivers an experiment package as a directory or
as a ``tar``, ``tar.gz``, ``tgz`` or ``zip`` archive.  This module deliberately
does not extract, import or execute anything from that package.  It inventories
regular files, reads a small set of JSON/text artifacts, and reports whether
the raw artifacts support the claims made by the package.

The validator is intentionally conservative:

* a prose/manual claim is never evidence by itself;
* an archive containing path traversal, links, or special files is invalid;
* an otherwise complete-looking package without a real-hardware signal is
  incomplete, not passed;
* missing artifacts are reported instead of being silently inferred.

The public entry point returns a JSON-serialisable dictionary so callers can
display or persist the result without depending on this module's internals.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union


PathLike = Union[str, os.PathLike]

# These limits protect the read-only inspection path from pathological input
# while remaining generous for normal camera images and experiment logs.
MAX_MEMBERS = 20_000
MAX_DECLARED_BYTES = 1_024 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_DOCX_BYTES = 16 * 1024 * 1024
MAX_HASH_BYTES = 512 * 1024 * 1024


class _PackageError(Exception):
    """An input package cannot be safely inspected."""


class _ReadLimitError(Exception):
    """A file is larger than the bounded read allowed for its use."""


@dataclass(frozen=True)
class _Entry:
    name: str
    source_name: str
    size: int
    kind: str = "file"


class _Reader:
    """Small common interface over a directory or an archive."""

    package_type = "unknown"

    def __init__(self) -> None:
        self.files: Dict[str, _Entry] = {}
        self.issues: List[str] = []
        self._unsafe = False

    @property
    def unsafe(self) -> bool:
        return self._unsafe

    def add_issue(self, message: str, *, unsafe: bool = True) -> None:
        self.issues.append(message)
        if unsafe:
            self._unsafe = True

    def read(self, name: str, limit: int) -> bytes:
        raise NotImplementedError

    def sha256(self, name: str, limit: int = MAX_HASH_BYTES) -> str:
        """Hash a regular file without materialising it beyond ``limit``."""

        raise NotImplementedError

    def close(self) -> None:
        """Close archive resources.  Directory readers have nothing to close."""


def _normalise_member_name(raw_name: str) -> Tuple[Optional[str], Optional[str]]:
    """Return a safe POSIX-relative member name, or a reason for rejection."""

    if not isinstance(raw_name, str):
        return None, "member name is not text"
    if "\x00" in raw_name:
        return None, "member name contains NUL"

    # Tar and zip names are POSIX-like, but accepting backslashes here would
    # make a package safe on one host and unsafe on another.  Treat them as
    # separators for validation purposes.
    value = raw_name.replace("\\", "/")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return None, "absolute member path"

    parts: List[str] = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None, "parent traversal in member path"
        parts.append(part)
    if not parts:
        return None, "empty member path"
    return "/".join(parts), None


def _join_name(root: str, leaf: str) -> str:
    return f"{root}/{leaf}" if root else leaf


def _under(name: str, root: str) -> bool:
    return bool(root == "" or name == root or name.startswith(root + "/"))


def _parents(name: str) -> Iterable[str]:
    """Yield a path's parent directories, nearest first, including ``""``."""

    parts = name.split("/") if name else []
    for index in range(len(parts) - 1, -1, -1):
        yield "/".join(parts[:index])


def _basename(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def _basename_matches(name: str, aliases: Sequence[str]) -> bool:
    """Match exact artifact names and the versioned names emitted by runners."""

    basename = _basename(name).lower()
    for alias in aliases:
        expected = alias.lower()
        if basename == expected:
            return True
        stem, extension = os.path.splitext(expected)
        # Real evaluator outputs are versioned, e.g. evaluation_<id>.json,
        # before_<id>.jpg and bridge_result_<id>.json.  Keep the prefix match
        # restricted to a separator so unrelated files do not qualify.
        if extension and basename.startswith(stem + "_") and basename.endswith(extension):
            return True
        if extension and basename.startswith(stem + "-") and basename.endswith(extension):
            return True
    return False


def _suffix_matches(names: Iterable[str], requested: str) -> List[str]:
    """Find exact/suffix matches for a checksum path without guessing wildly."""

    requested_name = requested.replace("\\", "/").lstrip("./")
    exact = [name for name in names if name == requested_name]
    if exact:
        return exact
    suffix = "/" + requested_name
    return [name for name in names if name.endswith(suffix)]


class _DirectoryReader(_Reader):
    package_type = "directory"

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        try:
            root_stat = root.lstat()
        except OSError as error:
            raise _PackageError(f"cannot stat package directory: {error}") from error
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise _PackageError("package path is not a real directory")

        root_real = os.path.realpath(str(root))
        member_count = 0
        declared_bytes = 0
        for current, directory_names, file_names in os.walk(
            str(root), topdown=True, followlinks=False
        ):
            current_path = Path(current)

            # Prune symlinked directories rather than following them.  They
            # remain an explicit safety issue so the result cannot be passed.
            for directory_name in list(directory_names):
                path = current_path / directory_name
                try:
                    path_stat = path.lstat()
                except OSError as error:
                    self.add_issue(f"cannot stat directory member {path}: {error}")
                    directory_names.remove(directory_name)
                    continue
                if stat.S_ISLNK(path_stat.st_mode):
                    self.add_issue(f"symlink directory member: {path}")
                    directory_names.remove(directory_name)
                elif not stat.S_ISDIR(path_stat.st_mode):
                    self.add_issue(f"non-directory in directory walk: {path}")
                    directory_names.remove(directory_name)

            for file_name in file_names:
                path = current_path / file_name
                try:
                    path_stat = path.lstat()
                except OSError as error:
                    self.add_issue(f"cannot stat file member {path}: {error}")
                    continue
                raw_name = os.path.relpath(str(path), str(root))
                name, reason = _normalise_member_name(raw_name)
                if reason or name is None:
                    self.add_issue(f"unsafe member path {raw_name!r}: {reason}")
                    continue
                member_count += 1
                if member_count > MAX_MEMBERS:
                    raise _PackageError("package contains too many members")
                if stat.S_ISLNK(path_stat.st_mode):
                    self.add_issue(f"symlink file member: {name}")
                    continue
                if not stat.S_ISREG(path_stat.st_mode):
                    self.add_issue(f"special file member: {name}")
                    continue

                # A parent directory symlink could still be introduced between
                # lstat calls.  This check also makes the intended no-escape
                # property explicit.
                try:
                    file_real = os.path.realpath(str(path))
                    if os.path.commonpath((root_real, file_real)) != root_real:
                        self.add_issue(f"member resolves outside package: {name}")
                        continue
                except (OSError, ValueError) as error:
                    self.add_issue(f"cannot resolve member {name}: {error}")
                    continue

                size = int(path_stat.st_size)
                declared_bytes += max(0, size)
                if declared_bytes > MAX_DECLARED_BYTES:
                    raise _PackageError("package declares too many bytes")
                if name in self.files:
                    self.add_issue(f"duplicate member name: {name}")
                    continue
                self.files[name] = _Entry(name, str(path), size)

    def read(self, name: str, limit: int) -> bytes:
        entry = self.files.get(name)
        if entry is None:
            raise FileNotFoundError(name)
        if entry.size > limit:
            raise _ReadLimitError(f"{name} is larger than the {limit}-byte read limit")
        with Path(entry.source_name).open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise _ReadLimitError(f"{name} exceeded the {limit}-byte read limit")
        return data

    def sha256(self, name: str, limit: int = MAX_HASH_BYTES) -> str:
        entry = self.files.get(name)
        if entry is None:
            raise FileNotFoundError(name)
        if entry.size > limit:
            raise _ReadLimitError(f"{name} is larger than the {limit}-byte hash limit")
        digest = hashlib.sha256()
        total = 0
        with Path(entry.source_name).open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise _ReadLimitError(f"{name} exceeded the {limit}-byte hash limit")
                digest.update(chunk)
        return digest.hexdigest()


class _TarReader(_Reader):
    def __init__(self, path: Path, package_type: str) -> None:
        super().__init__()
        self.package_type = package_type
        try:
            self._archive = tarfile.open(str(path), mode="r:*")
            members = self._archive.getmembers()
        except (OSError, tarfile.TarError) as error:
            raise _PackageError(f"cannot read tar archive: {error}") from error

        if len(members) > MAX_MEMBERS:
            self.close()
            raise _PackageError("archive contains too many members")
        declared_bytes = 0
        for member in members:
            name, reason = _normalise_member_name(member.name)
            if reason or name is None:
                self.add_issue(f"unsafe archive member path {member.name!r}: {reason}")
                continue
            if member.isdir():
                continue
            if member.issym() or member.islnk():
                self.add_issue(f"link archive member: {name}")
                continue
            if not member.isreg():
                self.add_issue(f"special archive member: {name}")
                continue
            size = int(member.size)
            if size < 0:
                self.add_issue(f"negative archive member size: {name}")
                continue
            declared_bytes += size
            if declared_bytes > MAX_DECLARED_BYTES:
                self.close()
                raise _PackageError("archive declares too many bytes")
            if name in self.files:
                self.add_issue(f"duplicate archive member name: {name}")
                continue
            self.files[name] = _Entry(name, member.name, size)

    def read(self, name: str, limit: int) -> bytes:
        entry = self.files.get(name)
        if entry is None:
            raise FileNotFoundError(name)
        if entry.size > limit:
            raise _ReadLimitError(f"{name} is larger than the {limit}-byte read limit")
        try:
            member = self._archive.getmember(entry.source_name)
            stream = self._archive.extractfile(member)
            if stream is None:
                raise OSError("tar member has no readable stream")
            with stream:
                data = stream.read(limit + 1)
        except (KeyError, OSError, tarfile.TarError) as error:
            raise OSError(f"cannot read tar member {name}: {error}") from error
        if len(data) > limit:
            raise _ReadLimitError(f"{name} exceeded the {limit}-byte read limit")
        return data

    def sha256(self, name: str, limit: int = MAX_HASH_BYTES) -> str:
        entry = self.files.get(name)
        if entry is None:
            raise FileNotFoundError(name)
        if entry.size > limit:
            raise _ReadLimitError(f"{name} is larger than the {limit}-byte hash limit")
        try:
            member = self._archive.getmember(entry.source_name)
            stream = self._archive.extractfile(member)
            if stream is None:
                raise OSError("tar member has no readable stream")
            digest = hashlib.sha256()
            total = 0
            with stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise _ReadLimitError(
                            f"{name} exceeded the {limit}-byte hash limit"
                        )
                    digest.update(chunk)
            return digest.hexdigest()
        except (KeyError, OSError, tarfile.TarError) as error:
            raise OSError(f"cannot hash tar member {name}: {error}") from error

    def close(self) -> None:
        archive = getattr(self, "_archive", None)
        if archive is not None:
            archive.close()


class _ZipReader(_Reader):
    package_type = "zip"

    def __init__(self, path: Path) -> None:
        super().__init__()
        try:
            self._archive = zipfile.ZipFile(str(path), mode="r")
            members = self._archive.infolist()
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            raise _PackageError(f"cannot read zip archive: {error}") from error

        if len(members) > MAX_MEMBERS:
            self.close()
            raise _PackageError("archive contains too many members")
        declared_bytes = 0
        for member in members:
            name, reason = _normalise_member_name(member.filename)
            if reason or name is None:
                self.add_issue(f"unsafe archive member path {member.filename!r}: {reason}")
                continue
            mode = (member.external_attr >> 16) & 0xFFFF
            if member.is_dir() or member.filename.endswith(("/", "\\")):
                continue
            if stat.S_ISLNK(mode):
                self.add_issue(f"link archive member: {name}")
                continue
            # ZIP producers are inconsistent about external attributes.  A
            # Unix mode with no file-type bits (for example ``0o600`` or
            # ``0o644``) is a normal regular file, even though
            # ``stat.S_ISREG(mode)`` returns False for that abbreviated mode.
            # Reject only an explicit non-regular file type; otherwise the
            # archive entry's ordinary file record is authoritative.
            file_type = stat.S_IFMT(mode)
            if file_type and file_type != stat.S_IFREG:
                self.add_issue(f"special archive member: {name}")
                continue
            size = int(member.file_size)
            if size < 0:
                self.add_issue(f"negative archive member size: {name}")
                continue
            declared_bytes += size
            if declared_bytes > MAX_DECLARED_BYTES:
                self.close()
                raise _PackageError("archive declares too many bytes")
            if name in self.files:
                self.add_issue(f"duplicate archive member name: {name}")
                continue
            if member.flag_bits & 0x1:
                # Do not fail the whole package merely for an encrypted
                # unrelated file; reading it later will produce a warning.
                self.add_issue(f"encrypted archive member: {name}", unsafe=False)
            self.files[name] = _Entry(name, member.filename, size)

    def read(self, name: str, limit: int) -> bytes:
        entry = self.files.get(name)
        if entry is None:
            raise FileNotFoundError(name)
        if entry.size > limit:
            raise _ReadLimitError(f"{name} is larger than the {limit}-byte read limit")
        try:
            with self._archive.open(entry.source_name, mode="r") as stream:
                data = stream.read(limit + 1)
        except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as error:
            raise OSError(f"cannot read zip member {name}: {error}") from error
        if len(data) > limit:
            raise _ReadLimitError(f"{name} exceeded the {limit}-byte read limit")
        return data

    def sha256(self, name: str, limit: int = MAX_HASH_BYTES) -> str:
        entry = self.files.get(name)
        if entry is None:
            raise FileNotFoundError(name)
        if entry.size > limit:
            raise _ReadLimitError(f"{name} is larger than the {limit}-byte hash limit")
        digest = hashlib.sha256()
        total = 0
        try:
            with self._archive.open(entry.source_name, mode="r") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise _ReadLimitError(
                            f"{name} exceeded the {limit}-byte hash limit"
                        )
                    digest.update(chunk)
        except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as error:
            raise OSError(f"cannot hash zip member {name}: {error}") from error
        return digest.hexdigest()

    def close(self) -> None:
        archive = getattr(self, "_archive", None)
        if archive is not None:
            archive.close()


def _open_reader(package: PathLike) -> Tuple[_Reader, Path, str]:
    try:
        path = Path(package).expanduser()
    except (TypeError, ValueError) as error:
        raise _PackageError(f"package path is invalid: {error}") from error
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise _PackageError(f"package does not exist or is inaccessible: {error}") from error
    if stat.S_ISLNK(path_stat.st_mode):
        raise _PackageError("package path itself is a symlink")
    if stat.S_ISDIR(path_stat.st_mode):
        return _DirectoryReader(path), path, "directory"
    if not stat.S_ISREG(path_stat.st_mode):
        raise _PackageError("package path is not a regular file or directory")

    suffixes = "".join(path.suffixes).lower()
    suffix = path.suffix.lower()
    if suffix == ".zip":
        reader = _ZipReader(path)
    elif suffix in {".tar", ".tgz"} or suffixes.endswith(".tar.gz"):
        package_type = "tgz" if suffix == ".tgz" else ("tar.gz" if suffixes.endswith(".tar.gz") else "tar")
        reader = _TarReader(path, package_type)
    else:
        raise _PackageError(f"unsupported package type: {path.name}")
    return reader, path, reader.package_type


def _read_text(reader: _Reader, name: str, limit: int = MAX_TEXT_BYTES) -> Tuple[Optional[str], Optional[str]]:
    try:
        data = reader.read(name, limit)
    except (OSError, _ReadLimitError) as error:
        return None, str(error)
    return data.decode("utf-8-sig", errors="replace"), None


def _read_json(reader: _Reader, name: str) -> Tuple[Optional[Mapping[str, Any]], Optional[str]]:
    text, error = _read_text(reader, name, MAX_JSON_BYTES)
    if error:
        return None, error
    try:
        value = json.loads(text or "")
    except json.JSONDecodeError as json_error:
        return None, str(json_error)
    if not isinstance(value, Mapping):
        return None, "JSON value is not an object"
    return value, None


def _docx_text(data: bytes) -> str:
    """Extract visible text from a DOCX in memory, without writing/executing."""

    try:
        with zipfile.ZipFile(io.BytesIO(data), mode="r") as archive:
            info = archive.getinfo("word/document.xml")
            # A nested document is treated as text only; enforce a separate
            # bound before reading it and reject unsafe nested paths.
            if info.file_size > MAX_TEXT_BYTES:
                return ""
            raw = archive.read(info)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        return ""
    text = raw.decode("utf-8", errors="replace")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def _documentation_claims(reader: _Reader) -> Set[str]:
    """Find strong prose claims, used only to report unsupported assertions."""

    claims: Set[str] = set()
    for name in sorted(reader.files):
        lower = name.lower()
        is_doc = lower.endswith((".md", ".txt", ".rst", ".adoc"))
        is_docx = lower.endswith(".docx")
        if not (is_doc or is_docx):
            continue
        limit = MAX_DOCX_BYTES if is_docx else MAX_TEXT_BYTES
        try:
            data = reader.read(name, limit)
        except (OSError, _ReadLimitError):
            continue
        text = _docx_text(data) if is_docx else data.decode("utf-8-sig", errors="replace")
        compact = re.sub(r"\s+", " ", text).lower()
        # Keep patterns deliberately specific: "complete source" is not a
        # physical success claim, while these phrases are.
        if re.search(
            r"(?:success[_ -]*p0|p0[^.]{0,80}(?:success|succeeded|passed|complete|成功|通过)|real_visual_evaluation_passed)",
            compact,
            re.IGNORECASE,
        ):
            claims.add("p0_success")
        if re.search(
            r"(?:failed[_ -]*p0|p0[^.]{0,80}(?:fail|failed|失败)|failure[_ -]*p0)",
            compact,
            re.IGNORECASE,
        ):
            claims.add("p0_failure")
        if re.search(
            r"(?:p1[_ -]*recovery[_ -]*demonstrated|p1[^.]{0,100}(?:success|succeeded|passed|complete|recovery|成功|通过|恢复))",
            compact,
            re.IGNORECASE,
        ):
            claims.add("p1_recovery")
    return claims


_MARKER_CLAIMS = {
    "success_p0": "p0_success",
    "p0_success": "p0_success",
    "p0_successful": "p0_success",
    "p0": "p0_success",
    "failed_p0": "p0_failure",
    "failure_p0": "p0_failure",
    "p0_failed": "p0_failure",
    "p0_failure": "p0_failure",
    "p1": "p1_recovery",
    "p1_recovery": "p1_recovery",
    "p1_success": "p1_recovery",
    "p1_replay": "p1_recovery",
}


def _marker_claim(component: str) -> Optional[str]:
    normalized = component.lower().replace("-", "_").replace(" ", "_")
    direct = _MARKER_CLAIMS.get(normalized)
    if direct:
        return direct
    # ``build_success_p0_package.py`` names runs ``success_<experiment_id>``;
    # accept that explicit producer convention without treating arbitrary
    # source filenames as experiment claims.
    if normalized.startswith("success_"):
        return "p0_success"
    if normalized.startswith("failed_") and normalized.endswith("p0"):
        return "p0_failure"
    if normalized.startswith("p1_"):
        return "p1_recovery"
    return None


def _all_direct_dirs(reader: _Reader) -> Set[str]:
    directories: Set[str] = {""}
    for name in reader.files:
        parts = name.split("/")
        for index in range(1, len(parts)):
            directories.add("/".join(parts[:index]))
    return directories


def _direct_file(reader: _Reader, root: str, aliases: Sequence[str]) -> Optional[str]:
    for alias in aliases:
        candidate = _join_name(root, alias)
        if candidate in reader.files:
            return candidate
    # Case-insensitive fallback for files produced on a case-insensitive host.
    for name in sorted(reader.files):
        if not _basename_matches(name, aliases):
            continue
        parent = name.rsplit("/", 1)[0] if "/" in name else ""
        if parent.lower() == root.lower():
            return name
    return None


def _locate(
    reader: _Reader,
    root: str,
    aliases: Sequence[str],
    *,
    global_fallback: bool = False,
) -> Optional[str]:
    """Locate an artifact below ``root``, with optional package-level fallback."""

    direct = _direct_file(reader, root, aliases)
    if direct:
        return direct

    descendants = [
        name
        for name in reader.files
        if _under(name, root)
        and name != root
        and _basename_matches(name, aliases)
    ]
    # The bridge builder may use a timestamped backend log name that does not
    # share one of the conventional aliases.  Restrict this fallback to log
    # files below the claim root; arbitrary source text must not become motion
    # evidence merely because it contains the word "log".
    if not descendants and "bridge_or_wrapper.log" in aliases:
        descendants = [
            name
            for name in reader.files
            if _under(name, root) and _basename(name).lower().endswith(".log")
        ]
    if descendants:
        descendants.sort(key=lambda value: (value.count("/"), value.lower()))
        return descendants[0]

    if not global_fallback:
        return None
    # Prefer a manifest/checksum in an ancestor of the run directory.
    for parent in _parents(root):
        for alias in aliases:
            candidate = _join_name(parent, alias)
            if candidate in reader.files:
                return candidate
        for name in sorted(reader.files):
            if _basename_matches(name, aliases):
                actual_parent = name.rsplit("/", 1)[0] if "/" in name else ""
                if actual_parent.lower() == parent.lower():
                    return name
    global_matches = [
        name for name in reader.files if _basename_matches(name, aliases)
    ]
    if global_matches:
        global_matches.sort(key=lambda value: (value.count("/"), value.lower()))
        return global_matches[0]
    return None


_MANIFEST_ALIASES = (
    "runtime_manifest.json",
    "manifest.json",
    "experiment_manifest.json",
    "package_manifest.json",
    "source_manifest.json",
    "experiment.json",
)
_EVALUATION_ALIASES = ("evaluation.json", "evaluation_result.json")
_BEFORE_ALIASES = (
    "before.jpg",
    "before.jpeg",
    "before.png",
    "before.webp",
    "before",
    "scene_before.rgb.png",
    "scene_before.jpg",
)
_LIFT_ALIASES = (
    "grasp_lift.jpg",
    "grasp_lift.jpeg",
    "grasp_lift.png",
    "grasp_lift.webp",
    "grasp_lift",
    "lift.jpg",
    "lift.jpeg",
    "lift.png",
    "scene_lift.rgb.png",
)
_AFTER_ALIASES = (
    "after.jpg",
    "after.jpeg",
    "after.png",
    "after.webp",
    "after",
    "scene_after.rgb.png",
    "scene_after.jpg",
)
_BRIDGE_RESULT_ALIASES = ("bridge_result.json", "execute_pick_place.json")
_LOG_ALIASES = (
    "bridge.log",
    "bridge_result.log",
    "wrapper.log",
    "grasp_events_session.log",
    "backend.log",
    "real_stop_test.log",
    "execution.log",
)
_CHECKSUM_ALIASES = (
    "SHA256SUMS.txt",
    "sha256sums.txt",
    "SHA256SUMS",
    "manifest.sha256",
    "checksums.sha256",
    "wrapper_files.sha256",
    "checksums.txt",
)
_P1_ID_ALIASES = ("p0_experiment_id.txt",)
_P1_CANDIDATE_ALIASES = (
    "candidate_skill.json",
    "latest_candidate_skill.json",
    "latest_p1_single_parameter_candidate.json",
)
_P1_COMPARISON_ALIASES = ("comparison.json", "p0_vs_p1.json")
_P1_PREFLIGHT_ALIASES = ("preflight.json",)


def _claim_roots(
    reader: _Reader,
    documentation_claims: Set[str],
    package_marker: Optional[str] = None,
) -> Dict[str, List[str]]:
    """Infer experiment roots without treating arbitrary source folders as runs."""

    roots: Dict[str, List[str]] = {"p0_success": [], "p0_failure": [], "p1_recovery": []}
    directories = _all_direct_dirs(reader)

    # When validating an already extracted delivery directory, the directory
    # basename is not part of the relative member names.  Preserve an
    # explicit producer marker such as ``success_<experiment_id>`` at the
    # package root so nested ``evaluation/`` and ``bridge/`` files are treated
    # as one run rather than as unrelated directories.
    if package_marker:
        marker = _marker_claim(package_marker)
        if marker:
            roots[marker].append("")

    # Explicit names are authoritative and survive an absent evaluation file,
    # allowing the missing list to explain exactly what the package omitted.
    for directory in sorted(directories, key=lambda value: (value.count("/"), value)):
        if not directory:
            continue
        components = directory.split("/")
        for index, component in enumerate(components):
            claim = _marker_claim(component)
            if claim:
                # Collapse descendants of an explicitly named run to that
                # run's directory.  Without this, ``success_<id>/bridge``
                # and ``success_<id>/evaluation`` would be mistaken for
                # independent P0 claims.
                roots[claim].append("/".join(components[: index + 1]))
                break

    explicit = {root for values in roots.values() for root in values}
    package_root_claimed = "" in explicit

    # The standard successful-package builder uses a package-level manifest
    # and nests the evaluation under ``evaluation/``.  If the extracted
    # directory itself has no semantic basename (for example ``delivery``),
    # classify that whole directory as one run before considering child
    # folders such as ``evaluation`` or ``bridge``.
    package_manifest_name = _direct_file(
        reader,
        "",
        ("package_manifest.json", "runtime_manifest.json"),
    )
    package_evaluation_name = _locate(
        reader,
        "",
        _EVALUATION_ALIASES,
        global_fallback=True,
    )
    if not package_root_claimed and package_manifest_name and package_evaluation_name:
        payload, _ = _read_json(reader, package_evaluation_name)
        outcome = payload.get("outcome") if isinstance(payload, Mapping) else None
        values = (
            [
                outcome.get(key)
                for key in ("object_grasped", "object_lifted", "object_placed")
            ]
            if isinstance(outcome, Mapping)
            else []
        )
        if values and all(value is True for value in values):
            roots["p0_success"].append("")
        elif values and any(value is False for value in values):
            roots["p0_failure"].append("")
        package_root_claimed = any("" in values for values in roots.values())
        explicit = {root for values in roots.values() for root in values}
    # Generic experiment folders (or a package containing files at its root)
    # are inferred from a direct evaluation artifact.  A directory named p0
    # is classified from its explicit outcome when available.
    for directory in sorted(directories, key=lambda value: (value.count("/"), value)):
        nested_in_explicit = any(
            explicit_root
            and _under(directory, explicit_root)
            for explicit_root in explicit
        )
        if (
            package_root_claimed
            or nested_in_explicit
            or directory in explicit
            or (directory == "" and any(roots.values()))
        ):
            continue
        evaluation_name = _direct_file(reader, directory, _EVALUATION_ALIASES)
        if not evaluation_name:
            continue
        claim: Optional[str] = None
        lower_parts = {part.lower().replace("-", "_") for part in directory.split("/")}
        if "p1" in lower_parts or "p1_recovery" in lower_parts:
            claim = "p1_recovery"
        else:
            payload, _ = _read_json(reader, evaluation_name)
            outcome = payload.get("outcome") if isinstance(payload, Mapping) else None
            values = [outcome.get(key) for key in ("object_grasped", "object_lifted", "object_placed")] if isinstance(outcome, Mapping) else []
            if values and all(value is True for value in values):
                claim = "p0_success"
            elif values and any(value is False for value in values):
                claim = "p0_failure"
            elif "p0" in lower_parts:
                claim = "p0_success"
        if claim:
            roots[claim].append(directory)

    # If a package contains only a single unlabelled run, its root is the most
    # useful place to report missing artifacts.
    if not any(roots.values()):
        if _direct_file(reader, "", _EVALUATION_ALIASES):
            payload, _ = _read_json(reader, _direct_file(reader, "", _EVALUATION_ALIASES) or "")
            outcome = payload.get("outcome") if isinstance(payload, Mapping) else None
            values = [outcome.get(key) for key in ("object_grasped", "object_lifted", "object_placed")] if isinstance(outcome, Mapping) else []
            inferred = "p0_success" if values and all(value is True for value in values) else "p0_failure" if values and any(value is False for value in values) else "p0_success"
            roots[inferred].append("")
        elif documentation_claims:
            for claim in documentation_claims:
                roots[claim].append("")
        else:
            # No claim and no run: report a useful incomplete baseline rather
            # than silently returning an empty success-shaped result.
            roots["p0_success"].append("")

    for claim in roots:
        roots[claim] = sorted(set(roots[claim]))
    return roots


def _json_experiment_id(payload: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("experiment_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    nested = payload.get("experiment")
    if isinstance(nested, Mapping):
        value = nested.get("experiment_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _outcome(payload: Optional[Mapping[str, Any]]) -> Optional[Dict[str, bool]]:
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("outcome")
    if not isinstance(value, Mapping):
        return None
    result: Dict[str, bool] = {}
    for key in ("object_grasped", "object_lifted", "object_placed"):
        if type(value.get(key)) is not bool:
            return None
        result[key] = bool(value[key])
    return result


def _flatten_values(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {
                "mode",
                "execution_mode",
                "adapter",
                "backend",
                "data_source",
                "hardware_status",
                "hardware",
                "real_arm",
                "real_motion",
                "physical",
                "runtime_status",
                "environment",
                "device",
                "evaluation_mode",
                "evaluation_status",
                "bridge_status",
                "package_type",
                "task_success",
                "synthetic",
                "motion_enabled",
            }:
                yield f"{key}={item}"
                yield str(item)
            yield from _flatten_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten_values(item)


def _real_hardware_signal(
    manifest: Optional[Mapping[str, Any]],
    evaluation: Optional[Mapping[str, Any]],
    bridge_result: Optional[Mapping[str, Any]],
    log_text: str,
) -> Tuple[bool, bool]:
    """Return ``(positive_signal, explicit_synthetic_signal)``."""

    payloads = (manifest, evaluation, bridge_result)
    values = " ".join(item.lower() for payload in payloads for item in _flatten_values(payload))
    log_lower = log_text.lower()
    combined = values + " " + log_lower
    explicit_synthetic = bool(
        re.search(r"\b(?:mock|dry[_ -]?run|simulation|synthetic)\b", combined)
    ) or any(
        isinstance(payload, Mapping) and payload.get("synthetic") is True
        for payload in payloads
    )
    positive = bool(
        re.search(
            r"(?:real[_ -]?arm|real[_ -]?motion|real[_ -]?multi|real[_ -]?visual|physical[_ -]?outcome|hardware_status\s*[:=]\s*(?:true|real|verified|connected)|hardware\s*[:=]\s*(?:true|real|verified|connected)|motion_enabled\s*[:=]\s*true|synthetic\s*[:=]\s*false)",
            combined,
        )
    )
    return positive, explicit_synthetic


def _image_ok(reader: _Reader, name: str) -> Tuple[bool, Optional[str]]:
    entry = reader.files.get(name)
    if entry is None:
        return False, "file is missing"
    if entry.size <= 0:
        return False, "image file is empty"
    try:
        header = reader.read(name, min(32, max(32, entry.size)))
    except (OSError, _ReadLimitError) as error:
        return False, str(error)
    suffix = Path(name).suffix.lower()
    known = (
        header.startswith(b"\xff\xd8\xff")
        or header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith((b"GIF87a", b"GIF89a"))
        or (header.startswith(b"RIFF") and b"WEBP" in header[:16])
    )
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        return True, "image filename has no standard extension"
    if not known:
        return True, "image header is not a recognised JPEG/PNG/WebP signature"
    return True, None


def _parse_checksum_lines(text: str) -> Tuple[List[Tuple[str, str]], int]:
    records: List[Tuple[str, str]] = []
    malformed = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^(?:sha256\s+)?([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", stripped)
        if match:
            records.append((match.group(2).strip(), match.group(1).lower()))
            continue
        reverse = re.match(r"^(.+?)\s*:\s*([0-9a-fA-F]{64})\s*$", stripped)
        if reverse:
            records.append((reverse.group(1).strip(), reverse.group(2).lower()))
            continue
        malformed += 1
    return records, malformed


def _verify_checksums(
    reader: _Reader,
    checksum_name: Optional[str],
    warnings: List[str],
) -> Tuple[bool, Set[str]]:
    """Verify parseable checksum records; return validity and covered names."""

    if not checksum_name:
        return False, set()
    text, error = _read_text(reader, checksum_name, MAX_TEXT_BYTES)
    if error:
        warnings.append(f"cannot read checksum manifest {checksum_name}: {error}")
        return False, set()
    records, malformed = _parse_checksum_lines(text or "")
    if malformed:
        warnings.append(f"checksum manifest has {malformed} unparseable line(s): {checksum_name}")
    if not records:
        warnings.append(f"checksum manifest has no parseable SHA-256 records: {checksum_name}")
        return False, set()

    covered: Set[str] = set()
    valid = True
    manifest_parent = checksum_name.rsplit("/", 1)[0] if "/" in checksum_name else ""
    all_names = set(reader.files)
    for listed, expected in records:
        listed_name, reason = _normalise_member_name(listed)
        if reason or listed_name is None:
            warnings.append(f"unsafe checksum path {listed!r}: {reason}")
            valid = False
            continue
        candidates: List[str] = []
        if manifest_parent:
            candidate = _join_name(manifest_parent, listed_name)
            if candidate in all_names:
                candidates = [candidate]
        if not candidates:
            candidates = _suffix_matches(all_names, listed_name)
        if len(candidates) != 1:
            warnings.append(f"checksum entry references missing or ambiguous file: {listed}")
            valid = False
            continue
        actual_name = candidates[0]
        try:
            actual = reader.sha256(actual_name)
        except (OSError, _ReadLimitError) as error:
            warnings.append(f"cannot verify checksum for {actual_name}: {error}")
            valid = False
            continue
        covered.add(actual_name)
        if actual != expected:
            warnings.append(f"checksum mismatch: {actual_name}")
            valid = False
    return valid, covered


def _verify_embedded_checksums(
    reader: _Reader,
    manifest_payload: Optional[Mapping[str, Any]],
    warnings: List[str],
) -> Tuple[bool, Set[str]]:
    """Verify checksum records embedded in a package/source manifest.

    The standard P0 builder stores records such as
    ``evaluation_json_sha256`` and ``bridge_result_json_sha256`` in
    ``package_manifest.json`` rather than writing a separate SHA256SUMS file.
    Its path values are often absolute paths from the ArmPi host, so only the
    basename/suffix is used to resolve them inside the delivered package.
    """

    if not isinstance(manifest_payload, Mapping):
        return False, set()
    raw_checksums = manifest_payload.get("checksums")
    if not isinstance(raw_checksums, Mapping):
        return False, set()
    packaged_paths = manifest_payload.get("packaged_paths")
    if not isinstance(packaged_paths, Mapping):
        packaged_paths = {}

    records: List[Tuple[str, str]] = []
    for key, value in raw_checksums.items():
        expected = str(value).strip().lower() if isinstance(value, str) else ""
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            warnings.append(f"embedded checksum is not a SHA-256 digest: {key}")
            continue
        key_text = str(key)
        if key_text.lower().endswith("_sha256"):
            path_key = key_text[:-7]
        else:
            path_key = key_text
        reference = packaged_paths.get(path_key)
        if not isinstance(reference, str) or not reference.strip():
            # A few producers use a compact key without a packaged_paths map.
            reference = path_key.replace("_json", ".json").replace("_log", ".log")
        records.append((reference, expected))

    if not records:
        warnings.append("embedded checksum manifest has no parseable SHA-256 records")
        return False, set()

    all_names = set(reader.files)
    covered: Set[str] = set()
    valid = True
    for reference, expected in records:
        reference_name = Path(reference.replace("\\", "/")).name
        candidates = [
            name
            for name in all_names
            if _basename(name).lower() == reference_name.lower()
            or name.endswith("/" + reference_name)
        ]
        # If the producer's key is descriptive (e.g. ``evaluation_json``),
        # fall back to the corresponding conventional stem.
        if not candidates:
            ref_stem = Path(reference_name).stem.lower()
            candidates = [
                name
                for name in all_names
                if Path(name).stem.lower() == ref_stem
            ]
        if len(candidates) != 1:
            warnings.append(
                f"embedded checksum references missing or ambiguous file: {reference}"
            )
            valid = False
            continue
        actual_name = candidates[0]
        try:
            actual = reader.sha256(actual_name)
        except (OSError, _ReadLimitError) as error:
            warnings.append(f"cannot verify embedded checksum for {actual_name}: {error}")
            valid = False
            continue
        covered.add(actual_name)
        if actual != expected:
            warnings.append(f"embedded checksum mismatch: {actual_name}")
            valid = False
    return valid, covered


def _assess_claim(
    reader: _Reader,
    claim: str,
    root: str,
    package_checksum_name: Optional[str],
) -> Tuple[bool, List[str], List[str], Dict[str, str]]:
    missing: List[str] = []
    warnings: List[str] = []
    found: Dict[str, str] = {}

    specs: List[Tuple[str, Sequence[str], bool]] = [
        ("manifest.json", _MANIFEST_ALIASES, True),
        ("evaluation.json", _EVALUATION_ALIASES, True),
        ("before.jpg", _BEFORE_ALIASES, True),
        ("grasp_lift.jpg", _LIFT_ALIASES, True),
        ("after.jpg", _AFTER_ALIASES, True),
        ("bridge_result.json", _BRIDGE_RESULT_ALIASES, True),
        ("bridge_or_wrapper.log", _LOG_ALIASES, True),
        # A package may carry a standalone SHA256SUMS file or embed its
        # records in package_manifest.json.  Defer the missing decision until
        # the manifest has been parsed.
        ("SHA256SUMS.txt", _CHECKSUM_ALIASES, False),
    ]
    if claim == "p1_recovery":
        specs.extend(
            [
                ("p0_experiment_id.txt", _P1_ID_ALIASES, True),
                ("candidate_skill.json", _P1_CANDIDATE_ALIASES, True),
                ("comparison.json", _P1_COMPARISON_ALIASES, True),
            ]
        )

    global_manifest = True
    global_checksum = True
    for label, aliases, required in specs:
        locate_global = global_manifest if label == "manifest.json" else global_checksum if label == "SHA256SUMS.txt" else False
        name = _locate(reader, root, aliases, global_fallback=locate_global)
        if name:
            found[label] = name
        elif required:
            missing.append(_join_name(root, label))

    # Helpful, non-fatal context artifacts required by the full hand-off
    # convention.  The core claim gate above remains usable with a minimal
    # package, while these omissions are visible to reviewers.
    for label, aliases in (
        ("experiment.json", ("experiment.json",)),
        ("plan_applied.json", ("plan_applied.json",)),
        ("joint_states_session.csv", ("joint_states_session.csv",)),
    ):
        if not _locate(reader, root, aliases):
            warnings.append(f"recommended artifact missing: {_join_name(root, label)}")

    manifest_payload: Optional[Mapping[str, Any]] = None
    evaluation_payload: Optional[Mapping[str, Any]] = None
    bridge_payload: Optional[Mapping[str, Any]] = None
    log_text = ""
    valid = not missing

    if "manifest.json" in found:
        manifest_payload, error = _read_json(reader, found["manifest.json"])
        if error:
            warnings.append(f"invalid manifest {found['manifest.json']}: {error}")
            valid = False
    if "evaluation.json" in found:
        evaluation_payload, error = _read_json(reader, found["evaluation.json"])
        if error:
            warnings.append(f"invalid evaluation {found['evaluation.json']}: {error}")
            valid = False
    if "bridge_result.json" in found:
        bridge_payload, error = _read_json(reader, found["bridge_result.json"])
        if error:
            warnings.append(f"invalid bridge result {found['bridge_result.json']}: {error}")
            valid = False
    if "bridge_or_wrapper.log" in found:
        log_text, error = _read_text(reader, found["bridge_or_wrapper.log"], MAX_TEXT_BYTES)
        if error:
            warnings.append(f"cannot read bridge/wrapper log {found['bridge_or_wrapper.log']}: {error}")
            valid = False
        elif not (log_text or "").strip():
            warnings.append(f"bridge/wrapper log is empty: {found['bridge_or_wrapper.log']}")
            valid = False

    checksum_name = found.get("SHA256SUMS.txt") or package_checksum_name
    embedded_checksum = False
    checksum_valid = False
    covered: Set[str] = set()
    if checksum_name:
        checksum_valid, covered = _verify_checksums(reader, checksum_name, warnings)
    elif manifest_payload is not None:
        embedded_checksum = True
        checksum_valid, covered = _verify_embedded_checksums(
            reader, manifest_payload, warnings
        )
    if not checksum_valid:
        missing.append(_join_name(root, "SHA256SUMS.txt"))
        valid = False

    outcome = _outcome(evaluation_payload)
    if outcome is None:
        warnings.append("evaluation.json must contain explicit boolean object_grasped/object_lifted/object_placed")
        valid = False
    elif claim == "p0_success" and not all(outcome.values()):
        warnings.append("p0_success claim conflicts with evaluation outcome")
        valid = False
    elif claim == "p0_failure" and not any(value is False for value in outcome.values()):
        warnings.append("p0_failure claim has no explicit failed physical outcome")
        valid = False
    elif claim == "p1_recovery" and not all(outcome.values()):
        warnings.append("p1_recovery claim requires all three successful physical outcomes")
        valid = False

    # Images must be present and non-empty.  Header mismatches are warnings so
    # lightweight fixtures and camera-specific formats remain inspectable.
    for label in ("before.jpg", "grasp_lift.jpg", "after.jpg"):
        name = found.get(label)
        if not name:
            continue
        image_valid, image_warning = _image_ok(reader, name)
        if not image_valid:
            warnings.append(f"invalid {label} artifact {name}: {image_warning}")
            valid = False
        elif image_warning:
            warnings.append(f"{label} artifact warning ({name}): {image_warning}")

    if checksum_valid and not embedded_checksum:
        required_paths = {path for label, path in found.items() if label != "SHA256SUMS.txt"}
        uncovered = required_paths - covered
        if required_paths and uncovered:
            # A checksum file that covers only itself (or one convenient
            # source file) does not make the evidence reproducible: any
            # unlisted image/result/log could have been changed after the
            # claimed run.  Keep the exact paths in the report so the hardware
            # team can repair the package without guessing.
            warnings.append(
                "checksum manifest does not cover claim artifact(s): "
                + ", ".join(sorted(uncovered))
            )
            valid = False

    positive_signal, synthetic_signal = _real_hardware_signal(
        manifest_payload, evaluation_payload, bridge_payload, log_text or ""
    )
    if synthetic_signal:
        warnings.append("package contains an explicit mock/dry-run/synthetic signal")
        valid = False
    if not positive_signal:
        warnings.append("no explicit real_arm/real_motion/hardware signal was found")
        valid = False

    if claim == "p1_recovery":
        p0_id_name = found.get("p0_experiment_id.txt")
        if p0_id_name:
            p0_id_text, error = _read_text(reader, p0_id_name, 4096)
            if error or not (p0_id_text or "").strip():
                warnings.append("p0_experiment_id.txt is empty or unreadable")
                valid = False
        for label in ("candidate_skill.json", "comparison.json"):
            name = found.get(label)
            if name:
                payload, error = _read_json(reader, name)
                if error or payload is None:
                    warnings.append(f"invalid {label}: {error or 'not an object'}")
                    valid = False
        if not _locate(reader, root, _P1_PREFLIGHT_ALIASES):
            warnings.append(f"recommended artifact missing: {_join_name(root, 'preflight.json')}")

    ids = {
        item
        for item in (
            _json_experiment_id(manifest_payload),
            _json_experiment_id(evaluation_payload),
            _json_experiment_id(bridge_payload),
        )
        if item
    }
    if len(ids) > 1:
        warnings.append("experiment_id differs across manifest/evaluation/bridge_result")
        valid = False

    return valid, missing, warnings, found


def _base_result(path: PathLike, package_type: str, files: Sequence[str]) -> Dict[str, Any]:
    try:
        display_path = str(Path(path).expanduser())
    except (TypeError, ValueError):
        display_path = str(path)
    return {
        "status": "incomplete",
        "package_type": package_type,
        "package_path": display_path,
        "files_checked": sorted(set(files)),
        "missing": [],
        "warnings": [],
        "claims_supported": {
            "p0_success": False,
            "p0_failure": False,
            "p1_recovery": False,
        },
    }


def validate_evidence_package(package: PathLike) -> Dict[str, Any]:
    """Validate a directory or supported archive without changing the filesystem.

    ``status`` is one of:

    ``passed``
        Every claim represented by the package has complete, internally
        consistent raw evidence and a real-hardware signal.
    ``incomplete``
        The package is safe to inspect, but one or more represented claims are
        missing or cannot be verified.  Documentation-only claims land here.
    ``invalid``
        The input is unreadable/unsupported or contains unsafe archive members.
    """

    try:
        reader, path, package_type = _open_reader(package)
    except _PackageError as error:
        result = _base_result(package, "unknown", [])
        result["status"] = "invalid"
        result["warnings"].append(str(error))
        return result

    try:
        result = _base_result(path, package_type, sorted(reader.files))
        if reader.issues:
            result["warnings"].extend(reader.issues)
        if reader.unsafe:
            result["status"] = "invalid"
            result["warnings"].append("package contains unsafe members; no extraction was attempted")
            return result

        docs = _documentation_claims(reader)
        package_marker = path.name if package_type == "directory" else None
        roots = _claim_roots(reader, docs, package_marker=package_marker)
        package_checksum = _locate(reader, "", _CHECKSUM_ALIASES, global_fallback=True)
        represented: Set[str] = {
            claim for claim, values in roots.items() if values
        }
        if docs:
            for claim in docs:
                if not roots.get(claim):
                    roots[claim].append("")
                    represented.add(claim)

        all_missing: List[str] = []
        all_warnings: List[str] = []
        supported: Dict[str, bool] = {
            "p0_success": False,
            "p0_failure": False,
            "p1_recovery": False,
        }
        for claim in ("p0_success", "p0_failure", "p1_recovery"):
            claim_seen = False
            claim_all_valid = True
            for root in roots.get(claim, []):
                claim_seen = True
                claim_valid, missing, warnings, _ = _assess_claim(
                    reader, claim, root, package_checksum
                )
                all_missing.extend(missing)
                all_warnings.extend(
                    f"{claim} ({root or '.'}): {warning}" for warning in warnings
                )
                claim_all_valid = claim_all_valid and claim_valid
            supported[claim] = claim_seen and claim_all_valid

        # A prose/manual assertion without the corresponding raw run is called
        # out explicitly.  It can never turn an incomplete package into pass.
        for claim in sorted(docs):
            if not supported.get(claim, False):
                all_warnings.append(
                    f"documentation claim without verifiable raw artifacts: {claim}"
                )

        # If no run was inferred, retain a clear package-level explanation.
        if not represented:
            all_warnings.append("no P0/P1 experiment directory or evaluation.json was found")

        result["claims_supported"] = supported
        result["missing"] = sorted(set(all_missing))
        result["warnings"].extend(sorted(set(all_warnings)))
        expected_claims = {
            claim for claim, values in roots.items() if values
        }
        result["expected_claims"] = sorted(expected_claims)
        if expected_claims and expected_claims.issubset(
            {claim for claim, value in supported.items() if value}
        ):
            result["status"] = "passed"
        else:
            result["status"] = "incomplete"
        return result
    finally:
        reader.close()


def validate_delivery_package(package: PathLike) -> Dict[str, Any]:
    """Backward-compatible descriptive alias for :func:`validate_evidence_package`."""

    return validate_evidence_package(package)


def validate_p0_p1_package(package: PathLike) -> Dict[str, Any]:
    """Short alias used by integration callers."""

    return validate_evidence_package(package)


def validate_package(package: PathLike) -> Dict[str, Any]:
    """Generic alias for callers that do not use the hardware-specific name."""

    return validate_evidence_package(package)


def validate_evidence_source(package: PathLike) -> Dict[str, Any]:
    """Alias retained for source-oriented API integrations."""

    return validate_evidence_package(package)


class EvidencePackageValidator:
    """Stateless object wrapper for dependency-injection-friendly callers."""

    def validate(self, package: PathLike) -> Dict[str, Any]:
        return validate_evidence_package(package)


__all__ = [
    "EvidencePackageValidator",
    "validate_delivery_package",
    "validate_evidence_package",
    "validate_evidence_source",
    "validate_package",
    "validate_p0_p1_package",
]
