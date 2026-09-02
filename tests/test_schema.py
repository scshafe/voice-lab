from __future__ import annotations

from pathlib import Path

from cole_voice_lab.capture import RejectedTakeLog
from cole_voice_lab.schema import SCHEMA_MODELS, generate_schemas, main, schema_bytes


def test_schema_bytes_is_stable_json() -> None:
    first_model = SCHEMA_MODELS[sorted(SCHEMA_MODELS)[0]]
    payload = schema_bytes(first_model)

    assert payload.endswith(b"\n")
    assert b'"$defs"' in payload or b'"properties"' in payload
    assert payload == schema_bytes(first_model)


def test_rejected_take_log_is_registered_at_the_durable_schema_name() -> None:
    assert SCHEMA_MODELS["capture-rejected-takes"] is RejectedTakeLog


def test_generate_and_check_exact_schema_set(tmp_path: Path) -> None:
    output = tmp_path / "schemas"

    assert generate_schemas(output, check=False) == []
    assert {path.name for path in output.iterdir()} == {
        f"{name}.schema.json" for name in SCHEMA_MODELS
    }
    assert generate_schemas(output, check=True) == []


def test_check_reports_changed_missing_and_stale_files(tmp_path: Path) -> None:
    output = tmp_path / "schemas"
    generate_schemas(output, check=False)
    names = sorted(SCHEMA_MODELS)
    changed = output / f"{names[0]}.schema.json"
    missing = output / f"{names[1]}.schema.json"
    stale = output / "retired.schema.json"
    changed.write_text("{}\n", encoding="utf-8")
    missing.unlink()
    stale.write_text("{}\n", encoding="utf-8")

    assert generate_schemas(output, check=True) == sorted((str(changed), str(missing), str(stale)))


def test_check_does_not_create_missing_output(tmp_path: Path) -> None:
    output = tmp_path / "absent"

    drift = generate_schemas(output, check=True)

    assert not output.exists()
    assert len(drift) == len(SCHEMA_MODELS)


def test_check_rejects_symlinked_schema_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    link = tmp_path / "schemas"
    generate_schemas(actual, check=False)
    link.symlink_to(actual, target_is_directory=True)

    assert str(link) in generate_schemas(link, check=True)


def test_check_rejects_symlinked_schema_file(tmp_path: Path) -> None:
    output = tmp_path / "schemas"
    generate_schemas(output, check=False)
    names = sorted(SCHEMA_MODELS)
    linked = output / f"{names[0]}.schema.json"
    target = output / f"{names[1]}.schema.json"
    linked.unlink()
    linked.symlink_to(target.name)

    assert str(linked) in generate_schemas(output, check=True)


def test_schema_main_reports_success_and_drift(tmp_path: Path, capsys: object) -> None:
    output = tmp_path / "schemas"
    assert main(["--output", str(output)]) == 0
    assert main(["--check", "--output", str(output)]) == 0

    changed = output / f"{sorted(SCHEMA_MODELS)[0]}.schema.json"
    changed.write_text("{}\n", encoding="utf-8")
    assert main(["--check", "--output", str(output)]) == 1

    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert f"schema drift: {changed}" in captured.out
