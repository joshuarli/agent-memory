"""age encryption for mirror objects, driven through an external executable."""
import hashlib
import os
import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence, cast

from agent_memory import transfer

INSTALL_HINT = "install age or rage (for example `brew install age`), or set AGE_BIN"
MAX_RECIPIENT = 4096


class CryptoError(RuntimeError):
    pass


def binary() -> str:
    configured = os.environ.get("AGE_BIN")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise CryptoError("AGE_BIN is not an executable file")
        return str(path)
    for name in ("rage", "age"):
        found = shutil.which(name)
        if found:
            return found
    raise CryptoError("no age or rage executable found; " + INSTALL_HINT)


def _keygen_cmd() -> list[str]:
    primary = Path(binary())
    for name in ("rage-keygen", "age-keygen"):
        sibling = primary.with_name(name)
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return [str(sibling)]
    for name in ("rage-keygen", "age-keygen"):
        found = shutil.which(name)
        if found:
            return [found]
    if primary.name == "rage":
        return [str(primary), "keygen"]
    raise CryptoError("no age-keygen or rage-keygen executable found next to " + primary.name)


def _keygen() -> str:
    return _keygen_cmd()[0]


def _fail(what, returncode, output, sensitive):
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else output
    detail = [line.strip() for line in text.splitlines() if line.strip()]
    message = next((line for line in detail if line.lower().startswith(("error", "age:", "rage:"))),
                   detail[0] if detail else "no diagnostic")
    for item in sensitive:
        message = message.replace(str(item), "<path>")
    raise CryptoError("%s failed (exit %d): %s" % (what, returncode, message[:500]))


def _run(argv, what, sensitive=()):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        raise CryptoError(what + " could not run: " + exc.strerror) from exc
    if result.returncode != 0:
        _fail(what, result.returncode, result.stderr or result.stdout, sensitive)
    return result


def _run_bytes(argv, what, data, sensitive=()) -> bytes:
    try:
        result = subprocess.run(argv, input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=600)
    except OSError as exc:
        raise CryptoError(what + " could not run: " + exc.strerror) from exc
    except subprocess.TimeoutExpired as exc:
        raise CryptoError(what + " timed out") from exc
    if result.returncode != 0:
        _fail(what, result.returncode, result.stderr or result.stdout, sensitive)
    return cast(bytes, result.stdout)


def _recipients(recipients: Sequence[str]) -> list[str]:
    clean = []
    for recipient in recipients:
        if not isinstance(recipient, str) or not recipient or recipient.startswith("-"):
            raise CryptoError("invalid age recipient")
        if len(recipient) > MAX_RECIPIENT:
            raise CryptoError("age recipient is too long")
        clean.append(recipient)
    if not clean:
        raise CryptoError("at least one age recipient is required")
    return clean


def encrypt_bytes(data, recipients: Sequence[str]) -> bytes:
    recipients = _recipients(recipients)
    argv = [binary()]
    for value in recipients:
        argv += ["-r", value]
    return _run_bytes(argv, "age encryption", bytes(data))


def _identity(identity_path) -> Path:
    path = Path(identity_path)
    if not path.is_file():
        raise CryptoError("age identity is not a readable file")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise CryptoError("age identity is accessible to other users; expected mode 0600")
    return path


def decrypt_bytes(ciphertext, identity_path) -> bytes:
    identity = _identity(identity_path)
    return _run_bytes([binary(), "-d", "-i", str(identity)], "age decryption", bytes(ciphertext),
                      sensitive=[identity])


def load_recipients(path) -> list[str]:
    try:
        lines = Path(path).read_text().splitlines()
    except OSError as exc:
        raise CryptoError("age recipients file is not readable") from exc
    return _recipients([line.strip() for line in lines
                        if line.strip() and not line.lstrip().startswith("#")])


@dataclass(frozen=True)
class PreparedObject:
    plaintext_digest: str
    plaintext_size: int
    encoding: str
    compressed_size: int
    ciphertext: bytes
    ciphertext_digest: str
    ciphertext_size: int
    compress_cpu_ms: float
    encrypt_cpu_ms: float

    @property
    def compression_ratio(self) -> float:
        return self.compressed_size / self.plaintext_size if self.plaintext_size else 1.0


def prepare_object(data, recipients: Sequence[str]) -> PreparedObject:
    raw = bytes(data)
    started = time.process_time()
    compressed = transfer.compress(raw) if raw else b""
    compress_cpu_ms = (time.process_time() - started) * 1000
    if raw and len(compressed) * 100 <= len(raw) * 95:
        encoding, payload = "zlib", compressed
    else:
        encoding, payload = "identity", raw
    started = time.process_time()
    ciphertext = encrypt_bytes(payload, recipients)
    encrypt_cpu_ms = (time.process_time() - started) * 1000
    return PreparedObject(
        plaintext_digest=hashlib.sha256(raw).hexdigest(),
        plaintext_size=len(raw),
        encoding=encoding,
        compressed_size=len(payload),
        ciphertext=ciphertext,
        ciphertext_digest=hashlib.sha256(ciphertext).hexdigest(),
        ciphertext_size=len(ciphertext),
        compress_cpu_ms=compress_cpu_ms,
        encrypt_cpu_ms=encrypt_cpu_ms,
    )


def decode_object(ciphertext, identity_path, encoding: str, plaintext_digest: str, plaintext_size: int) -> bytes:
    if encoding not in ("identity", "zlib"):
        raise CryptoError("object uses an unknown encoding")
    payload = decrypt_bytes(ciphertext, identity_path)
    if encoding == "zlib":
        try:
            data = transfer.decompress(payload, plaintext_size)
        except transfer.TransferError as exc:
            raise CryptoError("object failed to decode: " + str(exc)) from exc
    else:
        data = payload
    if len(data) != plaintext_size:
        raise CryptoError("object does not match its declared size")
    if hashlib.sha256(data).hexdigest() != plaintext_digest:
        raise CryptoError("object does not match its declared digest")
    return data


def keygen(identity_path) -> str:
    path = Path(identity_path)
    if path.exists():
        raise CryptoError("identity already exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _run(_keygen_cmd() + ["-o", str(path)], "age-keygen", sensitive=[path])
    os.chmod(path, 0o600)
    return recipient(path)


def recipient(identity_path) -> str:
    path = Path(identity_path)
    result = _run(_keygen_cmd() + ["-y", str(path)], "age-keygen", sensitive=[path])
    value = result.stdout.strip()
    if not value:
        raise CryptoError("identity has no recipient")
    return value


def encrypt_file(source, destination, recipients: Sequence[str]) -> dict[str, object]:
    source = Path(source)
    destination = Path(destination)
    recipients = _recipients(recipients)
    if not source.is_file():
        raise CryptoError("plaintext is not a readable file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".age-" + uuid.uuid4().hex)
    argv = [binary()]
    for value in recipients:
        argv += ["-r", value]
    argv += ["-o", str(temporary), str(source)]
    try:
        _run(argv, "age encryption", sensitive=[source, destination])
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    with destination.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"ciphertext_sha256": digest, "ciphertext_size": destination.stat().st_size}


def decrypt_file(source, destination, identity_path) -> None:
    source = Path(source)
    destination = Path(destination)
    identity_path = Path(identity_path)
    if destination.exists():
        raise CryptoError("refusing to overwrite an existing plaintext destination")
    if not source.is_file():
        raise CryptoError("ciphertext is not a readable file")
    identity_path = _identity(identity_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".age-" + uuid.uuid4().hex)
    try:
        _run([binary(), "-d", "-i", str(identity_path), "-o", str(temporary), str(source)],
             "age decryption", sensitive=[source, destination, identity_path])
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
