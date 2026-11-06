import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = Path(os.environ.get("AGENT_MEMORY_SRC") or ROOT / "src")
INSTALLED_BIN = os.environ.get("AGENT_MEMORY_BIN")
ENTRYPOINTS = {
    "agent-learn": "agent_memory.learning",
    "agent-sessions": "agent_memory.sessions_cli",
    "agent-sessions-serve": "agent_memory.serve",
}
USR_TARGET = ROOT / "tests" / "fixtures" / "usr_target"
CHECKER = "dotfiles/skills-check.sh"
MDTOOL = "dotfiles/base/.local/bin/mdtool"


def command(name):
    if INSTALLED_BIN:
        return [str(Path(INSTALLED_BIN) / name)]
    program = "import sys; sys.path.insert(0, %r); from %s import main; sys.exit(main())" % (str(SRC), ENTRYPOINTS[name])
    return [sys.executable, "-I", "-c", program]


def install_checker(repository):
    repository = Path(repository)
    for relative in (CHECKER, MDTOOL):
        target = repository / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(USR_TARGET / relative, target)


def create_usr_target(repository):
    repository = Path(repository)
    shutil.copytree(USR_TARGET, repository)
    for args in (("init", "-q", "-b", "main"), ("add", "."),
                 ("-c", "user.name=Synthetic Agent", "-c", "user.email=synthetic@example.invalid", "commit", "-q", "-m", "test: synthetic usr target")):
        subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)
    (repository / ".git/info/exclude").write_text(".work/\n")
    return repository
