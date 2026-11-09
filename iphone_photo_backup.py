# Backs up photos and videos taken with the iPhone camera (USB or Wi-Fi) to another machine via SSH.
"""
Backs up photos and videos taken with the iPhone camera to an SSH host.

Reads the iPhone's Photos database (PhotoData/Photos.sqlite) and keeps only
assets shot with the camera (front and back). Screenshots, screen recordings,
AirDrop, WhatsApp, images saved from apps, shared albums and deleted photos are
left out. Live Photos come with their video. Originals are copied, edits made in
the Photos app are not.

Layout on the target: flat in <dest>/<date>_<original name>, e.g. 2026-10-15_IMG_3135.HEIC.
The date is the capture date in the capture's own time zone.
A list on the Mac remembers what has been transferred, every run only fetches new assets.
Fetching and uploading happen in batches, concurrently: while one batch goes to the
target, the next one is already coming from the iPhone.
Nothing is ever deleted or overwritten, neither on the iPhone nor on the target.
"""

import argparse
import asyncio
import fcntl
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pymobiledevice3.exceptions import AfcException, ConnectionFailedError, ConnectionTerminatedError
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.usbmux import select_device

APPLE_EPOCH = 978307200  # 2001-01-01 as Unix time
MEDIA = {".heic", ".jpg", ".jpeg", ".png", ".dng", ".mov", ".mp4", ".hevc"}
CAMERA = (1, 2)  # ZIMPORTEDBY: 1 back camera, 2 front camera
LIVE_PHOTO = 2  # ZKINDSUBTYPE of a photo with video

CONFIG = Path.home() / ".config/iphone-photo-backup/config.toml"
STATE = Path.home() / ".local/state/iphone-photo-backup"
MANIFEST = STATE / "backed-up.tsv"
LOCK = STATE / "run.lock"
STAGING = Path.home() / "Library/Caches/iphone-photo-backup"
PENDING = STAGING / ".partial"  # files of one asset until all of them have arrived

BATCH_FILES = 100
BATCH_BYTES = 2 * 1024**3


class Disconnected(Exception):
    """Lost the connection to the iPhone: upload what is complete, then stop."""


def config():
    """Defaults for --host and --dest from the config file, if there is one."""
    if not CONFIG.exists():
        return {}
    try:
        data = tomllib.loads(CONFIG.read_text())
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"{CONFIG} is not valid TOML: {e}")
    return {k: str(data[k]) for k in ("host", "dest") if k in data}


def args():
    defaults = config()
    p = argparse.ArgumentParser(
        prog="iphone-photo-backup",
        description="Backs up photos and videos taken with the iPhone camera to another machine via SSH.",
        epilog=f"--host and --dest can be set in {CONFIG} (host = \"…\", dest = \"…\").",
    )
    p.add_argument("--host", default=defaults.get("host"), help="SSH target, e.g. nas or user@192.168.1.10")
    p.add_argument("--dest", default=defaults.get("dest"), help="folder on the target")
    p.add_argument("--udid", help="a specific iPhone (default: the first one, USB before Wi-Fi)")
    p.add_argument("--probe", action="store_true", help="only count, transfer nothing")
    p.add_argument("--limit", type=int, help="back up at most this many assets")
    p.add_argument("--skip-hidden", action="store_true", help="leave out hidden photos")
    a = p.parse_args()
    if not a.probe and not (a.host and a.dest):
        p.error(f"--host and --dest are missing (as options or in {CONFIG})")
    return a


def lock():
    STATE.mkdir(parents=True, exist_ok=True)
    f = LOCK.open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("A backup is already running.")
    return f


def load_manifest():
    """uuid -> paths and path -> uuid from the list of backed-up assets."""
    done, owner = set(), {}
    if MANIFEST.exists():
        for line in MANIFEST.read_text().splitlines():
            uuid, sep, paths = line.partition("\t")
            if not sep or not paths:
                continue  # truncated line, the asset gets checked again
            done.add(uuid)
            for p in paths.split("|"):
                owner[p] = uuid
    return done, owner


def append_manifest(rows):
    with MANIFEST.open("a") as f:
        for uuid, paths in rows:
            f.write(f"{uuid}\t{'|'.join(paths)}\n")
        f.flush()
        os.fsync(f.fileno())


def size_text(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024


def ssh(host, command):
    return subprocess.run(["ssh", host, command], check=True, capture_output=True, text=True).stdout


def remote_files(host, dest):
    """Name -> size of every file in the target folder."""
    out = ssh(host, f"find {shlex.quote(dest)} -maxdepth 1 -type f -printf '%f\\t%s\\n'")
    files = {}
    for line in out.splitlines():
        name, _, size = line.rpartition("\t")
        files[name] = int(size)
    return files


async def connect(udid):
    device = await select_device(udid=udid)
    if device is None:
        sys.exit("No iPhone found. Connect it by cable or enable \"Show this iPhone when on Wi-Fi\" in the Finder, then unlock it.")
    lockdown = await create_using_usbmux(serial=device.serial, connection_type=device.connection_type)
    afc = AfcService(lockdown)
    await afc.connect()
    print(f"Connected to {lockdown.short_info.get('DeviceName', device.serial)} ({'USB' if device.is_usb else 'Wi-Fi'})")
    return afc


def query_assets(path, skip_hidden):
    db = sqlite3.connect(path)
    try:
        columns = {row[1] for row in db.execute("pragma table_info(ZADDITIONALASSETATTRIBUTES)")}
        offset = "aa.ZTIMEZONEOFFSET" if "ZTIMEZONEOFFSET" in columns else "null"
        hidden = "and a.ZHIDDEN = 0" if skip_hidden else ""
        return db.execute(f"""
            select a.ZUUID, a.ZDIRECTORY, a.ZFILENAME, a.ZDATECREATED, {offset}, a.ZKINDSUBTYPE
            from ZASSET a join ZADDITIONALASSETATTRIBUTES aa on aa.ZASSET = a.Z_PK
            where a.ZTRASHEDSTATE = 0 and aa.ZIMPORTEDBY in ({','.join(map(str, CAMERA))})
              and a.ZDIRECTORY like 'DCIM/%' {hidden}
            order by a.ZDATECREATED
        """).fetchall()
    finally:
        db.close()


async def camera_assets(afc, skip_hidden):
    """Reads the Photos database and returns the camera assets."""
    for attempt in range(3):
        tmp = Path(tempfile.mkdtemp(prefix="iphone-photos-"))
        try:
            # No -shm: SQLite rebuilds it from the WAL, a stale one would only get in the way.
            for name in ("Photos.sqlite", "Photos.sqlite-wal"):
                if await afc.exists(f"/PhotoData/{name}"):
                    await afc.pull(f"/PhotoData/{name}", str(tmp / name), progress_bar=False)
            return query_assets(tmp / "Photos.sqlite", skip_hidden)
        except sqlite3.DatabaseError:
            if attempt == 2:
                raise  # the database changed during the copy three times in a row
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def taken_at(created, offset):
    """Capture time in the capture's time zone (otherwise the Mac's)."""
    if created is None:
        return None
    if offset is None:
        return datetime.fromtimestamp(created + APPLE_EPOCH)
    return datetime.fromtimestamp(created + APPLE_EPOCH, tz=timezone(timedelta(seconds=offset)))


def choose_name(day, filename, size, uuid, owner, remote):
    """Name on the target and whether the file is already there in full (None: no free name)."""
    stem, ext = os.path.splitext(filename)
    for name in (f"{day}_{filename}", f"{day}_{stem}_{uuid[:8]}{ext}"):
        if owner.get(name, uuid) != uuid:
            continue  # belongs to another asset
        if name in remote:
            if remote[name] == size:
                return name, True
            continue  # foreign or incomplete file, don't overwrite
        return name, False
    return None, False


def upload(host, dest, source, expected):
    """Uploads a batch, then checks the size of every file on the target."""
    subprocess.run(["rsync", "-rt", "-s", "--ignore-existing",
                    f"{source}/", f"{host}:{dest}/"], check=True)
    remote = remote_files(host, dest)
    wrong = [name for name, size in expected.items() if remote.get(name) != size]
    if wrong:
        sys.exit(f"Missing or different on {host}: {', '.join(wrong[:5])}"
                 f"{' …' if len(wrong) > 5 else ''}. None of them recorded as backed up.")


def finish_batch(host, dest, source, rows, expected):
    """Runs in the background: upload the batch, verify, record, clean up."""
    if expected:
        upload(host, dest, source, expected)
    append_manifest(rows)
    shutil.rmtree(source, ignore_errors=True)
    return len(rows), expected


async def main():
    a = args()
    _lock = lock()
    try:
        afc = await connect(a.udid)
    except (ConnectionFailedError, ConnectionTerminatedError):
        sys.exit("No connection to the iPhone. Unlock it and connect it by cable or to the same Wi-Fi.")
    assets = await camera_assets(afc, a.skip_hidden)
    done, owner = load_manifest()
    todo = [r for r in assets if r[0] not in done]
    print(f"{len(assets)} camera assets on the iPhone, {len(assets) - len(todo)} already backed up, {len(todo)} new.")
    if a.probe or not todo:
        return

    ssh(a.host, f"mkdir -p {shlex.quote(a.dest)}")
    remote = remote_files(a.host, a.dest)
    shutil.rmtree(STAGING, ignore_errors=True)
    PENDING.mkdir(parents=True)

    listings = {}
    batch, expected = [], {}
    saved = queued = batch_bytes = total_bytes = 0
    skipped = {"icloud": 0, "error": 0, "name": 0}
    batch_no = 1
    current = STAGING / f"batch-{batch_no}"
    current.mkdir()
    uploading = None  # background upload of the previous batch

    async def wait_upload():
        nonlocal uploading, saved
        if uploading is None:
            return
        task, uploading = uploading, None
        n, done_files = await task
        saved += n
        remote.update(done_files)
        print(f"  {saved} assets backed up to {a.host}")

    async def flush():
        nonlocal batch, expected, batch_bytes, batch_no, current, uploading
        if not batch:
            return
        await wait_upload()  # at most one upload at a time
        uploading = asyncio.create_task(asyncio.to_thread(finish_batch, a.host, a.dest, current, batch, expected))
        batch_no += 1
        current = STAGING / f"batch-{batch_no}"
        current.mkdir()
        batch, expected, batch_bytes = [], {}, 0

    async def pull_asset(uuid, directory, filename, created, offset, subtype):
        """Pulls all files of an asset into STAGING, returns their names on the target or None."""
        nonlocal batch_bytes, total_bytes
        if directory not in listings:
            listings[directory] = await afc.listdir(f"/{directory}")
        stem = os.path.splitext(filename)[0].casefold()
        files = sorted(f for f in listings[directory]
                       if os.path.splitext(f)[0].casefold() == stem and os.path.splitext(f)[1].lower() in MEDIA)
        if filename.casefold() not in {f.casefold() for f in files}:
            skipped["icloud"] += 1  # original only lives in iCloud ("Optimize iPhone Storage")
            return None
        if subtype == LIVE_PHOTO and not any(os.path.splitext(f)[1].lower() == ".mov" for f in files):
            skipped["icloud"] += 1  # the Live Photo's video isn't on the iPhone yet
            return None

        when = taken_at(created, offset)
        if when is None:
            when = (await afc.stat(f"/{directory}/{filename}"))["st_mtime"]
        day = f"{when:%Y-%m-%d}"

        names, staged = [], []
        for f in files:
            size = (await afc.stat(f"/{directory}/{f}"))["st_size"]
            name, present = choose_name(day, f, size, uuid, owner, remote)
            if name is None:
                skipped["name"] += 1
                print(f"  skipped: {directory}/{f}, other files on {a.host} already use both possible names")
                return None
            owner[name] = uuid
            names.append(name)
            if not present:
                await afc.pull(f"/{directory}/{f}", str(PENDING / name), progress_bar=False)
                os.utime(PENDING / name, (when.timestamp(), when.timestamp()))
                staged.append((name, size))
        for name, size in staged:
            (PENDING / name).rename(current / name)
            expected[name] = size
            batch_bytes += size
            total_bytes += size
        return names

    try:
        for i, (uuid, directory, filename, created, offset, subtype) in enumerate(todo, 1):
            if a.limit and queued >= a.limit:
                break
            try:
                names = await pull_asset(uuid, directory, filename, created, offset, subtype)
            except (ConnectionTerminatedError, ConnectionFailedError, ConnectionError) as e:
                raise Disconnected from e
            except AfcException as e:
                skipped["error"] += 1
                print(f"  skipped: {directory}/{filename} ({e.__class__.__name__})")
                names = None
            finally:
                for f in PENDING.iterdir():  # discard a half-pulled asset
                    f.unlink()
            if names is None:
                continue
            batch.append((uuid, names))
            queued += 1
            print(f"  [{i}/{len(todo)}] {', '.join(names)}")
            if len(batch) >= BATCH_FILES or batch_bytes >= BATCH_BYTES:
                await flush()
    except Disconnected:
        await flush()
        await wait_upload()
        sys.exit(f"Lost the connection to the iPhone. {saved} assets backed up, the next run continues.")
    await flush()
    await wait_upload()

    print(f"Done: {saved} assets ({size_text(total_bytes)} transferred) to {a.host}:{a.dest}.")
    if skipped["icloud"]:
        print(f"{skipped['icloud']} assets are (fully or partly) only in iCloud and were skipped "
              "(iPhone: Settings › Photos › \"Download and Keep Originals\").")
    if skipped["error"] or skipped["name"]:
        print(f"{skipped['error'] + skipped['name']} assets skipped (see above), the next run tries them again.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nCancelled. Everything already backed up stays recorded, the next run continues.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"Transfer failed ({' '.join(e.cmd[:2])} …, exit code {e.returncode}). Is the target reachable via SSH?")
