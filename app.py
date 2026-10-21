#!/usr/bin/env python3
"""Desktop entry point; package this file with PyInstaller on Windows."""
from mailclient.startup import main

if __name__ == "__main__":
    raise SystemExit(main())
