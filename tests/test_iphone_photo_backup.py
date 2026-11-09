"""Tests for everything that doesn't need an iPhone.

Run with the Python of the pymobiledevice3 venv:
  "$(pipx environment --value PIPX_LOCAL_VENVS)/pymobiledevice3/bin/python" -m unittest discover tests
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import iphone_photo_backup as m  # noqa: E402

UUID_A = "AAAAAAAA-1111-2222-3333-444444444444"
UUID_B = "BBBBBBBB-1111-2222-3333-444444444444"


class Config(unittest.TestCase):
    TEXT = '# comment\nhost = "nas"  # trailing\ndest = "/srv/photos"\nother = "x"\n'

    def test_toml(self):
        self.assertEqual(m.parse_config(self.TEXT), {"host": "nas", "dest": "/srv/photos"})

    def test_without_tomllib(self):
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            self.assertEqual(m.parse_config(self.TEXT), {"host": "nas", "dest": "/srv/photos"})
            with self.assertRaises(ValueError):
                m.parse_config("host = nas\n")

    def test_invalid_toml(self):
        with self.assertRaises(ValueError):
            m.parse_config('host = "nas\n')


class Args(unittest.TestCase):
    def run_args(self, argv, cfg=None):
        with mock.patch.object(sys, "argv", ["iphone-photo-backup", *argv]), \
             mock.patch.object(m, "config", return_value=cfg or {}), \
             mock.patch("sys.stderr"):
            return m.args()

    def test_from_config(self):
        a = self.run_args([], {"host": "nas", "dest": "/srv"})
        self.assertEqual((a.host, a.dest), ("nas", "/srv"))

    def test_option_beats_config(self):
        self.assertEqual(self.run_args(["--dest", "/x"], {"host": "nas", "dest": "/srv"}).dest, "/x")

    def test_missing_target(self):
        for argv in ([], ["--probe"], ["--host", "nas"]):
            with self.assertRaises(SystemExit):
                self.run_args(argv)

    def test_home_folder(self):
        for dest, want in (("~/Photos", "Photos"), ("~", "."), ("~/", "."), ("/srv", "/srv"), ("rel", "rel")):
            self.assertEqual(self.run_args(["--host", "h", "--dest", dest]).dest, want)
        with self.assertRaises(SystemExit):
            self.run_args(["--host", "h", "--dest", "~bob/x"])

    def test_limit(self):
        self.assertEqual(self.run_args(["--host", "h", "--dest", "/d", "--limit", "3"]).limit, 3)
        for bad in ("0", "-1", "x"):
            with self.assertRaises(SystemExit):
                self.run_args(["--host", "h", "--dest", "/d", "--limit", bad])


class Manifest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def test_roundtrip_and_truncated_line(self):
        path = self.dir / "list.tsv"
        m.append_manifest(path, [(UUID_A, ["2026-10-15_IMG_1.HEIC", "2026-10-15_IMG_1.MOV"])])
        with path.open("a") as f:
            f.write(UUID_B)  # cut off mid-line
        done, owner = m.load_manifest(path)
        self.assertEqual(done, {UUID_A})
        self.assertEqual(owner["2026-10-15_IMG_1.MOV"], UUID_A)

    def test_missing(self):
        self.assertEqual(m.load_manifest(self.dir / "nope.tsv"), (set(), {}))

    def test_one_list_per_target(self):
        a = m.manifest_path("nas", "/srv/photos/")
        self.assertEqual(a, m.manifest_path("nas", "/srv/photos"))
        self.assertNotEqual(a, m.manifest_path("nas", "/srv/other"))
        self.assertNotEqual(a, m.manifest_path("pi", "/srv/photos"))
        self.assertEqual(m.manifest_path("me@10.0.0.2", "/a b/ü").parent, m.STATE)


class Names(unittest.TestCase):
    def test_free(self):
        self.assertEqual(m.choose_name("2026-10-15", "IMG_1.HEIC", 10, UUID_A, {}, {}),
                         ("2026-10-15_IMG_1.HEIC", False))

    def test_already_there(self):
        self.assertEqual(m.choose_name("2026-10-15", "IMG_1.HEIC", 10, UUID_A, {}, {"2026-10-15_IMG_1.HEIC": 10}),
                         ("2026-10-15_IMG_1.HEIC", True))

    def test_taken_by_other_file(self):
        self.assertEqual(m.choose_name("2026-10-15", "IMG_1.HEIC", 10, UUID_A, {}, {"2026-10-15_IMG_1.HEIC": 99}),
                         ("2026-10-15_IMG_1_AAAAAAAA.HEIC", False))

    def test_taken_by_other_asset(self):
        owner = {"2026-10-15_IMG_1.HEIC": UUID_B}
        self.assertEqual(m.choose_name("2026-10-15", "IMG_1.HEIC", 10, UUID_A, owner, {})[0],
                         "2026-10-15_IMG_1_AAAAAAAA.HEIC")

    def test_no_free_name(self):
        remote = {"2026-10-15_IMG_1.HEIC": 1, "2026-10-15_IMG_1_AAAAAAAA.HEIC": 2}
        self.assertEqual(m.choose_name("2026-10-15", "IMG_1.HEIC", 10, UUID_A, {}, remote), (None, False))


class Time(unittest.TestCase):
    def test_capture_time_zone(self):
        # 2026-10-15 23:30 in Tokyo is still the 15th there, even though it's the 15th 14:30 UTC
        utc = datetime(2026, 10, 15, 14, 30, tzinfo=timezone.utc).timestamp()
        when = m.taken_at(utc - m.APPLE_EPOCH, 9 * 3600)
        self.assertEqual(when.utcoffset(), timedelta(hours=9))
        self.assertEqual(f"{when:%Y-%m-%d %H:%M}", "2026-10-15 23:30")

    def test_none(self):
        self.assertIsNone(m.taken_at(None, None))


class Listing(unittest.TestCase):
    def test_formats(self):
        gnu = ("total 8\n"
               "drwxr-xr-x 2 1000 1000 4096 Oct 15 12:00 sub\n"
               "-rw-r--r-- 1 1000 1000 2048 Oct 15 12:00 2026-10-15_IMG_1.HEIC\n"
               "-rw-r--r-- 1 1000 1000 77 Jan  3  2025 name with  spaces.txt\n"
               "lrwxrwxrwx 1 1000 1000 5 Oct 15 12:00 link -> x\n")
        self.assertEqual(m.parse_ls(gnu), {"2026-10-15_IMG_1.HEIC": 2048, "name with  spaces.txt": 77})
        bsd = "-rw-r--r--@ 1 501  20  12345678 Oct 15 12:00 2026-10-15_IMG_2.MOV\n"
        self.assertEqual(m.parse_ls(bsd), {"2026-10-15_IMG_2.MOV": 12345678})

    def test_rsync_flags(self):
        self.assertEqual(m.rsync_args("/var/mnt/data/Bilder/iPhone"), [])
        self.assertEqual(m.rsync_args("Bilder/ü+x.y-z"), [])
        with mock.patch("subprocess.run") as run:
            run.return_value.stdout = "openrsync: protocol version 29"
            with self.assertRaises(SystemExit):
                m.rsync_args("/srv/my photos")
            run.return_value.stdout = "rsync  version 3.4.1  protocol version 32"
            self.assertEqual(m.rsync_args("/srv/my photos"), ["-s"])


class Upload(unittest.TestCase):
    """Upload, size check and manifest end to end, with an ssh that runs locally."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        bin_ = self.dir / "bin"
        bin_.mkdir()
        ssh = bin_ / "ssh"
        ssh.write_text('#!/bin/sh\nshift\nexec sh -c "$*"\n')  # drop the host, run the rest here
        ssh.chmod(0o755)
        self.path = os.environ["PATH"]

    def run_with(self, rsync, dest_name):
        if not Path(rsync).exists():
            self.skipTest(f"{rsync} not installed")
        bin_ = self.dir / "bin"
        (bin_ / "rsync").unlink(missing_ok=True)
        (bin_ / "rsync").symlink_to(rsync)
        dest = self.dir / dest_name
        dest.mkdir()
        source = self.dir / "batch"
        source.mkdir()
        (source / "2026-10-15_IMG_1.HEIC").write_bytes(b"x" * 1000)
        (source / "2026-10-15_IMG_1.MOV").write_bytes(b"y" * 3000)
        (dest / "2026-10-14_IMG_0.HEIC").write_bytes(b"old")  # must stay untouched
        manifest = self.dir / "list.tsv"
        expected = {"2026-10-15_IMG_1.HEIC": 1000, "2026-10-15_IMG_1.MOV": 3000}
        rows = [(UUID_A, list(expected))]
        with mock.patch.dict(os.environ, {"PATH": f"{bin_}:{self.path}"}):
            flags = m.rsync_args(str(dest))
            n, files = m.finish_batch("host", str(dest), source, rows, expected, flags, manifest)
            self.assertEqual(m.remote_files("host", str(dest)),
                             {**expected, "2026-10-14_IMG_0.HEIC": 3})
        self.assertEqual((n, files), (1, expected))
        self.assertFalse(source.exists())
        self.assertEqual(m.load_manifest(manifest)[0], {UUID_A})

    def test_homebrew_rsync(self):
        self.run_with("/opt/homebrew/bin/rsync", "dest with space")

    def test_macos_openrsync(self):
        self.run_with("/usr/bin/rsync", "dest")

    def test_size_mismatch_is_not_recorded(self):
        dest = self.dir / "dest"
        dest.mkdir()
        manifest = self.dir / "list.tsv"
        with mock.patch.object(m, "remote_files", return_value={"a": 1}), \
             mock.patch("subprocess.run"):
            with self.assertRaises(SystemExit):
                m.finish_batch("host", str(dest), self.dir, [(UUID_A, ["a"])], {"a": 2}, [], manifest)
        self.assertFalse(manifest.exists())


if __name__ == "__main__":
    unittest.main()
