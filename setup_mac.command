#!/bin/bash
set -euo pipefail

project_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$project_dir"

if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is not installed."
  echo "Open https://brew.sh/ and install Homebrew, then run this file again."
  exit 1
fi

echo "Installing Python and FFmpeg..."
brew install python ffmpeg

echo "Creating the private Python environment..."
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install --no-deps --no-build-isolation -e .

if [ ! -f .env ]; then
  cp .env.example .env
fi

echo
echo "Setup complete."
echo "Next, add your Spotify Client ID to:"
echo "$project_dir/.env"
echo
echo "Then run:"
echo "source .venv/bin/activate"
echo "python spotify_sync.py"
