#!/bin/bash
# Double-click to rebuild the site and push it to the home server.
cd "$(dirname "$0")"

# ---- EDIT THESE TWO LINES ---------------------------------
SERVER="user@homepc"          # your Ubuntu login @ its Tailscale name (or 100.x.x.x address)
REMOTE_DIR="/var/www/portfolio/"
# -----------------------------------------------------------

if [ "$SERVER" = "user@homepc" ]; then
  echo "Open publish.command in a text editor and set SERVER first."
  read -n 1 -s -r -p "Press any key to close..."; exit 1
fi

echo "Building..."
python3 build.py || { echo "Build failed."; read -n 1 -s -r -p "Press any key to close..."; exit 1; }

echo; echo "Uploading to $SERVER ..."
rsync -av --progress --delete \
  --exclude ".DS_Store" --exclude "__pycache__" \
  --exclude "*.command" --exclude "build.py" --exclude "settings.csv" \
  --exclude "/.git/" --exclude ".gitignore" --exclude ".gitattributes" --exclude "/blog/" --exclude "links.csv" \
  ./ "$SERVER:$REMOTE_DIR"

echo; echo "Done."
read -n 1 -s -r -p "Press any key to close..."
