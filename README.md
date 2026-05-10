# 0913 Platform

独立微信消息管理与自动化平台。支持 wechatapi.net 回调接入、智能回复、消息聚合、情报分析。

## 安装

```bash
git clone git@github.com:leecyno1/wechat-chatlog-analysis-v0.8.git 0913
cd 0913
bash scripts/manage.sh install
bash scripts/manage.sh start
```

## 功能模块

| 模块 | 说明 |
|------|------|
| 微信网关 | wechatapi 回调 → 规则评估 → LLM 回复 → 出站发送 |
| 消息管理 | 微信/邮件消息列表、搜索、标签、导出 |
| 情报分析 | 市场观点、会议路演、新闻舆情、自媒体聚合、公众号聚合 |
| 联系人评分 | 基于消息频率和内容质量的联系人价值评分 |
| 消息群发 | 手动/自动批量发送 |
| 子 session | 独立 AI 分身（人格/路由/上下文隔离） |

## WeChat API 对接

0913 通过 wechatapi.net 的 iPad 协议接入微信。配置要求：

1. wechatapi token + app_id（从 [wechatapi 控制台](https://wechatapi.net/) 获取）
2. 回调公网 URL（需 natapp/ngrok/frp 隧道）
3. MiniMax API key（用于自动回复路由）

详细对接方案见 [wechat-automation](https://github.com/leecyno1/wechat-automation) 仓库。

## API 端点

| 端点 | 用途 |
|------|------|
| `POST /api/wechat-gateway/callback` | wechatapi 回调入口 |
| `GET /api/messages` | 消息列表 |
| `GET /api/messages/mp` | 公众号消息 |
| `GET /api/wechat-gateway/config` | 网关配置 |
| `POST /api/wechat-gateway/trigger-rules` | 触发规则 |

## 开发

```bash
python -m pytest -q              # 运行测试
bash scripts/manage.sh dev       # 热重载开发
```

## 许可证

Private — leecyno1
