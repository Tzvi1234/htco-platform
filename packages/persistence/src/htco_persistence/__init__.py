"""HTCO persistence: the only package allowed to import the database driver (ADR-0011).

Explicit, parameterized SQL in repository adapters that implement application ports. The schema
lives in `supabase/migrations`, never in this package.
"""
