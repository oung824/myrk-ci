from pathlib import Path
import sys
import time

Path(sys.argv[1]).write_bytes(b'partial-before-error-4713\r\n')
print('partial-stdout-4713', flush=True)
print('partial-stderr-4713', file=sys.stderr, flush=True)
if sys.argv[2] == 'wait':
    time.sleep(60)
raise SystemExit(7)
