"""Semantic diffing and merging system for CDUMM.

Operates at the record/field level instead of raw bytes. Parses game
binary formats (PABGB) into structured records, diffs fields between
vanilla and mods, and merges multiple mods' changes with conflict detection.
"""
