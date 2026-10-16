class BackupError(Exception):
    """The folder is not a readable backup, or a manifest is corrupt."""


class EncryptedBackupError(BackupError):
    """File contents of an encrypted backup were requested before it was unlocked."""


class MissingFileError(BackupError):
    """The manifest lists a file whose data is not on disk (common in partial backups)."""
