"""Uygun yerel Python ortamını bulup web uygulamasını başlatır."""

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
candidates = [
    ROOT / ".venv-ocr" / executable,
    ROOT / ".venv" / executable,
    Path(sys.executable),
]
if sys.platform == "darwin":
    candidates.append(
        Path.home()
        / ".cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
    )
for candidate in candidates:
    if not candidate.is_file():
        continue
    check = subprocess.run(
        [str(candidate), "-c", "import pdfplumber; from PIL import Image"],
        capture_output=True,
    )
    if check.returncode == 0:
        os.chdir(ROOT)
        arguments = sys.argv[1:]
        os.execv(str(candidate), [str(candidate), "-m", "fatura.web.app", *arguments])
print(
    "Gerekli Python paketleri bulunamadı. Önce: python -m pip install -r kurulum/requirements.txt",
    file=sys.stderr,
)
sys.exit(1)
