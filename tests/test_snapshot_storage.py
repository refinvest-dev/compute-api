import pytest

from refinvest.app.config import Settings
from refinvest.app.snapshot_storage import snapshot_storage_path


def test_builds_s3_snapshot_storage_path_from_explicit_bucket_and_prefix() -> None:
    path = snapshot_storage_path(
        Settings(
            None,
            snapshot_storage_backend="s3",
            snapshot_storage_s3_bucket="refinvest-snapshots",
            snapshot_storage_s3_prefix="daily",
        ),
        "snapshot-1",
    )

    assert path == "s3://refinvest-snapshots/daily/snapshot-1"


@pytest.mark.parametrize("prefix", [None, "", "/daily", "daily/../archive"])
def test_rejects_missing_or_non_normalized_s3_prefix(prefix: str | None) -> None:
    with pytest.raises(ValueError, match="S3_PREFIX|normalized"):
        snapshot_storage_path(
            Settings(
                None,
                snapshot_storage_backend="s3",
                snapshot_storage_s3_bucket="refinvest-snapshots",
                snapshot_storage_s3_prefix=prefix,
            ),
            "snapshot-1",
        )
