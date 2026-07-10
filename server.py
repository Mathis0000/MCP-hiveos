"""
HiveOS MCP Server — édition SSH locale
=======================================
Pilote le rig HiveOS directement via SSH (commandes locales), sans passer
par l'API cloud (payante sur les farms free depuis mars 2024).

Tools lecture  : get_gpu_stats, get_rig_info, get_miner_status,
                 get_wallet_conf, list_saved_configs, get_current_oc
Tools écriture : set_gpu_oc, miner_control, save_wallet_conf,
                 apply_saved_config, reboot_rig
                 (activés seulement si MCP_ENABLE_WRITE=true)

Auth entrante  : header  Authorization: Bearer <MCP_AUTH_TOKEN>
Auth sortante  : clé SSH (montée en volume, jamais de mot de passe)
"""

import json
import os
import shlex

import paramiko
from fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

RIG_HOST = os.environ["RIG_HOST"]
RIG_PORT = int(os.environ.get("RIG_PORT", "22"))
RIG_USER = os.environ.get("RIG_USER", "user")
SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", "/app/ssh/id_ed25519")
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
ENABLE_WRITE = os.environ.get("MCP_ENABLE_WRITE", "false").lower() == "true"

# Répertoire (sur le rig) où l'on stocke nos configs de minage nommées
CONFIGS_DIR = "/hive-config/mcp-configs"

mcp = FastMCP("HiveOS-SSH")


# ------------------------------------------------------------------- SSH --

def _ssh(command: str, timeout: int = 30) -> str:
    """Exécute une commande sur le rig et retourne stdout (+stderr si erreur)."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        RIG_HOST, port=RIG_PORT, username=RIG_USER,
        key_filename=SSH_KEY_PATH, timeout=10,
        look_for_keys=False, allow_agent=False,
    )
    try:
        _, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        code = stdout.channel.recv_exit_status()
        if code != 0:
            return json.dumps({"exit_code": code, "stdout": out, "stderr": err},
                              ensure_ascii=False)
        return out
    finally:
        client.close()


def _require_write():
    if not ENABLE_WRITE:
        raise PermissionError(
            "Tools d'écriture désactivés (MCP_ENABLE_WRITE=false). "
            "Active-les dans le .env après validation en lecture seule.")


# ---------------------------------------------------------------- LECTURE --

@mcp.tool
def get_gpu_stats() -> str:
    """Stats temps réel de chaque GPU du rig : température, fan, power,
    hashrate, clocks. C'est LA vue pour surveiller et optimiser les OC."""
    return _ssh("sudo gpu-stats")


@mcp.tool
def get_rig_info() -> str:
    """Infos générales du rig : nom du worker, uptime, load average,
    version HiveOS, IP locale."""
    return _ssh(
        "echo '--- rig.conf ---'; grep -E 'WORKER_NAME|FARM_ID' /hive-config/rig.conf 2>/dev/null | sed 's/PASSWD.*//' ;"
        "echo '--- uptime ---'; uptime ;"
        "echo '--- hive version ---'; dpkg -s hive 2>/dev/null | grep Version || cat /hive/etc/VERSION 2>/dev/null ;"
        "echo '--- ip ---'; hostname -I"
    )


@mcp.tool
def get_miner_status() -> str:
    """Statut du miner : tourne-t-il, sur quel écran, dernières lignes de log."""
    return _ssh(
        "echo '--- screens ---'; screen -ls || true ;"
        "echo '--- derniers logs miner ---'; tail -n 30 /var/log/miner/*/*.log 2>/dev/null | tail -n 40"
    )


@mcp.tool
def get_wallet_conf() -> str:
    """Config de minage active (/hive-config/wallet.conf) : coin, wallet,
    pool, miner. Équivalent local de la flight sheet appliquée."""
    return _ssh("cat /hive-config/wallet.conf")


@mcp.tool
def list_saved_configs() -> str:
    """Liste les configs de minage sauvegardées sur le rig (pearl, csd, ...)
    que l'on peut appliquer avec apply_saved_config."""
    return _ssh(f"ls -1 {CONFIGS_DIR} 2>/dev/null || echo '(aucune config sauvegardée)'")


@mcp.tool
def get_current_oc() -> str:
    """Overclock NVIDIA courant : clocks, power limit, fan par GPU."""
    return _ssh(
        "nvidia-smi --query-gpu=index,name,clocks.gr,clocks.mem,power.limit,"
        "power.draw,fan.speed,temperature.gpu --format=csv"
    )


# --------------------------------------------------------------- ÉCRITURE --

@mcp.tool
def set_gpu_oc(gpu_index: int, core_offset: int | None = None,
               mem_offset: int | None = None,
               power_limit: int | None = None,
               fan_speed: int | None = None) -> str:
    """Applique un OC à chaud sur UN GPU via nvtool (index = numéro du GPU,
    -1 pour tous). core_offset/mem_offset en MHz (offsets), power_limit en W,
    fan_speed en %. Note : OC runtime, perdu au reboot — pour persister,
    l'inclure dans une config sauvegardée."""
    _require_write()
    parts = []
    target = "" if gpu_index == -1 else f"-i {int(gpu_index)} "
    if core_offset is not None:
        parts.append(f"sudo nvtool {target}--setcoreoffset {int(core_offset)}")
    if mem_offset is not None:
        parts.append(f"sudo nvtool {target}--setmemoffset {int(mem_offset)}")
    if power_limit is not None:
        parts.append(f"sudo nvtool {target}--setpl {int(power_limit)}")
    if fan_speed is not None:
        parts.append(f"sudo nvtool {target}--setfan {int(fan_speed)}")
    if not parts:
        return "Aucun paramètre fourni."
    return _ssh(" && ".join(parts))


@mcp.tool
def miner_control(action: str) -> str:
    """Contrôle le miner : action = start | stop | restart | log."""
    _require_write()
    if action not in {"start", "stop", "restart", "log"}:
        raise ValueError("Action autorisée : start, stop, restart, log")
    return _ssh(f"sudo miner {shlex.quote(action)}")


@mcp.tool
def save_wallet_conf(name: str) -> str:
    """Sauvegarde la config de minage ACTIVE sous un nom (ex: 'pearl'),
    réutilisable ensuite via apply_saved_config."""
    _require_write()
    safe = shlex.quote(name.replace("/", "_") + ".conf")
    return _ssh(
        f"sudo mkdir -p {CONFIGS_DIR} && "
        f"sudo cp /hive-config/wallet.conf {CONFIGS_DIR}/{safe} && "
        f"echo Sauvegardé: {safe}"
    )


@mcp.tool
def apply_saved_config(name: str) -> str:
    """Applique une config sauvegardée (voir list_saved_configs) et
    redémarre le miner. Équivalent local de 'appliquer une flight sheet'."""
    _require_write()
    safe = shlex.quote(name.replace("/", "_") + ".conf")
    return _ssh(
        f"test -f {CONFIGS_DIR}/{safe} && "
        f"sudo cp {CONFIGS_DIR}/{safe} /hive-config/wallet.conf && "
        f"sudo miner restart && echo Config appliquée: {safe} "
        f"|| echo 'Config introuvable — voir list_saved_configs'"
    )


@mcp.tool
def reboot_rig() -> str:
    """Redémarre le rig proprement (sreboot)."""
    _require_write()
    return _ssh("sudo sreboot &", timeout=10) or "Reboot lancé."


# ------------------------------------------------------------------- AUTH --

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Refuse toute requête sans le bon token Bearer (protège l'exposition)."""

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
