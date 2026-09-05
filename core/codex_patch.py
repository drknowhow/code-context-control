"""Extract native apply_patch targets without evaluating shell or patch content."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PatchTarget:
    path: str
    change_type: str


def patch_targets(command: str, cwd: Path) -> list[PatchTarget]:
    if not isinstance(command, str) or len(command) > 8 * 1024 * 1024:
        raise ValueError("missing or oversized patch")
    lines = command.strip().splitlines()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("expected a complete apply_patch envelope")
    targets = {}
    current = None
    operation = None
    moved = False
    for line in lines[1:-1]:
        if line.startswith(("*** Add File: ", "*** Update File: ", "*** Delete File: ")):
            header, name = line.split(": ", 1)
            if not name.strip() or "\x00" in name:
                raise ValueError("empty or invalid patch path")
            current = str((cwd / name).resolve())
            operation = {"*** Add File": "created", "*** Update File": "modified",
                         "*** Delete File": "deleted"}[header]
            moved = False
            targets[current] = PatchTarget(current, operation)
        elif line.startswith("*** Move to: "):
            name = line[len("*** Move to: "):]
            if current is None or operation != "modified" or moved or not name.strip() or "\x00" in name:
                raise ValueError("invalid move target")
            destination = str((cwd / name).resolve())
            targets[current] = PatchTarget(current, "deleted")
            targets[destination] = PatchTarget(destination, "created")
            moved = True
        elif line.startswith("***") and line != "*** End of File":
            raise ValueError("unknown patch directive")
        elif current is None or (line and line[0] not in " +-@" and line != "*** End of File"):
            raise ValueError("invalid patch body")
    if not targets:
        raise ValueError("patch contains no target files")
    return list(targets.values())
