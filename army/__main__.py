"""Entry point so ``python -m army`` works without installing anything.

Kept deliberately: adding a console script to the repository's ``pyproject.toml``
would be a fork patch carried for convenience, and the point of the
contributor-fork model is that the patches are the ones that had to exist.
"""

from army.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
