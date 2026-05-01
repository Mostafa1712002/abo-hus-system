#!/bin/bash
# Ensure Arabic font is in place for thumbnail generation.
# Idempotent — safe to run on every deploy.

set -e

PROJECT_ROOT="${PROJECT_ROOT:-/opt/abuhafs/youtube-auto-uploader}"
FONT_DIR="$PROJECT_ROOT/fonts"
TARGET="$FONT_DIR/Cairo-Bold.ttf"

mkdir -p "$FONT_DIR"

# If target already exists with a reasonable size, skip
if [ -f "$TARGET" ] && [ "$(stat -c '%s' "$TARGET" 2>/dev/null || stat -f '%z' "$TARGET")" -gt 100000 ]; then
    echo "✓ Font already installed: $TARGET ($(stat -c '%s' "$TARGET" 2>/dev/null) bytes)"
    exit 0
fi

# Try to copy from system fonts (Linux)
SYSTEM_NASKH="/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf"
if [ -f "$SYSTEM_NASKH" ]; then
    cp "$SYSTEM_NASKH" "$TARGET"
    echo "✓ Copied system Naskh font → $TARGET"
    exit 0
fi

# Try alternate Arabic Bold fonts
for SRC in \
    /usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf \
    /usr/share/fonts/truetype/noto/NotoKufiArabic-Bold.ttf \
    /System/Library/Fonts/Supplemental/Arial.ttf
do
    if [ -f "$SRC" ]; then
        cp "$SRC" "$TARGET"
        echo "✓ Copied $SRC → $TARGET"
        exit 0
    fi
done

# Last resort: download from Google Fonts (uses fonts.gstatic.com)
echo "No system Arabic font found, downloading Cairo from Google Fonts..."
curl -sL -o "$TARGET" "https://fonts.gstatic.com/s/cairo/v28/SLXgc1nY6HkvalIhTpumxdt0UX8.ttf"
if file "$TARGET" 2>/dev/null | grep -qi "TrueType"; then
    echo "✓ Downloaded Cairo-Bold.ttf"
else
    echo "✗ Failed to obtain a usable Arabic font. Manual install required."
    rm -f "$TARGET"
    exit 1
fi
