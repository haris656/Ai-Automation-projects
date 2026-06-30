#!/usr/bin/env bash
# update_engines.sh
# Run daily via cron to keep all extraction engines current.
# Add to crontab:  0 3 * * * /path/to/update_engines.sh >> /var/log/instafetch_update.log 2>&1

set -euo pipefail
echo "=== InstaFetch engine update — $(date) ==="

# 1. Update yt-dlp (the most important one — Instagram changes break it fastest)
echo "Updating yt-dlp..."
pip install --upgrade yt-dlp --break-system-packages -q
python -c "import yt_dlp; print('  yt-dlp', yt_dlp.version.__version__, '✓')"

# 2. Smoke test — try extracting info from a known public Instagram reel.
#    If this fails, send a webhook alert so you can investigate before users notice.
TEST_URL="https://www.instagram.com/reel/C7Mq8Nop8pW/"   # replace with a stable public reel
echo "Smoke-testing extraction..."
if python -c "
import yt_dlp, sys
with yt_dlp.YoutubeDL({'quiet':True,'no_warnings':True}) as ydl:
    info = ydl.extract_info('${TEST_URL}', download=False)
    assert info.get('id'), 'No ID returned'
print('  Extraction OK, id=', info['id'])
"; then
    echo "  ✓ All engines healthy"
else
    echo "  ✗ EXTRACTION FAILED — sending alert"
    # Webhook alert (replace with your Slack/Discord/PagerDuty URL)
    curl -s -X POST "${ALERT_WEBHOOK_URL:-}" \
        -H "Content-Type: application/json" \
        -d '{"text":"⚠️ InstaFetch: yt-dlp extraction failed after update. Manual check needed."}' || true
fi

echo "=== Update complete ==="
