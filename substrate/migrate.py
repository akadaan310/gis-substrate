"""Legacy migration: index upload-tool files IN PLACE into the substrate.

Read-only with respect to existing files: nothing is moved, renamed, or deleted.
Writes only: substrate.db rows + blobs/ entries for NEW uploads (legacy bytes stay put).

Usage:
  python3 -m substrate.migrate --legacy-dir /home/azureuser/upload-tool/files \\
      --index /home/azureuser/upload-tool/index.jsonl --data-dir /home/azureuser/substrate
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from substrate import db as _db
from substrate.store import index_legacy_file, sha256_file


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy-dir", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    entries = {}
    if os.path.isfile(args.index):
        with open(args.index) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("id"):
                    entries[e["id"]] = e
    on_disk = sorted(fn for fn in os.listdir(args.legacy_dir)
                     if os.path.isfile(os.path.join(args.legacy_dir, fn))
                     and not fn.endswith(".part"))
    ids = sorted(set(entries) | set(on_disk))

    if args.dry_run:
        print("would index %d legacy files from %s" % (len(ids), args.legacy_dir))
        return 0

    con = _db.connect(args.data_dir)
    _db.init_db(con)
    indexed, skipped, errors = 0, 0, []
    for lid in ids:
        e = entries.get(lid, {})
        fpath = os.path.join(args.legacy_dir, lid)
        if not os.path.isfile(fpath):
            errors.append((lid, "indexed but file missing on disk"))
            continue
        try:
            art = index_legacy_file(con, args.data_dir, lid, fpath,
                                    orig_name=e.get("name", lid),
                                    mime=e.get("type"))
            # verify the recorded hash matches the file
            if art["blob_sha256"] != sha256_file(fpath):
                errors.append((lid, "hash mismatch after indexing"))
                continue
            indexed += 1
        except Exception as ex:  # noqa: BLE001 - report, don't stop
            errors.append((lid, "error: %s" % ex))
    con.close()
    print("indexed=%d errors=%d total_ids=%d" % (indexed, len(errors), len(ids)))
    for lid, msg in errors[:20]:
        print("  ERROR %s: %s" % (lid, msg))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
