"""``voice-lab-desktop``: the app's window, plus checks and chores that need no window.

    voice-lab-desktop                    open the window
    voice-lab-desktop --measure          open the window on the UMIK-1 measurement screen
    voice-lab-desktop --check-config     settings and the keychain
    voice-lab-desktop --check-online     also the issuer and the server, reachable?
    voice-lab-desktop --sign-out         forget the stored sign-in
    voice-lab-desktop --smoke            the window offscreen (every screen, a simulated session)
    voice-lab-desktop --install-desktop-entry [--replace]
                                         Linux: add the app to the desktop's menu (a .desktop
                                         file and its icon under $XDG_DATA_HOME)

The measurement capture's chores (voice-journey-capture's flags, unchanged):

    voice-lab-desktop --devices          input devices, the UMIK-1, its level against the pin
    voice-lab-desktop --pin-level        pin the UMIK-1's input level (a re-pin is an era event)
    voice-lab-desktop --import-cal PATH  import miniDSP's per-serial cal file
    voice-lab-desktop --sessions         local sessions and their upload state
    voice-lab-desktop --upload           upload every pending session (/api/v1/intake, the ledger)
    voice-lab-desktop --retry-failed     refused sessions back to pending (after a server fix)
    voice-lab-desktop --prune-accepted   delete local copies the server has accepted
    voice-lab-desktop --import-capture-data [DIR]
                                         copy voice-journey-capture's level pin, cal file and
                                         sessions into this app's data dir (never moves or
                                         deletes anything; refuses to overwrite)
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import __version__
from .config import ENV, Config, ConfigError, load_config, settings_file
from .measurement import chain

APP_NAME = "voice-lab-desktop"


def qml_dir() -> Path:
    return Path(str(files(__package__) / "qml"))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=APP_NAME, description="voice-lab's desktop app (sign-in with Pocket ID)."
    )
    ap.add_argument("--config", type=Path, help=f"settings file (default: {settings_file()})")
    ap.add_argument(
        "--server",
        help=f"the voice-lab server for this run (else ${ENV['server_url']}, the settings file)",
    )
    ap.add_argument(
        "--fixed-port",
        action="store_true",
        help="receive the sign-in on 127.0.0.1:53683 instead of a random port",
    )
    group = ap.add_mutually_exclusive_group()
    group.add_argument(
        "--measure", action="store_true", help="open the window on the measurement screen"
    )
    group.add_argument(
        "--check-config", action="store_true", help="print the settings and the keychain backend"
    )
    group.add_argument(
        "--check-online",
        action="store_true",
        help="like --check-config, and also contact the issuer and the server",
    )
    group.add_argument(
        "--devices", action="store_true", help="list input devices, the UMIK-1 and its level"
    )
    group.add_argument("--pin-level", action="store_true", help="pin the UMIK-1's input level")
    group.add_argument("--import-cal", type=Path, metavar="PATH", help="import the UMIK-1 cal file")
    group.add_argument("--sessions", action="store_true", help="list local measurement sessions")
    group.add_argument("--upload", action="store_true", help="upload every pending session")
    group.add_argument(
        "--retry-failed", action="store_true", help="mark refused sessions pending again"
    )
    group.add_argument(
        "--prune-accepted", action="store_true", help="delete local copies the server accepted"
    )
    group.add_argument(
        "--import-capture-data",
        nargs="?",
        const="",
        metavar="DIR",
        help="copy voice-journey-capture's data dir into this app's (default: its usual place)",
    )
    group.add_argument("--sign-out", action="store_true", help="forget the stored sign-in")
    group.add_argument(
        "--smoke", action="store_true", help="run the window offscreen once, then exit"
    )
    group.add_argument(
        "--install-desktop-entry",
        action="store_true",
        help="Linux: add the app to the desktop's menu (a .desktop file and its icon)",
    )
    ap.add_argument(
        "--replace",
        action="store_true",
        help="with --install-desktop-entry: overwrite an entry or icon that differs",
    )
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--capture-worker"]:
        # The capture worker's own process (D-039): no settings, no Qt.
        from .capture import worker

        return worker.main(arguments[1:])
    parser = build_parser()
    args = parser.parse_args(arguments)
    if args.replace and not args.install_desktop_entry:
        parser.error("--replace goes with --install-desktop-entry")
    if args.install_desktop_entry:
        from . import desktop_entry

        return desktop_entry.main(replace=args.replace)
    try:
        config = load_config(args.config)
        if args.server:
            config = dataclasses.replace(
                config,
                server_url=args.server,
                source=config.source + " + --server",
                env_keys=(*config.env_keys, "server_url"),
            ).validated()
    except ConfigError as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        return 2
    if args.check_config or args.check_online:
        return check(config, online=args.check_online)
    if args.smoke:
        return smoke(config)
    if args.devices:
        return devices(config)
    if args.pin_level:
        return pin_level(config)
    if args.import_cal:
        return import_cal(config, args.import_cal)
    if args.sessions:
        return sessions(config)
    if args.upload:
        return upload(config)
    if args.retry_failed or args.prune_accepted:
        return chores(config, retry=args.retry_failed)
    if args.import_capture_data is not None:
        source = Path(args.import_capture_data) if args.import_capture_data else None
        return import_capture_data(config, source)
    if args.sign_out:
        from .tokens import KeyringStore

        KeyringStore(config.issuer, config.client_id).clear()
        print("signed out (the keychain item is gone)")
        return 0
    if args.measure:
        return run(config, fixed_port=args.fixed_port, start_screen="measurement")
    return run(config, fixed_port=args.fixed_port)


def check(config: Config, online: bool) -> int:
    from .tokens import KeyringStore, NoKeychainError

    print(f"settings: {config.source}")
    print(f"  server_url  {config.server_url or '(not set)'}")
    print(f"  issuer      {config.issuer or '(not set)'}")
    print(f"  client_id   {config.client_id}")
    print(f"  distance_cm {config.distance_cm}")
    print(f"  bearer      {config.bearer}")
    print(f"  data dir    {config.data_dir}")
    status = 0 if config.configured else 1
    try:
        print(f"keychain: {KeyringStore.check_backend()}")
    except NoKeychainError as exc:
        print(f"keychain: NONE: {exc}")
        status = 1
    if online and config.configured:
        import httpx

        from .auth import AuthError, discover

        with httpx.Client(timeout=15, follow_redirects=False) as http:
            try:
                disc = discover(config, http)
                print(f"issuer: ok ({disc.authorization_endpoint})")
            except (AuthError, httpx.HTTPError) as exc:
                print(f"issuer: FAILED ({type(exc).__name__}: {exc})")
                status = 1
            try:
                health = http.get(f"{config.server_url}/healthz")
                meta = http.get(f"{config.server_url}/api/v1/meta")
                door = (
                    "asks for a sign-in"
                    if meta.status_code in (302, 401, 403)
                    else f"unexpected HTTP {meta.status_code}"
                )
                print(f"server: /healthz HTTP {health.status_code}; /api/v1 {door}")
                status = status if meta.status_code in (302, 401, 403) else 1
            except httpx.HTTPError as exc:
                print(f"server: unreachable ({type(exc).__name__})")
                status = 1
    return status


# -- the measurement capture's chores (voice-journey-capture's, unchanged) -------------------


def _real_backend() -> Any:
    from .measurement.devices import PortAudioBackend

    return PortAudioBackend()


def devices(config: Config) -> int:
    from .measurement.devices import DeviceError, choose_umik
    from .measurement.levels import LevelUnreadable, PlatformLevelReader
    from .measurement.pin import GuardError, StateStore, guard

    try:
        backend = _real_backend()
        found = backend.list_inputs()
    except DeviceError as exc:
        print(f"devices: {exc}", file=sys.stderr)
        return 1
    for d in found:
        mark = "UMIK-1" if chain.is_umik1(d.name, d.usb_id) else ""
        usb = ", usb " + d.usb_id if d.usb_id else ""
        detail = f"{d.host_api}, {d.channels} ch, {d.default_rate:.0f} Hz{usb}"
        print(f"  [{d.index}] {d.name} ({detail}) {mark}")
    try:
        umik = choose_umik(found)
    except DeviceError as exc:
        print(f"UMIK-1: {exc}")
        return 1
    print(f"UMIK-1: {umik.describe()}")
    try:
        level = PlatformLevelReader().read(umik)
    except LevelUnreadable as exc:
        print(f"level: UNREADABLE ({exc}); recording would be refused")
        return 1
    print(f"level: {level.describe()}")
    try:
        guard(StateStore(config.measurement_dir).pin(), level)
        print("pin: matches")
    except GuardError as exc:
        print(f"pin: {exc}")
        return 1
    return 0


def pin_level(config: Config) -> int:
    from .measurement.devices import DeviceError, choose_umik
    from .measurement.levels import LevelUnreadable, PlatformLevelReader
    from .measurement.pin import GuardError, StateStore

    try:
        umik = choose_umik(_real_backend().list_inputs())
        level = PlatformLevelReader().read(umik)
        previous, new = StateStore(config.measurement_dir).set_pin(level, umik.name)
    except (DeviceError, LevelUnreadable, GuardError) as exc:
        print(f"not pinned: {exc}", file=sys.stderr)
        return 1
    if previous and not previous.level.same_setting(level):
        was = previous.level.describe()
        print(f"re-pinned at {new.level.describe()} (was {was}): logged as a chain era event")
    else:
        print(f"pinned at {new.level.describe()}")
    return 0


def import_cal(config: Config, path: Path) -> int:
    from .measurement.pin import CalFileError, StateStore

    try:
        cal = StateStore(config.measurement_dir).import_cal_file(path)
    except CalFileError as exc:
        print(f"not imported: {exc}", file=sys.stderr)
        return 1
    print(f"imported {cal.name} (serial {cal.serial or 'unknown'}, sha256 {cal.sha256})")
    return 0


def sessions(config: Config) -> int:
    from .measurement.store import SessionStore

    rows = SessionStore(config.measurement_dir).sessions()
    if not rows:
        print("no local sessions")
    for s in rows:
        error = s.upload.get("lastError")
        attempts = s.upload.get("attempts", 0)
        tail = "  " + error if error else ""
        print(f"{s.session_id}  {s.status:9}  {s.upload_state:14}  attempts={attempts}{tail}")
    return 0


def upload(config: Config) -> int:
    """Every pending measurement session through ``/api/v1/intake`` (the D-021 ledger)."""
    from .auth import Session, SignInRequired
    from .measurement import home_host
    from .measurement.store import SessionStore
    from .net import new_http
    from .tokens import KeyringStore

    try:
        server = config.require_server()
        library = home_host.library_layout(config.library_dir)
    except ConfigError as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        return 2
    store = SessionStore(config.measurement_dir)
    with new_http() as http:
        session = Session(config, KeyringStore(config.issuer, config.client_id), http)
        if not session.signed_in:
            print("not signed in: open the window and sign in first", file=sys.stderr)
            return 1
        try:
            summary = home_host.upload_pending(
                store,
                library=library,
                server_url=server,
                bearer=session.bearer,
                on_session=lambda sid, state: print(f"{sid}: {state}"),
            )
        except home_host.HomeHostNotReady as exc:
            print(f"not uploaded: {exc}", file=sys.stderr)
            return 1
        except SignInRequired as exc:
            print(f"sign-in required: {exc}", file=sys.stderr)
            return 1
    print(f"accepted={summary.accepted} pending={summary.pending} refused={summary.failed}")
    return 0 if summary.pending == 0 and summary.failed == 0 else 1


def chores(config: Config, retry: bool) -> int:
    from .measurement.store import SessionStore

    store = SessionStore(config.measurement_dir)
    if retry:
        failed = [s for s in store.sessions() if s.upload_state == "failed"]
        for s in failed:
            store.retry_failed(s.path)
        print(f"{len(failed)} session(s) pending again")
    else:
        print(f"{store.prune_accepted()} accepted session(s) removed locally")
    return 0


def capture_client_data_dir() -> Path:
    """Where voice-journey-capture kept its data (its ``config.default_data_dir``)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "voice-journey-capture"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "voice-journey-capture"


#: What voice-journey-capture kept in its data dir that the measurement capture reads.
CAPTURE_DATA = ("state.json", "level-pins.jsonl", "calibration", "sessions")


def import_capture_data(config: Config, source: Path | None) -> int:
    """Copy the old client's level pin and its log, the cal file and the sessions into
    this app's measurement dir. Nothing is moved or deleted; nothing is overwritten."""
    source = source or capture_client_data_dir()
    target = config.measurement_dir
    if not source.is_dir():
        print(f"{APP_NAME}: no voice-journey-capture data at {source}", file=sys.stderr)
        return 1
    present = [name for name in CAPTURE_DATA if (source / name).exists()]
    clash = [name for name in present if (target / name).exists()]
    if clash:
        print(
            f"{APP_NAME}: {target} already holds {', '.join(clash)}; nothing was copied",
            file=sys.stderr,
        )
        return 1
    for name in present:
        if (source / name).is_symlink():
            print(f"{APP_NAME}: {source / name} is a symlink; nothing was copied", file=sys.stderr)
            return 1
    target.mkdir(parents=True, exist_ok=True)
    for name in present:
        origin = source / name
        if origin.is_dir():
            shutil.copytree(origin, target / name, symlinks=True)
        else:
            shutil.copy2(origin, target / name)
        print(f"copied {name}")
    if not present:
        print(f"nothing to copy from {source}")
    return 0


# -- the windows ---------------------------------------------------------------------------


def _application() -> Any:
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from . import desktop_entry

    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("voice-lab")
    app.setApplicationDisplayName("voice-lab")
    # The .desktop file's id (desktop_entry.py): a Wayland compositor matches the window to it.
    app.setDesktopFileName(desktop_entry.DESKTOP_ID)
    app.setWindowIcon(QIcon(str(desktop_entry.icon_file())))
    return app


def _engine(properties: dict[str, Any], qml: Path) -> Any:
    import scshafe_qt
    from PySide6.QtQml import QQmlApplicationEngine

    engine = QQmlApplicationEngine()
    scshafe_qt.register(engine)
    engine.addImportPath(str(qml_dir()))
    for name, value in properties.items():
        engine.rootContext().setContextProperty(name, value)
    engine.load(str(qml))
    return engine


def main_window_properties(model: Any) -> dict[str, Any]:
    """The context properties Main.qml reads."""
    return {
        "app": model,
        "status": model.status,
        "uploads": model.uploads,
        "measurement": model.measurement,
        "corpus": model.corpus,
        "journey": model.journey,
        "practice": model.practice,
        "referee": model.referee,
        "player": model.player,
        "approval": model.approval,
    }


def load_main_window(model: Any) -> Any:
    return _engine(main_window_properties(model), qml_dir() / "Main.qml")


def teardown(engine: Any, shutdown: Callable[[], None]) -> None:
    from PySide6.QtCore import QCoreApplication, QEvent

    # QML goes first: bindings must never outlive the models they read.
    for obj in engine.rootObjects():
        obj.deleteLater()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    shutdown()


def run(config: Config, fixed_port: bool = False, start_screen: str | None = None) -> int:
    app = _application()
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    from .app import AppModel, QuitGuard, keyring_store
    from .net import new_http

    model = AppModel(
        config,
        keyring_store(config),
        http=new_http(),
        open_url=lambda url: QDesktopServices.openUrl(QUrl(url)),
        fixed_port=fixed_port,
        start_screen=start_screen,
    )
    engine = load_main_window(model)
    if not engine.rootObjects():
        print(f"{APP_NAME}: the window failed to load", file=sys.stderr)
        return 1
    app.aboutToQuit.connect(model.shutdown)
    quit_guard = QuitGuard(model)  # Quit asks about an unsaved trim edit too
    app.installEventFilter(quit_guard)
    model.start()
    code = int(app.exec())
    app.removeEventFilter(quit_guard)
    teardown(engine, model.shutdown)
    return code


def smoke(config: Config) -> int:
    """Offscreen, in a temporary data dir: the window loads (not set up, signed out), shows
    every screen, and the measurement screen pins, records a whole simulated session and
    saves it; any QML warning fails it."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    problems: list[str] = []

    def handler(kind: QtMsgType, _context: Any, message: str) -> None:
        bad = (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg)
        if kind in bad and "Sans Serif" not in message:
            problems.append(message)

    qInstallMessageHandler(handler)
    app = _application()
    from .app import SCREENS, AppModel
    from .capture.process import WorkerRecorder
    from .measurement.simulated import SimulatedBackend, SimulatedLevelReader
    from .net import new_http
    from .tokens import MemoryStore

    def until(predicate: Callable[[], bool], seconds: float = 20.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        return False

    steps: list[bool] = []
    with tempfile.TemporaryDirectory(prefix="vld-smoke-") as tmp:
        cfg = dataclasses.replace(
            config, server_url="", issuer="", data_dir=Path(tmp), file=Path(tmp) / "config.toml"
        )
        model = AppModel(
            cfg,
            MemoryStore(),
            http=new_http(),
            open_url=lambda url: None,
            measurement_backend=SimulatedBackend(speed=200, block_frames=4800),
            level_reader=SimulatedLevelReader(),
            # Record → Prompts' worker on its simulated UMIK-1: the smoke opens no device.
            recorder_factory=lambda: WorkerRecorder(simulated={"speed": 20.0}),
        )
        engine = load_main_window(model)
        ok = bool(engine.rootObjects())
        model.start()
        for key, *_ in SCREENS:
            model.navigate(key)
            app.processEvents()
        model.navigate("measurement")
        controller = model.measurement
        steps.append(until(lambda: bool(controller.property("deviceOk"))))
        controller.pinLevel()
        controller.setSkipFree(True)
        steps.append(until(lambda: bool(controller.property("canStart"))))
        controller.startSession()
        steps.append(until(lambda: controller.property("phase") == "segment"))
        while ok and all(steps) and controller.property("phase") == "segment":
            controller.toggleRecording()
            if controller.property("currentMode") == "manual":

                def long_enough() -> bool:
                    ritual = controller.ritual
                    recording = ritual.recording if ritual is not None else None
                    return recording is not None and recording.seconds > 1.2

                steps.append(until(long_enough))
                controller.toggleRecording()
            else:
                steps.append(until(lambda: controller.property("phase") != "recording"))
        controller.finishSession()
        steps.append(controller.property("phase") == "done")
        steps.append(until(lambda: model.uploads.pendingCount == 1))
        teardown(engine, model.shutdown)
    qInstallMessageHandler(None)
    if not ok or problems or not all(steps):
        for p in problems:
            print(f"smoke: {p}", file=sys.stderr)
        loaded = "loaded" if ok else "did not load"
        print(f"smoke: FAILED (window {loaded}, steps {steps})", file=sys.stderr)
        return 1
    print("smoke: ok (every screen loaded offscreen, a simulated session recorded, no warnings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
