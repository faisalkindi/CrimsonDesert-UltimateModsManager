CDUMM v3.3.17

The Launch button works on more Steam setups now. On some installs CDUMM was sending a shortened Steam app id, so Steam answered with "Game configuration unavailable", and the Direct launch option could not find steam.exe when Steam sat in a custom folder. Both now come straight from the actual game folder. Thanks to lupo1190 (#186).

characterinfo mods can patch the appearance/mesh field and the model path field. lookup_22 and lookup_24 used to be rejected without a word. They apply now. Thanks to Yorivel (#192).

A failed import no longer shows up as a success. If the import worker died without printing an error, CDUMM counted it as finished. Now it shows the worker exit code, so a silent failure is actually visible. Thanks to RoGreat (#193).

Crimson Browser mods that add new preview textures keep them now. Barber and Character Creator images shipped inside a numbered folder were being treated as replacements for files that did not exist, so they got skipped and never loaded. They go through the new texture path instead. Thanks to RoGreat (#193).

Find Culprit Mod is turned off on Linux and macOS. It tells crash from stable by reading Windows crash dumps, which do not exist on those systems, so it reported a crash every round and blamed a random mod. It now shows a short note instead of running. Thanks to RoGreat (#195).

Reimport from source works for Crimson Browser, loose file and texture mods now. CDUMM had been saving the converted files as the mod source instead of the original download, so reimport kept re-applying stale data built for an older game version and the mod stayed outdated. It now keeps the original archive and rebuilds the mod against the current game. One catch: mods imported before this update need a single fresh import to store the right source. Thanks to jikulopo (#165).
