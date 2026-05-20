# 商业化低配与数据安全验收 - 2026-05-04

## 低配运行烟测
- 报告：`docs/qa-smoke/commercial-2026-05-04/low-resource-smoke.json`
- 结果：`ok`
- 健康探测次数：`20`
- `/api/ready`：`healthy=True`，检查项 `9`
- RSS：`173.3MB`，阈值 `250.0MB`

## 备份恢复演练
- 报告：`docs/qa-smoke/commercial-2026-05-04/backup-restore-drill.md`
- 真实项目备份：已生成并执行 SQLite `integrity_check`。
- 临时目录恢复：未确认恢复会拒绝；设置 `CONFIRM_RESTORE=RESTORE` 后恢复成功。

## 结论
- 低配目标 `2G / 2核` 的空闲 RSS 目标 `<250MB` 已通过本轮烟测。
- 数据安全演练未覆盖真实库恢复，避免破坏当前环境；恢复流程已在临时客户目录验证。
