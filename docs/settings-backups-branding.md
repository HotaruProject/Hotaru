# Settings, backups and artwork

`!settings` opens the combined settings module. `!conf` and `!config` open
the same menu; `!conf <module> <key> <value>` still works. The `alias`,
`rmalias`, `lsalias` and `prefix` commands belong to this module too.
The examples use the default prefix; use your configured prefix instead.

`!backup` opens the backup menu. `!backup now` creates a native `.hbk` archive
with the existing backup service and sends it to the **Backups** topic in
Hotaru's service forum. It never sends the archive into the chat where the
command was invoked. The archive contains the state database and modules,
as defined by `BackupService`; it is not a copy of the whole installation
or the account's vault/key files.

Automatic delivery is enabled by default, initially runs when the service
forum becomes available, then repeats every 24 hours. Use `!backup off` /
`!backup on`, or change `enabled` and `interval_hours` in `!settings backups`.
Only successful delivery advances the schedule. Failures retry on the next
one-minute task tick. Local retention uses the existing `backup-keep` setting.

Artwork is bundled under `hotaru/assets/`:

- `help.mp4`: the existing animated HELP banner.
- `logo.png`: the existing Hotaru logo used in Settings and Backups.
- `avatar.png`: the image applied to the inline bot and the service forum.
  This is the square HOTARU USERBOT artwork supplied by the owner, converted
  losslessly from BMP to PNG.

The banners use Telegram's native rich-message file attachments. Files upload
directly to Telegram and are cached per bot connection. An upload failure
omits the decoration and leaves the menu usable. No public file host is needed.
The bot avatar is applied immediately after bot creation/recovery, independently
of service-forum setup. The forum avatar is applied during forum setup. A per-target image digest is
stored after success; failed updates are retried on the next setup/restart.
Replacing `avatar.png` causes a new update on the next startup.

Keyboard, in-message and input-button references all use the core AES-GCM
codec backed by GoyGram, with random nonces and separate authenticated domains.
Screen tokens take 49 bytes of the 64-byte Telegram limit. Existing HMAC
references remain accepted for old menus, with the same actor, message,
generation and expiration checks. Stored `config` screens migrate to `settings`.

Run `python -m unittest discover -s checks -v` for regression checks without
logging into Telegram. GoyGram may download the official schema when its local
cache is empty. Real-client rendering and profile changes still require a
running, authenticated Hotaru instance.

Protocol references: [rich messages](https://core.telegram.org/bots/api#inputrichmessagemedia),
[bot profile photos](https://core.telegram.org/method/photos.uploadProfilePhoto),
[official TL schema](https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/mtproto/scheme/api.tl).
