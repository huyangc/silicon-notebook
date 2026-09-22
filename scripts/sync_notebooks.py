#!/usr/bin/env python3
"""Thin entry point for the cross-environment notebook sync operator CLI."""

from app.migration.sync.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
