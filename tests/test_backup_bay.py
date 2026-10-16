"""Offline synthetic regressions. No real backups, credentials, databases, or network."""
import concurrent.futures
import datetime as dt
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1]


class BackupBayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bay-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "source"
        self.root.mkdir()
        shutil.copytree(SOURCE / "scripts", self.root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        self.settings = {"project": "synthetic-project", "compression": "gzip"}
        self.config()
        (self.root / "document.txt").write_text("synthetic public content\n", encoding="utf-8")
        (self.root / "empty-directory").mkdir()

    def config(self, **updates):
        self.settings.update(updates)
        (self.root / "backup-bay.conf").write_text("\n".join(f"{k}={shlex.quote(str(v))}" for k, v in self.settings.items()) + "\n", encoding="utf-8")

    def run_cmd(self, command, *args, good=True):
        result = subprocess.run(["bash", str(self.root / "scripts" / f"project-bay-{command}.sh"), *map(str, args)],
                                cwd=self.base, text=True, capture_output=True, timeout=20)
        if good:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def archive(self, stage="阶段", tag=""):
        existing = set((self.root / "backups").glob("*.tar.*"))
        self.run_cmd("backup", stage, tag)
        return (set((self.root / "backups").glob("*.tar.*")) - existing).pop()

    def manifest_path(self, archive):
        return archive.with_name(archive.name.removesuffix(".tar.gz").removesuffix(".tar.zst") + ".manifest.json")

    def manifest(self, archive):
        return json.loads(self.manifest_path(archive).read_text(encoding="utf-8"))

    def save_manifest(self, archive, manifest):
        self.manifest_path(archive).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    def rehash(self, archive):
        manifest = self.manifest(archive)
        manifest["integrity"]["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        manifest["integrity"]["bytes"] = archive.stat().st_size
        self.save_manifest(archive, manifest)

    def repack(self, archive, transform):
        with tarfile.open(archive, "r:gz") as tar:
            members = [(m, tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()]
        members = transform(members)
        with tarfile.open(archive, "w:gz", format=tarfile.USTAR_FORMAT) as tar:
            for member, body in members:
                tar.addfile(member, io.BytesIO(body) if body is not None else None)
        self.rehash(archive)

    def assert_rejected_before_restore(self, archive):
        target = self.base / "must-not-be-created"
        self.run_cmd("check", good=False)
        self.run_cmd("restore", "--dry-run", archive, good=False)
        self.run_cmd("restore", "--target", target, archive, good=False)
        self.assertFalse(target.exists())

    def test_gzip_backup_check_and_exact_clean_restore(self):
        archive = self.archive()
        self.run_cmd("check")
        self.run_cmd("restore", "--dry-run", archive)
        target = self.base / "restored"
        self.run_cmd("restore", "--target", target, archive)
        m = self.manifest(archive)
        actual = {str(p.relative_to(target)) for p in target.rglob("*")}
        self.assertEqual(actual, {e["path"] for e in m["content"]["entries"]})
        for entry in m["content"]["entries"]:
            if entry["type"] == "file":
                self.assertEqual(hashlib.sha256((target / entry["path"]).read_bytes()).hexdigest(), entry["sha256"])
        self.assertIn("生成时间 UTC:", (target / "RESTORE.txt").read_text(encoding="utf-8"))
        self.assertNotIn(str(self.root), (target / "RESTORE.txt").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("zstd"), "optional zstd executable unavailable")
    def test_zstd_backup_check_and_restore(self):
        self.config(compression="zstd")
        archive = self.archive()
        self.assertTrue(archive.name.endswith(".tar.zst"))
        self.run_cmd("check")
        self.run_cmd("restore", "--dry-run", archive)

    def test_private_paths_excluded_and_no_secret_free_claim(self):
        blocked = [".publish/forbidden-patterns.txt", ".ssh/id_rsa", ".aws/credentials", ".env",
                   ".env.example", ".envrc", "nested/.publish/list.txt", "private/document.txt", "credentials.json",
                   "nested/secrets.yaml", "client.key", "nested/.config/gcloud/token.json"]
        for name in blocked:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SYNTHETIC_SENTINEL_ONLY", encoding="utf-8")
        archive = self.archive()
        m = self.manifest(archive)
        names = {e["path"] for e in m["content"]["entries"]}
        self.assertFalse(names.intersection(blocked))
        self.assertNotIn("backup-bay.conf", names)
        self.assertNotIn("secrets_included", m["content"])
        self.assertEqual(m["content"]["sensitivity_review"], "not_performed")
        with gzip.open(archive, "rb") as stream:
            self.assertNotIn(b"SYNTHETIC_SENTINEL_ONLY", stream.read())

    def test_exclude_extra_still_supported(self):
        self.config(exclude_extra="*.tmp scratch/")
        (self.root / "extra.tmp").write_text("synthetic")
        (self.root / "scratch").mkdir()
        (self.root / "scratch" / "work.txt").write_text("synthetic")
        names = {e["path"] for e in self.manifest(self.archive())["content"]["entries"]}
        self.assertNotIn("extra.tmp", names)
        self.assertNotIn("scratch", names)

    def test_wrong_archive_hash_nonzero(self):
        archive = self.archive()
        m = self.manifest(archive)
        m["integrity"]["archive_sha256"] = "0" * 64
        self.save_manifest(archive, m)
        self.assert_rejected_before_restore(archive)

    def test_matching_hash_but_broken_compression_nonzero(self):
        archive = self.archive()
        archive.write_bytes(b"\x1f\x8bcorrupt")
        self.rehash(archive)
        self.assert_rejected_before_restore(archive)

    def test_matching_hash_but_not_tar_nonzero(self):
        archive = self.archive()
        archive.write_bytes(gzip.compress(b"not a tar archive"))
        self.rehash(archive)
        self.assert_rejected_before_restore(archive)

    def test_nonzero_trailing_tar_data_rejected(self):
        archive = self.archive()
        payload = gzip.decompress(archive.read_bytes())
        archive.write_bytes(gzip.compress(payload + b"SYNTHETIC_UNLISTED_TRAILING_DATA".ljust(512, b"\0")))
        self.rehash(archive)
        self.assert_rejected_before_restore(archive)

    def test_concatenated_tar_rejected(self):
        archive = self.archive()
        payload = gzip.decompress(archive.read_bytes())
        archive.write_bytes(gzip.compress(payload + payload))
        self.rehash(archive)
        self.assert_rejected_before_restore(archive)

    def test_truncated_tar_end_rejected(self):
        archive = self.archive()
        payload = gzip.decompress(archive.read_bytes())
        archive.write_bytes(gzip.compress(payload[:-1]))
        self.rehash(archive)
        self.assert_rejected_before_restore(archive)

    def test_missing_manifest_nonzero(self):
        archive = self.archive()
        self.manifest_path(archive).unlink()
        self.assert_rejected_before_restore(archive)

    def test_missing_schema_field_nonzero(self):
        archive = self.archive()
        m = self.manifest(archive)
        del m["content"]["entries"]
        self.save_manifest(archive, m)
        self.assert_rejected_before_restore(archive)

    def test_duplicate_json_key_nonzero(self):
        archive = self.archive()
        path = self.manifest_path(archive)
        path.write_text('{"format":"backup-bay@0.2",' + path.read_text()[1:], encoding="utf-8")
        self.assert_rejected_before_restore(archive)

    def test_legacy_schema_rejected_without_silent_upgrade(self):
        archive = self.archive()
        m = self.manifest(archive)
        m["format"] = "backup-bay@0.1"
        self.save_manifest(archive, m)
        self.assert_rejected_before_restore(archive)

    def test_latest_is_created_timestamp_not_stage_filename(self):
        old = self.archive("出差", "zzz")
        new = self.archive("代码", "aaa")
        for archive, timestamp in [(old, "2020-01-01T00:00:00Z"), (new, "2021-01-01T00:00:00Z")]:
            m = self.manifest(archive)
            m["bay"]["created_utc"] = timestamp
            self.save_manifest(archive, m)
        result = self.run_cmd("check")
        self.assertIn("最新舱: " + new.name, result.stdout)

    def test_future_timestamp_nonzero(self):
        archive = self.archive()
        m = self.manifest(archive)
        m["bay"]["created_utc"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.save_manifest(archive, m)
        self.assert_rejected_before_restore(archive)

    def test_equal_timestamps_report_tie_and_do_not_prune(self):
        self.config(retain_code_bays="1")
        archives = [self.archive("代码", str(i)) for i in range(3)]
        for archive in archives:
            m = self.manifest(archive)
            m["bay"]["created_utc"] = "2021-01-01T00:00:00Z"
            self.save_manifest(archive, m)
        result = self.run_cmd("check")
        self.assertIn("时间戳并列", result.stdout)
        self.assertTrue(all(archive.exists() for archive in archives))

    def test_concurrent_backup_ids_do_not_collide(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.run_cmd("backup", "代码", "same"), range(2)))
        self.assertEqual(len(results), 2)
        self.assertEqual(len(list((self.root / "backups").glob("*.tar.gz"))), 2)
        self.run_cmd("check")

    def test_restore_requires_target_and_refuses_old_yes(self):
        archive = self.archive()
        self.run_cmd("restore", archive, good=False)
        self.run_cmd("restore", "--yes", archive, good=False)
        self.run_cmd("restore", "--dry-run", "--target", self.base / "x", archive, good=False)

    def test_restore_refuses_nonempty_target_preserves_stale_file(self):
        archive = self.archive()
        target = self.base / "nonempty"
        target.mkdir()
        sentinel = target / "POST_BACKUP_STALE_FILE.txt"
        sentinel.write_text("retain synthetic fixture")
        self.run_cmd("restore", "--target", target, archive, good=False)
        self.assertEqual(sentinel.read_text(), "retain synthetic fixture")
        self.assertEqual(list(target.iterdir()), [sentinel])

    def test_restore_refuses_live_tree_or_descendant(self):
        archive = self.archive()
        self.run_cmd("restore", "--target", self.root, archive, good=False)
        self.run_cmd("restore", "--target", self.root / "empty-target", archive, good=False)
        self.assertFalse((self.root / "empty-target").exists())

    def test_restore_refuses_symlinked_target_parent(self):
        archive = self.archive()
        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        self.run_cmd("restore", "--target", alias / "restored", archive, good=False)
        self.assertFalse((self.base / "restored").exists())

    def test_project_identity_mismatch(self):
        archive = self.archive()
        m = self.manifest(archive)
        m["project"] = "unrelated-project"
        self.save_manifest(archive, m)
        self.assert_rejected_before_restore(archive)

    def test_compression_field_mismatch(self):
        archive = self.archive()
        m = self.manifest(archive)
        m["integrity"]["compression"] = "zstd"
        self.save_manifest(archive, m)
        self.assert_rejected_before_restore(archive)

    def test_individual_file_hash_mismatch_even_when_archive_hash_matches(self):
        archive = self.archive()
        m = self.manifest(archive)
        next(e for e in m["content"]["entries"] if e["path"] == "document.txt")["sha256"] = "0" * 64
        self.save_manifest(archive, m)
        self.assert_rejected_before_restore(archive)

    def test_missing_restore_instructions_rejected(self):
        archive = self.archive()
        self.repack(archive, lambda members: [(m, b) for m, b in members if m.name != "RESTORE.txt"])
        self.assert_rejected_before_restore(archive)

    def test_duplicate_member_rejected(self):
        archive = self.archive()
        self.repack(archive, lambda members: members + [members[0]])
        self.assert_rejected_before_restore(archive)

    def test_tar_traversal_rejected(self):
        archive = self.archive()
        def mutate(members):
            member = tarfile.TarInfo("../escape.txt")
            member.size = 4
            return members + [(member, b"test")]
        self.repack(archive, mutate)
        self.assert_rejected_before_restore(archive)
        self.assertFalse((self.base / "escape.txt").exists())

    def test_tar_absolute_path_rejected(self):
        archive = self.archive()
        def mutate(members):
            member = tarfile.TarInfo(str(self.base / "escape.txt"))
            member.size = 4
            return members + [(member, b"test")]
        self.repack(archive, mutate)
        self.assert_rejected_before_restore(archive)
        self.assertFalse((self.base / "escape.txt").exists())

    def test_tar_symlink_rejected(self):
        archive = self.archive()
        def mutate(members):
            result = []
            for m, body in members:
                if m.name == "document.txt":
                    m.type, m.linkname, m.size, body = tarfile.SYMTYPE, "../escape", 0, None
                result.append((m, body))
            return result
        self.repack(archive, mutate)
        self.assert_rejected_before_restore(archive)

    def test_tar_special_file_rejected(self):
        archive = self.archive()
        def mutate(members):
            for m, body in members:
                if m.name == "document.txt":
                    m.type, m.size = tarfile.FIFOTYPE, 0
            return [(m, b if m.isfile() else None) for m, b in members]
        self.repack(archive, mutate)
        self.assert_rejected_before_restore(archive)

    def test_archive_size_limit(self):
        archive = self.archive()
        self.config(max_archive_bytes=1)
        self.assert_rejected_before_restore(archive)

    def test_unpacked_size_limit(self):
        archive = self.archive()
        self.config(max_unpacked_bytes=100000)
        archive.write_bytes(gzip.compress(b"\0" * 200000))
        self.rehash(archive)
        self.assert_rejected_before_restore(archive)

    def test_member_count_limit(self):
        archive = self.archive()
        self.config(max_members=1)
        self.assert_rejected_before_restore(archive)

    def test_db_dump_failure_aborts_publication(self):
        self.config(db_dump_hook="false")
        self.run_cmd("backup", "阶段", good=False)
        self.assertFalse(list((self.root / "backups").iterdir()))

    def test_empty_db_dump_aborts_publication(self):
        self.config(db_dump_hook="true")
        self.run_cmd("backup", "阶段", good=False)
        self.assertFalse(list((self.root / "backups").iterdir()))

    def synthetic_descendant_hook(self, parent_sleeps):
        marker = self.base / "descendant-marker"
        script = self.root / "synthetic-hook.py"
        script.write_text("import os, time\nfrom pathlib import Path\n"
            "child = os.fork()\nif child == 0:\n    time.sleep(1.5)\n"
            f"    Path({str(marker)!r}).write_text('SYNTHETIC_MARKER')\n    os._exit(0)\n"
            + ("time.sleep(5)\n" if parent_sleeps else "print('SYNTHETIC_DUMP', flush=True)\n"), encoding="utf-8")
        self.config(db_dump_hook=shlex.join([sys.executable, str(script)]), db_dump_timeout_seconds=1)
        self.run_cmd("backup", "阶段", good=False)
        time.sleep(1.6)
        self.assertFalse(marker.exists(), "DB child survived cleanup")
        self.assertFalse(list((self.root / "backups").iterdir()))

    def test_db_timeout_terminates_same_group_descendants(self):
        self.synthetic_descendant_hook(parent_sleeps=True)

    def test_db_parent_success_with_lingering_descendant_is_rejected(self):
        self.synthetic_descendant_hook(parent_sleeps=False)

    def test_synthetic_db_dump_is_declared_and_not_executed_on_restore(self):
        self.config(db_dump_hook="printf SYNTHETIC_DATABASE_DUMP")
        archive = self.archive()
        self.assertEqual(self.manifest(archive)["content"]["db_dump"], "included")
        target = self.base / "restored"
        self.run_cmd("restore", "--target", target, archive)
        self.assertEqual((target / "dump.sql").read_text(), "SYNTHETIC_DATABASE_DUMP")

    def test_shell_config_does_not_execute(self):
        marker = self.base / "must-not-run"
        self.config(dep_rebuild=f"$(touch {marker})")
        self.archive()
        self.assertFalse(marker.exists())

    def test_source_symlink_rejected(self):
        (self.root / "link.txt").symlink_to(self.root / "document.txt")
        self.run_cmd("backup", "阶段", good=False)
        self.assertFalse(list((self.root / "backups").iterdir()))

    def test_empty_library_is_nonzero(self):
        self.run_cmd("check", good=False)

    def test_older_corrupt_archive_does_not_hide_behind_good_latest(self):
        old = self.archive()
        m = self.manifest(old)
        m["bay"]["created_utc"] = "2020-01-01T00:00:00Z"
        m["integrity"]["archive_sha256"] = "0" * 64
        self.save_manifest(old, m)
        self.archive()
        self.run_cmd("check", good=False)


if __name__ == "__main__":
    unittest.main()
