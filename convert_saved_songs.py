#!/usr/bin/env python3
"""One-time migration: convert the legacy '*_' filename save marker to an
embedded metadata tag.

Usage:
    python convert_saved_songs.py <target_directory> [--dry-run]

For every '*_*.mp3' / '*_*.flac' file in the directory (non-recursive):
  1. the 'saved' tag is written into the file
       MP3  -> ID3 TXXX frame  TXXX:MUSIBISK_SAVED = '1'
       FLAC -> Vorbis Comment  MUSIBISK_SAVED = '1'
  2. the file is renamed with the leading '*_' removed
  3. the original access/modified timestamps are restored (the tag
     write updates the modified time, which would otherwise make every
     converted song look brand new — Musibisk orders its playlist by
     modified time). The created/birth time is not restored: Linux
     gives userspace no way to set it.

Files that fail tagging are left completely untouched (no rename).
Only 'mutagen' is required:  pip install mutagen

NOTE: the tag format must stay in sync with SAVED_TAG_KEY /
set_saved_tag / read_saved_tag in main.py.
"""

import os
import sys
from pathlib import Path

try:
    import mutagen
except ImportError:
    sys.exit("This script requires mutagen:  pip install mutagen")

SAVED_TAG_KEY = 'MUSIBISK_SAVED'
SAVED_TAG_VALUE = '1'
EXTENSIONS = {'.mp3', '.flac'}


def set_saved_tag(filepath: Path) -> bool:
    """Write the 'saved' tag into an MP3 (ID3 TXXX) or FLAC (Vorbis Comment)."""
    audio = mutagen.File(str(filepath))
    if audio is None:
        return False
    try:
        if getattr(audio, 'tags', None) is None:
            audio.add_tags()
        tags = audio.tags
        if tags is None:
            return False
        if isinstance(audio, mutagen.mp3.MP3):
            from mutagen.id3 import TXXX
            key = next((k for k in tags.keys()
                        if k.startswith('TXXX:')
                        and k[5:].upper() == SAVED_TAG_KEY), None)
            if key is not None:
                tags[key].text = [SAVED_TAG_VALUE]
            else:
                tags.add(TXXX(encoding=3, desc=SAVED_TAG_KEY,
                              text=[SAVED_TAG_VALUE]))
        elif isinstance(audio, mutagen.flac.FLAC):
            tags[SAVED_TAG_KEY] = [SAVED_TAG_VALUE]
        else:
            return False
        audio.save()
        return True
    except Exception as e:
        print(f"    tag write failed: {e}")
        return False


def read_saved_tag(filepath: Path) -> bool:
    audio = mutagen.File(str(filepath))
    if audio is None:
        return False
    tags = getattr(audio, 'tags', None)
    if tags is None:
        return False
    try:
        if isinstance(audio, mutagen.mp3.MP3):
            for key, frame in tags.items():
                if key.startswith('TXXX:') and key[5:].upper() == SAVED_TAG_KEY:
                    return str(frame.text[0]) == SAVED_TAG_VALUE
            return False
        vals = tags.get(SAVED_TAG_KEY)
        return bool(vals) and str(vals[0]) == SAVED_TAG_VALUE
    except Exception:
        return False


def restore_timestamps(filepath: Path, st):
    """Restore atime+mtime (nanosecond precision) after the tag write.

    The created/birth time is deliberately not touched: on Linux it can
    be read (statx) but there is no userspace API to set it.
    """
    try:
        os.utime(filepath, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError as e:
        print(f"    warning: could not restore timestamps: {e}")


def main() -> int:
    args = sys.argv[1:]
    dry_run = '--dry-run' in args
    args = [a for a in args if a != '--dry-run']
    if len(args) != 1:
        print(__doc__)
        return 1
    directory = Path(args[0])
    if not directory.is_dir():
        print(f"Not a directory: {directory}")
        return 1

    files = sorted(
        f for f in directory.iterdir()
        if f.is_file() and f.name.startswith('*_') and f.suffix.lower() in EXTENSIONS
    )
    if not files:
        print(f"No '*_' saved songs found in {directory}")
        return 0

    print(f"Found {len(files)} legacy saved song(s) in {directory}")
    if dry_run:
        print("Dry run — nothing will be changed.")

    failures = 0
    for f in files:
        new_name = f.name[2:]
        target = f.parent / new_name
        if target.exists():
            print(f"SKIP   {f.name}  ('{new_name}' already exists)")
            failures += 1
            continue
        if dry_run:
            print(f"WOULD  {f.name} -> {new_name}  (tag 'saved', "
                  f"timestamps kept)")
            continue
        st = f.stat()  # original timestamps, before any modification
        if set_saved_tag(f):
            f.rename(target)
            tagged = read_saved_tag(target)
            restore_timestamps(target, st)
            print(f"OK     {f.name} -> {new_name}"
                  + ("" if tagged else "  WARNING: tag not verified!"))
            if not tagged:
                failures += 1
        else:
            restore_timestamps(f, st)
            print(f"FAIL   {f.name}  (left untouched)")
            failures += 1

    if failures:
        print(f"\nDone with {failures} problem(s).")
        return 1
    print("\nDone — all files converted.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
