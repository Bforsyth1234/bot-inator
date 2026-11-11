"""Print every Mach-O file under a directory, NUL-separated, deepest first.

Used by bundle-daemon.sh to sign the bundled Python inside-out (#96): every
executable, .so and .dylib must carry its own signature before the app is
sealed, or notarization rejects the upload. Symlinks are skipped (their
targets are listed in their own right).

    python3 -I scripts/release/list-macho.py <dir>
"""
import os
import sys

_THIN = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"}
_FAT = b"\xca\xfe\xba\xbe"  # also the Java class-file magic: check the arch count


def is_macho(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError:
        return False
    if head[:4] in _THIN:
        return True
    return head[:4] == _FAT and 0 < int.from_bytes(head[4:8], "big") < 30


def main(root: str) -> None:
    found = []
    for directory, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(directory, name)
            if not os.path.islink(path) and is_macho(path):
                found.append(path)
    found.sort(key=lambda p: (-p.count(os.sep), p))
    sys.stdout.write("\0".join(found))


if __name__ == "__main__":
    main(sys.argv[1])
