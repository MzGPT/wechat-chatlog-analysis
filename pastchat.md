# 项目对话与改动总览（Past Chat Digest）

更新时间：$(date '+%Y-%m-%d %H:%M:%S')

## 背景与目标
- 搭建及迭代一个基于 FastAPI 的“微信/邮件/新闻”一体化分析与汇总系统。
- 关键诉求：
  - 邮件摘要严格基于“正文”，禁止拼接/复读“主题”。
  - 微信与邮件使用独立的小模型提示词与配置；垃圾筛选要剔除系统消息、黑名单、超短消息。
  - 新增“新闻聚合/舆情分析”能力（JSON-first，支持兜底），并在 UI 的 AI 总结页以独立模块呈现。
  - 领导视角的结构化报告：少废话、强逻辑，按模块输出可执行的要点与提醒。

---

## 里程碑（按时间）

1) 邮件摘要质量修复
- 调整小模型提示词：仅看正文，不得引用/拼接主题；识别会场/会议号/平台。
- 前端邮件详情显示 ID；列表摘要显示使用来源标记（tool/fallback）。
- 新增 API：`POST /api/email/derive/latest?limit=10`，一键重算最近 N 封邮件。

2) 微信小模型与垃圾筛选
- 微信与邮件的小模型提示词分离；UI 的“功能设置”可独立配置。
- 垃圾过滤：超短（<15 汉字）、系统提示（含“进入群聊”等）、黑名单。
- 列表“表头/筛选栏”吸顶与分页滚动体验调优。

3) 新闻聚合（Newsfeed）
- 移除对 4445 上游的强依赖，采用“直接 JSON”采集与归一化：
  - 华尔街见闻快讯、Hacker News(Algolia)、Spaceflight News、Reddit(r/stocks/r/investing/r/economy)、TechCrunch、CoinDesk、Engadget、Cointelegraph、Bitcoin.com News、NPR Business 等。
- API：`/api/newsfeed/items|search|ai/summarize`。
- 定时快照：新增后台循环每 3 小时写入 `data/datasets/news_snapshot_<ts>.json`（必要字段：id/source/title/content/pub_ts/published_at）。
- 前端“新闻聚合”页内嵌 4444（未配置时兜底到 http://127.0.0.1:4444 ）。

4) AI 总结页模块化
- 新增“新闻舆情监测（newswatch）”，并设为“默认必出”。
- 模块映射：市场观点、会议路演、分歧观点（原“矛盾观点”）、高分联系人、新闻舆情（+可选社交舆情）。
- 若 LLM 输出为空：后端与前端双兜底（基于直接新闻 JSON 生成结构化摘要）。

5) 提示词重构（面向领导摘要）
- 市场观点（market）
  - 必须给出“核心基调/关键风险”，分类小结（宏观/行业/公司/策略/情绪）→“主题：结论；依据；风险”，每条≤两行；正文不得出现券商、人名、日期。
- 分歧观点（counter，替代“矛盾观点”）
  - 同一议题聚合正反双方；给出“主流观点/对立观点/待核查/行动建议”，仅句尾短标来源 `(来源:#123 #456)`。
- 新闻舆情（newswatch）
  - 统计概览（总条数/来源/类别/情绪占比），主题脉络（合并多条成 3-5 个主题），关注动作（3-4 条针对交易/风控的建议）。
- 会议路演（meetings）
  - 表格重排为“时间 | 平台/会议号 | 主讲 | 主题要点”，主流平台统计；收紧行高与列宽规则。

6) 舆情“空白”问题排查与修复
- 根因：`/api/ai/summary` 中 newswatch 传入了 `raw_messages` 等大字段，导致上下文过大→LLM 空响应。
- 修复：只传递 `{messages:[新闻items]}`；并在前端发现为空时，自动调用 `/api/newsfeed/ai/summarize`。

7) 启动失败修复
- 两处字符串换行导致语法错误（`app/routers/ai.py`）。
- 修正后可正常 `start`，`/api/health` 返回 `ok`。

---

## 当前能力清单（摘要）
- 微信消息
  - 垃圾筛选与短文本剔除；吸顶表头；模块化 AI 总结（市场/会议/分歧/联系人）。
- 邮件消息
  - 仅正文透传给小模型；识别会议；可一键刷新最近 10 封摘要。
- 新闻聚合
  - 多源 JSON 采集 + 归一化；`/api/newsfeed/items|search` 查询；定时写入 datasets；newswatch 模块默认生成（兜底时也不空白）。
- 配置与模型
  - 功能设置中可独立配置：API Key、主模型温度（默认 0.6）、消息/邮件小模型与提示词；默认模块集包含 `newswatch`。

---

## 重要文件/改动点
- 后端
  - `app/routers/ai.py`：各模块汇总逻辑、newswatch 压缩 payload、兜底生成；会议/分歧/市场 fallback 重写。
  - `app/services/llm_client.py`：`DEFAULT_MODULE_PROMPTS` 重构（market/counter/newswatch）。
  - `app/services/news_client.py`：直连源、normalize 修复（`source_name` 回退）、快照写入与时间戳规范化。
  - `app/background.py`：`_news_snapshot_loop()` 定时落盘 datasets。
  - `app/routers/email.py`：`POST /api/email/derive/latest`。
- 前端（`static/index.html`）
  - 新增“新闻舆情监测”卡片；高级选项默认勾选；兜底拉取 summarize；统一表格样式；会议表四列；表头顶距修复；“矛盾观点”→“分歧观点”。

---

## 遗留与下一步（建议）
1. 进一步收紧市场/舆情篇幅：
   - 类别每段 2-3 条；短句 + 结构化数据（同比/环比/净流入/量价/估值等）。
2. newswatch 分片策略：
   - 当新闻>80 条时拆片汇总再二次合并，避免再次超长上下文。
3. 主题聚类/NER：
   - 对“分歧观点”与“新闻主题脉络”引入轻量中文分词+同义聚合（或引入短词典）。
4. 更多中文 JSON 源接入/白名单：
   - 证券时报/新华社/澎湃等，如仅 RSS 可加轻量 RSS→JSON 适配器。
5. 单元测试：
   - /api/newsfeed/* 与 summary-local 的集成测试；兜底与异常路径；快照轮转。

---

## 常用命令
- 启动/重启/状态/日志
  - `bash scripts/manage.sh start | restart | status | logs -f`
- 健康检查与快速验收
  - `curl http://127.0.0.1:8000/api/health`
  - `curl 'http://127.0.0.1:8000/api/newsfeed/items?limit=20'`
  - `curl -X POST 'http://127.0.0.1:8000/api/email/derive/latest?limit=10'`
- AI 总结（只跑舆情）
  - `curl -X POST 'http://127.0.0.1:8000/api/newsfeed/ai/summarize' -H 'Content-Type: application/json' -d '{"limit":60, "temperature":0.6}'`

---

## 备注
- 若 AI 总结卡片仍出现空白：
  - 检查 SiliconFlow API Key 是否配置；或先看兜底是否生效（卡片顶部会标注）。
- 若前端表头再次“增高/错位”：
  - 确认 `.module-panel` 的 padding 与 `--filters-height` 是否被其它样式覆盖；可刷新或切换标签页触发重新测量。

