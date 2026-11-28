from pathlib import Path
import sys

Path(sys.argv[1]).write_bytes(b'actual-command-product-741\r\n')
print('actual-command-stdout-741')
print('actual-command-stderr-741', file=sys.stderr)
