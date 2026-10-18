#!/usr/bin/env python3
# SPDX-License-Identifier: MPL-2.0
"""Record installed, locked distributions using their upstream wheel metadata."""

import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
rows = []
for line in (root / "requirements.lock").read_text().splitlines():
    if not line or line.startswith("#"):
        continue
    name, version = line.split("==")
    dist = metadata.distribution(name)
    if dist.version != version:
        raise ValueError("Install exact requirements.lock first: " + name)
    license_expression = dist.metadata.get("License-Expression")
    if not license_expression:
        license_expression = "; ".join(
            x.split(" :: ")[-1]
            for x in dist.metadata.get_all("Classifier", [])
            if x.startswith("License ::")
        )
    if not license_expression:
        license_expression = dist.metadata.get("License", "UNDECLARED; review license files")
        if len(license_expression) > 150:
            license_expression = "See bundled license text (manifest hashes)"
    files = []
    for entry in dist.files or []:
        if any(
            part.lower()
            in {
                "licenses",
                "license",
                "license.txt",
                "license.md",
                "copying",
                "notice",
                "notice.txt",
            }
            for part in entry.parts
        ):
            path = Path(dist.locate_file(entry))
            if path.is_file():
                files.append(
                    {"path": str(entry), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                )
    rows.append(
        {
            "name": dist.metadata["Name"],
            "version": dist.version,
            "requires_python": dist.metadata.get("Requires-Python"),
            "license_metadata": license_expression,
            "source_metadata": dist.metadata.get_all("Project-URL", []),
            "license_files": files,
        }
    )
(root / "licenses").mkdir(exist_ok=True)
(root / "licenses/python-dependencies.json").write_text(
    json.dumps(
        {
            "checked_at": "2026-10-18",
            "basis": "Installed upstream metadata matching requirements.lock; not a legal compatibility opinion",
            "packages": rows,
        },
        indent=2,
        ensure_ascii=False,
    )
    + "\n"
)
print(f"Recorded {len(rows)} locked distributions")
