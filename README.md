% 微信聊天记录分析系统（FastAPI + SQLite + n8n）

一个最小可运行的后端骨架：
- FastAPI + SQLite(FTS5) 持久化与检索
- 对接 chatlog HTTP 服务的拉取同步（新增 /api/sync/chatlog）
- AI 总结使用本地 SiliconFlow 接口 + JSON 快照数据库，发送管理可选接入 n8n
- 简单静态前端（`/static/index.html`）用于快速验收 API

## 快速开始

1. 复制环境变量并修改：

```
cp .env.example .env
```

2. 安装依赖并运行：

使用脚本（推荐）：

```
# 安装依赖
bash scripts/manage.sh install

# 后台启动
bash scripts/manage.sh start

# 查看状态/日志
bash scripts/manage.sh status
bash scripts/manage.sh logs -f

# 触发一次从 chatlog 拉取增量
bash scripts/manage.sh sync

# 停止
bash scripts/manage.sh stop
```

或手动运行：

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

3. 访问前端与 API（请先启动服务）：
- 前端最小页：`http://127.0.0.1:8000/`
- 健康检查：`GET /api/health`
- 消息查询：`GET /api/messages?q=关键词`
- 从 chatlog 拉取增量：`POST /api/sync/chatlog`（可选传 since）
- 生成候选回复：`POST /api/ai/suggest-replies`
- 生成报告：`POST /api/ai/summary`（基于本地快照 + SiliconFlow）
- 批量发送：`POST /api/send`

## 主要配置（.env）
- `CHATLOG_HTTP_BASE`：chatlog HTTP 服务地址
- `CHATLOG_DIR`：本地聊天目录，用于离线导入（留空亦可）
- `N8N_REPLY_WEBHOOK`、`N8N_SUMMARY_WEBHOOK`、`N8N_CONTACT_WEBHOOK`、`N8N_SEND_WEBHOOK`、`N8N_AUTH_TOKEN`
- `DATABASE_URL`：默认 `sqlite:///./data/app.db`

## 路由概览
- `GET /api/health`
- `GET /api/messages` `POST /api/messages/{id}/upvote|downvote` `POST /api/messages/{id}/tags`
- `GET /api/chats` `GET /api/contacts` `POST /api/contacts/{id}/rating?delta=1`
- `POST /api/ai/suggest-replies` `POST /api/ai/summary`
- `POST /api/send`
- `POST /api/sync/chatlog` （向 chatlog HTTP 拉取增量）

## n8n 对接约定（示例）
- 回复生成：`POST $N8N_REPLY_WEBHOOK`，请求包含：
```
{"request_id":"reply-1,2,3","context":{"messages":[{"id":1,"text":"...","sender":"...","ts":"..."}]},"prompt_hint":"..."}
```
- （可选）若仍需 n8n 生成报告，可参照历史结构：`{"request_id":"summary-task", ...}`
- 批量发送：`POST $N8N_SEND_WEBHOOK`，请求包含：
```
{"request_id":"send-task","items":[{"target":"wxid_xxx","text":"你好"}]}
```

> 所有请求将携带 `Authorization: Bearer $N8N_AUTH_TOKEN`（如配置）

## 说明
- 该骨架未包含本地目录增量导入任务调度（后续可加定时器/命令行）。
- FTS5 已为 `messages.content_text/sender_name/talker_name` 建索引并带上触发器。
- 前端最终会替换为 `wechat_analysis_report_0801_副本.html` 的完整交互；当前仅内置了最小验收页。

## 开发脚本
- 运行开发服务器：`uvicorn app.main:app --reload`  
- 生产建议使用：`uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2`

### AI 快照与调试
- `python scripts/seed_sample_data.py`：快速写入一批示例消息，便于演示 AI 总结。
- `python scripts/run_summary_snapshot.py --period 3days`：基于当前数据库生成快照并调用本地总结，可通过 `--output result.json` 导出。

> `/api/sync/chatlog` 在拉取新消息后会自动刷新 `analysis_snapshots` 表，AI 总结直接消费该 JSON 快照，无需依赖 n8n。
