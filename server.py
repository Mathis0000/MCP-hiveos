"""
HiveOS MCP Server
=================
Expose l'API HiveOS (api2.hiveos.farm) comme tools MCP pour Claude.

Tools lecture  : list_workers, get_worker, get_gpu_stats, list_oc_profiles,
                 get_worker_oc, list_flight_sheets, get_farm_stats
Tools écriture : set_manual_oc, apply_oc_profile, create_flight_sheet,
                 apply_flight_sheet, worker_command
                 (activés seulement si MCP_ENABLE_WRITE=true)

Auth entrante  : header  Authorization: Bearer <MCP_AUTH_TOKEN>
Auth sortante  : header  Authorization: Bearer <HIVEOS_TOKEN>
"""

import json
import os

import httpx
from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

HIVEOS_API = "https://api2.hiveos.farm/api/v2"
HIVEOS_TOKEN = os.environ["HIVEOS_TOKEN"]
FARM_ID = os.environ["HIVEOS_FARM_ID"]
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
ENABLE_WRITE = os.environ.get("MCP_ENABLE_WRITE", "false").lower() == "true"

# Commandes worker autorisées (liste blanche volontairement restreinte)
SAFE_COMMANDS = {"reboot", "miner restart", "miner stop", "miner start"}

mcp = FastMCP("HiveOS")


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=HIVEOS_API,
        headers={
            "Authorization": f"Bearer {HIVEOS_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )


def _get(path: str) -> dict:
    with _client() as c:
        r = c.get(path)
        r.raise_for_status()
        return r.json()


def _patch(path: str, payload: dict) -> dict:
    with _client() as c:
        r = c.patch(path, json=payload)
        r.raise_for_status()
        return r.json() if r.content else {"ok": True}


def _post(path: str, payload: dict) -> dict:
    with _client() as c:
        r = c.post(path, json=payload)
        r.raise_for_status()
        return r.json() if r.content else {"ok": True}


# ---------------------------------------------------------------- LECTURE --

@mcp.tool
def get_farm_stats() -> str:
    """Statistiques globales de la farm : hashrate total, consommation,
    nombre de workers online/offline."""
    farm = _get(f"/farms/{FARM_ID}")
    keep = {k: farm.get(k) for k in (
        "name", "workers_count", "rigs_count", "stats", "hashrates", "money")}
    return json.dumps(keep, ensure_ascii=False)


@mcp.tool
def list_workers() -> str:
    """Liste tous les workers de la farm avec leur statut résumé
    (online, hashrate, température max, flight sheet actif)."""
    data = _get(f"/farms/{FARM_ID}/workers")
    out = []
    for w in data.get("data", []):
        stats = w.get("stats", {}) or {}
        out.append({
            "id": w.get("id"),
            "name": w.get("name"),
            "online": stats.get("online"),
            "miner": (w.get("flight_sheet") or {}).get("name"),
            "gpus": w.get("gpu_summary", {}).get("gpus"),
            "hashrates": w.get("miners_summary", {}).get("hashrates"),
            "max_temp": stats.get("max_temp"),
            "power_w": stats.get("power_draw"),
        })
    return json.dumps(out, ensure_ascii=False)


@mcp.tool
def get_worker(worker_id: int) -> str:
    """Détail complet d'un worker (config, flight sheet, OC actif, miners)."""
    return json.dumps(_get(f"/farms/{FARM_ID}/workers/{worker_id}"),
                      ensure_ascii=False)


@mcp.tool
def get_gpu_stats(worker_id: int) -> str:
    """Stats par GPU d'un worker : modèle, température, fan, consommation,
    hashrate, core/mem clock actuels. C'est LA vue pour optimiser les OC."""
    w = _get(f"/farms/{FARM_ID}/workers/{worker_id}")
    gpus = []
    for g in w.get("gpu_info", []) or []:
        gpus.append({
            "bus": g.get("bus_number"),
            "index": g.get("index"),
            "model": g.get("model"),
            "brand": g.get("brand"),
            "power_limit_range": g.get("details", {}).get("power_limit"),
        })
    stats = []
    for s in w.get("gpu_stats", []) or []:
        stats.append({
            "bus": s.get("bus_number"),
            "temp": s.get("temp"),
            "fan": s.get("fan"),
            "power_w": s.get("power"),
            "hash": s.get("hash"),
            "core_clock": s.get("coreclk"),
            "mem_clock": s.get("memclk"),
        })
    oc = w.get("overclock")
    return json.dumps({"gpus": gpus, "live_stats": stats, "overclock": oc},
                      ensure_ascii=False)


@mcp.tool
def list_oc_profiles() -> str:
    """Liste les profils d'overclocking enregistrés dans la farm."""
    return json.dumps(_get(f"/farms/{FARM_ID}/oc"), ensure_ascii=False)


@mcp.tool
def get_worker_oc(worker_id: int) -> str:
    """OC actuellement appliqué sur un worker (par GPU si mode manuel)."""
    w = _get(f"/farms/{FARM_ID}/workers/{worker_id}")
    return json.dumps({
        "oc_id": w.get("oc_id"),
        "oc_config": w.get("oc_config"),
        "overclock": w.get("overclock"),
    }, ensure_ascii=False)


@mcp.tool
def list_flight_sheets() -> str:
    """Liste les flight sheets de la farm (coin, wallet, pool, miner)."""
    return json.dumps(_get(f"/farms/{FARM_ID}/fs"), ensure_ascii=False)


# --------------------------------------------------------------- ÉCRITURE --

def _require_write():
    if not ENABLE_WRITE:
        raise PermissionError(
            "Tools d'écriture désactivés (MCP_ENABLE_WRITE=false). "
            "Active-les dans le .env après validation en lecture seule.")


@mcp.tool
def set_manual_oc(worker_id: int, nvidia_oc_json: str) -> str:
    """Applique un OC manuel NVIDIA sur un worker.

    nvidia_oc_json : JSON avec des listes espacées par GPU ou une valeur
    unique. Exemple pour 7 GPUs :
    {"core_clock": "1100 1100 1100 1100 1100 1100 1080",
     "mem_clock": "2600 2600 2600 2600 2600 2600 2400",
     "power_limit": "110 110 110 110 110 110 100",
     "fan_speed": "70"}
    """
    _require_write()
    oc = json.loads(nvidia_oc_json)
    payload = {"oc_config": {"nvidia": oc}, "oc_apply_mode": "replace"}
    return json.dumps(_patch(f"/farms/{FARM_ID}/workers/{worker_id}", payload),
                      ensure_ascii=False)


@mcp.tool
def apply_oc_profile(worker_id: int, oc_id: int) -> str:
    """Applique un profil d'OC existant (voir list_oc_profiles) à un worker."""
    _require_write()
    return json.dumps(
        _patch(f"/farms/{FARM_ID}/workers/{worker_id}", {"oc_id": oc_id}),
        ensure_ascii=False)


@mcp.tool
def create_flight_sheet(name: str, coin: str, wallet_id: int,
                        pool_urls_json: str, miner: str,
                        miner_config_json: str = "{}") -> str:
    """Crée une flight sheet.

    pool_urls_json    : liste JSON d'URLs pool, ex '["eu1.alphapool.tech:5566"]'
    miner             : nom du miner HiveOS (ex "custom", "t-rex", "lolminer")
    miner_config_json : options additionnelles du miner (user_config, etc.)
    """
    _require_write()
    payload = {
        "name": name,
        "items": [{
            "coin": coin,
            "wal_id": wallet_id,
            "pool": "configurable",
            "pool_urls": json.loads(pool_urls_json),
            "miner": miner,
            "miner_config": json.loads(miner_config_json),
        }],
    }
    return json.dumps(_post(f"/farms/{FARM_ID}/fs", payload),
                      ensure_ascii=False)


@mcp.tool
def apply_flight_sheet(worker_id: int, fs_id: int) -> str:
    """Applique une flight sheet existante à un worker."""
    _require_write()
    return json.dumps(
        _patch(f"/farms/{FARM_ID}/workers/{worker_id}", {"fs_id": fs_id}),
        ensure_ascii=False)


@mcp.tool
def worker_command(worker_id: int, command: str) -> str:
    """Envoie une commande au worker. Autorisées : reboot, miner restart,
    miner stop, miner start."""
    _require_write()
    if command not in SAFE_COMMANDS:
        raise ValueError(f"Commande refusée. Autorisées : {SAFE_COMMANDS}")
    payload = {"command": command.split()[0]}
    if command.startswith("miner"):
        payload = {"command": "miner", "data": {"action": command.split()[1]}}
    return json.dumps(
        _post(f"/farms/{FARM_ID}/workers/{worker_id}/command", payload),
        ensure_ascii=False)


# ------------------------------------------------------------------- AUTH --

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Refuse toute requête sans le bon token Bearer (protège le tunnel)."""

    async def dispatch(self, request, call_next):
        if MCP_AUTH_TOKEN:
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {MCP_AUTH_TOKEN}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


app = mcp.http_app()
app.add_middleware(BearerAuthMiddleware)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
