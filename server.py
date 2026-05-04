"""
server.py — Servidor web para el visor 360° / video narrativo
=============================================================
Coloca este archivo en la MISMA carpeta que tus videos/imágenes.

Instalación:
    pip install fastapi uvicorn python-multipart

Uso:
    python server.py
    (luego exponer con ngrok: ngrok http 8000)
"""

import os
import re
import json
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ================================================================
#  CONFIGURACIÓN
# ================================================================
_BASE = os.path.dirname(os.path.abspath(__file__))
MEDIA_FOLDER = os.path.join(_BASE, "media")   # subcarpeta /media
COORDS_FILE  = os.path.join(MEDIA_FOLDER, "coords.json")

# Crear la carpeta si no existe (por si acaso)
os.makedirs(MEDIA_FOLDER, exist_ok=True)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ================================================================
#  VIDEO MAP (misma lógica que video_map.py original)
# ================================================================

def _count_commas(stem: str) -> int:
    return len(stem) - len(stem.rstrip(','))

def _stem_no_commas(stem: str) -> str:
    return stem.rstrip(',')

def parse_video_filename(filename: str):
    name, ext = os.path.splitext(filename)
    if ext.lower() not in (".mp4", ".jpg", ".jpeg", ".png", ".bmp"):
        return None
    commas = _count_commas(name)
    base   = _stem_no_commas(name)

    # Formato: 3.altaB  — num DOT path_id LETRA
    # La letra es exactamente UN carácter al final
    # Ejemplos: 1.0J  → (1, "0", "J")
    #           3.altaB → (3, "alta", "B")
    m = re.fullmatch(r"(\d+)\.(.+?)([A-Za-z])$", base)
    if m:
        num_part = int(m.group(1))
        pid_part = m.group(2)
        let_part = m.group(3).upper()
        # Asegurarse de que pid no contiene letras mezcladas con la letra final
        # ej: "1.0J" → pid="0", let="J" ✓
        return (num_part, pid_part, let_part, commas)

    # Formato simple: 3B  — sin punto, pid = "0"
    m = re.fullmatch(r"(\d+)([A-Za-z])$", base)
    if m:
        return (int(m.group(1)), "0", m.group(2).upper(), commas)

    return None

def build_video_map(folder: str) -> dict:
    vmap = {}
    for f in os.listdir(folder):
        parsed = parse_video_filename(f)
        if parsed is None:
            continue
        num, pid, letter, commas = parsed
        key = (num, pid, letter, commas)
        fpath = os.path.join(folder, f)
        # Preferir .mp4 sobre imagen si hay conflicto
        if key not in vmap or f.lower().endswith(".mp4"):
            vmap[key] = fpath
    return vmap

VIDEO_MAP: dict = {}

def key_to_str(k) -> str:
    return f"{k[0]}|{k[1]}|{k[2]}|{k[3]}"

def str_to_key(s: str):
    parts = s.split("|")
    return (int(parts[0]), parts[1], parts[2], int(parts[3]))

def map_for_client() -> dict:
    """Convierte el VIDEO_MAP a algo serializable para el cliente."""
    result = {}
    for (num, pid, letter, commas), fpath in VIDEO_MAP.items():
        k = key_to_str((num, pid, letter, commas))
        fname = os.path.relpath(fpath, MEDIA_FOLDER).replace("\\", "/")
        result[k] = {
            "num": num,
            "pid": pid,
            "letter": letter,
            "commas": commas,
            "file": fname,
            "type": "video" if fname.lower().endswith(".mp4") else "image",
        }
    return result


# ================================================================
#  LÓGICA DE CAMINOS (misma que video_map.py + modes.py)
# ================================================================

def get_next_video(num: int, pid: str, letter: str):
    """Siguiente video en la secuencia — cualquier letra excepto I, mismo pid, num mayor."""
    cands = [
        (n, p, l, c) for (n, p, l, c) in VIDEO_MAP
        if p == pid and n > num and c == 0 and l != "I"
    ]
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0]

def get_first_video(pid: str):
    cands = [
        (n, p, l, c) for (n, p, l, c) in VIDEO_MAP
        if p == pid and l != "I" and c == 0
    ]
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0]

def get_fail_video(num: int, pid: str):
    key = (num, pid, "I", 0)
    return key if key in VIDEO_MAP else None

def build_fusion_chain(num: int, pid: str, letter: str) -> list:
    """Cadena de fusión: busca commas 0,1,2,... mientras existan."""
    chain = []
    commas = 0
    while True:
        key = (num, pid, letter, commas)
        if key in VIDEO_MAP:
            chain.append(key)
            commas += 1
        else:
            break
    return chain

def get_j_paths(num: int, current_pid: str) -> list:
    """Paths disponibles desde una escena J."""
    next_nums = sorted(set(
        n for (n, p, l, c) in VIDEO_MAP
        if n > num and c == 0 and l != "I"
    ))
    if not next_nums:
        return [current_pid]
    next_num = next_nums[0]
    pids = sorted(set(
        p for (n, p, l, c) in VIDEO_MAP
        if n == next_num and c == 0 and l != "I"
    ), key=lambda x: (x != current_pid, x))
    if not pids:
        return [current_pid]
    # Poner el pid actual primero
    if current_pid in pids:
        pids.remove(current_pid)
        pids = [current_pid] + pids
    else:
        pids = [current_pid] + pids
    return pids[:4]

def get_j_key_mapping(paths: list) -> dict:
    if len(paths) == 1:
        return {"W": paths[0]}
    elif len(paths) == 2:
        return {"W": paths[0], "D": paths[1]}
    elif len(paths) == 3:
        return {"W": paths[0], "A": paths[1], "D": paths[2]}
    else:
        return {"W": paths[0], "A": paths[1], "S": paths[2], "D": paths[3]}


# ================================================================
#  COORDS JSON
# ================================================================

def load_coords() -> dict:
    if not os.path.exists(COORDS_FILE):
        return {}
    try:
        with open(COORDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def get_j_option_coords(num: int, option_key: str) -> Optional[dict]:
    data = load_coords()
    k = f"{num}J_{option_key}"
    return data.get(k)


# ================================================================
#  SESIÓN DE USUARIO (una por WebSocket)
# ================================================================

class Session:
    def __init__(self):
        self.current_num    = None
        self.current_pid    = "0"
        self.current_letter = None
        self.current_commas = 0
        self.fusion_queue   = []
        self.fusion_base    = None
        self.image_mode     = False
        self.image_letter   = None
        self.image_num      = None
        self.image_alt      = None   # path_id para modo J

    def to_dict(self) -> dict:
        return {
            "current_num":    self.current_num,
            "current_pid":    self.current_pid,
            "current_letter": self.current_letter,
            "current_commas": self.current_commas,
            "image_mode":     self.image_mode,
            "image_letter":   self.image_letter,
            "image_num":      self.image_num,
            "image_alt":      self.image_alt,
        }


# ================================================================
#  LÓGICA DE CARGA (refleja loader.py)
# ================================================================

def _resolve_key_to_file(key: tuple) -> Optional[str]:
    fpath = VIDEO_MAP.get(key)
    if fpath and os.path.exists(fpath):
        return os.path.relpath(fpath, MEDIA_FOLDER).replace("\\", "/")
    return None

def _load_key_raw(session: Session, key: tuple) -> dict:
    """Carga una clave y devuelve el evento a enviar al cliente."""
    num, pid, letter, commas = key
    session.current_num    = num
    session.current_pid    = pid
    session.current_letter = letter
    session.current_commas = commas
    session.image_mode     = False
    session.image_letter   = None
    session.image_num      = None
    session.image_alt      = None

    if letter == "J":
        # Modo J: imagen 360° con decisiones
        fpath = _resolve_key_to_file(key)
        if not fpath:
            return _advance_after_fusion(session, key)

        paths   = get_j_paths(num, pid)
        key_map = get_j_key_mapping(paths)

        # Coordenadas de cada opción (para la animación de cámara)
        option_coords = {}
        for opt_key, opt_pid in key_map.items():
            coords = get_j_option_coords(num, opt_key)
            if coords:
                option_coords[opt_key] = coords

        session.image_mode   = True
        session.image_letter = "J"
        session.image_num    = num
        session.image_alt    = pid

        return {
            "type":          "load_j",
            "file":          fpath,
            "num":           num,
            "pid":           pid,
            "key_map":       key_map,
            "option_coords": option_coords,
            "state":         session.to_dict(),
        }

    elif letter in ("B", "A", "C", "D", "E", "F"):
        # Videos normales (todos se manejan igual en web — solo reproducción)
        fpath = _resolve_key_to_file(key)
        if not fpath:
            return _advance_after_fusion(session, key)
        return {
            "type":  "load_video",
            "file":  fpath,
            "num":   num,
            "pid":   pid,
            "letter": letter,
            "state": session.to_dict(),
        }

    else:
        # Letra no soportada en web → avanzar
        return _advance_after_fusion(session, key)


def load_entry_by_key(session: Session, key: tuple) -> dict:
    if len(key) == 3:
        key = (key[0], key[1], key[2], 0)
    num, pid, letter, commas = key

    chain = build_fusion_chain(num, pid, letter)
    if not chain:
        # Sin cadena → avanzar al siguiente
        nxt = get_next_video(num, pid, letter)
        if nxt:
            return load_entry_by_key(session, nxt)
        return {"type": "end", "state": session.to_dict()}

    session.fusion_base  = chain[0]
    session.fusion_queue = list(chain[1:])
    return _load_key_raw(session, chain[0])


def _advance_after_fusion(session: Session, current_key: tuple) -> dict:
    if session.fusion_queue:
        nxt = session.fusion_queue.pop(0)
        return _load_key_raw(session, nxt)

    base = session.fusion_base
    session.fusion_base = None
    if base is None:
        return {"type": "end", "state": session.to_dict()}

    num, pid, letter, _ = base

    if letter == "I":
        first = get_first_video(pid)
        if first:
            return load_entry_by_key(session, first)
        return {"type": "end", "state": session.to_dict()}

    nxt = get_next_video(num, pid, letter)
    if nxt:
        return load_entry_by_key(session, nxt)
    return {"type": "end", "state": session.to_dict()}


def handle_video_end(session: Session) -> dict:
    key = (session.current_num, session.current_pid,
           session.current_letter, session.current_commas)
    return _advance_after_fusion(session, key)


def handle_j_choice(session: Session, choice_key: str) -> dict:
    num = session.image_num
    pid = session.image_alt
    paths   = get_j_paths(num, pid)
    key_map = get_j_key_mapping(paths)

    if choice_key not in key_map:
        return {"type": "error", "msg": f"Opción {choice_key} no válida"}

    chosen_pid = key_map[choice_key]

    # Buscar siguiente video con num > actual en el pid elegido
    cands = [
        (n, p, l, c) for (n, p, l, c) in VIDEO_MAP
        if p == chosen_pid and n > num and c == 0 and l != "I"
    ]
    if not cands:
        # Intentar desde el principio del pid elegido
        cands = [
            (n, p, l, c) for (n, p, l, c) in VIDEO_MAP
            if p == chosen_pid and c == 0 and l != "I"
        ]
    if not cands:
        return {"type": "end", "state": session.to_dict()}

    cands.sort(key=lambda x: x[0])
    nxt = cands[0]

    session.image_mode   = False
    session.image_letter = None
    session.current_pid  = chosen_pid
    return load_entry_by_key(session, nxt)


# ================================================================
#  HTTP ENDPOINTS
# ================================================================

@app.on_event("startup")
def startup():
    global VIDEO_MAP
    VIDEO_MAP = build_video_map(MEDIA_FOLDER)
    print(f"[Server] Carpeta: {MEDIA_FOLDER}")
    print(f"[Server] Archivos detectados: {len(VIDEO_MAP)}")
    for k, v in sorted(VIDEO_MAP.items()):
        print(f"  key={k}  →  {os.path.basename(v)}")
    if not VIDEO_MAP:
        print("[Server] ADVERTENCIA: No se encontró ningún archivo válido.")
        print("[Server] Archivos en la carpeta:")
        for f in sorted(os.listdir(MEDIA_FOLDER)):
            parsed = parse_video_filename(f)
            print(f"  '{f}'  →  parse={parsed}")

@app.get("/")
def root():
    index = os.path.join(MEDIA_FOLDER, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return HTMLResponse("<h1>Coloca index.html en la misma carpeta</h1>")

@app.get("/api/map")
def api_map():
    return JSONResponse(map_for_client())

@app.get("/api/first/{pid}")
def api_first(pid: str):
    first = get_first_video(pid)
    if first:
        return {"key": key_to_str(first)}
    return {"key": None}

@app.get("/api/debug")
def api_debug():
    """Diagnóstico — abrí /api/debug en el browser para ver qué detectó el servidor."""
    entries = []
    for (num, pid, letter, commas), fpath in sorted(VIDEO_MAP.items()):
        entries.append({
            "num": num, "pid": pid, "letter": letter, "commas": commas,
            "file": os.path.basename(fpath),
            "exists": os.path.exists(fpath),
        })
    pids = sorted(set(e["pid"] for e in entries))
    firsts = {pid: (key_to_str(get_first_video(pid)) if get_first_video(pid) else None) for pid in pids}
    return JSONResponse({
        "total": len(VIDEO_MAP),
        "folder": MEDIA_FOLDER,
        "pids": pids,
        "first_per_pid": firsts,
        "entries": entries,
    })

@app.get("/media/{filename:path}")
def media_file(filename: str):
    fpath = os.path.join(MEDIA_FOLDER, filename)
    if not os.path.exists(fpath):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(fpath)


# ================================================================
#  WEBSOCKET — una sesión por conexión
# ================================================================

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    session = Session()
    print("[WS] Cliente conectado")

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            response = None

            if action == "start":
                pid   = data.get("pid", "0")
                first = get_first_video(pid)
                if first:
                    response = load_entry_by_key(session, first)
                else:
                    response = {"type": "error", "msg": f"No hay videos para path '{pid}'"}

            elif action == "video_end":
                response = handle_video_end(session)

            elif action == "j_choice":
                choice = data.get("key", "W")
                response = handle_j_choice(session, choice)

            elif action == "fail":
                num = session.current_num
                pid = session.current_pid
                fail = get_fail_video(num, pid)
                if fail:
                    response = load_entry_by_key(session, fail)
                else:
                    first = get_first_video(pid)
                    response = load_entry_by_key(session, first) if first else {"type": "end"}

            elif action == "restart":
                pid   = data.get("pid", session.current_pid or "0")
                first = get_first_video(pid)
                if first:
                    response = load_entry_by_key(session, first)
                else:
                    response = {"type": "end"}

            if response:
                await websocket.send_json(response)

    except WebSocketDisconnect:
        print("[WS] Cliente desconectado")
    except Exception as e:
        print(f"[WS] Error: {e}")
        try:
            await websocket.send_json({"type": "error", "msg": str(e)})
        except Exception:
            pass


# ================================================================
#  ARRANQUE
# ================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  Servidor visor 360° / video narrativo")
    print(f"  Carpeta de media: {MEDIA_FOLDER}")
    print("  URL local:  http://localhost:8000")
    print("  Para exponer: ngrok http 8000")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=8000)
