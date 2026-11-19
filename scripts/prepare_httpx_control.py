"""Copy one pilot HTTPX task to a new root with compatible test tooling."""
import argparse
import shutil
from pathlib import Path


TASKS = ("encode__httpx-3367", "encode__httpx-3412")
# Versions from HTTPX 3367's frozen requirements.txt. The tests use AnyIO,
# not pytest-asyncio; that extra plugin can fail during pytest configuration.
DOCKERFILE_TAIL = """
# Core-20 compatibility overlay: use the pinned HTTPX pilot test tooling.
RUN python -m pip uninstall -y pytest-asyncio \\
    && python -m pip install pytest==8.3.3 trio==0.26.2 trio-typing==0.10.0 \\
       trustme==1.1.0 cryptography==43.0.1 uvicorn==0.31.0 chardet==5.2.0
# Click 8.5 deprecates the isolated_filesystem API used by the frozen test.
RUN python -m pip install click==8.1.7
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("new_root", type=Path)
    parser.add_argument("--task", choices=TASKS, default=TASKS[0])
    args = parser.parse_args()
    source = args.source_root.resolve() / args.task
    dest = args.new_root.resolve() / args.task
    if not (source / "task.toml").is_file():
        parser.error("source does not contain the frozen HTTPX task")
    args.new_root.resolve().mkdir(parents=True, exist_ok=False)
    shutil.copytree(source, dest, symlinks=True)
    dockerfile = dest / "environment" / "Dockerfile"
    if not dockerfile.resolve().is_relative_to(dest.resolve()):
        parser.error("copied Dockerfile must not resolve outside the task")
    with dockerfile.open("a", encoding="utf-8") as stream:
        stream.write(DOCKERFILE_TAIL)
    print(dest)


if __name__ == "__main__":
    main()
