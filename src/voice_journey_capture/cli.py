"""``voice-journey-capture``: the capture window, plus checks and chores that need no window.

    voice-journey-capture                    open the window
    voice-journey-capture --devices          input devices, the UMIK-1, its level against the pin
    voice-journey-capture --pin-level        pin the UMIK-1's current input level (logged; a re-pin is an era event)
    voice-journey-capture --import-cal PATH  import miniDSP's per-serial cal file
    voice-journey-capture --sessions         local sessions and their upload state
    voice-journey-capture --upload           upload every pending session (uses the stored sign-in)
    voice-journey-capture --check-online     settings, keychain, issuer and server reachable?
    voice-journey-capture --sign-out         forget the stored sign-in
    voice-journey-capture --smoke            run the window offscreen through a simulated session, then exit
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

from . import __version__, chain
from .config import ENV, Config, ConfigError, load_config, settings_file


def qml_path() -> Path:
    return Path(str(files(__package__) / "qml" / "Main.qml"))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="voice-journey-capture", description="Voice Journey's UMIK-1 capture client (sign-in with Pocket ID).")
    ap.add_argument("--config", type=Path, help=f"settings file (default: {settings_file()})")
    ap.add_argument("--server", help=f"the voice-journey server for this run (else ${ENV['server_url']}, the settings file, the default)")
    ap.add_argument("--fixed-port", action="store_true", help="receive the sign-in on 127.0.0.1:53683 instead of a random port")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--check-config", action="store_true", help="print the settings and the keychain backend, then exit")
    group.add_argument("--check-online", action="store_true", help="like --check-config, and also contact the issuer and the server")
    group.add_argument("--devices", action="store_true", help="list input devices, pick the UMIK-1 and read its level")
    group.add_argument("--pin-level", action="store_true", help="pin the UMIK-1's current input level")
    group.add_argument("--import-cal", type=Path, metavar="PATH", help="import the UMIK-1's cal file")
    group.add_argument("--sessions", action="store_true", help="list local sessions and their upload state")
    group.add_argument("--upload", action="store_true", help="upload every pending session, then exit")
    group.add_argument("--retry-failed", action="store_true", help="mark sessions the server refused as pending again")
    group.add_argument("--prune-accepted", action="store_true", help="delete local copies of sessions the server accepted")
    group.add_argument("--sign-out", action="store_true", help="forget the stored sign-in and exit")
    group.add_argument("--smoke", action="store_true", help="run the window offscreen through a simulated session, then exit")
    ap.add_argument("--version", action="version", version=f"voice-journey-capture {__version__}")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.server:
            config = dataclasses.replace(config, server_url=args.server, source=config.source + " + --server",
                                         env_keys=(*config.env_keys, "server_url")).validated()
    except ConfigError as exc:
        print(f"voice-journey-capture: {exc}", file=sys.stderr)
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
    if args.sign_out:
        from .tokens import KeyringStore

        KeyringStore(config.issuer, config.client_id).clear()
        print("signed out (the keychain item is gone)")
        return 0
    return run(config, fixed_port=args.fixed_port)


def check(config: Config, online: bool) -> int:
    from .tokens import KeyringStore, NoKeychainError

    print(f"settings: {config.source}")
    print(f"  server_url  {config.server_url}")
    print(f"  issuer      {config.issuer}")
    print(f"  client_id   {config.client_id}")
    print(f"  distance_cm {config.distance_cm}")
    print(f"  bearer      {config.bearer}")
    print(f"  data dir    {config.data_dir}")
    status = 0
    try:
        print(f"keychain: {KeyringStore.check_backend()}")
    except NoKeychainError as exc:
        print(f"keychain: NONE: {exc}")
        status = 1
    if online:
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
                intake = http.get(f"{config.server_url}/api/intake/sessions/s-check-0000")
                door = "asks for a sign-in" if intake.status_code in (302, 401, 403) else f"unexpected HTTP {intake.status_code}"
                print(f"server: /healthz HTTP {health.status_code}; intake {door}")
                status = status if intake.status_code in (302, 401, 403) else 1
            except httpx.HTTPError as exc:
                print(f"server: unreachable ({type(exc).__name__})")
                status = 1
    return status


def _real_backend():
    from .devices import PortAudioBackend

    return PortAudioBackend()


def devices(config: Config) -> int:
    from .devices import DeviceError, choose_umik
    from .levels import LevelUnreadable, PlatformLevelReader
    from .pin import GuardError, StateStore, guard

    try:
        backend = _real_backend()
        found = backend.list_inputs()
    except DeviceError as exc:
        print(f"devices: {exc}", file=sys.stderr)
        return 1
    for d in found:
        mark = "UMIK-1" if chain.is_umik1(d.name, d.usb_id) else ""
        print(f"  [{d.index}] {d.name} ({d.host_api}, {d.channels} ch, {d.default_rate:.0f} Hz{', usb ' + d.usb_id if d.usb_id else ''}) {mark}")
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
        guard(StateStore(config.data_dir).pin(), level)
        print("pin: matches")
    except GuardError as exc:
        print(f"pin: {exc}")
        return 1
    return 0


def pin_level(config: Config) -> int:
    from .devices import DeviceError, choose_umik
    from .levels import LevelUnreadable, PlatformLevelReader
    from .pin import GuardError, StateStore

    try:
        umik = choose_umik(_real_backend().list_inputs())
        level = PlatformLevelReader().read(umik)
        previous, new = StateStore(config.data_dir).set_pin(level, umik.name)
    except (DeviceError, LevelUnreadable, GuardError) as exc:
        print(f"not pinned: {exc}", file=sys.stderr)
        return 1
    if previous and not previous.level.same_setting(level):
        print(f"re-pinned at {new.level.describe()} (was {previous.level.describe()}): logged as a chain era event")
    else:
        print(f"pinned at {new.level.describe()}")
    return 0


def import_cal(config: Config, path: Path) -> int:
    from .pin import CalFileError, StateStore

    try:
        cal = StateStore(config.data_dir).import_cal_file(path)
    except CalFileError as exc:
        print(f"not imported: {exc}", file=sys.stderr)
        return 1
    print(f"imported {cal.name} (serial {cal.serial or 'unknown'}, sha256 {cal.sha256})")
    return 0


def sessions(config: Config) -> int:
    from .store import SessionStore

    rows = SessionStore(config.data_dir).sessions()
    if not rows:
        print("no local sessions")
    for s in rows:
        error = s.upload.get("lastError")
        print(f"{s.session_id}  {s.status:9}  {s.upload_state:14}  attempts={s.upload.get('attempts', 0)}{'  ' + error if error else ''}")
    return 0


def upload(config: Config) -> int:
    from .auth import Session, SignInRequired
    from .store import SessionStore
    from .tokens import KeyringStore
    from .upload import IntakeClient, new_http, upload_pending

    store = SessionStore(config.data_dir)
    with new_http() as http:
        session = Session(config, KeyringStore(config.issuer, config.client_id), http)
        if not session.signed_in:
            print("not signed in: open the window and sign in first", file=sys.stderr)
            return 1
        try:
            summary = upload_pending(IntakeClient(config.server_url, session, http), store, config.server_url,
                                     on_session=lambda sid, state: print(f"{sid}: {state}"))
        except SignInRequired as exc:
            print(f"sign-in required: {exc}", file=sys.stderr)
            return 1
    print(f"accepted={summary.accepted} pending={summary.pending} refused={summary.failed}")
    return 0 if summary.pending == 0 and summary.failed == 0 else 1


def chores(config: Config, retry: bool) -> int:
    from .store import SessionStore

    store = SessionStore(config.data_dir)
    if retry:
        failed = [s for s in store.sessions() if s.upload_state == "failed"]
        for s in failed:
            store.retry_failed(s.path)
        print(f"{len(failed)} session(s) pending again")
    else:
        print(f"{store.prune_accepted()} accepted session(s) removed locally")
    return 0


def _application():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("voice-journey-capture")
    app.setOrganizationName("scshafe")
    app.setApplicationDisplayName("Voice Journey capture")
    return app


def _engine(controller):
    from PySide6.QtQml import QQmlApplicationEngine

    import scshafe_qt

    engine = QQmlApplicationEngine()
    scshafe_qt.register(engine)
    engine.rootContext().setContextProperty("app", controller)
    engine.load(str(qml_path()))
    return engine


def _teardown(engine, controller) -> None:
    from PySide6.QtCore import QCoreApplication, QEvent

    # QML goes first: bindings must never outlive the controller they read.
    for obj in engine.rootObjects():
        obj.deleteLater()
    engine.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    controller.shutdown()


def run(config: Config, fixed_port: bool = False) -> int:
    app = _application()
    from .controller import Controller
    from .devices import DeviceError
    from .levels import PlatformLevelReader
    from .tokens import KeyringStore

    try:
        backend = _real_backend()
    except DeviceError as exc:
        print(f"voice-journey-capture: {exc}", file=sys.stderr)
        return 1
    controller = Controller(config, KeyringStore(config.issuer, config.client_id), backend, PlatformLevelReader(), fixed_port=fixed_port)
    engine = _engine(controller)
    if not engine.rootObjects():
        print("voice-journey-capture: the window failed to load", file=sys.stderr)
        return 1
    app.aboutToQuit.connect(controller.shutdown)
    controller.start()
    code = app.exec()
    _teardown(engine, controller)
    return code


def smoke(config: Config) -> int:
    """Offscreen, in a temporary data dir: pin, record a whole simulated session, save it; fail on any QML warning."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QUICK_BACKEND", "software")
    import time

    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    problems: list[str] = []

    def handler(kind, _context, message):
        if kind in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg) and "Sans Serif" not in message:
            problems.append(message)

    qInstallMessageHandler(handler)
    app = _application()
    from .controller import Controller
    from .simulated import SimulatedBackend, SimulatedLevelReader
    from .tokens import MemoryStore

    with tempfile.TemporaryDirectory(prefix="vjc-smoke-") as tmp:
        cfg = dataclasses.replace(config, data_dir=Path(tmp), file=Path(tmp) / "config.toml")
        controller = Controller(cfg, MemoryStore(), SimulatedBackend(speed=200, block_frames=4800), SimulatedLevelReader(), auto_upload=False)
        engine = _engine(controller)
        ok = bool(engine.rootObjects())

        def until(predicate, seconds=20.0) -> bool:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                app.processEvents()
                if predicate():
                    return True
                time.sleep(0.005)
            return False

        steps = []
        controller.start()
        steps.append(until(lambda: controller.property("deviceOk")))
        controller.pinLevel()
        controller.setSkipFree(True)
        steps.append(until(lambda: controller.property("canStart")))
        controller.startSession()
        steps.append(until(lambda: controller.property("phase") == "segment"))
        while ok and all(steps) and controller.property("phase") == "segment":
            controller.toggleRecording()
            if controller.property("currentMode") == "manual":
                steps.append(until(lambda: controller.ritual.recording is not None and controller.ritual.recording.seconds > 1.2))
                controller.toggleRecording()
            else:
                steps.append(until(lambda: controller.property("phase") != "recording"))
        controller.finishSession()
        steps.append(controller.property("phase") == "done")
        _teardown(engine, controller)
    qInstallMessageHandler(None)
    if not ok or problems or not all(steps):
        for p in problems:
            print(f"smoke: {p}", file=sys.stderr)
        print(f"smoke: FAILED (window {'loaded' if ok else 'did not load'}, steps {steps})", file=sys.stderr)
        return 1
    print("smoke: ok (the window loaded offscreen and recorded a simulated session, no QML warnings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
