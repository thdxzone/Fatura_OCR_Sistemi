#!/bin/zsh
# Python paketleri bu bilgisayara uygun yeni bir sanal ortama kurulur.
cd -- "$(dirname -- "$0")" || exit 1
python3.12 setup_local.py
