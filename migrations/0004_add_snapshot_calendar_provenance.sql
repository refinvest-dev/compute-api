ALTER TABLE compute_dataset_snapshots
    ADD COLUMN calendar_source TEXT NULL,
    ADD COLUMN calendar_version TEXT NULL,
    ADD COLUMN calendar_hash TEXT NULL;
