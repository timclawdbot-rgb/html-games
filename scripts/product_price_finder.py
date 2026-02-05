#!/usr/bin/env python3
"""DEPRECATED wrapper.

This path used to contain an older copy of the product price finder.

The up-to-date implementation lives in the private GitHub repo checked out at:
  /home/tnu/clawd/projects/product-price-finder/product_price_finder.py

This wrapper forwards all arguments to the up-to-date script so we don't
accidentally run stale logic.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    real = os.path.normpath(
        os.path.join(here, "..", "projects", "product-price-finder", "product_price_finder.py")
    )

    if not os.path.exists(real):
        print(f"ERROR: expected up-to-date product finder at: {real}", file=sys.stderr)
        sys.exit(2)

    os.execv(sys.executable, [sys.executable, real, *sys.argv[1:]])


if __name__ == "__main__":
    main()
