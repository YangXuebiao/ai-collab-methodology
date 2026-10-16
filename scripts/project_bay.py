#!/usr/bin/env python3
"""Offline backup-bay@0.2: bounded verification and recovery to an empty target.

Modified from public backup-bay@0.1, YangXuebiao/ai-collab-methodology
at 186bcd844d4f15cf5489a73d510b6ce2064d988f; see LICENSE and docs/backup-bay.md.
"""
import argparse
import contextlib
import datetime as dt
import fnmatch
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid

FORMAT = "backup-bay@0.2"
STAGES = {"阶段": "stage", "代码": "code", "出差": "travel"}
SUFFIXES = {".tar.zst": "zstd", ".tar.gz": "gzip"}
REGEN = {"backups", ".git", "node_modules", "build", "dist", ".venv",
         "coverage", ".dart_tool", "__pycache__"}
PRIVATE_DIRS = {".publish", ".ssh", ".aws", ".gnupg", ".azure", ".kube",
                ".gcloud", "gcloud", "private", "secrets", "credentials"}
PRIVATE_FILES = (".env*", "*.pem", "*.key", "*.p12", "*.pfx",
                 "id_rsa*", "id_ed25519*", "id_ecdsa*", "credentials*",
                 "secrets.*", "secret.*", ".netrc", ".npmrc", ".pypirc", "backup-bay.conf")
DEFAULTS = {"exclude_extra": "", "extra_include": "", "db_dump_hook": "",
    "dep_rebuild": "自行核对依赖；不自动执行", "stale_hours": "168", "compression": "zstd",
    "max_archive_bytes": str(256 * 1024 * 1024), "max_unpacked_bytes": str(512 * 1024 * 1024),
    "max_file_bytes": str(128 * 1024 * 1024), "max_members": "10000", "db_dump_timeout_seconds": "60"}
INTEGER_KEYS = {"stale_hours", "max_archive_bytes", "max_unpacked_bytes",
                "max_file_bytes", "max_members", "db_dump_timeout_seconds"}


class BayError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise BayError(message)


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value):
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value),
            "created_utc 必须为 UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise BayError("created_utc 日期无效") from exc
    require(parsed <= now_utc() + dt.timedelta(seconds=300), "created_utc 超出允许的五分钟时钟误差")
    return parsed


def read_config(root):
    path = root / "backup-bay.conf"
    require(path.is_file() and not path.is_symlink(), "缺少常规 backup-bay.conf")
    require(path.stat().st_size <= 65536, "配置文件过大")
    result, seen = dict(DEFAULTS), set()
    allowed = set(DEFAULTS) | {"project", "secrets_included", "retain_code_bays"}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        parts = shlex.split(line, comments=True, posix=True)
        if not parts:
            continue
        require(len(parts) == 1 and "=" in parts[0], f"配置第 {number} 行须为 key=value")
        key, value = parts[0].split("=", 1)
        require(key in allowed and key not in seen, f"未知或重复配置字段: {key}")
        seen.add(key)
        result[key] = value
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", result.get("project", "")), "project 须为 1–64 个安全 ASCII 字符")
    require(not result["extra_include"], "extra_include 不支持；须先分类后置于项目树")
    require(result.get("secrets_included", "false").lower() == "false", "不支持包含 secrets；旧声明字段不能作为扫描证据")
    require(result["compression"] in SUFFIXES.values(), "compression 须为 zstd 或 gzip")
    for key in INTEGER_KEYS:
        require(result[key].isdigit() and int(result[key]) > 0, f"{key} 须为正整数")
        result[key] = int(result[key])
    return result


def safe_name(value):
    require(isinstance(value, str) and value and len(value) <= 1024, "条目路径缺失或过长")
    require("\\" not in value and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value), "路径含反斜线或控制字符")
    pure = PurePosixPath(value)
    require(not pure.is_absolute() and all(part not in ("", ".", "..") for part in value.split("/"))
            and str(pure) == value and not any(":" in part for part in pure.parts), "条目路径不是安全规范相对路径")
    return value


def prohibited(name):
    parts = [part.casefold() for part in name.split("/")]
    return (any(part in REGEN or part in PRIVATE_DIRS for part in parts)
            or any(fnmatch.fnmatchcase(part, pattern) for part in parts for pattern in PRIVATE_FILES)
            or parts[-1].endswith(".pyc"))


def excluded(name, patterns):
    return prohibited(name) or any(fnmatch.fnmatchcase(name, p.rstrip("/")) or
        fnmatch.fnmatchcase(name.split("/")[-1], p.rstrip("/")) for p in patterns)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def exact_keys(value, keys, label):
    require(type(value) is dict and set(value) == set(keys), f"{label} schema 不匹配")


def bounded_int(value, maximum, label, zero=False):
    require(type(value) is int and (0 if zero else 1) <= value <= maximum, f"{label} 超出资源限额或不是整数")


def sha_field(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "sha256 字段无效")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "JSON 包含重复字段")
        result[key] = value
    return result


def archive_kind(path):
    for suffix, kind in SUFFIXES.items():
        if path.name.endswith(suffix):
            return suffix, kind
    raise BayError("仅支持 .tar.zst 或 .tar.gz")


def manifest_for(archive):
    suffix, _ = archive_kind(archive)
    return archive.with_name(archive.name[:-len(suffix)] + ".manifest.json")


def load_manifest(archive, config):
    suffix, kind = archive_kind(archive)
    require(archive.is_file() and not archive.is_symlink(), "归档须为常规文件，不能是符号链接")
    bounded_int(archive.stat().st_size, config["max_archive_bytes"], "归档大小")
    path = manifest_for(archive)
    require(path.is_file() and not path.is_symlink(), "同名 manifest 缺失或为链接")
    bounded_int(path.stat().st_size, 8 * 1024 * 1024, "manifest 大小")
    m = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    exact_keys(m, ("format", "project", "bay", "content", "integrity", "restore"), "manifest")
    require(m["format"] == FORMAT, "仅接受 backup-bay@0.2；旧包须在受控环境另行迁移核验")
    require(m["project"] == config["project"], "manifest 项目身份不匹配")
    exact_keys(m["bay"], ("id", "stage", "created_utc", "tag", "note"), "bay")
    bay = m["bay"]
    require(isinstance(bay["id"], str) and bay["id"] == archive.name[:-len(suffix)] and
            re.fullmatch(re.escape(config["project"]) + r"-[A-Za-z0-9._-]+", bay["id"]), "bay.id 与项目及归档文件名不匹配")
    require(isinstance(bay["stage"], str) and bay["stage"] in STAGES, "stage 无效")
    parse_time(bay["created_utc"])
    require(isinstance(bay["tag"], str) and re.fullmatch(r"[A-Za-z0-9._-]{0,64}", bay["tag"]), "tag 无效")
    require(isinstance(bay["note"], str) and len(bay["note"]) <= 512, "note 无效")
    exact_keys(m["integrity"], ("archive_sha256", "bytes", "compression"), "integrity")
    sha_field(m["integrity"]["archive_sha256"])
    bounded_int(m["integrity"]["bytes"], config["max_archive_bytes"], "manifest 归档大小")
    require(m["integrity"]["compression"] == kind, "压缩字段与后缀不一致")
    content = m["content"]
    exact_keys(content, ("entries", "excluded_patterns", "exclusion_policy", "sensitivity_review", "db_dump"), "content")
    require(content["exclusion_policy"] == "path-policy@1" and content["sensitivity_review"] == "not_performed", "不得将排除策略声明为秘密扫描结论")
    require(content["db_dump"] in ("not_configured", "included"), "db_dump 状态无效")
    require(type(content["excluded_patterns"]) is list and all(isinstance(x, str) for x in content["excluded_patterns"]), "excluded_patterns 无效")
    entries = content["entries"]
    require(type(entries) is list, "entries 须为数组")
    bounded_int(len(entries), config["max_members"], "条目数量")
    files, total = {}, 0
    for entry in entries:
        require(type(entry) is dict and entry.get("type") in ("file", "directory"), "仅支持普通文件和目录")
        keys = ("path", "type", "mode", "size", "sha256") if entry["type"] == "file" else ("path", "type", "mode")
        exact_keys(entry, keys, "entry")
        name = safe_name(entry["path"])
        require(name not in files and not prohibited(name), "重复或受排除策略禁止的条目")
        require(type(entry["mode"]) is int and 0 <= entry["mode"] <= 0o777, "权限含特殊位或无效")
        files[name] = entry
        if entry["type"] == "file":
            bounded_int(entry["size"], config["max_file_bytes"], "文件大小", zero=True)
            sha_field(entry["sha256"])
            total += entry["size"]
    require(total <= config["max_unpacked_bytes"], "文件总大小超过限额")
    for name in files:
        for parent in PurePosixPath(name).parents:
            if str(parent) != ".":
                require(files.get(str(parent), {}).get("type") == "directory", "父目录清单缺失或冲突")
    require(files.get("RESTORE.txt", {}).get("type") == "file", "缺少 RESTORE.txt")
    require((files.get("dump.sql", {}).get("type") == "file") == (content["db_dump"] == "included"), "dump.sql 与声明不符")
    require(isinstance(m["restore"], str) and 0 < len(m["restore"]) <= 4096, "restore 说明无效")
    return m


def bounded_copy(source, destination, limit):
    total = 0
    while chunk := source.read(min(1024 * 1024, limit - total + 1)):
        total += len(chunk)
        require(total <= limit, "解压或文件数据超过资源限额")
        destination.write(chunk)
    return total


def validate_tar_envelope(stream):
    """Reject hidden concatenated/trailing payload and unsupported tar extensions."""
    length = stream.seek(0, os.SEEK_END)
    require(length % tarfile.BLOCKSIZE == 0, "tar 未按 512 字节对齐")
    stream.seek(0)
    while True:
        header = stream.read(tarfile.BLOCKSIZE)
        require(len(header) == tarfile.BLOCKSIZE, "tar 缺少完整终结块")
        if header == bytes(tarfile.BLOCKSIZE):
            require(stream.read(tarfile.BLOCKSIZE) == bytes(tarfile.BLOCKSIZE), "tar 缺少第二终结零块")
            while chunk := stream.read(1024 * 1024):
                require(not any(chunk), "tar 终结后含未列入清单的数据")
            stream.seek(0)
            return
        member = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        require(member.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE), "tar 不支持链接、特殊文件或扩展头")
        require(0 <= member.size <= length - stream.tell(), "tar 数据长度无效")
        stream.seek(member.size, os.SEEK_CUR)
        padding = (-member.size) % tarfile.BLOCKSIZE
        require(stream.read(padding) == bytes(padding), "tar 文件填充不是零字节")


@contextlib.contextmanager
def opened_tar(archive, config):
    _, kind = archive_kind(archive)
    with archive.open("rb") as source:
        magic = source.read(4)
    require(magic.startswith(b"\x1f\x8b") if kind == "gzip" else magic == b"\x28\xb5\x2f\xfd", "压缩魔数与声明不一致")
    with tempfile.TemporaryFile() as unpacked:
        if kind == "gzip":
            with gzip.open(archive, "rb") as source:
                bounded_copy(source, unpacked, config["max_unpacked_bytes"])
        else:
            with subprocess.Popen(["zstd", "-q", "-d", "-c", "--", str(archive)], stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL) as process:
                try:
                    bounded_copy(process.stdout, unpacked, config["max_unpacked_bytes"])
                    require(process.wait(timeout=30) == 0, "zstd 解压失败")
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
        validate_tar_envelope(unpacked)
        with tarfile.open(fileobj=unpacked, mode="r:") as tar:
            yield tar


def inspect_archive(tar, manifest, config):
    expected = {entry["path"]: entry for entry in manifest["content"]["entries"]}
    found = set()
    for member in tar:
        require(len(found) < config["max_members"], "tar 条目数量超过限额")
        name = safe_name(member.name.rstrip("/") if member.isdir() else member.name)
        require(name not in found and name in expected, "tar 有重复或未列入清单的条目")
        found.add(name)
        entry = expected[name]
        require((member.isfile() and entry["type"] == "file") or
                (member.isdir() and entry["type"] == "directory"), "tar 链接、特殊文件或类型不符")
        require(member.mode == entry["mode"] and not member.pax_headers, "tar 权限或扩展属性与契约不符")
        if member.isfile():
            require(member.size == entry["size"], "tar 文件大小与清单不符")
            with tar.extractfile(member) as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            require(actual == entry["sha256"], "逐文件 sha256 不符")
        else:
            require(member.size == 0, "目录条目含数据")
    require(found == set(expected), "tar 缺失清单条目")


@contextlib.contextmanager
def validated_archive(archive, config):
    manifest = load_manifest(archive, config)
    require(archive.stat().st_size == manifest["integrity"]["bytes"], "归档长度不符")
    require(digest(archive) == manifest["integrity"]["archive_sha256"], "归档 sha256 不符")
    with opened_tar(archive, config) as tar:
        inspect_archive(tar, manifest, config)
        yield tar, manifest


def dump_database(config, target):
    if not config["db_dump_hook"]:
        return False
    args = shlex.split(config["db_dump_hook"])
    require(args and not any(arg in {"|", "||", "&&", ";", ">", ">>", "<", "&"} for arg in args), "db_dump_hook 仅支持命令和参数，不支持 shell 运算符")
    with tempfile.TemporaryFile() as errors, target.open("xb") as output:
        def limit_output():
            resource.setrlimit(resource.RLIMIT_FSIZE, (config["max_file_bytes"], config["max_file_bytes"]))
        process = subprocess.Popen(args, stdout=output, stderr=errors, preexec_fn=limit_output, start_new_session=True)
        descendants = False
        try:
            try:
                process.wait(timeout=config["db_dump_timeout_seconds"])
            except subprocess.TimeoutExpired as exc:
                raise BayError("DB dump 超时，已终止同组进程并中止备份") from exc
            try:
                os.killpg(process.pid, 0)
                descendants = True
            except ProcessLookupError:
                pass
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        require(not descendants, "DB dump 留下同组子进程，已终止并中止备份")
        require(process.returncode == 0 and output.tell() > 0, "DB dump 失败或为空，备份中止")
        require(output.tell() <= config["max_file_bytes"], "DB dump 超过文件限额，备份中止")
    return True


def build_backup(root, config, stage, tag, note):
    require(re.fullmatch(r"[A-Za-z0-9._-]{0,64}", tag) is not None, "tag 须为至多 64 个安全 ASCII 字符")
    require(len(note) <= 512, "note 须至多 512 字符")
    for reserved in ("RESTORE.txt", "dump.sql"):
        require(not os.path.lexists(root / reserved), f"项目根 {reserved} 为保留输出名，请先人工处置")
    bay_dir = root / "backups"
    require(not bay_dir.is_symlink(), "backups 不能是链接")
    bay_dir.mkdir(mode=0o700, exist_ok=True)
    created = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
    identifier = f"{config['project']}-{STAGES[stage]}-{now_utc():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:12]}" + (f"-{tag}" if tag else "")
    suffix = next(s for s, kind in SUFFIXES.items() if kind == config["compression"])
    patterns, entries, total = config["exclude_extra"].split(), [], 0
    with tempfile.TemporaryDirectory(prefix="project-bay-") as temp_name:
        temp = Path(temp_name)
        dump_ok = dump_database(config, temp / "dump.sql")
        restore_text = (f"项目: {config['project']}\n生成时间 UTC: {created}\n格式: {FORMAT}\n"
            "仅恢复清单中的普通文件与目录；不代表完整业务、数据库或秘密已备份。\n"
            "请使用 project-bay-restore.sh --target <空目录> <归档>，先做 --dry-run。\n"
            f"依赖提示（不会自动执行）: {config['dep_rebuild']}\n")
        (temp / "RESTORE.txt").write_text(restore_text, encoding="utf-8")
        os.chmod(temp / "RESTORE.txt", 0o600)
        if dump_ok:
            os.chmod(temp / "dump.sql", 0o600)
        with tarfile.open(temp / "payload.tar", "w", format=tarfile.USTAR_FORMAT) as tar:
            def add(path, name):
                nonlocal total
                safe_name(name)
                require(len(entries) < config["max_members"], "源条目数量超过限额")
                info = path.lstat()
                require(stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode), "不支持符号链接或其他特殊源条目")
                mode = stat.S_IMODE(info.st_mode) & 0o777
                member = tarfile.TarInfo(name)
                member.mode, member.mtime = mode, int(info.st_mtime)
                if stat.S_ISDIR(info.st_mode):
                    member.type = tarfile.DIRTYPE
                    entries.append({"path": name, "type": "directory", "mode": mode})
                    tar.addfile(member)
                    for child in sorted(path.iterdir(), key=lambda p: p.name):
                        child_name = name + "/" + child.name
                        if not excluded(child_name, patterns):
                            add(child, child_name)
                else:
                    bounded_int(info.st_size, config["max_file_bytes"], "源文件大小", zero=True)
                    total += info.st_size
                    require(total <= config["max_unpacked_bytes"], "源总大小超过限额")
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(fd, "rb") as source:
                        current = os.fstat(source.fileno())
                        require(stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino), "读取期间源文件改变")
                        member.size = current.st_size
                        tar.addfile(member, source)
                        source.seek(0)
                        h = hashlib.file_digest(source, "sha256").hexdigest()
                        require(os.fstat(source.fileno()).st_mtime_ns == current.st_mtime_ns, "备份期间源文件改变")
                    entries.append({"path": name, "type": "file", "mode": mode, "size": member.size, "sha256": h})
            for path in sorted(root.iterdir(), key=lambda p: p.name):
                if not excluded(path.name, patterns):
                    add(path, path.name)
            add(temp / "RESTORE.txt", "RESTORE.txt")
            if dump_ok:
                add(temp / "dump.sql", "dump.sql")
        archive = temp / (identifier + suffix)
        if config["compression"] == "gzip":
            with (temp / "payload.tar").open("rb") as source, gzip.open(archive, "wb") as output:
                shutil.copyfileobj(source, output)
        else:
            subprocess.run(["zstd", "-q", str(temp / "payload.tar"), "-o", str(archive)], check=True)
        manifest = {"format": FORMAT, "project": config["project"],
            "bay": {"id": identifier, "stage": stage, "created_utc": created, "tag": tag, "note": note},
            "content": {"entries": entries, "excluded_patterns": sorted(REGEN | PRIVATE_DIRS) + list(PRIVATE_FILES) + patterns,
                        "exclusion_policy": "path-policy@1", "sensitivity_review": "not_performed",
                        "db_dump": "included" if dump_ok else "not_configured"},
            "integrity": {"archive_sha256": digest(archive), "bytes": archive.stat().st_size, "compression": config["compression"]},
            "restore": restore_text}
        manifest_path = manifest_for(archive)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with validated_archive(archive, config):
            pass
        # Exclusive creation never overwrites a rollback point. Incomplete pairs fail check.
        for source in (archive, manifest_path):
            with source.open("rb") as incoming, (bay_dir / source.name).open("xb") as output:
                os.chmod(output.fileno(), 0o600)
                shutil.copyfileobj(incoming, output)
                output.flush()
                os.fsync(output.fileno())
    print(f"舱已生成: backups/{identifier}{suffix}\nmanifest: backups/{identifier}.manifest.json")
    print("已验证清单与逐文件 hash；未进行敏感内容审查；未删除旧备份。")


def check_backups(root, config):
    bay_dir = root / "backups"
    require(not bay_dir.is_symlink(), "backups 不能是链接")
    archives = sorted(path for path in bay_dir.iterdir() if any(path.name.endswith(s) for s in SUFFIXES)) if bay_dir.exists() else []
    require(archives, "舱库为空；没有可验证的备份")
    manifests = {manifest_for(path) for path in archives}
    require(not any(path not in manifests for path in bay_dir.glob("*.manifest.json")), "存在无归档的 manifest")
    found = []
    for archive in archives:
        with validated_archive(archive, config) as (_, m):
            found.append((parse_time(m["bay"]["created_utc"]), m["bay"]["id"], archive, m))
    created, _, archive, manifest = max(found, key=lambda item: (item[0], item[1]))
    print(f"已完整验证 {len(found)} 对归档及逐文件清单。\n最新舱: {archive.name}")
    print(f"created_utc: {manifest['bay']['created_utc']}\nstage: {manifest['bay']['stage']}")
    if sum(item[0] == created for item in found) > 1:
        print("时间戳并列：按 bay.id 稳定排序；未推断同秒内的真实先后。")
    if (now_utc() - created).total_seconds() >= config["stale_hours"] * 3600:
        print("备份已超过 stale_hours；请按保留策略人工处理。")


def extract_verified(tar, manifest, destination):
    # Avoid extractall: no links, owner changes, device nodes, or implicit writes.
    members = {member.name.rstrip("/"): member for member in tar.getmembers()}
    entries = manifest["content"]["entries"]
    for entry in sorted(entries, key=lambda e: (len(PurePosixPath(e["path"]).parts), e["path"])):
        target = destination / entry["path"]
        if entry["type"] == "directory":
            target.mkdir(mode=0o700)
        else:
            with tar.extractfile(members[entry["path"]]) as source, target.open("xb") as output:
                bounded_copy(source, output, entry["size"])
    for entry in entries:
        if entry["type"] == "file":
            require(digest(destination / entry["path"]) == entry["sha256"], "恢复后文件 hash 不符")
    for entry in sorted(entries, key=lambda e: len(PurePosixPath(e["path"]).parts), reverse=True):
        os.chmod(destination / entry["path"], entry["mode"])


def restore_backup(root, config, archive, dry, target):
    require(dry != (target is not None), "须二选一：--dry-run 或 --target <空目录>")
    archive = archive if archive.is_absolute() else root / archive
    with validated_archive(archive, config) as (tar, manifest):
        if dry:
            with tempfile.TemporaryDirectory(prefix="project-bay-dry-run-") as temp:
                extract_verified(tar, manifest, Path(temp))
            print("演练通过：在临时目录完成逐文件恢复及 hash 校验；项目未改写。")
        else:
            target = Path(os.path.abspath(target))
            require(target.resolve() == target and not target.is_symlink(), "恢复目标及父路径不能含符号链接")
            require(target != root and root not in target.parents, "恢复目标必须在当前项目树之外")
            require(target.parent.is_dir(), "恢复目标的父目录须已存在")
            if target.exists():
                require(target.is_dir() and not any(target.iterdir()), "恢复目标必须为空目录；不会删除已有数据")
            else:
                target.mkdir(mode=0o700)
            extract_verified(tar, manifest, target)
            print("已恢复到指定空目录并逐文件校验；未切换业务、执行依赖或导入数据库。")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("stage", choices=STAGES)
    backup.add_argument("tag", nargs="?", default="")
    backup.add_argument("note", nargs="?", default="")
    commands.add_parser("check")
    restore = commands.add_parser("restore")
    restore.add_argument("--dry-run", action="store_true")
    restore.add_argument("--target", type=Path)
    restore.add_argument("archive", type=Path)
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        config = read_config(root)
        if args.command == "backup":
            build_backup(root, config, args.stage, args.tag, args.note)
        elif args.command == "check":
            check_backups(root, config)
        else:
            restore_backup(root, config, args.archive, args.dry_run, args.target)
        return 0
    except (BayError, OSError, ValueError, KeyError, TypeError, tarfile.TarError, EOFError, subprocess.SubprocessError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
