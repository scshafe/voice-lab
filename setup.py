"""Build immutable package resources from their single maintained root source.

Schema migrations remain in root migrations/ for auditing and reversibility;
wheel/app consumers read a generated resource copy and never require a checkout.
"""

from pathlib import Path
from shutil import copyfile

from setuptools import setup
from setuptools.command.build_py import build_py


class RecordingResourceBuild(build_py):
    """Copy canonical SQLite migrations into the built Python resource tree."""

    def run(self) -> None:
        super().run()
        destination = Path(self.build_lib) / "cole_voice_lab/resources/recording_migrations"
        destination.mkdir(parents=True, exist_ok=True)
        sources = sorted((Path(__file__).parent / "migrations").glob("*.sql"))
        if not sources:
            raise RuntimeError("canonical recording index migrations are missing from build source")
        for source in sources:
            copyfile(source, destination / source.name)


setup(cmdclass={"build_py": RecordingResourceBuild})
