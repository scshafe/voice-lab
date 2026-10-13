"""Every object Main.qml reads keeps its class's metaobject.

Connecting a signal to a Python method that is not a declared ``@Slot`` makes PySide add a
dynamic slot to the receiver, which gives that instance a metaobject of its own. QML caches
what it knows about a type by the metaobject's address, process-wide: when such an instance
died and a later object's metaobject was allocated at the same address, a new window saw
the old type's signals and properties ("no signal of the target matches", every binding
``undefined``), in whichever test happened to run then. Two app models must therefore share
each model's metaobject.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import shiboken6
from conftest import RecordingOutput
from voice_lab_desktop.app import AppModel
from voice_lab_desktop.config import Config
from voice_lab_desktop.net import new_http
from voice_lab_desktop.tokens import MemoryStore

#: What Main.qml reads: its context properties (cli.main_window_properties) and the models
#: it reads through properties of the app model.
EXPOSED = (
    "status",
    "uploads",
    "prompts",
    "studio",
    "measurement",
    "corpus",
    "journey",
    "practice",
    "referee",
    "player",
)


def app(tmp_path: Path, name: str) -> AppModel:
    config = Config(data_dir=tmp_path / name, file=tmp_path / f"{name}.toml").validated()
    return AppModel(
        config,
        MemoryStore(None),
        http=new_http(),
        output=RecordingOutput(),
        open_url=lambda _url: None,
        recorder_factory=lambda: None,  # type: ignore[arg-type,return-value]
    )


def metaobject(model: Any) -> int:
    return int(shiboken6.getCppPointer(model.metaObject())[0])


def test_every_model_qml_reads_keeps_its_class_metaobject(tmp_path: Path) -> None:
    first, second = app(tmp_path, "first"), app(tmp_path, "second")
    try:
        assert metaobject(first) == metaobject(second)
        for name in EXPOSED:
            assert metaobject(getattr(first, name)) == metaobject(getattr(second, name)), name
        # The studio's takes, which QML reads through the studio (app.studioModel.takes).
        assert metaobject(first.studio.take_review) == metaobject(second.studio.take_review)
    finally:
        first.shutdown()
        second.shutdown()
