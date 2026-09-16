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


def _schema_keywords(schema: object, keywords: set[str]) -> set[str]:
    """Collect keywords from schema positions only; default/const/enum values are data."""
    if not isinstance(schema, dict):
        return keywords
    keywords.update(schema)
    for key in ("$defs", "properties"):
        for child in schema.get(key, {}).values():
            _schema_keywords(child, keywords)
    for key in ("items", "additionalProperties"):
        _schema_keywords(schema.get(key), keywords)
    for key in ("anyOf", "oneOf", "allOf"):
        for child in schema.get(key, ()):
            _schema_keywords(child, keywords)
    return keywords


def test_prompt_contracts_emit_only_keywords_the_native_validator_supports() -> None:
    """TM4: Swift fails closed on unknown keywords, so tuples must never become sets."""
    import re

    from cole_voice_lab.recording_contracts import RECORDING_SCHEMA_MODELS

    swift = (
        Path(__file__).resolve().parents[1]
        / "apps/macos/Sources/RecordingContracts/RecordingSchemaValidator.swift"
    ).read_text(encoding="utf-8")
    declaration = re.search(r"let supported: Set<String> = \[([^\]]*)\]", swift)
    assert declaration is not None
    supported = set(re.findall(r'"([^"]+)"', declaration.group(1)))
    prompt_definitions: dict[str, object] = {}
    for name, model in SCHEMA_MODELS.items():
        definitions = model.model_json_schema().get("$defs", {})
        if "Prompt" not in definitions:
            continue
        for shared in ("Prompt", "PromptLabel", "PromptSuite"):
            if shared in definitions:
                assert (
                    prompt_definitions.setdefault(shared, definitions[shared])
                    == (definitions[shared])
                ), name
                assert _schema_keywords(definitions[shared], set()) <= supported, name
    assert set(prompt_definitions) == {"Prompt", "PromptLabel", "PromptSuite"}
    for name, model in RECORDING_SCHEMA_MODELS.items():
        assert _schema_keywords(model.model_json_schema(), set()) <= supported, name
