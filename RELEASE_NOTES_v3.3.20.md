CDUMM v3.3.20

Hotfix for v3.3.19.

Mixing item mods no longer silently drops the JSON ones. When the combined item-table change was checked at apply time, a single stale byte anywhere, from an old contaminated backup or another mod already touching the table, failed the whole comparison and every JSON item mod was skipped at once, with a warning that printed megabytes of raw data. The change now rebuilds itself against the bytes actually present, so it layers cleanly on top of whatever else is there, and the warning shows a short summary instead of the data dump. Thanks to falobos76 (#191) for the quick retest that caught it.
