"""Delete old renthub.db.bak-* backups at the repo root, keeping only the N most recent.

Usage:
    python scripts/prune_db_backups.py            # keep 5 most recent (default)
    python scripts/prune_db_backups.py --keep 10
    python scripts/prune_db_backups.py --dry-run
"""

import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", type=int, default=5, help="number of most recent backups to keep")
    parser.add_argument("--dry-run", action="store_true", help="list what would be deleted without deleting")
    args = parser.parse_args()
    if args.keep < 0:
        parser.error("--keep must be >= 0")

    backups = sorted(REPO_ROOT.glob("renthub.db.bak-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    to_delete = backups[args.keep:]

    if not to_delete:
        print(f"Nothing to prune ({len(backups)} backup(s), keeping up to {args.keep}).")
        return

    for path in to_delete:
        if args.dry_run:
            print(f"[dry-run] would delete {path.name}")
        else:
            path.unlink()
            print(f"deleted {path.name}")


if __name__ == "__main__":
    main()
