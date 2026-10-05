"""Minimal frozen helper entrypoint: intentionally does not import Textual."""
from threadgoon.bridge import main

if __name__ == "__main__":
    raise SystemExit(main())
