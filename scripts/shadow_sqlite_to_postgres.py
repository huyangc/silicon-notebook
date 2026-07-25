#!/usr/bin/env python3
"""Thin entry point for the explicit forward-shadow operator CLI."""

from app.migration.shadow.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
