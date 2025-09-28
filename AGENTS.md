# Repository Guidelines

## 项目结构与模块组织
- `app/`: FastAPI 应用（`main.py`）、路由（`routers/*`）、服务（`services/*`）、模型/数据库/配置/模式。
- `data/`: SQLite 数据库与 AI 产物（默认 `data/app.db`），视为运行时输出；不要提交。
- `scripts/`: 辅助脚本；入口为 `scripts/manage.sh`。
- `static/`: 极简 UI，供 `/` 与 `/static/*` 使用。
- `docs/`、`n8n/`: 参考文档与示例 n8n 工作流。

## 构建、测试与开发命令
- 首次安装：`cp .env.example .env && bash scripts/manage.sh install`
- 开发热重载：`bash scripts/manage.sh dev`
- 后台服务：`bash scripts/manage.sh start` · 状态/日志/停止：`status` | `logs -f` | `stop`
- 数据同步：`bash scripts/manage.sh sync`（增量）或 `bash scripts/manage.sh syncfull 30`
- 手动运行（可选）：`uvicorn app.main:app --host 127.0.0.1 --port 8000`

## 代码风格与命名约定
- Python 3.11+；遵循 PEP 8；4 空格缩进；100–120 列软限制。
- 文件/模块：`snake_case.py`；类：`CamelCase`；函数/变量：`snake_case`。
- 倡导类型标注与小而专一的函数。HTTP 逻辑放 `routers/*`，业务逻辑放 `services/*`。

## 测试指南
- 暂无正式测试套件。推荐：`pytest` + FastAPI `TestClient`/`httpx`。
- 命名：`tests/test_*.py`；用临时 SQLite 文件隔离 DB，避免修改 `data/app.db`。
- 开发期快速检查：
  - 健康：`curl http://127.0.0.1:8000/api/health`
  - 搜索：`curl 'http://127.0.0.1:8000/api/messages?q=hello'`

## 提交与 Pull Request 规范
- 提交应清晰、聚焦；建议使用 Conventional Commits（如：`feat: add /api/reports`）。
- PR 应包含：目的/摘要、UI 改动截图、测试计划（curl 或步骤）、配置/迁移说明、关联 issue。

## 安全与配置提示
- 机密保存在 `.env`，切勿提交；新增键请更新 `.env.example`。
- 默认 CORS 较宽松；上线前在 `app/main.py` 收紧。
- 备份 `data/app.db`；若迁移存储位置，在 `.env` 设置 `DATABASE_URL`。
- 如使用 n8n，妥善保管 `N8N_AUTH_TOKEN`；请求使用 `Authorization: Bearer <token>`。
