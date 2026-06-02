CDUMM v3.3.16

This one fixes the wave of problems that hit after the Crimson Desert 1.09 patch, plus a few things from the last cycle.

Downloads and updates work again

After the 1.09 patch, Nexus downloads and the in-app update check started failing with a certificate error ("certificate has expired"). The frozen exe was trusting a fixed list of root certificates baked in when it was built, and once one of them expired it stopped trusting the Nexus API. CDUMM now pulls its certificate trust from certifi, which stays current with Mozilla's root list, so downloads and update checks keep working. Reports from OmiCron07, linkinzelda, animalt68 and HaZt-Panda, fix concept from OmiCron07.

In-app updater no longer freezes

The download-complete hang is properly fixed now. jikulopo ran the last build from source, added log lines, and showed the previous fix had not actually moved the work back to the main thread. The success popup now paints instead of locking up the window.

Mod version respects the manifest

If a mod author bumped the version inside the mod without renaming the download file, CDUMM was reading the old version off the filename. It now trusts the version the mod ships with. Thanks Balzhur.

Steam launch method option

On some installs the normal Steam launch returns "Game configuration unavailable" even though the Steam client launches the game fine. Settings now has a Steam launch method dropdown, and the Direct option uses the same call the Steam client uses for Play. The default is unchanged, so if launching already works for you nothing changes. Thanks lupo1190.

Quality of life

The Configure window grows on big monitors instead of sitting in a small box. The whole row on the About page links is clickable now, not just the text. Double-clicking a mod toggles it. Thanks devCKVargas.

Diagnostics

The Launch button and the iteminfo parser write clearer log lines, so the next bug report is easier to trace.

Known issue

Some Format 3 mods that touch iteminfo on 1.09 still get skipped, because the game changed that file's internal layout. I am still reverse engineering the new layout under issue 182. Format 2 offset based mods are fine and apply normally.
