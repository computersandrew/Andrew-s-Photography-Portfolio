#!/bin/bash
cd "$(dirname "$0")"
python3 build.py
echo
read -n 1 -s -r -p "Done. Press any key to close."
