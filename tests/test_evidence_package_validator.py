import hashlib
import io
import json
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from roboscientist.hardware_bridge.evidence_package_validator import (
    EvidencePackageValidator,
    validate_evidence_package,
)


JPEG = b"\xff\xd8\xff\xe0" + b"fixture-jpeg" + b"\xff\xd9"


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _make_p0(root: Path, *, synthetic: bool = False, checksum: bool = True) -> Path:
    run = root / "success_p0"
    run.mkdir(parents=True)
    _write_json(
        run / "runtime_manifest.json",
        {
            "experiment_id": "p0-real-001",
            "hardware_status": "synthetic" if synthetic else "real_arm",
            "device": "ArmPi Ultra",
        },
    )
    _write_json(
        run / "evaluation.json",
        {
            "experiment_id": "p0-real-001",
            "hardware_status": "synthetic" if synthetic else "real_arm_physical_outcome_verified",
            "evaluator_type": "vision",
            "evaluator_version": "fixture-v1",
            "outcome": {
                "object_grasped": True,
                "object_lifted": True,
                "object_placed": True,
            },
        },
    )
    _write_json(
        run / "bridge_result.json",
        {
            "experiment_id": "p0-real-001",
            "mode": "synthetic" if synthetic else "real_motion",
            "outcome": "completed",
        },
    )
    for name in ("before.jpg", "grasp_lift.jpg", "after.jpg"):
        (run / name).write_bytes(JPEG)
    (run / "wrapper.log").write_text(
        "synthetic execution\n" if synthetic else "real arm execution completed\n",
        encoding="utf-8",
    )
    if checksum:
        names = sorted(
            path.name
            for path in run.iterdir()
            if path.name != "SHA256SUMS.txt"
        )
        lines = []
        for name in names:
            digest = hashlib.sha256((run / name).read_bytes()).hexdigest()
            lines.append(f"{digest}  {name}")
        (run / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return run


class EvidencePackageValidatorTests(unittest.TestCase):
    def test_complete_real_p0_directory_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            _make_p0(Path(directory))
            result = validate_evidence_package(directory)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["package_type"], "directory")
        self.assertTrue(result["claims_supported"]["p0_success"])
        self.assertEqual(result["missing"], [])
        self.assertIn("success_p0/evaluation.json", result["files_checked"])

    def test_documentation_claim_without_raw_artifacts_is_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "hardware_sources.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("README.txt", "P0 success and p1_recovery_demonstrated")
                archive.writestr("source/p0_wrapper.py", "print('wrapper')\n")
            result = validate_evidence_package(package)

        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["package_type"], "zip")
        self.assertFalse(result["claims_supported"]["p0_success"])
        self.assertTrue(any("documentation claim" in warning for warning in result["warnings"]))
        self.assertTrue(any(path.endswith("evaluation.json") for path in result["missing"]))

    def test_explicit_synthetic_evidence_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            _make_p0(Path(directory), synthetic=True)
            result = validate_evidence_package(directory)

        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["claims_supported"]["p0_success"])
        self.assertTrue(any("mock/dry-run/synthetic" in warning for warning in result["warnings"]))

    def test_checksum_mismatch_is_reported_and_blocks_success(self):
        with tempfile.TemporaryDirectory() as directory:
            run = _make_p0(Path(directory))
            (run / "after.jpg").write_bytes(b"changed")
            result = validate_evidence_package(directory)

        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["claims_supported"]["p0_success"])
        self.assertTrue(any("checksum mismatch" in warning for warning in result["warnings"]))

    def test_targz_archive_is_read_without_extraction_and_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            _make_p0(source)
            archive_path = Path(directory) / "evidence.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(
                    source / "success_p0",
                    arcname="success_p0",
                    recursive=True,
                )
            result = validate_evidence_package(archive_path)

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["package_type"], "tar.gz")
        self.assertTrue(result["claims_supported"]["p0_success"])

    def test_standard_builder_layout_uses_embedded_checksums_and_versioned_names(self):
        with tempfile.TemporaryDirectory() as directory:
            # The package itself is often renamed during transfer; semantic
            # claim discovery must therefore also work without a success_* basename.
            package = Path(directory) / "delivery"
            (package / "evaluation").mkdir(parents=True)
            (package / "visual_evidence" / "exp-42").mkdir(parents=True)
            (package / "bridge").mkdir()
            evaluation = package / "evaluation" / "evaluation_exp-42.json"
            source_manifest = package / "evaluation" / "source_manifest.json"
            bridge_result = package / "bridge" / "bridge_result_exp-42.json"
            backend_log = package / "bridge" / "backend_exp-42.log"
            _write_json(
                evaluation,
                {
                    "experiment_id": "exp-42",
                    "evaluation_mode": "real_multistage_visual_evaluation",
                    "status": "real_visual_evaluation_passed",
                    "outcome": {
                        "object_grasped": True,
                        "object_lifted": True,
                        "object_placed": True,
                    },
                },
            )
            _write_json(
                source_manifest,
                {"experiment_id": "exp-42", "final_bridge_result": {"status": "completed"}},
            )
            _write_json(bridge_result, {"experiment_id": "exp-42", "status": "completed"})
            backend_log.write_text("real arm backend completed\n", encoding="utf-8")
            for stage in ("before", "grasp_lift", "after"):
                (package / "visual_evidence" / "exp-42" / f"{stage}_exp-42.jpg").write_bytes(JPEG)

            def digest(path: Path) -> str:
                return hashlib.sha256(path.read_bytes()).hexdigest()

            _write_json(
                package / "package_manifest.json",
                {
                    "package_type": "standard_successful_p0_package",
                    "experiment_id": "exp-42",
                    "packaged_paths": {
                        "evaluation_json": str(evaluation),
                        "source_manifest": str(source_manifest),
                        "bridge_result_json": str(bridge_result),
                        "backend_log": str(backend_log),
                    },
                    "checksums": {
                        "evaluation_json_sha256": digest(evaluation),
                        "source_manifest_sha256": digest(source_manifest),
                        "bridge_result_json_sha256": digest(bridge_result),
                        "backend_log_sha256": digest(backend_log),
                    },
                },
            )
            result = validate_evidence_package(package)

        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["claims_supported"]["p0_success"])
        self.assertEqual(result["missing"], [])

    def test_zip_path_traversal_is_invalid_and_not_extracted(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "unsafe.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("../outside.txt", "must not be written")
                archive.writestr("success_p0/evaluation.json", "{}")
            result = validate_evidence_package(package)
            self.assertFalse((Path(directory).parent / "outside.txt").exists())

        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("unsafe archive member path" in warning for warning in result["warnings"]))

    def test_tar_symlink_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "unsafe.tar"
            with tarfile.open(package, "w") as archive:
                info = tarfile.TarInfo("success_p0/link.log")
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                archive.addfile(info)
            result = validate_evidence_package(package)

        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("link archive member" in warning for warning in result["warnings"]))

    def test_p1_claim_requires_p0_id_candidate_and_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = _make_p0(root)
            p1 = root / "p1"
            p1.mkdir()
            # Copy the core artifacts under p1 and add the P1-specific records.
            for name in (
                "runtime_manifest.json",
                "evaluation.json",
                "bridge_result.json",
                "before.jpg",
                "grasp_lift.jpg",
                "after.jpg",
                "wrapper.log",
            ):
                data = (run / name).read_bytes()
                if name == "runtime_manifest.json":
                    payload = json.loads(data)
                    payload["experiment_id"] = "p1-real-001"
                    data = json.dumps(payload).encode()
                elif name == "evaluation.json":
                    payload = json.loads(data)
                    payload["experiment_id"] = "p1-real-001"
                    data = json.dumps(payload).encode()
                elif name == "bridge_result.json":
                    payload = json.loads(data)
                    payload["experiment_id"] = "p1-real-001"
                    data = json.dumps(payload).encode()
                (p1 / name).write_bytes(data)
            (p1 / "p0_experiment_id.txt").write_text("p0-real-001\n", encoding="utf-8")
            _write_json(p1 / "candidate_skill.json", {"source_experiment_id": "p0-real-001"})
            _write_json(p1 / "comparison.json", {"p0_experiment_id": "p0-real-001", "improved": True})
            (p1 / "SHA256SUMS.txt").write_text(
                "\n".join(
                    f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
                    for path in sorted(p1.iterdir())
                    if path.name != "SHA256SUMS.txt"
                )
                + "\n",
                encoding="utf-8",
            )
            result = validate_evidence_package(root)

        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["claims_supported"]["p1_recovery"])

    def test_alias_class_uses_same_read_only_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            _make_p0(Path(directory))
            result = EvidencePackageValidator().validate(directory)
        self.assertEqual(result["status"], "passed")


if __name__ == "__main__":
    unittest.main()
