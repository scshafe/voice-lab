#!/bin/sh
# Fetch the whisper.cpp ggml model into the data root, verified against a pinned
# sha256. The model is not baked into the image (1.5 GB for medium): it lives on
# the data volume and is fetched once. Idempotent; a file that fails the check
# is deleted, never used.
#
#   docker run --rm -v voice-journey-data:/data <image> vj-fetch-model
#
# Override the model with VJ_WHISPER_MODEL_FILE / _URL / _SHA256 together
# (and point WHISPER_CPP_MODEL at it).
set -eu

DATA="${VOICE_JOURNEY_DATA:-/data}"
FILE="${VJ_WHISPER_MODEL_FILE:-ggml-medium.bin}"
URL="${VJ_WHISPER_MODEL_URL:-https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin}"
SHA256="${VJ_WHISPER_MODEL_SHA256:-6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208}"
DEST="$DATA/models/$FILE"

verify() { [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$SHA256" ]; }

mkdir -p "$DATA/models"
if [ -f "$DEST" ] && verify "$DEST"; then
  echo "model present and verified: $DEST"
  exit 0
fi
rm -f "$DEST" "$DEST.part"
curl --fail --location --silent --show-error --retry 3 --output "$DEST.part" "$URL"
if ! verify "$DEST.part"; then
  rm -f "$DEST.part"
  echo "sha256 mismatch for $URL (expected $SHA256); nothing installed" >&2
  exit 1
fi
mv "$DEST.part" "$DEST"
echo "installed and verified: $DEST"
