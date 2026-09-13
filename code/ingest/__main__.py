"""Run ``python -m code.ingest`` to ingest the dataset and print a summary."""

from __future__ import annotations

import sys

from .pipeline import load_dataset


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dataset_dir = argv[0] if argv else None

    store = load_dataset(dataset_dir)
    print(store.summary())
    for name in store.keys():
        frame = store.table(name)
        print(f"\n[{name}] dtypes:")
        print(frame.schema)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
