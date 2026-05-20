# 备份恢复演练 - 2026-05-04

## 真实项目备份
- 备份目录：`backups/backup-20260504-205355`
- 备份大小：`1.1G`
- SQLite integrity_check：`ok`

## 临时目录恢复演练
- 恢复方式：临时客户目录，不覆盖当前真实 `.env` / `data/app.db`。
- 未确认恢复：已拒绝。
- 设置 `CONFIRM_RESTORE=RESTORE` 后恢复：`ok`。

## Manifest 摘要
```
created_at=20260504-205355
root=/Volumes/PSSD/Projects/0913
1.1G	/Volumes/PSSD/Projects/0913/backups/backup-20260504-205355
/Volumes/PSSD/Projects/0913/backups/backup-20260504-205355/.env
/Volumes/PSSD/Projects/0913/backups/backup-20260504-205355/ai_config.json
/Volumes/PSSD/Projects/0913/backups/backup-20260504-205355/app.db
/Volumes/PSSD/Projects/0913/backups/backup-20260504-205355/app.db-shm
/Volumes/PSSD/Projects/0913/backups/backup-20260504-205355/app.db-wal
/Volumes/PSSD/Projects/0913/backups/backup-20260504-205355/manifest.txt
```
