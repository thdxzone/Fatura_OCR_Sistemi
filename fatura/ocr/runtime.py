"""PaddleOCR'ı ayrı Python sürecinde çalıştırır; sunucuda model belleği tutmaz."""

import json
import os
from pathlib import Path
import subprocess
import sys

from fatura.paths import PROJECT_ROOT


def model_python():
    """Kurulu model ortamını bulur; eksik kurulumda eski motora sessizce dönmez."""
    executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    configured = os.environ.get("FATURA_OCR_PYTHON")
    candidates = (
        [Path(configured)]
        if configured
        else [
            PROJECT_ROOT / ".venv-ocr" / executable,
            Path(sys.executable),
        ]
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        probe = subprocess.run(
            [
                str(candidate),
                "-c",
                "import importlib.util; assert all(importlib.util.find_spec(name) for name in ('paddleocr','paddle','pdfplumber','PIL'))",
            ],
            capture_output=True,
            timeout=15,
        )
        if probe.returncode == 0:
            return candidate
    raise RuntimeError(
        "PaddleOCR ortamı bulunamadı. kurulum/requirements-paddle.txt kurulumunu kontrol edin."
    )


def analyze_paddle(source, mime):
    source = Path(source).resolve()
    output = source.parent / "paddle-result.json"
    log = source.parent / "paddle-run.log"
    # Önceki denemeden kalan çıktı yeni denemenin sonucu kabul edilemez.
    output.unlink(missing_ok=True)
    with log.open("w", encoding="utf-8") as stream:
        subprocess.run(
            [
                str(model_python()),
                "-m",
                "fatura.ocr.worker",
                "--reader",
                "paddle",
                "--source",
                str(source),
                "--mime",
                mime,
                "--output",
                str(output),
            ],
            stdout=stream,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=900,
            cwd=PROJECT_ROOT,
        )
    result = json.loads(output.read_text(encoding="utf-8"))
    if result.get("reader") != "paddle":
        raise ValueError("PaddleOCR sonuç motoru doğrulanamadı.")
    return result
