# 0913 WeChat Automation Platform — Deployment Guide

## Quick Start (New Server)

```bash
# 1. Clone repo
git clone <your-repo-url> /opt/0913
cd /opt/0913

# 2. One-click deploy
bash scripts/deploy-0913.sh

# Follow the prompts to enter:
#   - wechatapi token + app_id
#   - Callback public URL
#   - SiliconFlow API key
#   - MiniMax API key
#   - API token
```

After deploy, set up a public tunnel (ngrok/natapp/frp) to expose port 8000,
then visit http://127.0.0.1:8000.

## What Gets Deployed

| Component | Description |
|-----------|-------------|
| FastAPI server | Port 8000 — message ingestion, auto-reply, analysis dashboard |
| SQLite DB | data/app.db — messages, contacts, config, subsession state |
| WeChat gateway | Callback → rules → reply generation → outbound send |
| 8000 Dashboard | Message list, WeChat settings, analysis modules, 公众号, send management |
| Sub-session | wechat_gateway_default — independent persona, MiniMax routing, multi-turn history |

## Configuration Files

| File | Purpose |
|------|---------|
| `.env` | Server settings, API keys, paths |
| `data/ai_config.json` | LLM model routes, channels, prompts |
| SyncState `wechat_gateway_config` | Gateway config (token, app_id, callback URL) |
| SyncState `wechat_gateway_trigger_rules` | Auto-reply trigger rules (prefix, wakeup, suppression) |
| wechat_subsessions table | Sub-session persona, routing, history |

## Post-Deploy Checklist

- [ ] `curl http://127.0.0.1:8000/api/health` returns 200
- [ ] Public tunnel set up to forward to port 8000
- [ ] 8000 → WeChat Settings → verify callback URL → Bind Callback
- [ ] Send `ai test` from WeChat → verify auto-reply
- [ ] 8000 → Message list shows WeChat traffic
- [ ] 8000 → 公众号 tab loads articles

## Directory Structure

```
/opt/0913/
├── app/                 # FastAPI app (routers, services, models)
├── static/              # 8000 dashboard frontend
├── data/                # SQLite DB, ai_config.json
├── scripts/             # manage.sh, deploy-0913.sh
├── tests/               # pytest test suite
├── docs/                # WeChat API docs mirror (142 pages)
├── .env                 # Server config (from template)
├── .env.example         # Config template
├── requirements.txt     # Python dependencies
└── DEPLOY.md            # This file
```

## Adding Hermes Skill (Optional)

To give Hermes deep knowledge of the 0913 system on a new server:

```bash
# Copy the skill from your existing Hermes setup
cp -r ~/.hermes/skills/software-development/0913-wechat-smart-reply \
     /target/.hermes/skills/software-development/
```

Or package and transfer:
```bash
tar czf 0913-skill.tar.gz -C ~/.hermes/skills/software-development 0913-wechat-smart-reply
```

## Docker (100-server Scale)

```dockerfile
FROM python:3.11-slim
COPY . /app
WORKDIR /app
RUN pip install -r requirements.txt
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Environment variables are injected per instance — no code changes needed.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| No messages | curl /api/health; check wechatapi checkOnline; verify token |
| No auto-reply | check trigger_rules; verify MiniMax API key; check logs |
| 公众号 empty | Check /api/mp/articles returns data; verify mp_config |
| Token expired | Update token in 8000 → WeChat Settings; re-bind callback |
