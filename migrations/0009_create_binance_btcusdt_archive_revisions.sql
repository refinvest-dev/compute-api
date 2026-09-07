CREATE TABLE compute_binance_btcusdt_archive_revisions (
    uri TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    daily_bars JSONB NOT NULL,
    PRIMARY KEY (uri, sha256)
);
