"""The rows of a ``SuiTable``: a list model with one role, ``row``.

``row`` maps each column key to the value the cell shows (already formatted), plus
``_key`` (a stable row id) and ``_highlight`` (tints the row). The rows are kept as the
caller hands them; with a ``formatter`` they are kept raw and formatted when the view reads
them, so a change the formatter depends on (a player's state) needs only ``refresh``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    Qt,
)

type Json = dict[str, Any]
type Index = QModelIndex | QPersistentModelIndex

ROW_ROLE: Final = int(Qt.ItemDataRole.UserRole) + 1


class TableRows(QAbstractListModel):
    """SuiTable's model."""

    ROW_ROLE: Final = ROW_ROLE

    def __init__(
        self,
        formatter: Callable[[Json], Json] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._rows: list[Json] = []
        self._format = formatter

    def rowCount(self, parent: Index = QModelIndex()) -> int:  # noqa: B008, N802 (Qt's name)
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: Index, role: int = int(Qt.ItemDataRole.DisplayRole)) -> Any:
        if role != ROW_ROLE or not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        return self._format(row) if self._format is not None else row

    def roleNames(self) -> dict[int, QByteArray]:  # noqa: N802
        return {ROW_ROLE: QByteArray(b"row")}

    @property
    def rows(self) -> list[Json]:
        """The rows as kept (raw, when there is a formatter)."""
        return self._rows

    def replace(self, rows: list[Json]) -> None:
        """All the rows at once (a reset, unless they are the same)."""
        if rows == self._rows:
            return
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def append(self, rows: list[Json]) -> None:
        if not rows:
            return
        first = len(self._rows)
        self.beginInsertRows(QModelIndex(), first, first + len(rows) - 1)
        self._rows.extend(rows)
        self.endInsertRows()

    def index_of(self, field: str, value: Any) -> int:
        """The position of the first row whose ``field`` is ``value``; -1 for none."""
        for position, row in enumerate(self._rows):
            if row.get(field) == value:
                return position
        return -1

    def update(self, position: int, changes: Mapping[str, Any]) -> None:
        self._rows[position] = {**self._rows[position], **changes}
        self.refresh(position)

    def refresh(self, position: int | None = None) -> None:
        """Re-read one row (or all) after something its cells show changed."""
        if not self._rows:
            return
        first = 0 if position is None else position
        last = len(self._rows) - 1 if position is None else position
        self.dataChanged.emit(self.index(first), self.index(last), [ROW_ROLE])


class RoleRows(QAbstractListModel):
    """A list model whose rows are maps and whose roles are their keys (a sidebar).

    ``update`` changes rows in place (``dataChanged``) when only their values change, so a
    view keeps its delegates; it resets only when the number of rows changes."""

    def __init__(self, roles: tuple[str, ...], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._roles = roles
        self._rows: list[Json] = []

    def rowCount(self, parent: Index = QModelIndex()) -> int:  # noqa: B008, N802 (Qt's name)
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: Index, role: int = int(Qt.ItemDataRole.DisplayRole)) -> Any:
        position = role - ROW_ROLE
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        if not 0 <= position < len(self._roles):
            return None
        return self._rows[index.row()].get(self._roles[position])

    def roleNames(self) -> dict[int, QByteArray]:  # noqa: N802
        return {ROW_ROLE + i: QByteArray(name.encode()) for i, name in enumerate(self._roles)}

    @property
    def rows(self) -> list[Json]:
        return self._rows

    def update(self, rows: list[Json]) -> None:
        if len(rows) != len(self._rows):
            self.beginResetModel()
            self._rows = [dict(row) for row in rows]
            self.endResetModel()
            return
        for position, row in enumerate(rows):
            if row != self._rows[position]:
                self._rows[position] = dict(row)
                index = self.index(position)
                self.dataChanged.emit(index, index)
