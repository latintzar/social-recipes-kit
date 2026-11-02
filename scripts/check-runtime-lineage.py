"""Verify every recovered public source byte against the recorded release manifest."""

import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / "provenance/original-runtime.json").read_text())
expected = set()
for row in manifest["files"]:
    path = (root / row["public_path"]).resolve()
    assert path.is_relative_to(root / "original-runtime"), row["public_path"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row["public_sha256"], row[
        "public_path"
    ]
    expected.add(path)
actual = {
    p.resolve()
    for p in (root / "original-runtime").rglob("*")
    if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc")
}
assert actual == expected, {
    "missing": list(map(str, expected - actual)),
    "unlisted": list(map(str, actual - expected)),
}
print(f"Verified {len(expected)} recovered files from {manifest['source_revision']}")
