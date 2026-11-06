import fcntl
import hashlib
import os
import re
import stat
import subprocess
import tempfile
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


KNOWN_LEAF_SKILLS = frozenset(
    {
        "api-surface-and-module-boundaries",
        "benchmark-checklist",
        "comment-cleanup",
        "dead-code-archaeology",
        "error-model",
        "isolate-effects",
        "lifecycle-concurrency",
        "make-code-search-a-reliable-navigation-boundary",
        "make-it-fast",
        "make-invalid-states-unrepresentable",
        "reference-parity",
        "root-cause",
        "single-source-of-truth",
        "technical-writing",
        "test-audit",
        "rust-cli-lexopt",
        "rust-hash",
        "refactor-ts",
    }
)

_HEX = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_PROPOSAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CONTEXTUAL_ACTION = re.compile(
    r"^(?:When|If|For|Before|After|During|On)\b", re.IGNORECASE
)
_AUTHORITY_WEAKENING = re.compile(
    r"\b(?:ignore|override|bypass|disable|circumvent|skip|remove|suppress)\s+"
    r"(?:all\s+)?(?:prior|previous|existing|current|global|owner|user|system|developer|"
    r"review|validation|permission|approval|security|safety|sandbox|authorization|"
    r"authentication|policy|policies|rules?|checks?|tests?|verification)\b|"
    r"\bwithout\s+(?:(?:owner|user|security)\s+)?"
    r"(?:asking|approval|permission|review|validation)\b|"
    r"\b(?:never|do\s+not|don't|avoid)\s+(?:ask|request)\s+(?:the\s+)?(?:user|owner)\b|"
    r"\b(?:always\s+approve|approve\s+all|allow\s+all|unrestricted\s+access)\b",
    re.IGNORECASE,
)
_PROTECTED_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|"
        r"github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{16,})\b",
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
        r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}={0,2}",
        r"\b(?:api[_ -]?key|access[_ -]?token|password|secret|client[_ -]?secret|"
        r"private[_ -]?key|credential)\b\s*[:=]\s*\S{6,}",
        r"/(?:Users|home)/[^/\s]+(?:/|\b)",
        r"(?:^|\s)~/(?:\.ssh|\.aws|\.gnupg|\.config/(?:gcloud|credentials))\b",
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        r"\b(?:system|developer|global|root|organization|org-wide)\s+"
        r"(?:prompt|instructions?|policy|contract)\b",
        r"\b(?:approval_policy|bypassPermissions|danger-full-access|sandbox_mode)\b",
        r"\b(?:agent|harness|tool)\s+(?:permissions?|approval|sandbox)\s+"
        r"(?:policy|rules?|settings?)\b",
        r"\b(?:global|root|owner|user|system|developer)\s+"
        r"(?:permissions?|approval|sandbox|security)\s+(?:policy|rules?|settings?)\b",
    )
)

_PROPOSAL_FIELDS = frozenset(
    {
        "proposal_id",
        "action",
        "owner",
        "target",
        "target_skill",
        "observation",
        "original_action",
        "better_action",
        "future_action",
        "applicability",
        "alternatives",
        "uncertainty",
        "evidence_refs",
        "edit",
        "independent_review",
    }
)
_REVIEW_FIELDS = frozenset(
    {"approves", "risks", "owner", "does_not_weaken"}
)
_EDIT_FIELDS = frozenset({"old", "new"})
_REF_FIELDS = frozenset({"session", "position", "digest"})
_MAX_TEXT = 1600
_MAX_EDIT = 5000
_MAX_EVIDENCE = 12
_MAX_SKILL_FILE = 8000
_WORKTREE_PREFIXES = (
    "agent-learning-promote-",
    "agent-learning-rollback-",
)


class _PromotionFailure(Exception):
    def __init__(self, reason, reason_code="validation_failed", blocked_path=None):
        super().__init__(reason)
        self.reason_code = reason_code
        self.blocked_path = blocked_path


class _CommandFailure(_PromotionFailure):
    pass


def protected_text(text):
    if not isinstance(text, str) or len(text) > 16000:
        return True
    return bool(_AUTHORITY_WEAKENING.search(text)) or any(
        pattern.search(text) for pattern in _PROTECTED_PATTERNS
    )


def safe_action(text):
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text) > _MAX_TEXT
        or not _CONTEXTUAL_ACTION.match(text.strip())
        or not text.strip().endswith((".", "!", "?"))
        or any(unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp") for char in text)
    ):
        return False
    return not protected_text(text)


def _result(status, commit=None, reason=None):
    return {"status": status, "commit": commit, "reason": reason}


def _blocked(reason, reason_code="validation_failed", blocked_path=None):
    result = _result("blocked", reason=reason)
    result["reason_code"] = reason_code
    if blocked_path is not None:
        result["blocked_path"] = os.fspath(blocked_path)
    return result


def _environment(overrides=None):
    env = os.environ.copy()
    for key in tuple(env):
        if key in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_COMMON_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_PREFIX",
            "GIT_CONFIG_PARAMETERS",
        } or key.startswith(("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "rerere.enabled"
    env["GIT_CONFIG_VALUE_0"] = "false"
    if overrides:
        env.update(overrides)
    return env


def _command(args, cwd, *, input_bytes=None, env=None, check=True, timeout=60):
    try:
        result = subprocess.run(
            args,
            cwd=os.fspath(cwd),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_environment(env),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _CommandFailure("command could not complete") from exc
    if check and result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace").strip()
        detail = stderr[:300] if stderr else "command failed"
        raise _CommandFailure(detail)
    return result


def _git(repository, *args, config=(), check=True, timeout=60):
    full = ["git", "-C", os.fspath(repository)]
    for key, value in config:
        full.extend(("-c", "{}={}".format(key, value)))
    full.extend(args)
    result = _command(full, repository, check=check, timeout=timeout)
    return result.stdout.decode("utf-8", "surrogateescape").strip()


def _git_bytes(repository, *args, config=(), check=True, timeout=60):
    full = ["git", "-C", os.fspath(repository)]
    for key, value in config:
        full.extend(("-c", "{}={}".format(key, value)))
    full.extend(args)
    return _command(full, repository, check=check, timeout=timeout).stdout


def _repository_root(repository):
    try:
        root = Path(repository).resolve(strict=True)
        prefix = _git_bytes(root, "rev-parse", "--show-prefix")
    except (OSError, _PromotionFailure) as exc:
        raise _PromotionFailure("repository is unavailable") from exc
    if prefix != b"\n":
        raise _PromotionFailure("repository must name its Git worktree root")
    return root


def _head(repository):
    value = _git(repository, "rev-parse", "--verify", "HEAD^{commit}")
    if not _HEX.fullmatch(value):
        raise _PromotionFailure("Git returned an invalid commit identifier")
    return value


def _expected_head(repository, expected_head):
    if not isinstance(expected_head, str) or not _HEX.fullmatch(expected_head):
        raise _PromotionFailure("expected_head must be a full commit identifier", "invalid_proposal")
    actual = _head(repository)
    if actual != expected_head:
        raise _PromotionFailure("repository HEAD changed", "repository_changed")
    return actual


def _worktree_clean(repository):
    result = _git_bytes(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        config=(("core.fsmonitor", "false"), ("core.untrackedCache", "false")),
    )
    if result:
        raise _PromotionFailure("repository has local or staged changes", "repository_dirty")


def _current_branch(repository):
    branch = _git(repository, "symbolic-ref", "--quiet", "HEAD", check=False)
    if not branch.startswith("refs/heads/"):
        raise _PromotionFailure("repository HEAD is not on a branch", "repository_changed")
    return branch


def _common_git_dir(repository):
    return Path(
        _git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
    ).resolve(strict=True)


@contextmanager
def _promotion_lock(repository):
    common = _common_git_dir(repository)
    lock_path = common / "agent-learning-promote.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(os.fspath(lock_path), flags, 0o600)
    except OSError as exc:
        raise _PromotionFailure("could not open promotion lock") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _PromotionFailure("promotion lock is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise _PromotionFailure("another promotion is active", "lock_contended") from exc
        yield
    finally:
        os.close(descriptor)


def _validate_proposal(proposal) -> Any:
    if not isinstance(proposal, dict) or proposal.keys() - _PROPOSAL_FIELDS:
        raise _PromotionFailure("proposal has an unsupported shape")
    if proposal.get("action") != "improve":
        return None
    proposal_id = proposal.get("proposal_id")
    if not isinstance(proposal_id, str) or not _PROPOSAL_ID.fullmatch(proposal_id):
        raise _PromotionFailure("proposal_id is invalid")
    owner = proposal.get("owner")
    if (
        not isinstance(owner, str)
        or owner not in KNOWN_LEAF_SKILLS
        or proposal.get("target") != "skill"
    ):
        raise _PromotionFailure("target is outside the promotion allowlist")
    if proposal.get("target_skill") != owner:
        raise _PromotionFailure("target_skill must equal the owner skill")
    for field in (
        "observation",
        "original_action",
        "better_action",
        "future_action",
        "applicability",
        "uncertainty",
    ):
        value = proposal.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
            raise _PromotionFailure("proposal is missing a bounded {}".format(field))
        if protected_text(value):
            raise _PromotionFailure("proposal narrative contains protected text")
    alternatives = proposal.get("alternatives")
    if not isinstance(alternatives, str) or not alternatives.strip() or len(alternatives) > _MAX_TEXT:
        raise _PromotionFailure("alternatives must be bounded text")
    if protected_text(alternatives):
        raise _PromotionFailure("proposal alternatives contain protected text")
    review = proposal.get("independent_review")
    if (
        not isinstance(review, dict)
        or review.keys() != _REVIEW_FIELDS
        or review.get("approves") is not True
        or review.get("risks") != []
        or review.get("owner") != owner
        or review.get("does_not_weaken") is not True
    ):
        raise _PromotionFailure("independent review did not approve the edit")
    edit = proposal.get("edit")
    if not isinstance(edit, dict) or edit.keys() != _EDIT_FIELDS:
        raise _PromotionFailure("edit must contain only old and new text")
    old = edit.get("old")
    new = edit.get("new")
    if (
        not isinstance(old, str)
        or not isinstance(new, str)
        or not old.strip()
        or len(old) > _MAX_EDIT
        or len(new) > _MAX_EDIT
    ):
        raise _PromotionFailure("edit text is empty or too large")
    if old.startswith("description: ") and new.startswith("description: "):
        added = _description_insertion(old, new)
        mode = "description"
        if len(added) > 120:
            raise _PromotionFailure("description trigger changes are limited to 120 characters")
    else:
        if new.startswith(old + "\n") or new.startswith(old + " "):
            added = new[len(old) + 1 :]
        else:
            raise _PromotionFailure("edit may only append one contextual action to existing guidance")
        mode = "paragraph"
        if "\n" in added:
            raise _PromotionFailure("appended guidance must be one line")
    if (
        not added.strip()
        or added.strip() != proposal["future_action"].strip()
        or not safe_action(added.strip())
    ):
        raise _PromotionFailure("added guidance failed the narrow safety gate")
    refs = _validate_evidence_refs(proposal.get("evidence_refs"))
    return proposal_id, owner, old, new, added, mode, refs


def _description_insertion(old, new):
    if "\n" in old or "\n" in new or "\r" in old or "\r" in new:
        raise _PromotionFailure("description edits must stay on one frontmatter line")
    prefix = "description: "
    old_value = old[len(prefix) :]
    new_value = new[len(prefix) :]
    if old_value[:1] in ('"', "'"):
        if not old_value.endswith(old_value[0]) or new_value[:1] != old_value[:1] or not new_value.endswith(old_value[0]):
            raise _PromotionFailure("description quoting must remain unchanged")
        quote = old_value[0]
        old_inner = old_value[1:-1]
        new_inner = new_value[1:-1]
    else:
        raise _PromotionFailure("description trigger edits require quoted metadata")
    if old_inner.count("Skip") != 1:
        raise _PromotionFailure("description must retain one existing Skip clause")
    skip = old_inner.index("Skip")
    before_skip = old_inner[:skip]
    skip_clause = old_inner[skip:]
    if not new_inner.startswith(before_skip) or not new_inner.endswith(skip_clause):
        raise _PromotionFailure("description refinement must preserve its existing wording")
    added = new_inner[len(before_skip) : len(new_inner) - len(skip_clause)]
    if not added or before_skip + added + skip_clause != new_inner:
        raise _PromotionFailure("description refinement must insert before Skip")
    if quote in added or "\\" in added or re.search(r"\bSkip\b", added):
        raise _PromotionFailure("description insertion contains metadata syntax")
    return added


def _validate_evidence_refs(refs):
    if not isinstance(refs, list) or len(refs) < 2 or len(refs) > _MAX_EVIDENCE:
        raise _PromotionFailure("promotion requires two or more evidence references")
    seen = set()
    for ref in refs:
        if not isinstance(ref, dict) or ref.keys() != _REF_FIELDS:
            raise _PromotionFailure("evidence reference has an unsupported shape")
        session = ref.get("session")
        position = ref.get("position")
        digest = ref.get("digest")
        if (
            not isinstance(session, str)
            or not session
            or len(session) > 256
            or type(position) is not int
            or position < 0
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
        ):
            raise _PromotionFailure("evidence reference is invalid")
        key = (session, position)
        if key in seen:
            raise _PromotionFailure("evidence references must be distinct")
        seen.add(key)
    return refs


def _verify_evidence(refs, evidence, verifier):
    if (
        not callable(verifier)
        or not isinstance(evidence, list)
        or len(evidence) != len(refs)
        or len(evidence) > _MAX_EVIDENCE
    ):
        raise _PromotionFailure("verified original evidence is required")
    records = {}
    for record in evidence:
        if not isinstance(record, dict):
            raise _PromotionFailure("evidence record has an unsupported shape")
        session = record.get("session")
        position = record.get("position")
        root_session = record.get("root_session")
        if (
            not isinstance(session, str)
            or type(position) is not int
            or not isinstance(root_session, str)
            or not root_session
            or len(root_session) > 256
        ):
            raise _PromotionFailure("evidence records need session, position, and root_session")
        key = (session, position)
        if key in records:
            raise _PromotionFailure("evidence contains duplicate session positions")
        records[key] = root_session
    root_sessions = set()
    for ref in refs:
        key = (ref["session"], ref["position"])
        if key not in records:
            raise _PromotionFailure("an evidence reference did not resolve to an original record")
        root_sessions.add(records[key])
    if len(root_sessions) < 2:
        raise _PromotionFailure("evidence must come from two independent root sessions")
    try:
        valid = verifier(refs, evidence)
    except Exception as exc:
        raise _PromotionFailure("original evidence verification failed") from exc
    if valid is not True:
        raise _PromotionFailure("original evidence verification failed")


def _ignored_worktree_path(repository, relative):
    ignored = _command(
        [
            "git",
            "-C",
            os.fspath(repository),
            "check-ignore",
            "--verbose",
            "--no-index",
            "-z",
            "--stdin",
        ],
        repository,
        input_bytes=os.fsencode(relative) + b"\0",
        check=False,
    )
    if ignored.returncode != 0:
        raise _PromotionFailure(".work/wt is not ignored by .git/info/exclude")
    fields = ignored.stdout.split(b"\0")
    if len(fields) < 4 or not os.fsdecode(fields[0]).replace("\\", "/").endswith("/info/exclude"):
        raise _PromotionFailure(".work/wt must be ignored by .git/info/exclude")


def _safe_worktree_root(repository):
    relative = Path(".work") / "wt" / (_WORKTREE_PREFIXES[0] + "check")
    _ignored_worktree_path(repository, relative.as_posix())
    root = repository / ".work"
    for path in (root, root / "wt"):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise _PromotionFailure(".work/wt must contain real directories")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise _PromotionFailure(".work/wt must contain real directories") from exc
        if resolved != path:
            raise _PromotionFailure(".work/wt must not resolve through another path")
    return root / "wt"


def _worktree_path_identity(repository, path):
    worktree_root = _safe_worktree_root(repository)
    if path.parent != worktree_root:
        raise _PromotionFailure("promotion worktree path is outside .work/wt", "worktree_occupied")
    identities = []
    for directory in (repository / ".work", worktree_root):
        try:
            metadata = directory.lstat()
        except OSError as exc:
            raise _PromotionFailure("promotion worktree ancestry changed", "worktree_occupied") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise _PromotionFailure("promotion worktree ancestry is not real", "worktree_occupied")
        try:
            resolved = directory.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise _PromotionFailure("promotion worktree ancestry changed", "worktree_occupied") from exc
        if resolved != directory:
            raise _PromotionFailure("promotion worktree ancestry changed", "worktree_occupied")
        identities.append((metadata.st_dev, metadata.st_ino))
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise _PromotionFailure("promotion worktree path is unavailable", "worktree_occupied") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise _PromotionFailure("promotion worktree path is not a real directory", "worktree_occupied")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _PromotionFailure("promotion worktree path changed physical identity", "worktree_occupied") from exc
    if resolved != path:
        raise _PromotionFailure("promotion worktree path changed physical identity", "worktree_occupied")
    identities.append((metadata.st_dev, metadata.st_ino))
    return tuple(identities)


def _registered_worktree(repository, path, alternate_path=None):
    try:
        _worktree_path_identity(repository, path)
        top = Path(_git(path, "rev-parse", "--show-toplevel")).resolve(strict=True)
        git_dir = Path(
            _git(path, "rev-parse", "--path-format=absolute", "--git-dir")
        ).resolve(strict=True)
        common = _common_git_dir(repository)
        managed_root = common / "worktrees"
        managed_stat = managed_root.lstat()
        if (
            stat.S_ISLNK(managed_stat.st_mode)
            or not stat.S_ISDIR(managed_stat.st_mode)
            or managed_root.resolve(strict=True) != managed_root
            or top != path
            or git_dir.parent != managed_root
        ):
            return None
        git_dir_stat = git_dir.lstat()
        if stat.S_ISLNK(git_dir_stat.st_mode) or not stat.S_ISDIR(git_dir_stat.st_mode):
            return None
        metadata_pointer = git_dir / "gitdir"
        worktree_pointer = path / ".git"
        allowed_metadata_pointers = {os.fsencode(path / ".git") + b"\n"}
        if alternate_path is not None:
            allowed_metadata_pointers.add(os.fsencode(alternate_path / ".git") + b"\n")
        metadata_stat = metadata_pointer.lstat()
        worktree_stat = worktree_pointer.lstat()
        if (
            stat.S_ISLNK(metadata_stat.st_mode)
            or not stat.S_ISREG(metadata_stat.st_mode)
            or stat.S_ISLNK(worktree_stat.st_mode)
            or not stat.S_ISREG(worktree_stat.st_mode)
            or metadata_pointer.read_bytes() not in allowed_metadata_pointers
            or worktree_pointer.read_bytes()
            != b"gitdir: " + os.fsencode(git_dir) + b"\n"
        ):
            return None
        return git_dir
    except (OSError, RuntimeError, ValueError, _PromotionFailure):
        return None


def _worktree_owner_record(repository, path, operation, identity):
    values = (
        b"agent-learning-promotion-v1",
        os.fsencode(repository),
        os.fsencode(path),
        operation.encode("ascii"),
        identity.encode("ascii"),
    )
    return b"".join(len(value).to_bytes(8, "big") + value for value in values)


def _owned_worktree(repository, path, operation, identity, git_dir=None):
    git_dir = git_dir or _registered_worktree(repository, path)
    if git_dir is None:
        return False
    expected = _worktree_owner_record(repository, path, operation, identity)
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        directory_descriptor = os.open(os.fspath(git_dir), directory_flags)
    except OSError:
        return False
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(
            "agent-learning-promotion-owner",
            flags,
            dir_fd=directory_descriptor,
        )
    except OSError:
        os.close(directory_descriptor)
        return False
    try:
        stream = os.fdopen(descriptor, "rb")
        metadata = os.fstat(stream.fileno())
        with stream:
            return (
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_nlink == 1
                and stream.read(len(expected) + 1) == expected
            )
    finally:
        os.close(directory_descriptor)


def _write_worktree_owner(repository, path, operation, identity, git_dir):
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_descriptor = os.open(os.fspath(git_dir), directory_flags)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(
            "agent-learning-promotion-owner",
            flags,
            0o600,
            dir_fd=directory_descriptor,
        )
    except OSError:
        os.close(directory_descriptor)
        raise
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_worktree_owner_record(repository, path, operation, identity))
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        try:
            os.unlink("agent-learning-promotion-owner", dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
        os.close(directory_descriptor)
        raise
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _worktree_cleanup_path(path, operation, identity):
    return path.parent / (".agent-learning-cleanup-{}-{}".format(operation, identity))


def _path_entry_exists(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _worktree_parent_descriptor(repository, worktree_root):
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(os.fspath(worktree_root), flags)
    metadata = os.fstat(descriptor)
    expected = worktree_root.lstat()
    if (metadata.st_dev, metadata.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(descriptor)
        raise _PromotionFailure("promotion worktree ancestry changed", "worktree_occupied")
    if worktree_root != _safe_worktree_root(repository):
        os.close(descriptor)
        raise _PromotionFailure("promotion worktree ancestry changed", "worktree_occupied")
    return descriptor


def _worktree_changes(path):
    status = _git_bytes(
        path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        config=(("core.fsmonitor", "false"), ("core.untrackedCache", "false")),
    )
    ignored = _git_bytes(
        path,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "-z",
        config=(("core.fsmonitor", "false"), ("core.untrackedCache", "false")),
    )
    return status, ignored


def _tracked_worktree_entries(worktree, head):
    output = _git_bytes(worktree, "ls-tree", "-r", "-z", "--full-tree", head)
    files = []
    directories = set()
    for record in output.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise _PromotionFailure("tracked worktree tree is malformed", "worktree_occupied", worktree)
        mode, kind, raw_oid = fields
        relative = Path(os.fsdecode(raw_path))
        if relative.is_absolute() or not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
            raise _PromotionFailure("tracked worktree path is unsafe", "worktree_occupied", worktree)
        for count in range(1, len(relative.parts)):
            directories.add(Path(*relative.parts[:count]))
        if mode == b"160000" and kind == b"commit":
            directories.add(relative)
            continue
        if mode not in (b"100644", b"100755", b"120000") or kind != b"blob":
            raise _PromotionFailure("tracked worktree entry has an unsupported type", "worktree_occupied", worktree)
        oid = raw_oid.decode("ascii")
        if not _HEX.fullmatch(oid):
            raise _PromotionFailure("tracked worktree object ID is invalid", "worktree_occupied", worktree)
        files.append((relative, mode.decode("ascii"), oid))
    return files, directories


def _relative_parent_descriptor(root_descriptor, relative):
    if relative.is_absolute() or not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise _PromotionFailure("tracked worktree path is unsafe", "worktree_occupied")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.dup(root_descriptor)
    try:
        for component in relative.parts[:-1]:
            child = os.open(component, flags, dir_fd=descriptor)
            metadata = os.fstat(child)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(child)
                raise _PromotionFailure("tracked worktree ancestry is not a real directory", "worktree_occupied")
            os.close(descriptor)
            descriptor = child
        return descriptor, relative.parts[-1]
    except Exception:
        os.close(descriptor)
        raise


def _tracked_file_signature(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _tracked_file_snapshot(root_descriptor, relative, mode, expected_oid):
    parent_descriptor, name = _relative_parent_descriptor(root_descriptor, relative)
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if mode == "120000":
            if not stat.S_ISLNK(metadata.st_mode):
                raise _PromotionFailure("tracked symlink changed type", "worktree_occupied")
            contents = os.fsencode(os.readlink(name, dir_fd=parent_descriptor))
            hasher = hashlib.new("sha1" if len(expected_oid) == 40 else "sha256")
            hasher.update(b"blob " + str(len(contents)).encode("ascii") + b"\0")
            hasher.update(contents)
            if hasher.hexdigest() != expected_oid:
                raise _PromotionFailure("tracked symlink changed during cleanup", "worktree_occupied")
            return _tracked_file_signature(metadata)
        if not stat.S_ISREG(metadata.st_mode):
            raise _PromotionFailure("tracked file changed type", "worktree_occupied")
        if bool(metadata.st_mode & 0o111) != (mode == "100755"):
            raise _PromotionFailure("tracked file mode changed", "worktree_occupied")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        try:
            before = os.fstat(descriptor)
            if _tracked_file_signature(before) != _tracked_file_signature(metadata):
                raise _PromotionFailure("tracked file changed during cleanup", "worktree_occupied")
            hasher = hashlib.new("sha1" if len(expected_oid) == 40 else "sha256")
            hasher.update(b"blob " + str(before.st_size).encode("ascii") + b"\0")
            while True:
                contents = os.read(descriptor, 1024 * 1024)
                if not contents:
                    break
                hasher.update(contents)
            after = os.fstat(descriptor)
            if _tracked_file_signature(before) != _tracked_file_signature(after):
                raise _PromotionFailure("tracked file changed during cleanup", "worktree_occupied")
            if hasher.hexdigest() != expected_oid:
                raise _PromotionFailure("tracked file differs from its commit", "worktree_occupied")
            return _tracked_file_signature(after)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def _unlink_tracked_worktree_file(root_descriptor, relative, mode, expected_oid, expected_signature):
    current_signature = _tracked_file_snapshot(root_descriptor, relative, mode, expected_oid)
    if current_signature != expected_signature:
        raise _PromotionFailure("tracked file changed during cleanup", "worktree_occupied")
    parent_descriptor, name = _relative_parent_descriptor(root_descriptor, relative)
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _tracked_file_signature(metadata) != expected_signature:
            raise _PromotionFailure("tracked file path changed during cleanup", "worktree_occupied")
        os.unlink(name, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _remove_empty_tracked_directory(root_descriptor, relative):
    parent_descriptor, name = _relative_parent_descriptor(root_descriptor, relative)
    try:
        os.rmdir(name, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _unlink_worktree_git_pointer(root_descriptor, git_dir):
    signature, contents = _read_private_file(root_descriptor, ".git")
    if contents != b"gitdir: " + os.fsencode(git_dir) + b"\n":
        raise _PromotionFailure("promotion worktree Git pointer changed", "worktree_occupied")
    _unlink_private_file(root_descriptor, ".git", signature)


def _restore_worktree_git_pointer(root_descriptor, git_dir):
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(".git", flags, 0o600, dir_fd=root_descriptor)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(b"gitdir: " + os.fsencode(git_dir) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _prune_tracked_worktree(root_descriptor, worktree, head):
    entries, directories = _tracked_worktree_entries(worktree, head)
    snapshots = []
    try:
        for relative, mode, oid in entries:
            snapshots.append((relative, mode, oid, _tracked_file_snapshot(root_descriptor, relative, mode, oid)))
    except _PromotionFailure as exc:
        raise _PromotionFailure(str(exc), "worktree_occupied", worktree) from exc
    for relative, mode, oid, signature in snapshots:
        try:
            _unlink_tracked_worktree_file(root_descriptor, relative, mode, oid, signature)
        except _PromotionFailure as exc:
            raise _PromotionFailure(str(exc), "worktree_occupied", worktree) from exc
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        try:
            _remove_empty_tracked_directory(root_descriptor, directory)
        except OSError as exc:
            raise _PromotionFailure(
                "unknown data prevents promotion worktree cleanup",
                "worktree_occupied",
                worktree,
            ) from exc


def _read_private_file(directory_descriptor, name):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise _PromotionFailure("owned Git metadata changed type", "worktree_occupied")
        chunks = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return _tracked_file_signature(metadata), b"".join(chunks)
    finally:
        os.close(descriptor)


def _open_verified_directory(path):
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or path.resolve(strict=True) != path:
        raise _PromotionFailure("owned directory changed physical identity", "worktree_occupied", path)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(os.fspath(path), flags)
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
        os.close(descriptor)
        raise _PromotionFailure("owned directory changed during cleanup", "worktree_occupied", path)
    return descriptor


def _unlink_private_file(directory_descriptor, name, expected_signature):
    metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if _tracked_file_signature(metadata) != expected_signature:
        raise _PromotionFailure("owned Git metadata changed during cleanup", "worktree_occupied")
    os.unlink(name, dir_fd=directory_descriptor)


def _remove_owned_worktree_metadata(
    repository,
    git_dir,
    owner_path,
    registered_path,
    operation,
    identity,
    head,
):
    if not _owned_worktree(repository, owner_path, operation, identity, git_dir):
        raise _PromotionFailure("owned Git metadata could not be verified", "worktree_occupied", git_dir)
    managed_root = git_dir.parent
    managed_descriptor = _open_verified_directory(managed_root)
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    try:
        directory_descriptor = os.open(git_dir.name, directory_flags, dir_fd=managed_descriptor)
    except OSError as exc:
        os.close(managed_descriptor)
        raise _PromotionFailure("owned Git metadata path changed", "worktree_occupied", git_dir) from exc
    try:
        metadata = os.fstat(directory_descriptor)
        if (metadata.st_dev, metadata.st_ino) != (git_dir.lstat().st_dev, git_dir.lstat().st_ino):
            raise _PromotionFailure("owned Git metadata path changed", "worktree_occupied", git_dir)
        names = set(os.listdir(directory_descriptor))
        known = {
            "COMMIT_EDITMSG",
            "HEAD",
            "ORIG_HEAD",
            "commondir",
            "gitdir",
            "index",
            "logs",
            "refs",
            "agent-learning-promotion-owner",
        }
        if "agent-learning-promotion-owner" not in names or names - known:
            raise _PromotionFailure("unknown data occupies owned Git metadata", "worktree_occupied", git_dir)
        expected_marker = _worktree_owner_record(repository, owner_path, operation, identity)
        marker_signature, marker_contents = _read_private_file(
            directory_descriptor, "agent-learning-promotion-owner"
        )
        if marker_contents != expected_marker:
            raise _PromotionFailure("owned Git metadata marker changed", "worktree_occupied", git_dir)
        file_records = {}
        for name in names - {"logs", "refs", "agent-learning-promotion-owner"}:
            signature, contents = _read_private_file(directory_descriptor, name)
            file_records[name] = signature
            if name == "HEAD" and contents.strip() != head.encode("ascii"):
                raise _PromotionFailure("owned Git HEAD metadata changed", "worktree_occupied", git_dir)
            if name == "commondir":
                expected_common = os.path.relpath(_common_git_dir(repository), git_dir).encode("utf-8")
                if contents.strip() != expected_common:
                    raise _PromotionFailure("owned Git common directory changed", "worktree_occupied", git_dir)
            if name == "gitdir" and contents != os.fsencode(registered_path / ".git") + b"\n":
                raise _PromotionFailure("owned Git worktree pointer changed", "worktree_occupied", git_dir)
            if name == "ORIG_HEAD" and not _HEX.fullmatch(contents.strip().decode("ascii", "ignore")):
                raise _PromotionFailure("owned Git original HEAD metadata changed", "worktree_occupied", git_dir)
        for directory in ("logs", "refs"):
            if directory not in names:
                continue
            entry = os.stat(directory, dir_fd=directory_descriptor, follow_symlinks=False)
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
                raise _PromotionFailure("owned Git metadata directory changed type", "worktree_occupied", git_dir)
            child_flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                child_flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                child_flags |= os.O_NOFOLLOW
            child_descriptor = os.open(directory, child_flags, dir_fd=directory_descriptor)
            try:
                children = set(os.listdir(child_descriptor))
                if directory == "refs" and children:
                    raise _PromotionFailure("unknown refs occupy owned Git metadata", "worktree_occupied", git_dir)
                if directory == "logs" and children - {"HEAD"}:
                    raise _PromotionFailure("unknown logs occupy owned Git metadata", "worktree_occupied", git_dir)
                if directory == "logs" and "HEAD" in children:
                    signature, _contents = _read_private_file(child_descriptor, "HEAD")
                    file_records["logs/HEAD"] = signature
            finally:
                os.close(child_descriptor)
        if "logs" in names:
            child_descriptor = os.open("logs", directory_flags, dir_fd=directory_descriptor)
            try:
                if "HEAD" in os.listdir(child_descriptor):
                    _unlink_private_file(child_descriptor, "HEAD", file_records["logs/HEAD"])
            finally:
                os.close(child_descriptor)
            os.rmdir("logs", dir_fd=directory_descriptor)
        if "refs" in names:
            os.rmdir("refs", dir_fd=directory_descriptor)
        for name, signature in file_records.items():
            if "/" not in name:
                _unlink_private_file(directory_descriptor, name, signature)
        _unlink_private_file(directory_descriptor, "agent-learning-promotion-owner", marker_signature)
    except OSError as exc:
        os.close(managed_descriptor)
        raise _PromotionFailure("owned Git metadata cleanup was blocked", "worktree_occupied", git_dir) from exc
    except Exception:
        os.close(managed_descriptor)
        raise
    finally:
        os.close(directory_descriptor)
    try:
        os.rmdir(git_dir.name, dir_fd=managed_descriptor)
    except OSError as exc:
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            directory_descriptor = os.open(git_dir.name, directory_flags, dir_fd=managed_descriptor)
            try:
                descriptor = os.open(
                    "agent-learning-promotion-owner",
                    flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(_worktree_owner_record(repository, owner_path, operation, identity))
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(directory_descriptor)
        except OSError:
            pass
        raise _PromotionFailure("owned Git metadata directory is not empty", "worktree_occupied", git_dir) from exc
    finally:
        os.close(managed_descriptor)


def _worktree_parent(worktree, expected_head):
    head = _head(worktree)
    if head == expected_head:
        return head
    parents = _git(worktree, "rev-list", "--parents", "-n", "1", head).split()
    if parents != [head, expected_head]:
        raise _PromotionFailure(
            "owned promotion worktree has an unexpected commit",
            "worktree_occupied",
            worktree,
        )
    return head


def _restore_cleanup_worktree(repository, path, cleanup_path, operation, identity, expected_head):
    if _path_entry_exists(path) and _path_entry_exists(cleanup_path):
        raise _PromotionFailure("promotion and cleanup paths are both occupied", "worktree_occupied", cleanup_path)
    physical_path = cleanup_path if _path_entry_exists(cleanup_path) else path
    alternate_path = path if physical_path == cleanup_path else cleanup_path
    identity_before = _worktree_path_identity(repository, physical_path)
    git_dir = _registered_worktree(repository, physical_path, alternate_path)
    if git_dir is None or not _owned_worktree(repository, path, operation, identity, git_dir):
        raise _PromotionFailure("unowned promotion cleanup path requires recovery", "worktree_occupied", physical_path)
    status, ignored = _worktree_changes(physical_path)
    if status or ignored:
        raise _PromotionFailure("promotion cleanup worktree contains unreviewed changes", "worktree_occupied", physical_path)
    _worktree_parent(physical_path, expected_head)
    if _worktree_path_identity(repository, physical_path) != identity_before:
        raise _PromotionFailure("promotion cleanup path changed physical identity", "worktree_occupied", physical_path)
    _git(repository, "worktree", "repair", os.fspath(physical_path))
    if _registered_worktree(repository, physical_path) != git_dir:
        raise _PromotionFailure("promotion cleanup registration could not be repaired", "worktree_occupied", physical_path)
    if physical_path == path:
        return
    parent_descriptor = _worktree_parent_descriptor(repository, path.parent)
    try:
        if _path_entry_exists(path):
            raise _PromotionFailure("promotion worktree path became occupied", "worktree_occupied", path)
        os.rename(
            cleanup_path.name,
            path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
    finally:
        os.close(parent_descriptor)
    _git(repository, "worktree", "repair", os.fspath(path))
    if (
        _registered_worktree(repository, path) != git_dir
        or not _owned_worktree(repository, path, operation, identity, git_dir)
    ):
        raise _PromotionFailure("promotion worktree restoration could not be verified", "worktree_occupied", path)


def _restore_worktree(repository, path, cleanup_path, parent_descriptor):
    if _path_entry_exists(path):
        return False
    os.rename(
        cleanup_path.name,
        path.name,
        src_dir_fd=parent_descriptor,
        dst_dir_fd=parent_descriptor,
    )
    _git(repository, "worktree", "repair", os.fspath(path))
    return True


def _remove_worktree(repository, path, operation, identity):
    try:
        path.lstat()
    except FileNotFoundError:
        return
    identity_before = _worktree_path_identity(repository, path)
    git_dir = _registered_worktree(repository, path)
    if git_dir is None or not _owned_worktree(repository, path, operation, identity, git_dir):
        raise _PromotionFailure(
            "unowned data occupies a promotion worktree path",
            "worktree_occupied",
            path,
        )
    cleanup_path = _worktree_cleanup_path(path, operation, identity)
    if _path_entry_exists(cleanup_path):
        raise _PromotionFailure("promotion cleanup path is occupied", "worktree_occupied", cleanup_path)
    worktree_root = path.parent
    parent_descriptor = _worktree_parent_descriptor(repository, worktree_root)
    pruning_started = False
    try:
        try:
            status, ignored = _worktree_changes(path)
        except _PromotionFailure as exc:
            raise _PromotionFailure(
                "could not verify promotion worktree cleanup state",
                "worktree_occupied",
                path,
            ) from exc
        if status or ignored:
            reason = "promotion worktree contains ignored files" if ignored else "promotion worktree contains local or staged changes"
            raise _PromotionFailure(reason, "worktree_occupied", path)
        if _worktree_path_identity(repository, path) != identity_before:
            raise _PromotionFailure("promotion worktree path changed during cleanup", "worktree_occupied", path)
        if not _owned_worktree(repository, path, operation, identity, git_dir):
            raise _PromotionFailure("promotion worktree ownership changed during cleanup", "worktree_occupied", path)
        os.rename(
            path.name,
            cleanup_path.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        cleanup_identity = _worktree_path_identity(repository, cleanup_path)
        if cleanup_identity != identity_before:
            raise _PromotionFailure(
                "promotion worktree identity changed during cleanup",
                "worktree_occupied",
                cleanup_path,
            )
        try:
            _git(repository, "worktree", "repair", os.fspath(cleanup_path))
            repaired_git_dir = _registered_worktree(repository, cleanup_path)
            if (
                repaired_git_dir != git_dir
                or not _owned_worktree(repository, path, operation, identity, repaired_git_dir)
            ):
                raise _PromotionFailure("promotion worktree ownership changed during cleanup", "worktree_occupied", cleanup_path)
            status, ignored = _worktree_changes(cleanup_path)
            if status or ignored:
                reason = "promotion worktree contains ignored files" if ignored else "promotion worktree contains local or staged changes"
                raise _PromotionFailure(reason, "worktree_occupied", cleanup_path)
            if _worktree_path_identity(repository, cleanup_path) != identity_before:
                raise _PromotionFailure("promotion worktree changed during cleanup", "worktree_occupied", cleanup_path)
        except _PromotionFailure as exc:
            if _path_entry_exists(cleanup_path):
                try:
                    _restore_worktree(repository, path, cleanup_path, parent_descriptor)
                except (OSError, _PromotionFailure):
                    pass
            raise _PromotionFailure(
                "promotion worktree cleanup could not be verified",
                "worktree_occupied",
                cleanup_path,
            ) from exc
        worktree_head = _head(cleanup_path)
        root_descriptor = _open_verified_directory(cleanup_path)
        try:
            root_metadata = os.fstat(root_descriptor)
            if (root_metadata.st_dev, root_metadata.st_ino) != identity_before[-1]:
                raise _PromotionFailure("promotion worktree changed during cleanup", "worktree_occupied", cleanup_path)
            pruning_started = True
            _prune_tracked_worktree(root_descriptor, cleanup_path, worktree_head)
            _unlink_worktree_git_pointer(root_descriptor, git_dir)
            try:
                os.rmdir(cleanup_path.name, dir_fd=parent_descriptor)
            except OSError as exc:
                try:
                    _restore_worktree_git_pointer(root_descriptor, git_dir)
                except OSError:
                    pass
                raise _PromotionFailure(
                    "unknown data prevents promotion worktree cleanup",
                    "worktree_occupied",
                    cleanup_path,
                ) from exc
        finally:
            os.close(root_descriptor)
        if _path_entry_exists(cleanup_path):
            raise _PromotionFailure("a path was created during promotion worktree cleanup", "worktree_occupied", cleanup_path)
        if _path_entry_exists(path):
            raise _PromotionFailure("a path was created during promotion worktree cleanup", "worktree_occupied", path)
        _remove_owned_worktree_metadata(
            repository,
            git_dir,
            path,
            cleanup_path,
            operation,
            identity,
            worktree_head,
        )
    except _PromotionFailure:
        if not pruning_started and _path_entry_exists(cleanup_path) and not _path_entry_exists(path):
            try:
                _restore_worktree(repository, path, cleanup_path, parent_descriptor)
            except (OSError, _PromotionFailure):
                pass
        raise
    except OSError as exc:
        raise _PromotionFailure(
            "promotion worktree cleanup was blocked",
            "worktree_occupied",
            cleanup_path if _path_entry_exists(cleanup_path) else git_dir,
        ) from exc
    finally:
        os.close(parent_descriptor)


def _staging_worktree(repository, expected_head, worktree_root, operation, identity):
    prefix = "agent-learning-{}-".format(operation)
    if operation not in ("promote", "rollback"):
        raise _PromotionFailure("unsupported staging operation")
    if worktree_root != _safe_worktree_root(repository):
        raise _PromotionFailure("promotion worktree root changed", "worktree_occupied")
    path = worktree_root / (prefix + identity)
    cleanup_path = _worktree_cleanup_path(path, operation, identity)
    if _path_entry_exists(path) or _path_entry_exists(cleanup_path):
        _restore_cleanup_worktree(repository, path, cleanup_path, operation, identity, expected_head)
        git_dir = _registered_worktree(repository, path)
        if git_dir is None or not _owned_worktree(repository, path, operation, identity, git_dir):
            raise _PromotionFailure("unowned data occupies a promotion worktree path", "worktree_occupied", path)
        existing_head = _worktree_parent(path, expected_head)
        if existing_head != expected_head:
            return path
        _remove_worktree(repository, path, operation, identity)
    try:
        _git(
            repository,
            "worktree",
            "add",
            "--detach",
            os.fspath(path),
            expected_head,
            config=(("core.hooksPath", "/dev/null"),),
        )
    except _PromotionFailure as exc:
        if path.exists() or path.is_symlink():
            _worktree_path_identity(repository, path)
            raise _PromotionFailure(
                "promotion worktree path became occupied during creation",
                "worktree_occupied",
                path,
            ) from exc
        raise
    git_dir = _registered_worktree(repository, path)
    if git_dir is None:
        raise _PromotionFailure(
            "Git did not register the promotion worktree",
            "worktree_occupied",
            path,
        )
    try:
        _write_worktree_owner(repository, path, operation, identity, git_dir)
    except OSError as exc:
        raise _PromotionFailure(
            "could not record promotion worktree ownership",
            "worktree_occupied",
            path,
        ) from exc
    return path


def _skill_file(worktree, skill):
    relative = Path("dotfiles/base/.agents/skills") / skill / "SKILL.md"
    current = worktree
    mode = 0
    for component in relative.parts:
        current = current / component
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise _PromotionFailure("target skill file is missing") from exc
        if stat.S_ISLNK(mode):
            raise _PromotionFailure("target skill path contains a symlink")
    if not stat.S_ISREG(mode):
        raise _PromotionFailure("target skill file is not regular")
    return relative, current


def _edit_is_paragraph(text, old):
    if text.count(old) != 1:
        raise _PromotionFailure("old guidance must match exactly once")
    start = text.index(old)
    end = start + len(old)
    if (start and text[max(0, start - 2) : start] != "\n\n") or (
        end < len(text) and not text[end:].startswith("\n\n")
    ):
        raise _PromotionFailure("edit must select a complete paragraph")
    if "\n\n" in old or "\r" in old or "\x00" in old:
        raise _PromotionFailure("edit paragraph has unsupported boundaries")
    for line in old.splitlines():
        if line.startswith(("#", "- ", "* ", ">", "|")) or re.match(r"^\d+\. ", line):
            raise _PromotionFailure("headings, lists, and tables are protected")
    if text.startswith("---\n"):
        frontmatter_end = text.find("\n---\n", 4)
        if frontmatter_end < 0 or start < frontmatter_end + 5:
            raise _PromotionFailure("frontmatter is protected")
    return start


def _edit_is_description(text, old):
    if not text.startswith("---\n"):
        raise _PromotionFailure("description frontmatter is missing")
    frontmatter_end = text.find("\n---\n", 4)
    if frontmatter_end < 0:
        raise _PromotionFailure("description frontmatter is malformed")
    end = frontmatter_end + 1
    frontmatter = text[:end]
    if frontmatter.count(old) != 1:
        raise _PromotionFailure("old description must match exactly once in frontmatter")
    start = frontmatter.index(old)
    if (start and text[start - 1] != "\n") or (start + len(old) < end and text[start + len(old)] != "\n"):
        raise _PromotionFailure("description edit must select the complete metadata line")
    return start


def _replace_atomically(path, contents):
    original_stat = path.lstat()
    if stat.S_ISLNK(original_stat.st_mode) or not stat.S_ISREG(original_stat.st_mode):
        raise _PromotionFailure("target skill file changed type")
    descriptor, temporary = tempfile.mkstemp(prefix=".agent-learning-", dir=os.fspath(path.parent))
    try:
        os.fchmod(descriptor, stat.S_IMODE(original_stat.st_mode))
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _run_skill_check(worktree):
    checker = worktree / "dotfiles/skills-check.sh"
    skills = worktree / "dotfiles/base/.agents/skills"
    try:
        checker_mode = checker.lstat().st_mode
        skills_mode = skills.lstat().st_mode
        mdtool_mode = (worktree / "dotfiles/base/.local/bin/mdtool").lstat().st_mode
        if (
            stat.S_ISLNK(checker_mode)
            or not stat.S_ISREG(checker_mode)
            or stat.S_ISLNK(skills_mode)
            or not stat.S_ISDIR(skills_mode)
            or stat.S_ISLNK(mdtool_mode)
            or not stat.S_ISREG(mdtool_mode)
        ):
            raise _PromotionFailure("trusted skill checker paths are not regular project files", "validation_failed")
    except OSError as exc:
        raise _PromotionFailure("trusted skill checker is unavailable", "validation_failed") from exc
    result = _command(
        ["sh", os.fspath(checker), os.fspath(skills)],
        worktree,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise _PromotionFailure(
            "trusted skill checker rejected the candidate"
            + (": " + message[:240] if message else ""),
            "validation_failed",
        )


def _edit_digest(old, new):
    return hashlib.sha256((old + "\0" + new).encode("utf-8")).hexdigest()


def _commit_message(repository, commit):
    return _git(repository, "show", "-s", "--format=%B", commit)


def _promotion_commit_for(repository, proposal_id, edit_digest, skill, new):
    search = "Learning-Proposal: " + proposal_id
    output = _git(
        repository,
        "log",
        "HEAD",
        "--format=%H%x00%B",
        "--fixed-strings",
        "--grep=" + search,
        check=True,
    )
    pieces = output.split("\0")
    matches = []
    for index in range(0, len(pieces) - 1, 2):
        commit = pieces[index].strip()
        message = pieces[index + 1]
        if any(line.strip() == search for line in message.splitlines()):
            matches.append((commit, message))
    if len(matches) > 1:
        raise _PromotionFailure("proposal provenance is ambiguous")
    if not matches:
        return None
    commit, message = matches[0]
    if "Learning-Edit: " + edit_digest not in message.splitlines():
        raise _PromotionFailure("proposal ID already belongs to a different edit")
    parents = _git(repository, "rev-list", "--parents", "-n", "1", commit).split()
    if len(parents) != 2:
        raise _PromotionFailure("promotion provenance is not a single-parent commit")
    changed = _git_bytes(
        repository,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "-z",
        commit,
    ).split(b"\0")
    paths = [item.decode("utf-8", "surrogateescape") for item in changed if item]
    expected_path = "dotfiles/base/.agents/skills/{}/SKILL.md".format(skill)
    if paths != [expected_path]:
        raise _PromotionFailure("proposal provenance does not match its skill target")
    _relative, current_path = _skill_file(repository, skill)
    current = current_path.read_text(encoding="utf-8")
    if new not in current:
        raise _PromotionFailure("promotion guidance is no longer present at HEAD")
    rollback_search = "Reverts-Learning-Promotion: " + commit
    reverted = _git(
        repository,
        "log",
        "HEAD",
        "--format=%H%x00%B",
        "--fixed-strings",
        "--grep=" + rollback_search,
        check=True,
    )
    if any(line.strip() == rollback_search for line in reverted.splitlines()):
        raise _PromotionFailure("proposal has already been rolled back")
    return commit


def _commit_candidate(worktree, message, trailers):
    config = (
        ("core.hooksPath", "/dev/null"),
        ("core.fsmonitor", "false"),
        ("commit.gpgsign", "false"),
        ("user.name", "Learning Promotion"),
        ("user.email", "agent-learning-promotion@localhost"),
    )
    _git(worktree, "add", "--", message[1], config=config)
    staged = _git_bytes(worktree, "diff", "--cached", "--name-only", "-z", config=config)
    paths = [item.decode("utf-8", "surrogateescape") for item in staged.split(b"\0") if item]
    if paths != [message[1]]:
        raise _PromotionFailure("candidate index contains an unexpected path")
    if _git_bytes(worktree, "diff", "--name-only", "-z", config=config):
        raise _PromotionFailure("candidate has unstaged changes")
    args = ["commit", "--no-gpg-sign", "-m", message[0]]
    for trailer in trailers:
        args.extend(("-m", trailer))
    _git(worktree, *args, config=config)
    return _head(worktree)


def _recovered_promotion_candidate(worktree, expected_head, proposal_id, skill, old, new, edit_mode):
    candidate = _head(worktree)
    parents = _git(worktree, "rev-list", "--parents", "-n", "1", candidate).split()
    edit_digest = _edit_digest(old, new)
    message = _commit_message(worktree, candidate).splitlines()
    expected_path = "dotfiles/base/.agents/skills/{}/SKILL.md".format(skill)
    changed = _git_bytes(
        worktree,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "-z",
        candidate,
    ).split(b"\0")
    paths = [item.decode("utf-8", "surrogateescape") for item in changed if item]
    expected_message = [
        "docs(skills): improve skill guidance",
        "",
        "Learning-Proposal: " + proposal_id,
        "",
        "Learning-Edit: " + edit_digest,
    ]
    if (
        parents != [candidate, expected_head]
        or message != expected_message
        or paths != [expected_path]
    ):
        raise _PromotionFailure(
            "owned candidate commit does not match this proposal",
            "worktree_occupied",
            worktree,
        )
    relative, candidate_path = _skill_file(worktree, skill)
    try:
        original = _git_bytes(worktree, "show", "{}:{}".format(expected_head, relative.as_posix()))
        if len(original) > _MAX_SKILL_FILE:
            raise _PromotionFailure("skill file exceeds the promotion size limit")
        text = original.decode("utf-8")
        if edit_mode == "description":
            start = _edit_is_description(text, old)
        else:
            start = _edit_is_paragraph(text, old)
        expected = (text[:start] + new + text[start + len(old) :]).encode("utf-8")
        if len(expected) > _MAX_SKILL_FILE:
            raise _PromotionFailure("candidate skill file exceeds the size limit")
        actual = candidate_path.read_bytes()
    except (OSError, UnicodeError, _PromotionFailure) as exc:
        raise _PromotionFailure(
            "owned candidate could not be matched to this proposal",
            "worktree_occupied",
            worktree,
        ) from exc
    if actual != expected:
        raise _PromotionFailure(
            "owned candidate content does not match this proposal",
            "worktree_occupied",
            worktree,
        )
    try:
        _run_skill_check(worktree)
    except _PromotionFailure as exc:
        raise _PromotionFailure(str(exc), "validation_failed", worktree) from exc
    return candidate


def _changed_lines(repository, before, after, target):
    output = _git_bytes(
        repository,
        "diff",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        before,
        after,
        "--",
        target,
    )
    lines = output.split(b"\n")
    added = sorted(line[1:] for line in lines if line.startswith(b"+") and not line.startswith(b"+++"))
    removed = sorted(line[1:] for line in lines if line.startswith(b"-") and not line.startswith(b"---"))
    return added, removed


def _inverse_patch(repository, commit, target):
    return _git_bytes(
        repository,
        "diff",
        "-U1",
        "--no-color",
        "--no-ext-diff",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        commit + "^",
        commit,
        "--",
        target,
    )


def _recovered_rollback_candidate(worktree, expected_head, promotion_commit, target):
    candidate = _head(worktree)
    parents = _git(worktree, "rev-list", "--parents", "-n", "1", candidate).split()
    message = _commit_message(worktree, candidate).splitlines()
    changed = _git_bytes(
        worktree,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "-z",
        candidate,
    ).split(b"\0")
    paths = [item.decode("utf-8", "surrogateescape") for item in changed if item]
    trailer = "Reverts-Learning-Promotion: " + promotion_commit
    expected_message = [
        "revert(skills): restore prior skill guidance",
        "",
        trailer,
    ]
    if (
        parents != [candidate, expected_head]
        or message != expected_message
        or paths != [target]
    ):
        raise _PromotionFailure(
            "owned rollback commit does not match this promotion",
            "worktree_occupied",
            worktree,
        )
    try:
        if expected_head == promotion_commit:
            expected = _git_bytes(worktree, "show", "{}^:{}".format(promotion_commit, target))
            actual = (worktree / target).read_bytes()
            matches = actual == expected
        else:
            added, removed = _changed_lines(worktree, promotion_commit + "^", promotion_commit, target)
            matches = _changed_lines(worktree, expected_head, candidate, target) == (removed, added)
    except (OSError, _PromotionFailure) as exc:
        raise _PromotionFailure(
            "owned rollback content could not be verified",
            "worktree_occupied",
            worktree,
        ) from exc
    if not matches:
        raise _PromotionFailure(
            "owned rollback content does not match this promotion",
            "worktree_occupied",
            worktree,
        )
    try:
        _run_skill_check(worktree)
    except _PromotionFailure as exc:
        raise _PromotionFailure(str(exc), "validation_failed", worktree) from exc
    return candidate


def _integrate(repository, candidate_commit, expected_head, branch):
    if _head(repository) != expected_head:
        raise _PromotionFailure("repository HEAD changed before integration", "repository_changed")
    if _current_branch(repository) != branch:
        raise _PromotionFailure("repository branch changed before integration", "repository_changed")
    _worktree_clean(repository)
    try:
        _git(
            repository,
            "merge",
            "--ff-only",
            "--no-edit",
            candidate_commit,
            config=(("core.hooksPath", "/dev/null"),),
        )
    except _PromotionFailure as exc:
        raise _PromotionFailure("repository changed during integration", "repository_changed") from exc
    if _head(repository) != candidate_commit:
        raise _PromotionFailure("fast-forward integration did not reach the candidate commit", "repository_changed")


def recover_promotion(repository: Path, proposal: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(proposal, dict):
        return _blocked("proposal has an unsupported shape", "invalid_proposal")
    if proposal.get("action") != "improve":
        return _blocked("proposal action is unsupported", "invalid_proposal")
    try:
        try:
            proposal_id, skill, old, new, _added, _edit_mode, _refs = _validate_proposal(proposal)
        except _PromotionFailure as exc:
            return _blocked(str(exc), "invalid_proposal")
        root = _repository_root(repository)
        with _promotion_lock(root):
            existing = _promotion_commit_for(
                root,
                proposal_id,
                _edit_digest(old, new),
                skill,
                new,
            )
            if existing is None:
                return _result("absent")
            return _result("promoted", existing, "proposal was already integrated")
    except _PromotionFailure as exc:
        return _blocked(str(exc), exc.reason_code, exc.blocked_path)
    except (OSError, ValueError, UnicodeError) as exc:
        return _blocked("promotion recovery failed: {}".format(type(exc).__name__))


def promote(
    repository: Path,
    proposal: dict[str, Any],
    evidence: list[Any],
    expected_head: str,
    *,
    verify_evidence: Callable[[list[dict[str, Any]], list[dict[str, Any]]], bool],
) -> dict[str, Any]:
    if not isinstance(proposal, dict):
        return _blocked("proposal has an unsupported shape", "invalid_proposal")
    action = proposal.get("action")
    if proposal.get("target") == "memory":
        return _result("not_applicable", reason="memory targets use the parent memory path")
    if action in ("defer", "reject", "supersede"):
        return _result("not_applicable", reason="proposal action is not improve")
    if action != "improve":
        return _blocked("proposal action is unsupported", "invalid_proposal")
    try:
        try:
            proposal_id, skill, old, new, _added, edit_mode, refs = _validate_proposal(proposal)
            _verify_evidence(refs, evidence, verify_evidence)
        except _PromotionFailure as exc:
            return _blocked(str(exc), "invalid_proposal")
        root = _repository_root(repository)
        with _promotion_lock(root):
            _worktree_clean(root)
            _current_branch(root)
            existing = _promotion_commit_for(
                root,
                proposal_id,
                _edit_digest(old, new),
                skill,
                new,
            )
            if existing:
                return _result("promoted", existing, "proposal was already integrated")
            head = _expected_head(root, expected_head)
            branch = _current_branch(root)
            worktree_root = _safe_worktree_root(root)
            worktree = _staging_worktree(
                root, head, worktree_root, "promote", proposal_id
            )
            integrated_commit = None
            cleanup_failure = None
            primary_failure = None
            cleanup_allowed = False
            try:
                recovered_candidate = _head(worktree) != head
                cleanup_allowed = not recovered_candidate
                if recovered_candidate:
                    candidate_commit = _recovered_promotion_candidate(
                        worktree,
                        head,
                        proposal_id,
                        skill,
                        old,
                        new,
                        edit_mode,
                    )
                else:
                    relative, path = _skill_file(worktree, skill)
                    original = path.read_bytes()
                    if len(original) > _MAX_SKILL_FILE:
                        raise _PromotionFailure("skill file exceeds the promotion size limit")
                    try:
                        text = original.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise _PromotionFailure("skill file is not UTF-8") from exc
                    if edit_mode == "description":
                        start = _edit_is_description(text, old)
                    else:
                        if " ".join(proposal["future_action"].split()).lower() in " ".join(text.split()).lower():
                            raise _PromotionFailure("owning skill already represents the proposed action")
                        start = _edit_is_paragraph(text, old)
                    updated = text[:start] + new + text[start + len(old) :]
                    encoded = updated.encode("utf-8")
                    if len(encoded) > _MAX_SKILL_FILE:
                        raise _PromotionFailure("candidate skill file exceeds the size limit")
                    _replace_atomically(path, encoded)
                    _run_skill_check(worktree)
                    edit_digest = _edit_digest(old, new)
                    cleanup_allowed = False
                    candidate_commit = _commit_candidate(
                        worktree,
                        ("docs(skills): improve skill guidance", relative.as_posix()),
                        (
                            "Learning-Proposal: " + proposal_id,
                            "Learning-Edit: " + edit_digest,
                        ),
                    )
                parents = _git(worktree, "rev-list", "--parents", "-n", "1", candidate_commit).split()
                if parents != [candidate_commit, head]:
                    raise _PromotionFailure("candidate commit does not directly follow expected_head")
                _verify_evidence(refs, evidence, verify_evidence)
                _integrate(root, candidate_commit, head, branch)
                integrated_commit = candidate_commit
                cleanup_allowed = True
            except Exception as exc:
                primary_failure = exc
                raise
            finally:
                if cleanup_allowed:
                    try:
                        _remove_worktree(root, worktree, "promote", proposal_id)
                    except _PromotionFailure as cleanup_exc:
                        if primary_failure is not None:
                            if isinstance(primary_failure, _PromotionFailure):
                                reason_code = primary_failure.reason_code
                            else:
                                reason_code = "validation_failed"
                            raise _PromotionFailure(
                                "{}; candidate worktree preserved: {}".format(primary_failure, cleanup_exc),
                                reason_code,
                                cleanup_exc.blocked_path,
                            ) from primary_failure
                        if integrated_commit is None:
                            raise
                        cleanup_failure = cleanup_exc
                elif primary_failure is not None:
                    if isinstance(primary_failure, _PromotionFailure):
                        reason_code = primary_failure.reason_code
                    else:
                        reason_code = "validation_failed"
                    raise _PromotionFailure(
                        "{}; candidate worktree preserved".format(primary_failure),
                        reason_code,
                        worktree,
                    ) from primary_failure
            reason = (
                "promotion integrated; staging worktree preserved: {}".format(cleanup_failure)
                if cleanup_failure is not None
                else None
            )
            result = _result("promoted", integrated_commit, reason)
            if cleanup_failure is not None:
                result["reason_code"] = cleanup_failure.reason_code
                if cleanup_failure.blocked_path is not None:
                    result["blocked_path"] = os.fspath(cleanup_failure.blocked_path)
            return result
    except _PromotionFailure as exc:
        return _blocked(str(exc), exc.reason_code, exc.blocked_path)
    except (OSError, ValueError, UnicodeError) as exc:
        return _blocked("promotion failed: {}".format(type(exc).__name__))


def rollback(repository: Path, commit: str, expected_head: str) -> dict[str, Any]:
    try:
        if not isinstance(commit, str) or not _HEX.fullmatch(commit):
            raise _PromotionFailure("promotion commit is invalid", "invalid_proposal")
        root = _repository_root(repository)
        with _promotion_lock(root):
            head = _expected_head(root, expected_head)
            branch = _current_branch(root)
            _worktree_clean(root)
            in_history = _command(
                ["git", "-C", os.fspath(root), "merge-base", "--is-ancestor", commit, head],
                root,
                check=False,
            )
            if in_history.returncode != 0:
                raise _PromotionFailure("promotion commit is not in the current branch history")
            message = _commit_message(root, commit)
            proposal_lines = [
                line.partition(": ")[2]
                for line in message.splitlines()
                if line.startswith("Learning-Proposal: ")
            ]
            edit_lines = [
                line.partition(": ")[2]
                for line in message.splitlines()
                if line.startswith("Learning-Edit: ")
            ]
            if (
                len(proposal_lines) != 1
                or not _PROPOSAL_ID.fullmatch(proposal_lines[0])
                or len(edit_lines) != 1
                or not _DIGEST.fullmatch(edit_lines[0])
            ):
                raise _PromotionFailure("current HEAD is not a learning promotion commit")
            changed = _git_bytes(
                root,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                commit,
            ).split(b"\0")
            paths = [item.decode("utf-8", "surrogateescape") for item in changed if item]
            if len(paths) != 1:
                raise _PromotionFailure("promotion commit changed an unexpected number of paths")
            target = paths[0]
            match = re.fullmatch(
                r"dotfiles/base/\.agents/skills/([a-z0-9][a-z0-9-]*)/SKILL\.md",
                target,
            )
            if not match or match.group(1) not in KNOWN_LEAF_SKILLS:
                raise _PromotionFailure("promotion commit target is outside the rollback allowlist")
            parents = _git(root, "rev-list", "--parents", "-n", "1", commit).split()
            if len(parents) != 2:
                raise _PromotionFailure("promotion commit is not a single-parent commit")
            inverse = _inverse_patch(root, commit, target)
            applies = _command(
                ["git", "-C", os.fspath(root), "apply", "-R", "--check", "-"],
                root,
                input_bytes=inverse,
                check=False,
            )
            if applies.returncode != 0:
                raise _PromotionFailure(
                    "the promoted paragraph changed since promotion",
                    "paragraph_changed",
                )
            worktree_root = _safe_worktree_root(root)
            worktree = _staging_worktree(
                root,
                head,
                worktree_root,
                "rollback",
                commit[:16],
            )
            integrated_commit = None
            cleanup_failure = None
            primary_failure = None
            cleanup_allowed = False
            try:
                recovered_candidate = _head(worktree) != head
                cleanup_allowed = not recovered_candidate
                if recovered_candidate:
                    revert_commit = _recovered_rollback_candidate(
                        worktree,
                        head,
                        commit,
                        target,
                    )
                else:
                    _command(
                        ["git", "-C", os.fspath(worktree), "apply", "-R", "--index", "-"],
                        worktree,
                        input_bytes=inverse,
                    )
                    _run_skill_check(worktree)
                    _git(worktree, "add", "--", target)
                    staged = _git_bytes(worktree, "diff", "--cached", "--name-only", "-z")
                    paths = [item.decode("utf-8", "surrogateescape") for item in staged.split(b"\0") if item]
                    if paths != [target] or _git_bytes(worktree, "diff", "--name-only", "-z"):
                        raise _PromotionFailure("rollback candidate has unexpected changes")
                    cleanup_allowed = False
                    _git(
                        worktree,
                        "-c",
                        "core.hooksPath=/dev/null",
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        "user.name=Learning Promotion",
                        "-c",
                        "user.email=agent-learning-promotion@localhost",
                        "commit",
                        "--no-gpg-sign",
                        "-m",
                        "revert(skills): restore prior skill guidance",
                        "-m",
                        "Reverts-Learning-Promotion: " + commit,
                    )
                    revert_commit = _head(worktree)
                parents = _git(worktree, "rev-list", "--parents", "-n", "1", revert_commit).split()
                if parents != [revert_commit, head]:
                    raise _PromotionFailure("rollback commit does not directly follow expected_head")
                _integrate(root, revert_commit, head, branch)
                integrated_commit = revert_commit
                cleanup_allowed = True
            except Exception as exc:
                primary_failure = exc
                raise
            finally:
                if cleanup_allowed:
                    try:
                        _remove_worktree(root, worktree, "rollback", commit[:16])
                    except _PromotionFailure as cleanup_exc:
                        if primary_failure is not None:
                            if isinstance(primary_failure, _PromotionFailure):
                                reason_code = primary_failure.reason_code
                            else:
                                reason_code = "validation_failed"
                            raise _PromotionFailure(
                                "{}; candidate worktree preserved: {}".format(primary_failure, cleanup_exc),
                                reason_code,
                                cleanup_exc.blocked_path,
                            ) from primary_failure
                        if integrated_commit is None:
                            raise
                        cleanup_failure = cleanup_exc
                elif primary_failure is not None:
                    if isinstance(primary_failure, _PromotionFailure):
                        reason_code = primary_failure.reason_code
                    else:
                        reason_code = "validation_failed"
                    raise _PromotionFailure(
                        "{}; candidate worktree preserved".format(primary_failure),
                        reason_code,
                        worktree,
                    ) from primary_failure
            reason = (
                "rollback integrated; staging worktree preserved: {}".format(cleanup_failure)
                if cleanup_failure is not None
                else None
            )
            result = _result("reverted", integrated_commit, reason)
            if cleanup_failure is not None:
                result["reason_code"] = cleanup_failure.reason_code
                if cleanup_failure.blocked_path is not None:
                    result["blocked_path"] = os.fspath(cleanup_failure.blocked_path)
            return result
    except _PromotionFailure as exc:
        return _blocked(str(exc), exc.reason_code, exc.blocked_path)
    except (OSError, ValueError, UnicodeError) as exc:
        return _blocked("rollback failed: {}".format(type(exc).__name__))
