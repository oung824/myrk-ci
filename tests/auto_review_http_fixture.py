"""Local HTTP provider fixture. Tests configure only their loopback receiver."""
from pathlib import Path
import sys
from pydantic_ai import models
from myrk.desktop_service import main
models.ALLOW_MODEL_REQUESTS = True
if __name__ == '__main__': main(data_directory=Path(sys.argv[1]))
