import pytest

from ibm_calibration_collector.backfill import parse_args


def test_backfill_arguments_reject_conflicting_run_selection():
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--new-run",
                "--run-id",
                "00000000-0000-0000-0000-000000000001",
            ]
        )
