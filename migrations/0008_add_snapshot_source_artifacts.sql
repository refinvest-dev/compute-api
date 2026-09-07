ALTER TABLE compute_dataset_snapshots
    ADD COLUMN source_artifacts JSONB NOT NULL DEFAULT '[]'::jsonb;
