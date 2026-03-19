#!/bin/sh
set -e

CONFIG_PATH="$APP_DIR/data/config.json"

# Only generate the file if it does not exist
if [ ! -f "$CONFIG_PATH" ]; then
    echo "Config not found. Generating from environment variables..."
    
    # Ensure the directory exists (important for mounts)
    mkdir -p "$(dirname "$CONFIG_PATH")"

    python3 -c "
import os, json

config = {
    \"printer_urls\": json.loads(os.getenv('PRINTER_URLS', '[]')),
    \"filament_diameter_mm\": float(os.getenv('FILAMENT_DIAMETER', '1.75')),
    \"spoolman_url\": os.getenv('SPOOLMAN_URL', ''),
    \"spoolman_mode\": os.getenv('SPOOLMAN_MODE', 'remote')
}

with open('$CONFIG_PATH', 'w') as f:
    json.dump(config, f, indent=4)
"
else
    echo "Config file already exists in volume, skipping generation."
fi

# Hand off to the CMD (uvicorn)
exec "$@"

