"""Independent final check of both child and parent-completed files."""
import hashlib
import json
from pathlib import Path
import runpy
artifact=Path("lines.py")
assert artifact.read_bytes()==b"def total(values):\n    return sum(values)\n"
total=runpy.run_path(str(artifact))["total"]
for values, expected in [([],0),([1],1),([-2,3],1),([0,0],0)]:
    assert total(values)==expected
remainder=Path("remainder.txt")
assert remainder.read_bytes()==b"parent-finished"
print(json.dumps({"cases":4,"sha256":hashlib.sha256(artifact.read_bytes()).hexdigest(),"remainder_sha256":hashlib.sha256(remainder.read_bytes()).hexdigest()}))
