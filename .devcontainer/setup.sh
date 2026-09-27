#!/usr/bin/env bash
# Codespaces の作成時に一度だけ動く：部品のインストール → テーブル作成 → 成分表の取り込み → 試用データの作成
set -euo pipefail
pip install --no-cache-dir -r requirements.txt
python manage.py migrate --noinput
python manage.py import_food_composition
python manage.py setup_demo
