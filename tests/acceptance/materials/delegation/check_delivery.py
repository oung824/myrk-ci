"""Independent inputs and fixed content for the synthetic delivery."""

import hashlib
import json
from pathlib import Path
import runpy

artifact = Path("lines.py")
assert artifact.read_bytes() == b"def total(values):\n    return sum(values)\n"
total = runpy.run_path(str(artifact))["total"]
for values, expected in [([], 0), ([1], 1), ([-2, 3], 1), ([0, 0], 0)]:
    assert total(values) == expected
print(json.dumps({"cases": 4, "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}))
