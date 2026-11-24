#!/bin/zsh
# Bu dosyaya çift tıklamak uygulamayı yerelde başlatır.
cd -- "$(dirname -- "$0")" || exit 1
python3 start_local.py --open
