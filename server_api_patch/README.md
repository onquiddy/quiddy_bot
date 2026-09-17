# Quiddy API 1.1 patch

This patch extends the already-running FastAPI service with endpoints required by Quiddy Core 0.2:

- `/v1/core/users/upsert`
- `/v1/core/guilds/upsert`
- `/v1/core/members/upsert`
- `/v1/core/members/left`
- `/v1/core/guilds/left`
- `/v1/audit/batch`

It keeps `/health`, `/v1/me`, `/v1/users/{discord_id}` and the existing HMAC contract.

On the VPS, back up `/opt/quiddy/api/app`, replace `main.py` and `security.py` with these versions, then rebuild only the API:

```bash
cd /opt/quiddy
cp -a api/app api/app.backup-$(date +%Y%m%d-%H%M%S)
# copy patched main.py + security.py to /opt/quiddy/api/app/
docker compose build api
docker compose up -d api
docker compose logs api --tail=100
curl https://api.quiddy.net/health
```

Do not expose PostgreSQL or Redis ports.
