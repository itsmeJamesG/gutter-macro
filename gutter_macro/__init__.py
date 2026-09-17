"""Gutter Macro Dashboard — the data layer behind the AI-impact panels.

A declarative registry of everything tracked (series.py), thin per-vendor fetchers
(sources/), and a build step that lands tidy parquet in data_cache/ plus a compact
JSON payload for the web app.
"""
