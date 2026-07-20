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
