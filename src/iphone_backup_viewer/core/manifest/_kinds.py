from __future__ import annotations

from ..models import FileKind

_S_IFMT = 0xF000
_MODE_KINDS = {
    0x8000: FileKind.FILE,
    0x4000: FileKind.DIRECTORY,
    0xA000: FileKind.SYMLINK,
}


def kind_from_mode(mode: int | None) -> FileKind | None:
    """POSIX st_mode file-type bits to a FileKind. None if the bits are absent or unrecognised."""
    if not isinstance(mode, int) or isinstance(mode, bool):
        return None
    return _MODE_KINDS.get(mode & _S_IFMT)
