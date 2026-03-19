# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

CFSync is a local web application for tracking 3D printer filament/spool usage, built for Creality K2 Plus CFS (4x4 slot grid) and Klipper/Moonraker-based printers. It runs as a FastAPI backend with a vanilla JavaScript SPA frontend.

## Development Commands

```bash
# Setup
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run development server (with hot-reload)
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Health check
curl http://localhost:8000/api/health
```

### Mock printer server (no real hardware needed)

`mock_printer.py` simulates one or more K2 Plus printers (WebSocket CFS server on port 9999 + Moonraker HTTP on port 7125). Run multiple instances on different loopback IPs to test multi-printer setup:

```bash
# Terminal 1
python3 mock_printer.py --host 127.0.0.2 --name "Printer Alpha"
# Terminal 2
python3 mock_printer.py --host 127.0.0.3 --name "Printer Beta"
```

Then set `"printer_urls": ["127.0.0.2", "127.0.0.3"]` in `data/config.json`.

Control API (same port 7125):
```bash
curl http://127.0.0.2:7125/mock/state
curl -X POST http://127.0.0.2:7125/mock/print/start
curl -X POST http://127.0.0.2:7125/mock/print/complete
curl -X POST http://127.0.0.2:7125/mock/slot/1A \
     -H 'Content-Type: application/json' \
     -d '{"state": 2, "type": "PLA", "color": "#FF0000", "name": "Red PLA", "vendor": "Bambu"}'
```

SSH auto-linking can also be tested without a real SSH server using `mock_sshpass.sh`. Copy it to `sshpass` and put it on PATH before starting CFSync:

```bash
cp mock_sshpass.sh sshpass && chmod +x sshpass
PATH="$PWD:$PATH" uvicorn main:app --reload --host 0.0.0.0 --port 8000
# Set rfid to a plain integer matching a Spoolman spool ID to trigger auto-link:
curl -X POST http://127.0.0.2:7125/mock/slot/1A \
     -H 'Content-Type: application/json' \
     -d '{"state": 2, "rfid": "42"}'
```

There are no automated tests, linting tools, or CI/CD pipelines configured.

## Architecture

**Backend:** Single-file FastAPI app (`main.py`, ~1500 lines) with Pydantic models in `models/schemas.py`. Data is persisted as JSON files in `data/` (state.json, config.json, profiles.json) — no database.

**Frontend:** Vanilla JS SPA in `static/` (index.html, app.js, app.css, style.css). No build step, no framework — pure DOM manipulation. `fluidd-panel.js` is a standalone script injected into the Fluidd UI via bookmarklet or Tampermonkey userscript (generated from the settings page).

**Moonraker integration:** Optional async background polling loop that queries the printer's Moonraker API for print job status, filament usage, and CFS slot info. Includes Creality K2 Plus-specific object parsing (box.T1-T4, filament_rack). Printer identity (`printer_name`, `printer_firmware`) is parsed from the Moonraker WebSocket.

**Multi-printer support:** Multiple printers are stored as separate entries under `state["printers"][printer_id]`. Each printer runs its own independent WebSocket loop and Moonraker poll loop. Config accepts `printer_urls` (array of IPs/hostnames) or the legacy `printer_url` (single), both are migrated into the same internal `printers` list at startup.

## Key Patterns

- **Pydantic v1/v2 compatibility:** Helper functions `_model_dump()`, `_model_validate()`, `_req_dump()` abstract over version differences. Always use these instead of calling `.dict()` or `.model_dump()` directly.
- **State migration:** `_migrate_state_dict()` handles legacy field names (e.g., `color` → `color_hex`, `vendor` → `manufacturer`) and older state.json formats.
- **Two API tiers:** `/api/*` returns raw JSON; `/api/ui/*` wraps responses in `{"result": {...}}` for the frontend.
- **Slot IDs:** Literal type `SlotId` = `"1A"` through `"4D"` (4 boxes × 4 colors, 16 total) plus `"SP"` for the printer's direct spool holder (box type=1 in the WebSocket protocol).
- **Spool epochs:** Incrementing `spool_epoch` counter tracks spool changes per slot, enabling per-spool history filtering.
- **History conventions:** `_hist_push()` prepends (newest-first); `_hist_upsert_by_src()` updates existing entries by source marker during live prints.
- **Internal functions** are prefixed with `_` (e.g., `_http_get_json`, `_hist_push`).
- **Filament calculation:** grams = density × π × (diameter/2)² × length, with material-specific density from profiles.json.

## Spoolman Integration (Optional)

Set `spoolman_url` in `data/config.json` to enable. This app acts as the only bridge between Spoolman and the printer (Moonraker's Spoolman plugin is not used). Spools are linked manually via the slot modal dropdown, or auto-linked via SSH serialNum lookup (see below). On link, `remaining_weight` is imported from Spoolman. Consumption is synced back via `PUT /api/v1/spool/{id}/use` (fire-and-forget) when prints finalize or manual allocations are made. Roll changes auto-unlink the Spoolman spool. All Spoolman calls are best-effort (`_spoolman_*` helpers) and never block local tracking.

**Auto-linking (SSH serialNum):** When a slot transitions to RFID state (CFS state=2), `_ssh_fetch_and_apply()` is triggered (at most every 30s per printer). It SSHes into the printer using `sshpass` + system `ssh`, tries multiple passwords (`creality_2023`, `creality_2024`, `creality`) and two file paths (stock firmware: `/usr/data/creality/userdata/box/material_box_info.json`; K2-Improvements: `/mnt/UDISK/creality/userdata/box/material_box_info.json`). Reads each slot's `serialNum` field; if it's a valid integer matching a Spoolman spool ID, that slot is auto-linked. Requires `sshpass` installed and SSH access to the printer.

**Auto-unlinking:** Spoolman is auto-unlinked when: (a) a slot transitions from RFID to any other state, (b) any loaded slot goes to empty, or (c) a loaded slot's material/name/vendor/color fingerprint changes (catches manual spool swaps).

**Spoolman API endpoints:** `GET /api/ui/spoolman/spools`, `POST /api/ui/spoolman/link`, `POST /api/ui/spoolman/unlink`, `GET /api/ui/spoolman/spool_detail`.

**Percentage calculation:** Remaining % is calculated the same way for all linked slots — using Spoolman's `remaining_weight` divided by the spool's initial weight (`filament.weight`). Cached per-slot with a 60s TTL (`_SPOOLMAN_PCT_TTL`).

## Production Deployment

Installs to `/opt/filament-management/` as a systemd service. See `install.sh`, `update.sh`, `uninstall.sh`, and `filament-management.service.example`.
