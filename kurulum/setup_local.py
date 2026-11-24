"""Yeni bilgisayarda platforma özel PaddleOCR sanal ortamını oluşturur."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not (3, 10) <= sys.version_info[:2] <= (3, 12):
        raise SystemExit(
            "PaddleOCR kurulumu için Python 3.10–3.12 kullanın; önerilen: 3.12."
        )
    target = ROOT / ".venv-ocr"
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print("Yerel Python ortamı hazırlanıyor…", flush=True)
        venv.EnvBuilder(with_pip=True).create(target)
    # Sanal ortam kopyalanmaz: paketler hedef bilgisayarın mimarisine göre kurulur.
    subprocess.run(
        [str(python), "-m", "pip", "install", "--upgrade", "pip"], check=True
    )
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "-r",
            str(ROOT / "kurulum" / "requirements-paddle.txt"),
        ],
        check=True,
    )
    subprocess.run(
        [
            str(python),
            "-c",
            "import paddle, paddleocr, pdfplumber; from PIL import Image",
        ],
        check=True,
    )
    print("PaddleOCR ortamı hazır.")
    if not shutil.which("pdftoppm"):
        print("PDF okuma için ayrıca Poppler kurup pdftoppm komutunu PATH'e ekleyin.")
    if not shutil.which("tesseract"):
        print(
            "İsteğe bağlı Tesseract kurulu değil; küçük miktar hücresi yardımcı okuması kullanılmaz."
        )
    print("Başlatma: python kurulum/start_local.py --open")


if __name__ == "__main__":
    main()
