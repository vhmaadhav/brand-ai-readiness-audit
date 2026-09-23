"""The shared run directory that lets the skills compose.

Each skill writes one JSON artefact into ``.audit/<run-id>/`` and the
orchestrator reads them back. Passing data through the filesystem rather than
in-process keeps every skill independently runnable and independently
debuggable — you can run one detector on its own and inspect exactly what it
concluded.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LAYER_FILES = {
    "preflight": "01-url-validation.json",
    "reachability": "02-crawl-reachability.json",
    "selection": "03-structured-data-identity.json",
    "absorption": "04-evidence-density.json",
    "trust": "05-freshness-corroboration.json",
    "engagement": "06-engagement-audit.json",
    "advisory": "07-advisory-review.json",
    "review": "08-review.json",
    "review_summary": "09-review-summary.json",
}


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "-", value.lower()).strip("-")[:60] or "site"


@dataclass
class Workspace:
    """One audit run's working directory."""

    root: Path
    run_id: str

    @classmethod
    def create(cls, site: str, base: str | os.PathLike | None = None) -> "Workspace":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{slugify(site)}-{stamp}"
        root = Path(base or Path.cwd() / ".audit") / run_id
        root.mkdir(parents=True, exist_ok=True)
        return cls(root=root, run_id=run_id)

    @classmethod
    def open(cls, path: str | os.PathLike) -> "Workspace":
        root = Path(path).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"no audit workspace at {root}")
        return cls(root=root, run_id=root.name)

    @classmethod
    def latest(cls, base: str | os.PathLike | None = None) -> "Workspace":
        """Open the most recent run, so skills chain without passing paths."""
        base_path = Path(base or Path.cwd() / ".audit")
        runs = sorted((p for p in base_path.glob("*") if p.is_dir()), key=lambda p: p.stat().st_mtime)
        if not runs:
            raise FileNotFoundError(
                f"no audit runs under {base_path}. Run the url-validation skill first."
            )
        return cls(root=runs[-1], run_id=runs[-1].name)

    # ------------------------------------------------------------------

    def path_for(self, layer: str) -> Path:
        return self.root / LAYER_FILES.get(layer, f"{slugify(layer)}.json")

    def write(self, layer: str, payload: dict) -> Path:
        target = self.path_for(layer)
        with target.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        return target

    def read(self, layer: str) -> dict | None:
        target = self.path_for(layer)
        if not target.is_file():
            return None
        try:
            with target.open(encoding="utf-8") as handle:
                return json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{target} is not valid JSON: {exc}") from exc

    def require(self, layer: str) -> dict:
        payload = self.read(layer)
        if payload is None:
            raise FileNotFoundError(
                f"layer {layer!r} has not run yet (expected {self.path_for(layer)})"
            )
        return payload

    def available_layers(self) -> list[str]:
        return [name for name in LAYER_FILES if self.path_for(name).is_file()]

    def findings_from(self, layer: str) -> list[dict]:
        payload = self.read(layer)
        return list(payload.get("findings", [])) if payload else []
