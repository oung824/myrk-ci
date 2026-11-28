from pathlib import Path
import sys
import time

Path(sys.argv[1]).write_bytes(b'partial-before-stop-6351\r\n')
print('partial-stdout-6351', flush=True)
time.sleep(60)
Path(sys.argv[2]).write_bytes(b'must-not-complete')
