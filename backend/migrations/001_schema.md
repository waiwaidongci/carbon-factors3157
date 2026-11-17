# 初始结构

SQLAlchemy启动创建以下业务表：activity_periods, activities, emission_factors, carbon_runs, delivery_schedules, deliveries、users、audits。Source专属JSON字段由对应validation严格校验；Policy为不可覆盖的版本记录；Run保存source/policy冻结快照；Period仅用于已有期间选择器，不计入核心复杂度。已有数据库升级需先备份，后续字段演进应以独立迁移执行。

# 002 因子联合抽样（排放不确定区间）

新增两张表，与既有 carbon_runs 完全独立，旧报告/交付链路与表结构不变：

- `sampling_jobs`：抽样作业。id、name、status(queued/running/finalizing/completed/failed)、snapshot（冻结底稿+口径+抽样依据 means/stds/correlation/seed/sample_count/chunk_size）、checksum(SHA256)、sample_count、chunk_size、seed、progress（已计样本数）、result（全体与部门精确95%区间）、error、created、finished、finalize_attempts（最终化重试次数）、retry_after（退避至该时间戳后才可再次最终化）。
- `sampling_chunks`：分块领取记录。job_id、seq、start、size、status(pending/running/done/failed)、attempts、claim_by、claim_at、partial（块内部分和与极值）。

升级方式：本项目由 SQLAlchemy `Base.metadata.create_all` 启动建表，新表会在 backend/worker 启动时自动创建，无需手工 DDL；旧表不受影响。若需手工建表，可在备份后参照 src/models/sampling.py 执行等价 CREATE TABLE。

队列：Redis key `sampling-jobs`（与 `report-jobs` 分离），消息仅作唤醒；分块领取以数据库行锁（SELECT ... FOR UPDATE SKIP LOCKED）为准，故消息丢失、worker 重启、多执行者并发均不会重复累计或卡死。SQLite 不支持 SKIP LOCKED 时退化为普通行锁，仅用于本地测试，生产使用 PostgreSQL16。
