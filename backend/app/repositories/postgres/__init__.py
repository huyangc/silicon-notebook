"""PostgreSQL persistence adapter.

Keep package import inert: selecting SQLite must not import Psycopg or open a
PostgreSQL pool.  Concrete components are imported from their modules only by
the PostgreSQL composition root.
"""
