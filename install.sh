#!/usr/bin/env bash
# Create the Python environment for the demo GUIs (Ubuntu 22.04 / 24.04, Python >= 3.10).
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
echo "Done. Try:  .venv/bin/python -m same_laptop.mockup"
