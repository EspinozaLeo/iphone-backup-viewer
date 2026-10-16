# iPhone backup format — research notes (2026-10-16)

Gathered by a research agent. Items marked UNVERIFIED were not confirmed against a primary source.
Current iOS at time of writing: **27**.

## On-disk format

- **File ID** = `SHA1("{domain}-{relativePath}")`, e.g. `HomeDomain-Library/SMS/sms.db` → `3d0d7e5f…`.
- **iOS 4–9**: flat directory + binary `Manifest.mbdb` (header `mbdb\5\0`). Records: domain, path, link target,
  hash, wrapped key, mode, times, size, protection class. **The mbdb is plaintext even in encrypted backups.**
  Early `Manifest.mbdx` index — UNVERIFIED.
- **iOS 10+**: `Manifest.db` (SQLite), table `Files(fileID, domain, relativePath, flags, file)`. `file` is an
  NSKeyedArchiver bplist (ProtectionClass, EncryptionKey, Size, mtime). Data files live at `fileID[:2]/fileID`.
  ([dunhamsteve/ios](https://github.com/dunhamsteve/ios))
- **Backup-list fields**
  - `Info.plist`: Device Name, Display Name, Product Type, Product Version, Build Version, Last Backup Date,
    Serial Number, Unique Identifier.
  - `Manifest.plist`: `IsEncrypted`, `BackupKeyBag`, `ManifestKey`, `Lockdown{DeviceName, ProductType, ProductVersion}`, `Date`.
  - `Status.plist`: `Version` (3.x ⇒ Manifest.db), `Date`, `IsFullBackup`, `SnapshotState`, `BackupState`.
    Skip in-progress `Snapshot/` dirs.
- iOS 17/18/26/27: no local-format change found (UNVERIFIED negative). Since iOS 16.1 the device asks for its passcode to start a backup.

## Encryption

- `BackupKeyBag` (in Manifest.plist) is TLV: UUID / CLAS / WRAP / KTYP / WPKY. First block holds SALT/ITER;
  iOS 10.2+ adds DPSL/DPIC. ([irq5](https://irq5.io/2017/03/07/cracking-itunes-backup-passwords-with-hashcat/))
- **10.2+ KDF**: PBKDF2-SHA256 (DPSL, 10,000,000 rounds) → PBKDF2-SHA1 (SALT, 10,000) → RFC 3394 unwrap of
  class keys. Measured **~2.8 s** on the dev PC (Python/OpenSSL). Pre-10.2 is the 10k SHA1 step only.
  Cache the derived key per session.
- **Per-file**: strip the 4-byte prefix from EncryptionKey, unwrap with the class key, then AES-256-CBC with zero IV and PKCS7 padding.
- **Manifest.db is itself encrypted** on 10.2+. `ManifestKey` = 4-byte LE class + wrapped key.
- Encryption is off by default, but saved passwords, Wi-Fi, website history, **call history** and Health exist
  *only* in encrypted backups ([Apple 108353](https://support.apple.com/en-us/108353)). Treat encrypted as first-class.

## Default backup folders

| Source | Path |
|---|---|
| iTunes (desktop installer) | `%APPDATA%\Apple Computer\MobileSync\Backup` |
| iTunes (Microsoft Store) / Apple Devices app | `%USERPROFILE%\Apple\MobileSync\Backup` |
| macOS | `~/Library/Application Support/MobileSync/Backup` |

Users sometimes relocate these with junctions/symlinks. Also allow "open folder…" for backups copied elsewhere.

## Where the data lives

| Data | Domain / path | Gotchas |
|---|---|---|
| Messages | `HomeDomain` / `Library/SMS/sms.db` | Tables message, handle, chat, chat_message_join, attachment. iOS 5 had `madrid_*` columns. Dates changed from seconds to nanoseconds since 2001-01-01: detect by magnitude. `text` is often NULL and the body is in the `attributedBody` typedstream (NSArchiver). The cutover version is unconfirmed, so **always fall back**. Attachment `~/Library/SMS/Attachments/…` resolves to `MediaDomain`. Messages in iCloud is excluded from local backups. |
| Photos/videos | `CameraRollDomain` / `Media/DCIM/1xxAPPLE/IMG_*` | Edits in `Media/PhotoData/Mutations/`. DB: `Media/PhotoData/Photos.sqlite` (`ZGENERICASSET` → `ZASSET` around iOS 14, inferred). HEIC/HEVC from iOS 11 (iPhone 7+). iCloud Photos is excluded from backups and Optimize Storage keeps originals off the device, so expect a partial DCIM. Possible cache under `Media/PhotoData/CPLAssets/` (UNVERIFIED). |
| Contacts | `HomeDomain` / `Library/AddressBook/AddressBook.sqlitedb` | |
| Call history | `HomeDomain` / `Library/CallHistoryDB/CallHistory.storedata` (ZCALLRECORD, iOS 8+) | iOS ≤7: `WirelessDomain` / `Library/CallHistory/call_history.db`. Encrypted backups only. |
| Notes | iOS ≤8: `HomeDomain` / `Library/Notes/notes.sqlite` (HTML). iOS 9+: `AppDomainGroup-group.com.apple.notes` / `NoteStore.sqlite` | `ZICNOTEDATA.ZDATA` is gzip + protobuf ([apple_cloud_notes_parser](https://github.com/threeplanetssoftware/apple_cloud_notes_parser)) |
| Voicemail | `HomeDomain` / `Library/Voicemail/voicemail.db` + `*.amr` | UNVERIFIED for current iOS |
| WhatsApp | `AppDomainGroup-group.net.whatsapp.WhatsApp.shared` / `ChatStorage.sqlite` (ZWAMESSAGE), media in `Message/Media/` | Business app: `…WhatsAppSMB.shared` |

## Libraries

| Project | Lang | License | Status (2026) |
|---|---|---|---|
| iphone_backup_decrypt | Python | MIT | 0.11.2 (2026-09-24), active. Encrypted only, iOS 13+ |
| iLEAPP | Python | MIT | v2026.4.4 (2026-10-02), very active. Reads encrypted backups |
| WhatsApp-Chat-Exporter | Python | MIT | active |
| python-typedstream | Python | LGPL-3.0+ | 0.1.0 (2023) |
| iOSbackup | Python | LGPL | 0.9.925 (2022), stale |
| mvt | Python | MVT License 1.1 (modified MPL, consent restriction) | active. **Not usable as a dependency** |
| crabapple | Rust | GPL-3.0+ | 0.4.7. Encrypted 10.2+ only |
| imessage-exporter / crabstep | Rust | GPL-3.0 | 4.3.0 / 0.7.0 (2026-09). Good reference for typedstream + iMessage quirks |
| libimobiledevice (idevicebackup2) | C | LGPL-2.1+ | 1.4.0 (2025-10). Device-side tool |
| ibackuptool | Node | MIT | stale (2023) |
| .NET (reedy, iPhoneTools) | C# | MIT | stale (2019–2022) |

Neither iphone_backup_decrypt nor crabapple reads iOS 4–9 `Manifest.mbdb`, so we write that parser ourselves
([pearback](https://github.com/mx-pycoder/pearback) is a reference). GPL code can be read for reference, not linked into an MIT/Apache app.
