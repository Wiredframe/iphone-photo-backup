# Sichert selbst aufgenommene Fotos und Videos vom iPhone (USB oder WLAN) per SSH auf einen anderen Rechner.
"""
Backs up photos and videos taken with the iPhone camera to an SSH host.

Reads the iPhone's Photos database (PhotoData/Photos.sqlite) and keeps only
assets shot with the camera (front and back). Screenshots, screen recordings,
AirDrop, WhatsApp, images saved from apps, shared albums and deleted photos are
left out. Live Photos come with their video. Originals are copied, edits made in
the Photos app are not.

Layout on the target: flat in <ziel>/<date>_<original name>, e.g. 2026-10-15_IMG_3135.HEIC.
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

CONFIG = Path.home() / ".config/iphone-fotos-sichern/config.toml"
STATE = Path.home() / ".local/state/iphone-fotos-sichern"
MANIFEST = STATE / "gesichert.tsv"
LOCK = STATE / "lauf.lock"
STAGING = Path.home() / "Library/Caches/iphone-fotos-sichern"
PENDING = STAGING / ".teil"  # files of one asset until all of them have arrived

BATCH_FILES = 100
BATCH_BYTES = 2 * 1024**3


class Abbruch(Exception):
    """Lost the connection to the iPhone: upload what is complete, then stop."""


def config():
    """Defaults for --host and --ziel from the config file, if there is one."""
    if not CONFIG.exists():
        return {}
    try:
        data = tomllib.loads(CONFIG.read_text())
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"{CONFIG} ist kein gültiges TOML: {e}")
    return {k: str(data[k]) for k in ("host", "ziel") if k in data}


def args():
    defaults = config()
    p = argparse.ArgumentParser(
        prog="iphone-fotos-sichern",
        description="Sichert selbst aufgenommene Fotos und Videos vom iPhone per SSH auf einen anderen Rechner.",
        epilog=f"--host und --ziel lassen sich in {CONFIG} vorgeben (host = \"…\", ziel = \"…\").",
    )
    p.add_argument("--host", default=defaults.get("host"), help="SSH-Ziel, z. B. nas oder user@192.168.1.10")
    p.add_argument("--ziel", default=defaults.get("ziel"), help="Ordner auf dem Ziel")
    p.add_argument("--udid", help="bestimmtes iPhone (Standard: das erste, USB vor WLAN)")
    p.add_argument("--probe", action="store_true", help="nur zählen, nichts übertragen")
    p.add_argument("--limit", type=int, help="höchstens so viele Aufnahmen sichern")
    p.add_argument("--ohne-ausgeblendete", action="store_true", help="ausgeblendete Fotos auslassen")
    a = p.parse_args()
    if not a.probe and not (a.host and a.ziel):
        p.error(f"--host und --ziel fehlen (als Option oder in {CONFIG})")
    return a


def lock():
    STATE.mkdir(parents=True, exist_ok=True)
    f = LOCK.open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Es läuft schon eine Sicherung.")
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
            return f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024


def ssh(host, command):
    return subprocess.run(["ssh", host, command], check=True, capture_output=True, text=True).stdout


def remote_files(host, ziel):
    """Name -> size of every file in the target folder."""
    out = ssh(host, f"find {shlex.quote(ziel)} -maxdepth 1 -type f -printf '%f\\t%s\\n'")
    files = {}
    for line in out.splitlines():
        name, _, size = line.rpartition("\t")
        files[name] = int(size)
    return files


async def connect(udid):
    device = await select_device(udid=udid)
    if device is None:
        sys.exit("Kein iPhone gefunden. Per Kabel anschließen oder im Finder „Dieses iPhone im WLAN anzeigen“ aktivieren, iPhone entsperren.")
    lockdown = await create_using_usbmux(serial=device.serial, connection_type=device.connection_type)
    afc = AfcService(lockdown)
    await afc.connect()
    print(f"Verbunden mit {lockdown.short_info.get('DeviceName', device.serial)} ({'Kabel' if device.is_usb else 'WLAN'})")
    return afc


def query_assets(path, ohne_ausgeblendete):
    db = sqlite3.connect(path)
    try:
        columns = {row[1] for row in db.execute("pragma table_info(ZADDITIONALASSETATTRIBUTES)")}
        offset = "aa.ZTIMEZONEOFFSET" if "ZTIMEZONEOFFSET" in columns else "null"
        hidden = "and a.ZHIDDEN = 0" if ohne_ausgeblendete else ""
        return db.execute(f"""
            select a.ZUUID, a.ZDIRECTORY, a.ZFILENAME, a.ZDATECREATED, {offset}, a.ZKINDSUBTYPE
            from ZASSET a join ZADDITIONALASSETATTRIBUTES aa on aa.ZASSET = a.Z_PK
            where a.ZTRASHEDSTATE = 0 and aa.ZIMPORTEDBY in ({','.join(map(str, CAMERA))})
              and a.ZDIRECTORY like 'DCIM/%' {hidden}
            order by a.ZDATECREATED
        """).fetchall()
    finally:
        db.close()


async def camera_assets(afc, ohne_ausgeblendete):
    """Reads the Photos database and returns the camera assets."""
    for attempt in range(3):
        tmp = Path(tempfile.mkdtemp(prefix="iphone-fotos-"))
        try:
            # No -shm: SQLite rebuilds it from the WAL, a stale one would only get in the way.
            for name in ("Photos.sqlite", "Photos.sqlite-wal"):
                if await afc.exists(f"/PhotoData/{name}"):
                    await afc.pull(f"/PhotoData/{name}", str(tmp / name), progress_bar=False)
            return query_assets(tmp / "Photos.sqlite", ohne_ausgeblendete)
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


def upload(host, ziel, source, expected):
    """Uploads a batch, then checks the size of every file on the target."""
    subprocess.run(["rsync", "-rt", "-s", "--ignore-existing",
                    f"{source}/", f"{host}:{ziel}/"], check=True)
    remote = remote_files(host, ziel)
    wrong = [name for name, size in expected.items() if remote.get(name) != size]
    if wrong:
        sys.exit(f"Auf {host} fehlen oder weichen ab: {', '.join(wrong[:5])}"
                 f"{' …' if len(wrong) > 5 else ''}. Nichts davon als gesichert vermerkt.")


def finish_batch(host, ziel, source, rows, expected):
    """Runs in the background: upload the batch, verify, record, clean up."""
    if expected:
        upload(host, ziel, source, expected)
    append_manifest(rows)
    shutil.rmtree(source, ignore_errors=True)
    return len(rows), expected


async def main():
    a = args()
    _lock = lock()
    try:
        afc = await connect(a.udid)
    except (ConnectionFailedError, ConnectionTerminatedError):
        sys.exit("Keine Verbindung zum iPhone. Entsperren und im selben WLAN oder per Kabel verbinden.")
    assets = await camera_assets(afc, a.ohne_ausgeblendete)
    done, owner = load_manifest()
    todo = [r for r in assets if r[0] not in done]
    print(f"{len(assets)} eigene Aufnahmen auf dem iPhone, {len(assets) - len(todo)} schon gesichert, {len(todo)} neu.")
    if a.probe or not todo:
        return

    ssh(a.host, f"mkdir -p {shlex.quote(a.ziel)}")
    remote = remote_files(a.host, a.ziel)
    shutil.rmtree(STAGING, ignore_errors=True)
    PENDING.mkdir(parents=True)

    listings = {}
    batch, expected = [], {}
    saved = queued = batch_bytes = total_bytes = 0
    skipped = {"icloud": 0, "fehler": 0, "name": 0}
    batch_no = 1
    current = STAGING / f"stapel-{batch_no}"
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
        print(f"  {saved} Aufnahmen auf {a.host} gesichert")

    async def flush():
        nonlocal batch, expected, batch_bytes, batch_no, current, uploading
        if not batch:
            return
        await wait_upload()  # at most one upload at a time
        uploading = asyncio.create_task(asyncio.to_thread(finish_batch, a.host, a.ziel, current, batch, expected))
        batch_no += 1
        current = STAGING / f"stapel-{batch_no}"
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
                print(f"  übersprungen: {directory}/{f}, auf {a.host} liegen andere Dateien unter beiden möglichen Namen")
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
                raise Abbruch from e
            except AfcException as e:
                skipped["fehler"] += 1
                print(f"  übersprungen: {directory}/{filename} ({e.__class__.__name__})")
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
    except Abbruch:
        await flush()
        await wait_upload()
        sys.exit(f"Verbindung zum iPhone verloren. {saved} Aufnahmen gesichert, der nächste Lauf macht weiter.")
    await flush()
    await wait_upload()

    print(f"Fertig: {saved} Aufnahmen ({size_text(total_bytes)} übertragen) nach {a.host}:{a.ziel}.")
    if skipped["icloud"]:
        print(f"{skipped['icloud']} Aufnahmen liegen (ganz oder teilweise) nur in iCloud und wurden übersprungen "
              "(iPhone: Einstellungen › Fotos › „Originale laden und behalten“).")
    if skipped["fehler"] or skipped["name"]:
        print(f"{skipped['fehler'] + skipped['name']} Aufnahmen übersprungen (siehe oben), der nächste Lauf versucht sie erneut.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAbgebrochen. Schon Gesichertes bleibt gemerkt, der nächste Lauf macht weiter.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"Übertragung fehlgeschlagen ({' '.join(e.cmd[:2])} …, Code {e.returncode}). Ist das Ziel per SSH erreichbar?")
