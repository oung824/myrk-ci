from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import csv
import io
import json
import sys

raw = Path(sys.argv[1]).read_bytes()
assert raw == b'item,amount\napple,0.10\npear,0.20\nrefund,-0.05\n'
rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8'))))
assert sum(Decimal(row['amount']) for row in rows) == Decimal('0.25')
p = Path('runs.txt')
p.write_bytes(p.read_bytes() + b'x' if p.exists() else b'x')
print(json.dumps({'checked_sha256': sha256(raw).hexdigest(), 'size_bytes': len(raw), 'result': 'passed'}))
