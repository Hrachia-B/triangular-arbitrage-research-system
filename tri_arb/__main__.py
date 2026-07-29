"""Allow ``python -m tri_arb`` to behave like ``python -m tri_arb.main``."""

from tri_arb.main import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
