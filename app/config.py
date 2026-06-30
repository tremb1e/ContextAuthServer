from __future__ import annotations

import os
import stat
import secrets
from dataclasses import dataclass
from pathlib import Path


DEFAULT_STUDY_SALT = "Continuous_Authentication"
DEFAULT_TIME_SYNC_REGION = "CN"
DEFAULT_TIME_SYNC_NTP_SERVERS = (
    "ntp.aliyun.com",
    "ntp.tencent.com",
    "0.cn.pool.ntp.org",
    "1.cn.pool.ntp.org",
    "2.cn.pool.ntp.org",
    "3.cn.pool.ntp.org",
)


def _csv_env_values(name: str) -> tuple[str, ...]:
    raw = os.getenv(name)
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _positive_int_env(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name}_must_be_positive")
    return value


def _non_negative_int_env(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError(f"{name}_must_be_non_negative")
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    rules_file: Path
    study_salt_env: str | None
    rules_version: str
    ingest_require_auth: bool
    min_free_bytes: int
    log_dir: Path
    time_sync_region: str
    time_sync_ntp_servers: tuple[str, ...]
    time_sync_max_acceptable_rtt_millis: int

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("SERVER_DATA_DIR", "./data/paper")).resolve()
        rules_file_env = os.getenv("SERVER_RULES_FILE")
        rules_file = Path(rules_file_env).expanduser().resolve() if rules_file_env else (data_dir / "rules.json").resolve()
        log_dir = Path(os.getenv("SERVER_LOG_DIR", "./logs")).resolve()
        ntp_servers = _csv_env_values("TIME_SYNC_NTP_SERVERS") or DEFAULT_TIME_SYNC_NTP_SERVERS
        ingest_require_auth = os.getenv("INGEST_REQUIRE_AUTH", "false").lower() == "true"
        if ingest_require_auth:
            raise ValueError("INGEST_REQUIRE_AUTH_unsupported")
        return cls(
            data_dir=data_dir,
            rules_file=rules_file,
            study_salt_env=os.getenv("SERVER_STUDY_SALT"),
            rules_version=os.getenv("RULES_VERSION", "1"),
            ingest_require_auth=ingest_require_auth,
            min_free_bytes=_non_negative_int_env("SERVER_MIN_FREE_BYTES", 10 * 1024 * 1024),
            log_dir=log_dir,
            time_sync_region=os.getenv("TIME_SYNC_REGION", DEFAULT_TIME_SYNC_REGION),
            time_sync_ntp_servers=ntp_servers,
            time_sync_max_acceptable_rtt_millis=_positive_int_env("TIME_SYNC_MAX_ACCEPTABLE_RTT_MILLIS", 3000),
        )


def _private_file(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except PermissionError:
        pass


def get_server_study_salt(settings: Settings) -> str:
    """Return stable salt without logging it.

    Prompt requires stable reuse. If env is unset, create/reuse
    data/paper/server_study_salt.txt with 0600 permissions.
    """
    if settings.study_salt_env:
        return settings.study_salt_env

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    salt_file = settings.data_dir / "server_study_salt.txt"
    if salt_file.exists():
        _private_file(salt_file)
        value = salt_file.read_text(encoding="utf-8").strip()
        return value or DEFAULT_STUDY_SALT

    salt = DEFAULT_STUDY_SALT
    # If the deployment explicitly opts out of the canonical study salt,
    # keep the file stable but unpredictable.
    if os.getenv("SERVER_GENERATE_RANDOM_STUDY_SALT", "false").lower() == "true":
        salt = secrets.token_urlsafe(32)
    salt_file.write_text(salt + "\n", encoding="utf-8")
    _private_file(salt_file)
    return salt


SETTINGS = Settings.from_env()
