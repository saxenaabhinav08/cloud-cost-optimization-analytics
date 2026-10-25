from datetime import date
from pathlib import Path
import sys
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy.dialects import mysql

from cloud_cost_analytics.database import database_url_from_env
from cloud_cost_analytics.mysql_import import import_csv, import_records, main, record_to_row
from cloud_cost_analytics.validation import DataValidationError, load_billing_data
from sqlalchemy.exc import IntegrityError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE = PROJECT_ROOT / "data" / "raw" / "fictional_aws_billing.csv"


def test_database_url_uses_mysql_driver_and_escapes_password():
    url = database_url_from_env(
        {
            "DATABASE_NAME": "cloud_cost_analytics",
            "DATABASE_USER": "local_user",
            "DATABASE_PASSWORD": "a@b:/?secret",
            "DATABASE_HOST": "127.0.0.1",
            "DATABASE_PORT": "3307",
        }
    )
    assert url.drivername == "mysql+pymysql"
    assert url.password == "a@b:/?secret"
    assert url.host == "127.0.0.1"
    assert url.port == 3307
    assert url.database == "cloud_cost_analytics"


def test_database_url_requires_local_settings():
    with pytest.raises(ValueError, match="DATABASE_NAME, DATABASE_USER, DATABASE_PASSWORD"):
        database_url_from_env({})


def test_database_url_rejects_non_mysql_driver():
    with pytest.raises(ValueError, match=r"mysql\+pymysql"):
        database_url_from_env({"DATABASE_URL": "sqlite:///not_mysql.db"})


def test_fingerprint_is_deterministic_and_month_maps_to_first_day():
    record = load_billing_data(SAMPLE)[0]
    first = record_to_row(record)
    second = record_to_row(record)
    assert first["record_fingerprint"] == second["record_fingerprint"]
    assert first["billing_month"] == date(2025, 1, 1)
    assert len(first["record_fingerprint"]) == 64


def test_import_submits_plain_mysql_inserts_in_one_transaction():
    records = load_billing_data(SAMPLE)[:3]
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value

    submitted = import_records(records, engine)

    assert submitted == 3
    engine.begin.assert_called_once_with()
    assert connection.execute.call_count == 3
    rows = []
    for statement, row in (call.args for call in connection.execute.call_args_list):
        compiled = str(statement.compile(dialect=mysql.dialect()))
        assert compiled.startswith("INSERT INTO billing_records")
        assert "ON DUPLICATE KEY UPDATE" not in compiled
        rows.append(row)
    assert all(row["is_fictional"] is True for row in rows)
    assert len({row["record_fingerprint"] for row in rows}) == 3


def test_reimporting_identical_rows_submits_same_idempotency_keys():
    records = load_billing_data(SAMPLE)[:2]
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    call_count = 0

    def simulate_unique_fingerprint(statement, row):
        nonlocal call_count
        call_count += 1
        if call_count > len(records):
            raise IntegrityError("INSERT", row, Exception(1062, "Duplicate entry"))

    connection.execute.side_effect = simulate_unique_fingerprint

    assert import_records(records, engine) == 2
    assert import_records(records, engine) == 2

    execute_calls = connection.execute.call_args_list
    first_fingerprints = [call.args[1]["record_fingerprint"] for call in execute_calls[:2]]
    second_fingerprints = [call.args[1]["record_fingerprint"] for call in execute_calls[2:]]
    assert first_fingerprints == second_fingerprints


def test_import_csv_validates_before_creating_a_database_engine(tmp_path, monkeypatch):
    invalid_csv = tmp_path / "not_fictional.csv"
    invalid_csv.write_text(
        SAMPLE.read_text(encoding="utf-8").replace(",true,", ",false,", 1),
        encoding="utf-8",
    )
    create_engine = Mock(side_effect=AssertionError("engine must not be created"))
    monkeypatch.setattr("cloud_cost_analytics.mysql_import.create_db_engine", create_engine)

    with pytest.raises(DataValidationError, match="fictional data only"):
        import_csv(invalid_csv)
    create_engine.assert_not_called()


def test_empty_import_does_not_open_transaction():
    engine = Mock()
    assert import_records([], engine) == 0
    engine.begin.assert_not_called()


def test_cli_redacts_database_error_details(monkeypatch, capsys):
    fake_error = IntegrityError(
        "connection", {}, Exception("simulated credential-like value")
    )
    monkeypatch.setattr("cloud_cost_analytics.mysql_import.import_csv", Mock(side_effect=fake_error))
    monkeypatch.setattr(sys, "argv", ["cloud_cost_analytics.mysql_import"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "IntegrityError" in captured.err
    assert "simulated credential-like value" not in captured.err
