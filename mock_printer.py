#!/usr/bin/env python3
"""
mock_printer.py — Mock Creality K2 Plus / Moonraker server for testing CFSync.

Simulates:
  - WebSocket CFS server on port 9999  (boxsInfo, heartbeat protocol)
  - Moonraker HTTP API on port 7125    (print_stats, gcode/script)
  - Control API at /mock/*             (change state without a real printer)

Usage (run each in a separate terminal):
    python3 mock_printer.py --host 127.0.0.2 --name "Printer Alpha"
    python3 mock_printer.py --host 127.0.0.3 --name "Printer Beta"

Then in CFSync data/config.json:
    {"printer_urls": ["127.0.0.2", "127.0.0.3"]}

On Linux, all 127.x.x.x addresses work as loopback aliases without extra config.

Control API examples:
    curl http://127.0.0.2:7125/mock/state
    curl -X POST http://127.0.0.2:7125/mock/print/start
    curl -X POST http://127.0.0.2:7125/mock/print/complete
    curl -X POST http://127.0.0.2:7125/mock/print/cancel
    curl -X POST http://127.0.0.2:7125/mock/slot/1A \\
         -H 'Content-Type: application/json' \\
         -d '{"state": 2, "type": "PETG", "color": "#00FF00", "name": "Green PETG", "vendor": "Bambu"}'
    curl -X POST http://127.0.0.2:7125/mock/slot/1A/empty

SSH auto-linking (mocked without a real SSH server):
    # Run CFSync with mock_sshpass.sh on PATH so it intercepts SSH calls:
    PATH="$PWD:$PATH" uvicorn main:app --reload --host 0.0.0.0 --port 8000

    # Set slot RFID to a plain Spoolman spool ID integer to trigger auto-link:
    curl -X POST http://127.0.0.2:7125/mock/slot/1A \\
         -H 'Content-Type: application/json' \\
         -d '{"state": 2, "rfid": "42"}'  # links to Spoolman spool ID 42
"""

import argparse
import asyncio
import json
import time
from typing import Optional

import uvicorn
import websockets
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Default CFS slot configuration
# ---------------------------------------------------------------------------

def _default_slots() -> list[dict]:
    """Return a realistic 4-box CFS state with mixed slot occupancy."""
    materials = [
        # Box 1 — fully loaded with RFID spools
        {"box": 1, "id": 0, "state": 2, "type": "PLA",  "color": "#FF5733", "name": "Orange PLA",  "vendor": "Bambu",        "rfid": "AABBCC001", "selected": 1, "usedMaterialLength": 1250.5},
        {"box": 1, "id": 1, "state": 2, "type": "PLA",  "color": "#3375FF", "name": "Blue PLA",    "vendor": "Bambu",        "rfid": "AABBCC002", "selected": 0, "usedMaterialLength": 340.0},
        {"box": 1, "id": 2, "state": 2, "type": "PETG", "color": "#33FF57", "name": "Green PETG",  "vendor": "eSUN",         "rfid": "AABBCC003", "selected": 0, "usedMaterialLength": 820.0},
        {"box": 1, "id": 3, "state": 2, "type": "PETG", "color": "#FF33F5", "name": "Pink PETG",   "vendor": "eSUN",         "rfid": "AABBCC004", "selected": 0, "usedMaterialLength": 100.0},
        # Box 2 — 2 RFID, 2 empty
        {"box": 2, "id": 0, "state": 2, "type": "ABS",  "color": "#FFFFFF", "name": "White ABS",   "vendor": "Polymaker",    "rfid": "AABBCC005", "selected": 0, "usedMaterialLength": 500.0},
        {"box": 2, "id": 1, "state": 1, "type": "TPU",  "color": "#000000", "name": "Black TPU",   "vendor": "NinjaFlex",    "rfid": "",          "selected": 0, "usedMaterialLength": 60.0},
        {"box": 2, "id": 2, "state": 0, "type": "",     "color": "",        "name": "",            "vendor": "",             "rfid": "",          "selected": 0, "usedMaterialLength": 0.0},
        {"box": 2, "id": 3, "state": 0, "type": "",     "color": "",        "name": "",            "vendor": "",             "rfid": "",          "selected": 0, "usedMaterialLength": 0.0},
        # Box 3 — mostly empty
        {"box": 3, "id": 0, "state": 2, "type": "PLA",  "color": "#FFD700", "name": "Gold PLA",    "vendor": "Hatchbox",     "rfid": "AABBCC006", "selected": 0, "usedMaterialLength": 2000.0},
        {"box": 3, "id": 1, "state": 0, "type": "",     "color": "",        "name": "",            "vendor": "",             "rfid": "",          "selected": 0, "usedMaterialLength": 0.0},
        {"box": 3, "id": 2, "state": 0, "type": "",     "color": "",        "name": "",            "vendor": "",             "rfid": "",          "selected": 0, "usedMaterialLength": 0.0},
        {"box": 3, "id": 3, "state": 0, "type": "",     "color": "",        "name": "",            "vendor": "",             "rfid": "",          "selected": 0, "usedMaterialLength": 0.0},
        # Box 4 — all empty
        {"box": 4, "id": 0, "state": 0, "type": "",     "color": "",        "name": "",            "vendor": "",             "rfid": "",          "selected": 0, "usedMaterialLength": 0.0},
        {"box": 4, "id": 1, "state": 0, "type": "",     "color": "",        "name": "",            "vendor": "",             "rfid": "",          "selected": 0, "usedMaterialLength": 0.0},
        {"box": 4, "id": 2, "state": 0, "type": "",     "color": "",        "name": "",            "vendor": "",             "rfid": "",          "selected": 0, "usedMaterialLength": 0.0},
        {"box": 4, "id": 3, "state": 0, "type": "",     "color": "",        "name": "",            "vendor": "",             "rfid": "",          "selected": 0, "usedMaterialLength": 0.0},
    ]
    return materials


# ---------------------------------------------------------------------------
# Shared mock state (mutated by control API and simulated print loop)
# ---------------------------------------------------------------------------

class MockState:
    def __init__(self, name: str, firmware: str):
        self.name = name
        self.firmware = firmware

        # Flat list of material dicts: {"box": 1..4, "id": 0..3, "state", ...}
        self.materials: list[dict] = _default_slots()

        # Print job state ("standby" | "printing" | "paused" | "complete" | "error" | "cancelled")
        self.print_state: str = "standby"
        self.print_filename: str = ""
        self.filament_used_mm: float = 0.0
        self._print_started_at: Optional[float] = None

        # Connected WebSocket clients for pushing updates
        self._ws_clients: set = set()
        self._lock = asyncio.Lock()

    def _slot_key(self, box: int, mat_id: int) -> int:
        return (box - 1) * 4 + mat_id

    def get_material(self, box: int, mat_id: int) -> Optional[dict]:
        for m in self.materials:
            if m["box"] == box and m["id"] == mat_id:
                return m
        return None

    def set_material(self, slot_str: str, data: dict) -> bool:
        """Update a slot by slot string like '1A', '2C', 'SP'."""
        if slot_str == "SP":
            return False  # SP not supported in mock CFS
        if len(slot_str) != 2 or slot_str[0] not in "1234" or slot_str[1] not in "ABCD":
            return False
        box = int(slot_str[0])
        mat_id = "ABCD".index(slot_str[1])
        for m in self.materials:
            if m["box"] == box and m["id"] == mat_id:
                m.update(data)
                return True
        return False

    def build_boxes_info(self) -> dict:
        """Build the boxsInfo WS payload from current state."""
        # Group by box
        boxes_by_id: dict[int, list] = {1: [], 2: [], 3: [], 4: []}
        for m in self.materials:
            boxes_by_id[m["box"]].append({
                "id": m["id"],
                "state": m["state"],
                "type": m["type"],
                "color": m["color"],
                "name": m["name"],
                "vendor": m["vendor"],
                "rfid": m["rfid"],
                "selected": m["selected"],
                "usedMaterialLength": m["usedMaterialLength"],
            })

        material_boxs = []
        for box_id in [1, 2, 3, 4]:
            material_boxs.append({
                "type": 0,
                "id": box_id,
                "temp": 23.5 + box_id * 0.3,
                "humidity": 42.0 - box_id * 1.5,
                "materials": sorted(boxes_by_id[box_id], key=lambda x: x["id"]),
            })

        # Also add the direct spool holder (type=1) — always empty in mock
        material_boxs.append({
            "type": 1,
            "materials": [{"id": 0, "state": 0, "type": "", "color": "", "name": "",
                           "vendor": "", "rfid": "", "selected": 0, "usedMaterialLength": 0.0}],
        })

        return {
            "hostname": self.name,
            "softVersion": self.firmware,
            "boxsInfo": {
                "materialBoxs": material_boxs,
            },
        }

    def build_material_box_info(self) -> dict:
        """Build the SSH material_box_info.json format from current slot state.

        Used by the /mock/material_box_info endpoint and the fake sshpass script.
        serialNum is set to a fake integer for slots that have RFID (state=2).
        Override via POST /mock/slot/{id} with a real Spoolman spool ID to test auto-linking.
        """
        boxes: dict[str, list] = {}
        for m in self.materials:
            box_key = f"T{m['box']}"
            if box_key not in boxes:
                boxes[box_key] = []
            serial = ""
            # Only include serialNum for RFID slots (state=2); use RFID string hash as fake ID
            if m["state"] == 2 and m.get("rfid"):
                # If rfid looks like a pure integer, use it directly as the Spoolman spool ID
                rfid = m["rfid"].strip()
                serial = rfid if rfid.isdigit() else ""
            boxes[box_key].append({
                "materialId": "ABCD"[m["id"]],
                "serialNum": serial,
            })

        return {
            "Material": {
                "info": [
                    {"boxID": box_key, "list": items}
                    for box_key, items in sorted(boxes.items())
                ]
            }
        }

    def build_print_stats(self) -> dict:
        return {
            "result": {
                "status": {
                    "print_stats": {
                        "state": self.print_state,
                        "filament_used": self.filament_used_mm,
                        "filename": self.print_filename,
                        "job_name": self.print_filename,
                    }
                }
            }
        }

    async def start_print(self, filename: str = "mock_print.gcode") -> None:
        self.print_state = "printing"
        self.print_filename = filename
        self.filament_used_mm = 0.0
        self._print_started_at = time.time()
        print(f"[MOCK] ({self.name}) Print started: {filename}")

    async def complete_print(self) -> None:
        self.print_state = "complete"
        self._print_started_at = None
        print(f"[MOCK] ({self.name}) Print completed, filament_used={self.filament_used_mm:.1f}mm")

    async def cancel_print(self) -> None:
        self.print_state = "cancelled"
        self._print_started_at = None
        print(f"[MOCK] ({self.name}) Print cancelled")

    async def push_cfs_update(self) -> None:
        """Broadcast current boxsInfo to all connected WS clients."""
        if not self._ws_clients:
            return
        payload = json.dumps(self.build_boxes_info())
        dead = set()
        for ws in list(self._ws_clients):
            try:
                await ws.send(payload)
            except Exception:
                dead.add(ws)
        self._ws_clients -= dead


# ---------------------------------------------------------------------------
# Background task: simulate filament consumption during print
# ---------------------------------------------------------------------------

async def _simulate_print_loop(state: MockState) -> None:
    """Increment filament_used during an active print (simulates ~10mm/s usage)."""
    while True:
        await asyncio.sleep(2)
        if state.print_state == "printing":
            state.filament_used_mm += 20.0  # ~10mm/s × 2s tick
            # Also bump the active slot's usedMaterialLength
            for m in state.materials:
                if m["selected"] == 1 and m["state"] > 0:
                    m["usedMaterialLength"] += 0.02  # metres


# ---------------------------------------------------------------------------
# WebSocket server (port 9999) — mimics Creality K2 Plus CFS hub
# ---------------------------------------------------------------------------

async def _ws_handler(websocket, state: MockState) -> None:
    """Handle one WebSocket connection from CFSync."""
    print(f"[WS] ({state.name}) Client connected: {websocket.remote_address}")
    state._ws_clients.add(websocket)

    # Send initial printer identity message immediately on connect
    await websocket.send(json.dumps({
        "hostname": state.name,
        "softVersion": state.firmware,
    }))

    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw) if isinstance(raw, str) else {}
            except Exception:
                # Plain string messages like "ok"
                continue

            # Heartbeat handshake: {"ModeCode": "heart_beat"} → reply "ok"
            if msg.get("ModeCode") == "heart_beat":
                await websocket.send("ok")
                continue

            # boxsInfo request: {"method": "get", "params": {"boxsInfo": 1}}
            if msg.get("method") == "get" and (msg.get("params") or {}).get("boxsInfo"):
                await websocket.send(json.dumps(state.build_boxes_info()))
                continue

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        state._ws_clients.discard(websocket)
        print(f"[WS] ({state.name}) Client disconnected")


async def _ws_heartbeat_loop(state: MockState) -> None:
    """Periodically send a heartbeat ping and boxsInfo push to all WS clients."""
    while True:
        await asyncio.sleep(5)
        if state._ws_clients:
            heartbeat = json.dumps({"heart_beat": int(time.time())})
            dead = set()
            for ws in list(state._ws_clients):
                try:
                    await ws.send(heartbeat)
                    # Also push fresh CFS data right after heartbeat
                    await ws.send(json.dumps(state.build_boxes_info()))
                except Exception:
                    dead.add(ws)
            state._ws_clients -= dead


# ---------------------------------------------------------------------------
# FastAPI app: Moonraker mock + control API
# ---------------------------------------------------------------------------

def build_app(state: MockState) -> FastAPI:
    app = FastAPI(title=f"Mock Printer: {state.name}")

    # ---- Moonraker endpoints -----------------------------------------------

    @app.get("/printer/objects/query")
    async def query_printer_objects(request: Request):
        """Moonraker print_stats query — polled every 5s by CFSync."""
        return state.build_print_stats()

    @app.post("/printer/gcode/script")
    async def gcode_script(request: Request):
        """Accept gcode macros like SET_ACTIVE_SPOOL / CLEAR_ACTIVE_SPOOL."""
        try:
            body = await request.json()
            script = body.get("script", "")
        except Exception:
            script = ""
        print(f"[HTTP] ({state.name}) GCode script received: {script!r}")
        return {"result": "ok"}

    # ---- Control API -------------------------------------------------------

    @app.get("/mock/state")
    async def mock_get_state():
        """Return full mock state for inspection."""
        slots = {}
        for m in state.materials:
            slot_str = f"{m['box']}{'ABCD'[m['id']]}"
            slots[slot_str] = {k: v for k, v in m.items() if k not in ("box", "id")}
        return {
            "name": state.name,
            "firmware": state.firmware,
            "print_state": state.print_state,
            "print_filename": state.print_filename,
            "filament_used_mm": state.filament_used_mm,
            "ws_clients": len(state._ws_clients),
            "slots": slots,
        }

    @app.post("/mock/print/start")
    async def mock_start_print(request: Request):
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        filename = body.get("filename", "mock_print.gcode")
        await state.start_print(filename)
        await state.push_cfs_update()
        return {"ok": True, "print_state": state.print_state}

    @app.post("/mock/print/complete")
    async def mock_complete_print():
        await state.complete_print()
        return {"ok": True, "print_state": state.print_state}

    @app.post("/mock/print/cancel")
    async def mock_cancel_print():
        await state.cancel_print()
        return {"ok": True, "print_state": state.print_state}

    @app.post("/mock/print/pause")
    async def mock_pause_print():
        if state.print_state == "printing":
            state.print_state = "paused"
            print(f"[MOCK] ({state.name}) Print paused")
        return {"ok": True, "print_state": state.print_state}

    @app.post("/mock/print/resume")
    async def mock_resume_print():
        if state.print_state == "paused":
            state.print_state = "printing"
            print(f"[MOCK] ({state.name}) Print resumed")
        return {"ok": True, "print_state": state.print_state}

    @app.post("/mock/slot/{slot_id}")
    async def mock_set_slot(slot_id: str, request: Request):
        """
        Update a CFS slot. Body fields (all optional):
          state   : 0=empty, 1=manual, 2=RFID
          type    : "PLA" / "PETG" / "ABS" / ...
          color   : "#rrggbb"
          name    : spool name
          vendor  : manufacturer
          rfid    : RFID tag string (required for state=2)
          selected: 0 or 1
        """
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not state.set_material(slot_id.upper(), data):
            return JSONResponse({"error": f"unknown slot {slot_id!r}"}, status_code=404)
        await state.push_cfs_update()
        return {"ok": True, "slot": slot_id.upper()}

    @app.post("/mock/slot/{slot_id}/empty")
    async def mock_empty_slot(slot_id: str):
        """Convenience: mark a slot as empty."""
        if not state.set_material(slot_id.upper(), {
            "state": 0, "type": "", "color": "", "name": "",
            "vendor": "", "rfid": "", "selected": 0, "usedMaterialLength": 0.0,
        }):
            return JSONResponse({"error": f"unknown slot {slot_id!r}"}, status_code=404)
        await state.push_cfs_update()
        return {"ok": True, "slot": slot_id.upper()}

    @app.get("/mock/material_box_info")
    async def mock_material_box_info():
        """
        Returns the material_box_info.json structure that CFSync normally reads via SSH.

        Used by mock_sshpass.sh to intercept SSH calls without a real SSH server.
        For SSH auto-linking to work, set the rfid field to a plain integer matching
        the Spoolman spool ID:
            curl -X POST http://127.0.0.2:7125/mock/slot/1A \\
                 -H 'Content-Type: application/json' \\
                 -d '{"state": 2, "rfid": "42"}'
        """
        return state.build_material_box_info()

    @app.post("/mock/slot/{slot_id}/select")
    async def mock_select_slot(slot_id: str):
        """Set the active/selected slot (simulates CFS switching)."""
        # Deselect all first
        for m in state.materials:
            m["selected"] = 0
        if not state.set_material(slot_id.upper(), {"selected": 1}):
            return JSONResponse({"error": f"unknown slot {slot_id!r}"}, status_code=404)
        await state.push_cfs_update()
        return {"ok": True, "active_slot": slot_id.upper()}

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main(host: str, name: str, firmware: str) -> None:
    state = MockState(name=name, firmware=firmware)

    # Start background tasks
    asyncio.create_task(_simulate_print_loop(state))
    asyncio.create_task(_ws_heartbeat_loop(state))

    # Start WebSocket server on port 9999
    ws_server = await websockets.serve(
        lambda ws: _ws_handler(ws, state),
        host=host,
        port=9999,
    )
    print(f"[MOCK] WebSocket server listening on ws://{host}:9999")

    # Start FastAPI / Moonraker HTTP server on port 7125
    app = build_app(state)
    config = uvicorn.Config(app, host=host, port=7125, log_level="warning")
    server = uvicorn.Server(config)
    print(f"[MOCK] HTTP (Moonraker) server listening on http://{host}:7125")
    print(f"[MOCK] Control API: http://{host}:7125/mock/state")
    print(f"[MOCK] Printer name: {name!r}, firmware: {firmware!r}")
    print()

    try:
        await server.serve()
    finally:
        ws_server.close()
        await ws_server.wait_closed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock Creality K2 Plus / Moonraker server")
    parser.add_argument(
        "--host", default="127.0.0.2",
        help="IP address to bind to (default: 127.0.0.2). Use 127.0.0.3, etc. for more printers.",
    )
    parser.add_argument(
        "--name", default="Mock K2 Plus",
        help="Printer display name (default: 'Mock K2 Plus')",
    )
    parser.add_argument(
        "--firmware", default="1.3.5.22",
        help="Reported firmware version (default: 1.3.5.22)",
    )
    args = parser.parse_args()

    asyncio.run(main(host=args.host, name=args.name, firmware=args.firmware))
