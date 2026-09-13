"""Run ``python -m code.tools`` for the currency converter CLI."""

from __future__ import annotations

from .currency_converter import main

if __name__ == "__main__":
    raise SystemExit(main())
