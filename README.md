# HiveOS MCP Server

Serveur MCP (FastMCP) qui permet à Claude de lire les stats de ton rig HiveOS
et, une fois activé, d'appliquer des overclocks et des flight sheets.

## Tools exposés

Lecture (toujours actifs) :
- `get_farm_stats` — hashrate total, conso, workers online
- `list_workers` — résumé de chaque rig
- `get_worker` — détail complet d'un worker
- `get_gpu_stats` — stats par GPU (temp, fan, power, hashrate, clocks)
- `list_oc_profiles`, `get_worker_oc`
- `list_flight_sheets`

Écriture (si `MCP_ENABLE_WRITE=true`) :
- `set_manual_oc` — OC par GPU (core/mem/PL/fan)
- `apply_oc_profile`
- `create_flight_sheet`, `apply_flight_sheet`
- `worker_command` — reboot / miner restart-stop-start uniquement

## Déploiement

```bash
# 1. Copier le projet sur le PC Ubuntu puis :
cp .env.example .env
nano .env          # remplir HIVEOS_TOKEN, HIVEOS_FARM_ID

# 2. Générer le token d'auth du serveur MCP
openssl rand -hex 32   # -> coller dans MCP_AUTH_TOKEN

# 3. Créer le tunnel Cloudflare
#    dash.cloudflare.com -> Zero Trust -> Networks -> Tunnels -> Create tunnel
#    - Type : Cloudflared
#    - Copier le token du tunnel -> CLOUDFLARE_TUNNEL_TOKEN dans .env
#    - Public hostname : ex. hiveos-mcp.tondomaine.com
#      Service : HTTP  ->  hiveos-mcp:8000
#      (le nom "hiveos-mcp" = nom du service docker, résolu dans le réseau compose)

# 4. Lancer
docker compose up -d --build

# 5. Vérifier en local
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/mcp
# 401 attendu (auth requise) => le serveur tourne
```

## Connexion à Claude.ai

Settings -> Connectors -> Add custom connector :
- URL : `https://hiveos-mcp.tondomaine.com/mcp`
- Authentification : Bearer token -> coller la valeur de `MCP_AUTH_TOKEN`

Puis dans une conversation, active le connecteur et demande par exemple :
« Montre-moi les stats GPU de mon rig ».

## Passage en écriture

Une fois la lecture validée :
```bash
sed -i 's/MCP_ENABLE_WRITE=false/MCP_ENABLE_WRITE=true/' .env
docker compose up -d
```

## Sécurité

- Le port 8000 n'est bindé que sur 127.0.0.1 : seul le tunnel y accède.
- Toute requête sans `Authorization: Bearer <MCP_AUTH_TOKEN>` reçoit un 401.
- Les commandes worker sont limitées à une liste blanche.
- Le token HiveOS ne quitte jamais le serveur.
- Option en plus : ajouter Cloudflare Access devant le hostname pour
  restreindre par email/pays.
