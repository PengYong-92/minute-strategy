import hashlib
from collections.abc import Sequence
from pathlib import Path


def python_source_fingerprint(
    source_root: Path,
    *,
    prefix: str,
    paths: Sequence[Path] | None = None,
) -> str:
    root = Path(source_root).resolve()
    if paths is None:
        source_paths = tuple(
            path
            for path in root.rglob("*.py")
            if "__pycache__" not in path.parts
            and not any(part.startswith(".") for part in path.relative_to(root).parts)
        )
    else:
        source_paths = tuple(Path(path).resolve() for path in paths)

    digest = hashlib.sha256()
    for path in sorted(
        source_paths,
        key=lambda item: (
            item.relative_to(root).as_posix()
            if item.is_relative_to(root)
            else item.name
        ),
    ):
        relative = (
            path.relative_to(root).as_posix()
            if path.is_relative_to(root)
            else path.name
        )
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"{prefix}-{digest.hexdigest()[:16]}"
