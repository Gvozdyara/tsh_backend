"""Maintenance scripts shipped inside the backend image.

* :mod:`scripts.import_json` - parse a legacy TSH JSON database and fill the
  PostgreSQL database (idempotent, supports --check / --dry-run).
"""
