"""Append-only JSON experiment packages for reproducible S0/S1 evidence."""

import json
from pathlib import Path
from typing import Union

from roboscientist.schemas import (
    ExperimentPlan,
    ExperimentResult,
    FailureAnalysisResult,
    SkillVersion,
)


class ExperimentStore:
    def __init__(self, root: Union[Path, str] = "data"):
        self.root = Path(root)

    @staticmethod
    def _write_new(path: Path, payload: dict) -> None:
        if path.exists():
            raise FileExistsError(f"refusing to overwrite experiment evidence: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_plan(self, plan: ExperimentPlan) -> Path:
        path = self.root / "experiments" / plan.experiment_id / "plan.json"
        self._write_new(path, plan.model_dump(mode="json"))
        return path

    def write_result(self, result: ExperimentResult) -> Path:
        path = self.root / "experiments" / result.experiment_id / "result.json"
        self._write_new(path, result.model_dump(mode="json"))
        return path

    def write_analysis(self, analysis: FailureAnalysisResult) -> Path:
        path = self.root / "experiments" / analysis.experiment_id / "analysis.json"
        self._write_new(path, analysis.model_dump(mode="json"))
        return path

    def write_skill(self, skill: SkillVersion) -> Path:
        path = self.root / "skills" / f"{skill.version}.json"
        self._write_new(path, skill.model_dump(mode="json"))
        return path

    def write_campaign(self, campaign_id: str, payload: dict) -> Path:
        """Write the campaign summary once; individual evidence remains append-only."""
        path = self.root / "campaigns" / campaign_id / "campaign.json"
        self._write_new(path, payload)
        return path

    def write_campaign_qwen_evidence(self, campaign_id: str, phase: str, payload: dict) -> dict:
        """Persist request, response and metadata without ever storing credentials."""
        if phase not in {"planning", "adjustment"}:
            raise ValueError("Qwen evidence phase must be planning or adjustment")
        directory = self.root / "campaigns" / campaign_id / "qwen"
        directory.mkdir(parents=True, exist_ok=True)
        written = {}
        for name in ("request", "response", "metadata", "output"):
            value = payload.get(name)
            if value is None:
                continue
            path = directory / f"{phase}_{name}.json"
            self._write_new(path, value if isinstance(value, dict) else {"value": value})
            written[name] = str(path)
        # Error records have no output and should still be independently visible.
        if "error" in payload:
            path = directory / f"{phase}_error.json"
            self._write_new(path, {"error": str(payload["error"])})
            written["error"] = str(path)
        metadata_path = directory / f"{phase}_metadata.json"
        if not metadata_path.exists():
            metadata = payload.get("metadata", {})
            self._write_new(metadata_path, metadata if isinstance(metadata, dict) else {"value": metadata})
        written["metadata"] = str(metadata_path)
        return written

    def read_campaign(self, campaign_id: str) -> dict:
        path = self.root / "campaigns" / campaign_id / "campaign.json"
        if not path.is_file():
            raise FileNotFoundError(f"campaign does not exist: {campaign_id}")
        campaign = json.loads(path.read_text(encoding="utf-8"))
        qwen_dir = path.parent / "qwen"
        qwen = {}
        if qwen_dir.is_dir():
            for evidence in sorted(qwen_dir.glob("*.json")):
                qwen[evidence.name] = json.loads(evidence.read_text(encoding="utf-8"))
        campaign["qwen_evidence"] = qwen
        return campaign

    def ensure_skill(self, skill: SkillVersion) -> Path:
        """Archive a stable input version once without rewriting prior evidence."""
        path = self.root / "skills" / f"{skill.version}.json"
        if not path.exists():
            self.write_skill(skill)
        return path

    def read_experiment(self, experiment_id: str) -> dict:
        directory = self.root / "experiments" / experiment_id
        if not directory.is_dir():
            raise FileNotFoundError(f"experiment does not exist: {experiment_id}")

        def read_json(name: str):
            path = directory / name
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

        return {
            "plan": read_json("plan.json"),
            "result": read_json("result.json"),
            "analysis": read_json("analysis.json"),
        }

    def read_skill(self, version: str) -> SkillVersion:
        path = self.root / "skills" / f"{version}.json"
        return SkillVersion.model_validate_json(path.read_text(encoding="utf-8"))

    def list_skills(self) -> list:
        directory = self.root / "skills"
        if not directory.exists():
            return []
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json"))
        ]
