from hashlib import sha256
from pathlib import Path
import json
import sys

raw = Path(sys.argv[1]).read_bytes()
digest = sha256(raw).hexdigest()
assert digest == sys.argv[2], 'The checked source differs from the independently frozen original.'
print(json.dumps({'checked_sha256': digest, 'size_bytes': len(raw), 'result': 'passed'}))
