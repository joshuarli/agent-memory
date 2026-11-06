#!/usr/bin/env python3

import errno
import os
import plistlib
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence


LAUNCHD_LABEL = "com.josh.agent-learning"
LAUNCHD_TEMPLATE = Path("dotfiles/launchd/com.josh.agent-learning.plist.in")
LAUNCHD_FILENAME = "com.josh.agent-learning.plist"


@dataclass(frozen=True)
class SchedulerStatus:
    supported: bool
    configured: bool
    loaded: Optional[bool]


class SchedulerError(RuntimeError):
    pass


def hook(state_dir: Path, payload: Mapping[str, object]) -> bool:
    if "AGENT_LEARN_WORKER" in os.environ:
        return False
    if not isinstance(payload, Mapping) or payload.get("hook_event_name") != "Stop":
        return False

    cwd_value = payload.get("cwd")
    if not isinstance(cwd_value, str) or not cwd_value or len(cwd_value) > 4096:
        return False

    cwd = _canonical_path(cwd_value)
    if cwd is None:
        return False

    from agent_memory.store import Store

    database_path = Path(Store.database_path(Path(state_dir)))
    if not database_path.is_file():
        return False

    db = _connect(database_path, "ro")
    try:
        projects = _matching_projects(db, cwd)
    finally:
        db.close()
    if not projects:
        return False

    db = _connect(database_path, "rw")
    try:
        db.execute("BEGIN IMMEDIATE")
        projects = _matching_projects(db, cwd)
        if not projects:
            db.rollback()
            return False
        project = projects[0]
        db.execute(
            "CREATE TABLE IF NOT EXISTS learning_hints ("
            "project TEXT PRIMARY KEY REFERENCES learning_projects(project) ON DELETE CASCADE, "
            "observed_at REAL NOT NULL"
            ")"
        )
        db.execute(
            "INSERT INTO learning_hints(project, observed_at) VALUES (?, ?) "
            "ON CONFLICT(project) DO UPDATE SET observed_at = excluded.observed_at",
            (project, time.time()),
        )
        db.commit()
    finally:
        db.close()
    return True


def scheduler_status(
    home: Path, runner: Optional[Callable[..., object]] = None
) -> SchedulerStatus:
    if sys.platform != "darwin":
        return SchedulerStatus(False, False, None)

    home_path = Path(home).expanduser().resolve(strict=True)
    directory_fd = _open_launch_agents(home_path, create=False)
    try:
        configured = False
        if directory_fd is not None:
            configured = _read_owned_plist(directory_fd) is not None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)

    loaded = _launchd_loaded(runner)
    return SchedulerStatus(True, configured, loaded)


def install_scheduler(
    repository: Path,
    state_dir: Path,
    home: Path,
    runner: Optional[Callable[..., object]] = None,
) -> Path:
    if sys.platform != "darwin":
        raise SchedulerError("launchd scheduling is available only on macOS")

    home_path = Path(home).expanduser().resolve(strict=True)
    state_path = Path(state_dir).expanduser().resolve()
    repository_path = Path(repository).expanduser().resolve(strict=True)
    template_path = repository_path / LAUNCHD_TEMPLATE
    template = plistlib.loads(template_path.read_bytes())
    program_arguments = template.get("ProgramArguments")
    if not isinstance(program_arguments, list):
        raise SchedulerError("LaunchAgent template has no ProgramArguments array")
    values = {
        "@@HOME@@": str(home_path),
        "@@STATE_DIR@@": str(state_path),
    }
    template["ProgramArguments"] = [
        _replace_tokens(argument, values) for argument in program_arguments
    ]
    environment = template.get("EnvironmentVariables", {})
    if not isinstance(environment, dict):
        raise SchedulerError("LaunchAgent template has no EnvironmentVariables dictionary")
    template["EnvironmentVariables"] = {
        name: _replace_tokens(value, values)
        for name, value in environment.items()
    }
    if template.get("Label") != LAUNCHD_LABEL:
        raise SchedulerError("LaunchAgent template label does not match the managed label")

    directory_fd = _open_launch_agents(home_path, create=True)
    try:
        if directory_fd is None:
            raise SchedulerError("could not open the LaunchAgents directory")
        _read_owned_plist(directory_fd)
        _replace_plist(directory_fd, plistlib.dumps(template, sort_keys=False))
    finally:
        if directory_fd is not None:
            os.close(directory_fd)

    if _launchd_loaded(runner):
        _run_launchctl(
            ["bootout", _launchd_target()], runner, "unload the LaunchAgent"
        )
    _run_launchctl(
        [
            "bootstrap",
            _launchd_domain(),
            str(home_path / "Library" / "LaunchAgents" / LAUNCHD_FILENAME),
        ],
        runner,
        "load the LaunchAgent",
    )
    if not _launchd_loaded(runner):
        raise SchedulerError("launchd did not report the LaunchAgent as loaded")
    return home_path / "Library" / "LaunchAgents" / LAUNCHD_FILENAME


def remove_scheduler(
    home: Path, runner: Optional[Callable[..., object]] = None
) -> bool:
    if sys.platform != "darwin":
        raise SchedulerError("launchd scheduling is available only on macOS")

    home_path = Path(home).expanduser().resolve(strict=True)
    directory_fd = _open_launch_agents(home_path, create=False)
    try:
        configured = directory_fd is not None and _read_owned_plist(directory_fd) is not None
        loaded = _launchd_loaded(runner)
        if loaded:
            _run_launchctl(
                ["bootout", _launchd_target()], runner, "unload the LaunchAgent"
            )
        if configured and directory_fd is not None:
            os.unlink(LAUNCHD_FILENAME, dir_fd=directory_fd)
            os.fsync(directory_fd)
        return configured or loaded
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _matching_projects(db: sqlite3.Connection, cwd: Path) -> list[str]:
    rows = db.execute(
        "SELECT project, cwd FROM learning_projects WHERE enabled = 1"
    ).fetchall()
    matches = []
    for project, project_cwd in rows:
        if not isinstance(project, str) or not isinstance(project_cwd, str):
            continue
        project_path = _canonical_path(project_cwd)
        if project_path is None:
            continue
        try:
            cwd.relative_to(project_path)
        except ValueError:
            continue
        matches.append((len(project_path.parts), project))
    matches.sort(reverse=True)
    return [project for _, project in matches]


def _canonical_path(value: str) -> Optional[Path]:
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _connect(database_path: Path, mode: str) -> sqlite3.Connection:
    if mode not in {"ro", "rw"}:
        raise ValueError("SQLite mode must be 'ro' or 'rw'")
    db = sqlite3.connect(
        database_path.as_uri() + "?mode=" + mode, uri=True, timeout=1
    )
    db.row_factory = None
    return db


def _replace_tokens(value: object, values: Mapping[str, str]) -> object:
    if not isinstance(value, str):
        raise SchedulerError("LaunchAgent template contains a non-string argument")
    return re.sub(
        "|".join(re.escape(token) for token in values),
        lambda match: values[match.group(0)],
        value,
    )


def _open_launch_agents(home: Path, create: bool) -> Optional[int]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(str(home), flags)
    except FileNotFoundError:
        if create:
            raise
        return None
    try:
        for component in ("Library", "LaunchAgents"):
            try:
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            except FileNotFoundError:
                if not create:
                    os.close(directory_fd)
                    return None
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                child_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _read_owned_plist(directory_fd: int) -> Optional[dict[str, object]]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(LAUNCHD_FILENAME, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise FileExistsError("LaunchAgent path is not a regular file")
        with os.fdopen(file_fd, "rb") as stream:
            file_fd = -1
            plist = plistlib.load(stream)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
    if not isinstance(plist, dict) or plist.get("Label") != LAUNCHD_LABEL:
        raise FileExistsError("LaunchAgent path belongs to another configuration")
    return {key: item for key, item in plist.items() if isinstance(key, str)}


def _replace_plist(directory_fd: int, contents: bytes) -> None:
    temporary_name = "." + LAUNCHD_FILENAME + "." + secrets.token_hex(8)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    file_fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
    try:
        view = memoryview(contents)
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "short write while saving LaunchAgent")
            view = view[written:]
        os.fsync(file_fd)
    except BaseException:
        os.close(file_fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise
    else:
        os.close(file_fd)
    try:
        os.replace(
            temporary_name,
            LAUNCHD_FILENAME,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise


def _launchd_domain() -> str:
    return "gui/" + str(os.getuid())


def _launchd_target() -> str:
    return _launchd_domain() + "/" + LAUNCHD_LABEL


def _launchd_loaded(runner: Optional[Callable[..., object]]) -> bool:
    result = _run_launchctl(["print", _launchd_target()], runner)
    returncode = getattr(result, "returncode", 1)
    return isinstance(returncode, int) and returncode == 0


def _run_launchctl(
    arguments: Sequence[str],
    runner: Optional[Callable[..., object]],
    action: Optional[str] = None,
) -> object:
    command = ["launchctl", *arguments]
    run = runner or subprocess.run
    result = run(command, check=False, capture_output=True, text=True)
    if action and getattr(result, "returncode", 1) != 0:
        detail = getattr(result, "stderr", "") or getattr(result, "stdout", "")
        raise SchedulerError("launchctl could not " + action + (": " + detail.strip() if detail else ""))
    return result
