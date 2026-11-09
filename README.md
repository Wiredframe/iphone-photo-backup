# iphone-fotos-sichern

Back up the photos and videos you took with your iPhone camera to any machine you can reach via SSH (a NAS, a home server, a Linux box). Over USB or Wi-Fi, without iCloud, without Photos.app, without an import dialog. The name is German for "back up iPhone photos", and so is the command line output.

## What it backs up

Only what the camera shot, front and back. The script reads the iPhone's own Photos database (`PhotoData/Photos.sqlite`) and filters by `ZIMPORTEDBY`, so these stay out:

- screenshots and screen recordings
- images from AirDrop, WhatsApp, Safari and other apps
- shared albums
- recently deleted photos
- optionally hidden photos (`--ohne-ausgeblendete`)

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
- `~/.local/state/iphone-fotos-sichern/gesichert.tsv` remembers what has been backed up (asset UUID and file names), so every run only fetches new assets. Delete it to start over, existing files on the target are recognized and skipped.
- A lost connection (iPhone locked, cable pulled, Wi-Fi gone) ends the run cleanly, the next run picks up where it stopped.
- Fetching and uploading run concurrently in batches of 100 files or 2 GB.

## Requirements

- macOS (the iPhone is reached through `usbmuxd`, which ships with macOS)
- [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) via pipx: `pipx install pymobiledevice3`
- `ssh` and `rsync`, with key based login to the target
- GNU `find` on the target (any Linux; `-printf` is used to list sizes)
- The iPhone trusts the Mac (tap "Trust" once over USB). For Wi-Fi, enable "Show this iPhone when on Wi-Fi" in the Finder once, keep the iPhone unlocked during the run.
- Settings › Photos › "Download and Keep Originals" on the iPhone. With "Optimize iPhone Storage", originals living only in iCloud are skipped and counted.

## Install

```sh
git clone https://github.com/Wiredframe/iphone-fotos-sichern.git
ln -s "$PWD/iphone-fotos-sichern/iphone-fotos-sichern" ~/.local/bin/
mkdir -p ~/.config/iphone-fotos-sichern
cp iphone-fotos-sichern/config.example.toml ~/.config/iphone-fotos-sichern/config.toml
```

Edit `config.toml`: `host` is anything `ssh` accepts (an alias from `~/.ssh/config`, `user@host`), `ziel` the target folder, created if missing. Both can also be given as options.

## Usage

```sh
iphone-fotos-sichern --probe          # count only: on the iPhone, already backed up, new
iphone-fotos-sichern                  # back up everything new
iphone-fotos-sichern --limit 50       # at most 50 assets in this run
iphone-fotos-sichern --host nas --ziel /srv/photos/iphone
iphone-fotos-sichern --udid <udid>    # pick one iPhone if several are connected
```

USB is preferred over Wi-Fi when both are available.

## How it works

1. `pymobiledevice3` finds the iPhone via `usbmuxd` and opens an AFC session (the same file service Finder and Image Capture use) on the media partition.
2. `Photos.sqlite` and its WAL are copied to a temp folder and queried there. If the database changes during the copy, it retries.
3. For every new camera asset, all files with the same base name in its `DCIM` folder are pulled (photo plus Live Photo video).
4. Batches are uploaded with `rsync`, checked by size via `find -printf`, then appended to the list of backed-up assets.

## License

MIT
