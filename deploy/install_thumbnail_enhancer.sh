#!/bin/bash
# Install OpenCV super-resolution model files for thumbnail enhancement.
# Idempotent — safe to run on every deploy.
#
# Models live in <PROJECT_ROOT>/models/ and are referenced from config.json:
#   thumbnail.enhance.model_path = "models/EDSR_x4.pb"
#
# We download both EDSR_x4 (best quality, ~38MB) and FSRCNN_x4 (fast fallback, ~50KB)
# so users can switch in config.json without re-running this script.

set -e

PROJECT_ROOT="${PROJECT_ROOT:-/opt/abuhafs/youtube-auto-uploader}"
MODELS_DIR="$PROJECT_ROOT/models"

mkdir -p "$MODELS_DIR"

# (filename, primary URL, mirror URL, min size in bytes)
declare -a MODELS=(
    "EDSR_x4.pb|https://github.com/Saafke/EDSR_Tensorflow/raw/master/models/EDSR_x4.pb|https://github.com/Saafke/EDSR_Tensorflow/raw/refs/heads/master/models/EDSR_x4.pb|30000000"
    "EDSR_x2.pb|https://github.com/Saafke/EDSR_Tensorflow/raw/master/models/EDSR_x2.pb|https://github.com/Saafke/EDSR_Tensorflow/raw/refs/heads/master/models/EDSR_x2.pb|30000000"
    "FSRCNN_x4.pb|https://github.com/Saafke/FSRCNN_Tensorflow/raw/master/models/FSRCNN_x4.pb|https://github.com/Saafke/FSRCNN_Tensorflow/raw/refs/heads/master/models/FSRCNN_x4.pb|20000"
    "FSRCNN_x2.pb|https://github.com/Saafke/FSRCNN_Tensorflow/raw/master/models/FSRCNN_x2.pb|https://github.com/Saafke/FSRCNN_Tensorflow/raw/refs/heads/master/models/FSRCNN_x2.pb|20000"
    "ESPCN_x4.pb|https://github.com/fannymonori/TF-ESPCN/raw/master/export/ESPCN_x4.pb|https://github.com/fannymonori/TF-ESPCN/raw/refs/heads/master/export/ESPCN_x4.pb|50000"
)

download_model() {
    local name="$1"
    local primary="$2"
    local mirror="$3"
    local min_size="$4"
    local target="$MODELS_DIR/$name"

    # Skip if already there with reasonable size
    if [ -f "$target" ]; then
        local cur_size
        cur_size=$(stat -c '%s' "$target" 2>/dev/null || stat -f '%z' "$target")
        if [ "$cur_size" -ge "$min_size" ]; then
            echo "✓ $name already installed (${cur_size} bytes)"
            return 0
        else
            echo "⚠ $name exists but too small (${cur_size} bytes < ${min_size}) — re-downloading"
            rm -f "$target"
        fi
    fi

    echo "↓ Downloading $name..."
    if curl -sLf -o "$target" "$primary"; then
        local sz
        sz=$(stat -c '%s' "$target" 2>/dev/null || stat -f '%z' "$target")
        if [ "$sz" -ge "$min_size" ]; then
            echo "✓ Downloaded $name (${sz} bytes)"
            return 0
        fi
    fi

    # Try mirror
    echo "  primary failed, trying mirror..."
    rm -f "$target"
    if curl -sLf -o "$target" "$mirror"; then
        local sz
        sz=$(stat -c '%s' "$target" 2>/dev/null || stat -f '%z' "$target")
        if [ "$sz" -ge "$min_size" ]; then
            echo "✓ Downloaded $name from mirror (${sz} bytes)"
            return 0
        fi
    fi

    echo "✗ Failed to download $name"
    rm -f "$target"
    return 1
}

FAILED=0
for entry in "${MODELS[@]}"; do
    IFS='|' read -r name primary mirror min_size <<< "$entry"
    if ! download_model "$name" "$primary" "$mirror" "$min_size"; then
        FAILED=$((FAILED + 1))
    fi
done

echo ""
echo "Models directory: $MODELS_DIR"
ls -lh "$MODELS_DIR" 2>/dev/null | grep -v '^total' || true

if [ "$FAILED" -gt 0 ]; then
    echo ""
    echo "⚠ $FAILED model(s) failed to download. Pipeline will fall back to original frame for those."
    exit 1
fi

echo ""
echo "✓ All super-resolution models installed."
