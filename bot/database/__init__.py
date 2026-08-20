"""Local persistence — SQLite case database (standard library only).

Implements the case repository abstraction (see ``repository.py``) with a
versioned schema (see ``migrations.py``). No hosted or paid database
services are used; the database file lives outside Git.
"""
