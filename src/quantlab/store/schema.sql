-- quantlab 数据契约 DDL —— 对应 LOCAL_DEPLOYMENT_PLAN.md §P2.1
--
-- 设计原则（违反会导致后续 Phase 返工）：
--   1. Parquet 是真相，DuckDB 是查询层。本 DDL 只描述**结构**，不承载清洗策略。
--   2. available_utc（数据可用时间）与 ts（交易日期）**必须分离** —— 防未来函数的基础。
--   3. 所有事实表带 snapshot_id；原始快照**不可原地覆盖**（P2.5 原子替换）。
--   4. 每条记录可溯源（source）。
--
-- 时间与时区口径（全局统一，勿在别处另立）：
--   ts / ex_date / pay_date / period_end / as_of_date  DATE      本地交易日 'YYYY-MM-DD'
--   available_utc / close_utc / downloaded_at
--   / session_close_utc / started_at / finished_at     TIMESTAMP **naive UTC**（无时区）
--   UTC 存储一律用 `now() AT TIME ZONE 'UTC'` 产生，**绝不**用 `now()`（后者带时区）。
--
-- 层的松紧（重要取舍）：
--   bronze/契约层**只做结构性约束**（主键、NOT NULL、跨字段硬逻辑）；
--   「low <= open <= high」「异常跳变」「日历缺口」等属 P2.6 的**质量规则**。
--   在契约层加这些 CHECK 会把待捕获的坏数据直接打死、丢失证据（违背 F.3「先报错或隔离」）。
--   唯一保留的跨字段约束是 fundamentals.as_of_date >= period_end —— 它表达的是
--   「信息可得性」的硬逻辑，不是数据质量。

CREATE TABLE IF NOT EXISTS symbols (
    symbol_id   BIGINT PRIMARY KEY,          -- 内部永久 ID：解决标的代码变更
    ticker      TEXT NOT NULL,
    exchange    TEXT NOT NULL,               -- XSHG / XHKG / XNYS
    calendar    TEXT NOT NULL,               -- exchange-calendars 日历名
    currency    TEXT NOT NULL,               -- 交易币种（原币），ISO-4217
    isin        TEXT,
    lot_size    INTEGER,
    listed_on   DATE,
    delisted_on DATE,
    CHECK (delisted_on IS NULL OR listed_on IS NULL OR delisted_on >= listed_on)
);

CREATE TABLE IF NOT EXISTS bars_daily (
    symbol_id     BIGINT  NOT NULL REFERENCES symbols(symbol_id),
    ts            DATE    NOT NULL,
    open          DOUBLE,
    high          DOUBLE,
    low           DOUBLE,
    close         DOUBLE,
    volume        DOUBLE,
    currency      TEXT,
    close_utc     TIMESTAMP,                 -- 该根 bar 的本地收盘时刻（naive UTC）
    available_utc TIMESTAMP NOT NULL,        -- **关键**：此数据何时可用
    source        TEXT    NOT NULL,
    downloaded_at TIMESTAMP NOT NULL,
    snapshot_id   TEXT    NOT NULL,
    PRIMARY KEY (symbol_id, ts, snapshot_id)
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol_id   BIGINT NOT NULL REFERENCES symbols(symbol_id),
    ex_date     DATE   NOT NULL,
    kind        TEXT   NOT NULL,             -- dividend / split
    ratio       DOUBLE,                      -- 拆分：1 股拆成 ratio 股
    cash        DOUBLE,                      -- 每股现金分红（原币）
    pay_date    DATE,
    -- 公司行动的「信息可得时间」：公告/生效公告的发布时刻（naive UTC）。
    -- 与 bars_daily.available_utc 语义同类，但方向**相反**：公司行动通常**先公告、后除权**，
    -- 故一般 available_utc <= ex_date。缺了它就会产生「除权日之前就知道要拆分」的未来函数。
    -- 不做 CHECK：公告时点与除权日的时间关系受具体行动类型与交易所规则影响，属 P2.6 校验范畴。
    available_utc TIMESTAMP NOT NULL,
    source      TEXT   NOT NULL,
    snapshot_id TEXT   NOT NULL,
    -- 主键含 kind：同日可同时存在分红与拆分
    PRIMARY KEY (symbol_id, ex_date, kind, snapshot_id),
    CHECK (kind IN ('dividend', 'split')),
    CHECK (kind <> 'split'    OR ratio IS NOT NULL),
    CHECK (kind <> 'dividend' OR cash  IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    base          TEXT NOT NULL,             -- 原币
    quote         TEXT NOT NULL,             -- 计价币（报告的基准货币）
    ts            DATE NOT NULL,
    rate          DOUBLE NOT NULL,           -- **恒为「1 base = rate quote」**，正数
    available_utc TIMESTAMP NOT NULL,
    source        TEXT NOT NULL,
    snapshot_id   TEXT NOT NULL,
    PRIMARY KEY (base, quote, ts, snapshot_id),
    CHECK (rate > 0)
);

-- 各交易所日历。若统一由 exchange-calendars 派生可不落表，但**必须声明来源**。
CREATE TABLE IF NOT EXISTS trading_calendar (
    exchange          TEXT NOT NULL,
    ts                DATE NOT NULL,
    is_open           BOOLEAN NOT NULL,
    session_close_utc TIMESTAMP,             -- 休市日为 NULL
    source            TEXT NOT NULL,
    PRIMARY KEY (exchange, ts),
    CHECK (is_open OR session_close_utc IS NULL)
);

-- 宏观：本平台的一等数据，与行情同级维护。**不得**塞进行情表当附属列。
CREATE TABLE IF NOT EXISTS macro_series (
    series_id     TEXT NOT NULL,             -- 如 DGS10 / CPIAUCSL
    source        TEXT NOT NULL,
    ts            DATE NOT NULL,
    value         DOUBLE,
    unit          TEXT,
    available_utc TIMESTAMP NOT NULL,
    snapshot_id   TEXT NOT NULL,
    PRIMARY KEY (series_id, ts, snapshot_id)
);

CREATE TABLE IF NOT EXISTS fundamentals (
    symbol_id   BIGINT NOT NULL REFERENCES symbols(symbol_id),
    period_end  DATE   NOT NULL,             -- 会计期间结束
    as_of_date  DATE   NOT NULL,             -- **该项数据何时可得** —— 防未来函数
    item        TEXT   NOT NULL,
    value       DOUBLE,
    -- 与 as_of_date 互补而非重复：as_of_date 是**日期**粒度（该数据以哪天为准 / 何时可得），
    -- available_utc 是**时刻**粒度（具体发布时点，含时区与盘中发布的情形）。
    available_utc TIMESTAMP NOT NULL,
    source      TEXT   NOT NULL,
    snapshot_id TEXT   NOT NULL,
    PRIMARY KEY (symbol_id, period_end, as_of_date, item, snapshot_id),
    -- 硬逻辑：数据不可能在会计期间结束前就可得。属「信息可得性」约束，故 fail-closed。
    CHECK (as_of_date >= period_end),
    -- 同理：发布时刻不可能早于该数据「可得日期」的零点。
    CHECK (available_utc >= CAST(as_of_date AS TIMESTAMP))
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    -- 主键为 (snapshot_id, dataset)：**一次 ingest 会产出多张表**，故每个数据集一行；
    -- 若只用 snapshot_id 作主键，就无法表达「本次快照的 bars_daily 已 ok、
    -- 而 fundamentals 仍 failed」这种**按表**状态，v_bars_latest 也就无从按表筛选。
    snapshot_id TEXT NOT NULL,
    source      TEXT NOT NULL,
    dataset     TEXT NOT NULL,               -- symbols / bars_daily / fx_rates / ...
    started_at  TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    rows        BIGINT,
    watermark   DATE,                        -- 已覆盖到的数据边界
    file_hash   TEXT,                        -- 产出快照的内容哈希（见 P2.5）
    status      TEXT NOT NULL,
    note        TEXT,
    PRIMARY KEY (snapshot_id, dataset),
    CHECK (status IN ('running', 'ok', 'failed', 'partial', 'aborted'))
);

-- ---------------------------------------------------------------------------
-- 回测 run 登记（P6.3）：把「一次回测」及其复现三件套固化下来。
-- **不是事实表**（无 snapshot_id）：run 是**产出**，不是可叠加的行情快照。
-- 研究侧只读可查；写入只在 ingest/单进程（DuckDB 单写多读）。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,       -- 本次回测唯一 ID
    spec_id          TEXT NOT NULL,          -- StrategySpec 内容哈希（spec_id）
    engine           TEXT NOT NULL,          -- reference / bt / backtrader / portfolio
    origin           TEXT NOT NULL,          -- handwritten / x2strategy
    created_at       TIMESTAMP NOT NULL,     -- naive UTC
    data_snapshot_id TEXT NOT NULL,          -- 复现三件套之一：数据快照
    env_lock_hash    TEXT NOT NULL,          -- 复现三件套之二：环境锁
    git_sha          TEXT NOT NULL,          -- 复现三件套之三：代码版本
    params_json      TEXT NOT NULL,          -- 成本 / 初始资金 / 动量窗口等
    status           TEXT NOT NULL,
    CHECK (status IN ('ok', 'failed', 'partial'))
);

CREATE TABLE IF NOT EXISTS run_metrics (
    run_id TEXT   NOT NULL REFERENCES runs(run_id),
    metric TEXT   NOT NULL,                  -- total_return / annualized_return / ...
    value  DOUBLE NOT NULL,
    PRIMARY KEY (run_id, metric)
);

-- ---------------------------------------------------------------------------
-- 快照读取约定（P2.1 强制）
-- ---------------------------------------------------------------------------
-- 因主键含 snapshot_id，同一 (symbol_id, ts) 会**同时**存在于多个快照中。
-- 因此**禁止**裸 `SELECT * FROM bars_daily` —— 会把多份快照叠加成脏数据。
--     可执行哨兵见 src/quantlab/store/snapshot_guard.py
-- 研究侧请走下列**单快照**视图，或显式写 `WHERE snapshot_id = :one_snapshot`。
--
-- v_bars_latest：取 bars_daily 最近一次**成功** ingest 的那一份快照，绝不跨快照混合。
CREATE OR REPLACE VIEW v_bars_latest AS
SELECT b.*
FROM bars_daily AS b
WHERE b.snapshot_id = (
    SELECT r.snapshot_id
    FROM ingest_runs AS r
    WHERE r.dataset = 'bars_daily' AND r.status = 'ok'
    ORDER BY r.finished_at DESC
    LIMIT 1
);
