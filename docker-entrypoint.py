#!/usr/local/bin/python
from __future__ import annotations

import os
import pwd
import sys
from pathlib import Path


APP_USER = os.getenv("APP_USER", "appuser")
TRUE_VALUES = {"1", "true", "yes", "on"}
UNSAFE_RECURSIVE_CHOWN_ROOTS = {
    Path("/"),
    Path("/app"),
    Path("/bin"),
    Path("/boot"),
    Path("/dev"),
    Path("/etc"),
    Path("/home"),
    Path("/lib"),
    Path("/lib64"),
    Path("/proc"),
    Path("/root"),
    Path("/run"),
    Path("/sbin"),
    Path("/sys"),
    Path("/usr"),
    Path("/var"),
}


def _truthy(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in TRUE_VALUES


def _path_from_env(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser().resolve()


def _chown_one(path: Path, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid, follow_symlinks=False)
    except PermissionError as exc:
        print(f"warning: cannot chown {path}: {exc}", file=sys.stderr, flush=True)


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    if path in UNSAFE_RECURSIVE_CHOWN_ROOTS:
        raise SystemExit(f"refusing recursive chown of unsafe path: {path}")

    _chown_one(path, uid, gid)
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames:
            _chown_one(Path(dirpath) / name, uid, gid)
        for name in filenames:
            _chown_one(Path(dirpath) / name, uid, gid)


def _prepare_writable_paths(uid: int, gid: int) -> None:
    data_dir = _path_from_env("SERVER_DATA_DIR", "/data/paper")
    log_dir = _path_from_env("SERVER_LOG_DIR", "/app/logs")
    rules_file = _path_from_env("SERVER_RULES_FILE", str(data_dir / "rules.json"))
    required_dirs = {
        data_dir,
        data_dir / "devices",
        data_dir / "index",
        data_dir / "quarantine",
        log_dir,
        rules_file.parent,
    }

    for path in sorted(required_dirs):
        path.mkdir(parents=True, exist_ok=True)

    if not _truthy("SERVER_FIX_PERMISSIONS", "true"):
        return

    recursive = _truthy("SERVER_CHOWN_RECURSIVE", "true")
    for path in sorted({data_dir, log_dir, rules_file.parent}):
        if recursive:
            _chown_tree(path, uid, gid)
        else:
            _chown_one(path, uid, gid)
            for child in path.iterdir():
                _chown_one(child, uid, gid)


def _target_identity() -> tuple[pwd.struct_passwd, int, int]:
    user = pwd.getpwnam(APP_USER)
    uid = int(os.getenv("APP_UID", str(user.pw_uid)))
    gid = int(os.getenv("APP_GID", str(user.pw_gid)))
    if uid <= 0 or gid <= 0:
        raise SystemExit("APP_UID and APP_GID must be positive non-root ids")
    return user, uid, gid


def _drop_privileges() -> None:
    if os.geteuid() != 0:
        return

    user, uid, gid = _target_identity()
    _prepare_writable_paths(uid, gid)
    if uid == user.pw_uid and gid == user.pw_gid:
        os.initgroups(APP_USER, gid)
    else:
        os.setgroups([gid])
    os.setgid(gid)
    os.setuid(uid)
    os.environ["HOME"] = user.pw_dir


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("missing command")
    _drop_privileges()
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
