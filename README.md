# iphone-photo-backup

Back up the photos and videos you took with your iPhone camera to any machine you can reach via SSH (a NAS, a home server, a Linux box). Over USB or Wi-Fi, without iCloud, without Photos.app, without an import dialog.

## What it backs up

Only what the camera shot, front and back. The script reads the iPhone's own Photos database (`PhotoData/Photos.sqlite`) and filters by `ZIMPORTEDBY`, so these stay out:

- screenshots and screen recordings
- images from AirDrop, WhatsApp, Safari and other apps
- shared albums
- recently deleted photos
- optionally hidden photos (`--skip-hidden`)

Live Photos come with their video. Originals are copied (HEIC, JPEG, DNG, MOV, ...), edits made in Photos.app are not.

## Layout on the target

Flat, one folder, named `<capture date>_<original name>`:

```
2026-10-15_IMG_3135.HEIC
2026-10-15_IMG_3135.MOV
2026-10-16_IMG_3140.HEIC
```

The date is the capture date in the time zone the photo was taken in. Files keep the capture time as their modification time. If a name is already taken by a different file (iPhones restart `IMG_0001` at some point), the asset's UUID prefix is appended.

## Safety

- Nothing is ever deleted or overwritten, neither on the iPhone nor on the target (`rsync --ignore-existing`).
- After every batch, the size of each uploaded file is checked on the target. Only then is the batch recorded as done.
- A list per target in `~/.local/state/iphone-photo-backup/` remembers what has been backed up (asset UUID and file names), so every run only fetches new assets. A second target gets its own list and a full backup. Delete a list to start over, files already on the target are recognized by name and size and skipped.
- A lost connection (iPhone locked, cable pulled, Wi-Fi gone) ends the run cleanly, the next run picks up where it stopped.
- Fetching and uploading run concurrently in batches of 100 files or 2 GB, so the Mac needs a few GB of free space in `~/Library/Caches`.

## Requirements

- macOS (the iPhone is reached through `usbmuxd`, which ships with macOS)
- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) via pipx: `pipx install pymobiledevice3`
- `ssh` with key based login to the target, `rsync` on both sides. The rsync that ships with macOS works, unless the target folder contains spaces or special characters, then use Homebrew's (`brew install rsync`).
- Any target with a POSIX shell: Linux, a NAS, another Mac.
- The iPhone trusts the Mac (tap "Trust" once over USB). For Wi-Fi, enable "Show this iPhone when on Wi-Fi" in the Finder once, keep the iPhone unlocked during the run.
- Settings › Photos › "Download and Keep Originals" on the iPhone. With "Optimize iPhone Storage", originals living only in iCloud are skipped and counted.

## Install

```sh
git clone https://github.com/Wiredframe/iphone-photo-backup.git
ln -s "$PWD/iphone-photo-backup/iphone-photo-backup" ~/.local/bin/
mkdir -p ~/.config/iphone-photo-backup
cp iphone-photo-backup/config.example.toml ~/.config/iphone-photo-backup/config.toml
```

Edit `config.toml`: `host` is anything `ssh` accepts (an alias from `~/.ssh/config`, `user@host`), `dest` the target folder: absolute, or relative to the home folder on the target (`~/Photos/iPhone`), created if missing. Both can also be given as options.

## Usage

```sh
iphone-photo-backup --probe          # count only: on the iPhone, already backed up, new
iphone-photo-backup                  # back up everything new
iphone-photo-backup --limit 50       # at most 50 assets in this run
iphone-photo-backup --host nas --dest /srv/photos/iphone
iphone-photo-backup --udid <udid>    # pick one iPhone if several are connected
```

USB is preferred over Wi-Fi when both are available.

## How it works

1. `pymobiledevice3` finds the iPhone via `usbmuxd` and opens an AFC session (the same file service Finder and Image Capture use) on the media partition.
2. `Photos.sqlite` and its WAL are copied to a temp folder and queried there. If the database changes during the copy, it retries.
3. For every new camera asset, all files with the same base name in its `DCIM` folder are pulled (photo plus Live Photo video).
4. Batches are uploaded with `rsync`, checked by size via `ls -ln` on the target, then appended to the list of backed-up assets.

## Development

The tests cover everything that doesn't need an iPhone, including a real `rsync` upload through a stand-in `ssh` that runs locally (with Homebrew's rsync and macOS' openrsync, whichever is installed):

```sh
"$(pipx environment --value PIPX_LOCAL_VENVS)/pymobiledevice3/bin/python" -m unittest discover tests
```

## License

MIT
