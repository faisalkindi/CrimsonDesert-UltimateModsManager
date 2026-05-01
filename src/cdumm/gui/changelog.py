"""Changelog data and patch notes dialog for CDUMM."""

from PySide6.QtWidgets import QTextBrowser

from qfluentwidgets import (
    BodyLabel,
    MessageBoxBase,
    SubtitleLabel,
)

from cdumm.i18n import tr

# Changelog entries — newest first. Add new versions at the top.
CHANGELOG = [
    {
        "version": "3.2.8",
        "date": "2026-05-01",
        "notes": [
            "<b>Format 3 mods that change a primitive field on iteminfo (item stack count, durability, item flags, etc.) now actually take effect in-game.</b> The Format 3 expansion was emitting per-mod byte offsets relative to the record start, but the apply pipeline anchors offsets at the position AFTER each item's name string. The two coordinate systems differed by 8 + name_length bytes per record, so primitive Format 3 intents were silently writing to the wrong byte position on every record. The verification check then caught the mismatch and skipped the patch, leaving the mod with zero effect. Bug from Faisal's Can It Stack JSON V3 test on 2026-05-01 (1812 of 1827 patches reported \"byte mismatch\"); latent since v3.2.3 when Format 3 primitive support shipped. ZirconX1, Lichtnocht, jscrump1278's earlier \"applies cleanly but doesn't work in-game\" reports likely trace back to this.",
            "<b>RAR and 7z mod archives that ship Format 3 / OG_ XML / plain XML drop content now import.</b> The shared RAR/7z helper (`_import_from_extracted`) was missing the format detectors that import_from_zip already had, so the same JSON dropped as a ZIP imported fine while the same JSON dropped as a RAR errored with \"no recognized mod format\". Bug from RockNBeard's Standard Gamepad Layout test (Nexus mod 1489) and Lovexvirus007's Can It Stack JSON V3 (Nexus mod 2180), both 2026-05-01.",
            "<b>RAR and 7z mod archives that mix .asi plugins with PAZ data now install both halves.</b> The ZIP path stages ASI plugins to a persistent dir before format detection so the GUI handler can copy them into bin64/, but the RAR/7z path had no such staging. Mixed RAR/7z mods silently dropped the ASI half. Now the RAR/7z wrappers run the same staging as the ZIP wrapper.",
            "<b>The \"X mods were dropped during apply\" warning now names real mod names instead of the placeholders \"byte_merge\" or \"semantic_merge\".</b> When two mods conflict on the same entry and CDUMM merges them via the byte-level or semantic-level fallback, the merged delta was being tagged with a synthetic mod_name that then leaked into the size-merge fallback warning at apply time. The v3.2.7 fix that named dropped mods correctly was being undone by this layer. Now the merged delta carries a composed name like \"Mod A + Mod B\" (with a +N more tail for big merges) so the conflict warning stays informative.",
            "<b>Variant packs (multiple Format 3 JSONs in one archive) dropped as RAR/7z now show the same \"drop on the main window to pick a variant\" message that ZIP imports get.</b> Before, RAR/7z drops with multiple Format 3 JSONs fell through to the generic \"no recognized format\" error.",
            "<b>iteminfo and skill Format 3 writers now log clear warnings when an intent gets dropped.</b> Earlier: silent debug-level skips for unsupported ops (only `set` is supported), unknown record keys, and unknown field names. Now: warning-level log lines that name the intent, plus a per-change skip summary in the change label so the apply summary shows \"3 intents applied, 5 skipped: 2 unknown key, 3 unknown field\". Also surfaces a warning when zero intents apply at all, instead of silently producing no change.",
        ],
    },
    {
        "version": "3.2.7",
        "date": "2026-04-30",
        "notes": [
            "<b>Gamepad / UI XML mods that ship as raw .xml files in a folder now import.</b> Some mod authors package XML replacements as plain files (e.g. <i>Standard Gamepad Layout/ui/inputmap_common.xml</i>) with a <i>modinfo.json</i>, no <code>OG_</code> prefix and no Crimson Browser manifest. Earlier versions rejected those with \"no recognised mod format\". CDUMM now scans for plain .xml files at vanilla basenames, looks each up in the game's PAMT, and routes the matches through the same full-file-replacement pipeline OG_ mods use. RockNBeard's <i>Standard Gamepad Layout</i> (Nexus mod 1489) is the test case. Thanks to RockNBeard on Nexus for the report.",
            "<b>The \"X mod(s) targeting Y were dropped\" warning now names the actual dropped mod instead of \"mod #0\".</b> The v3.2.6 fix that named which mod got dropped was working at the merge function but the overlay-routing path upstream lost the <i>mod_name</i> field before the entry reached the merge. Result: the warning template fell back to a placeholder that was useless for users trying to figure out which mod to re-prioritise. CDUMM now propagates <i>mod_name</i> (and <i>priority</i>) from the delta dict into the routed metadata so the warning shows real names like \"Active: 'aggregated JSON'. Dropped: 'My Loot Mod'\". Thanks to DerBambusbjoern on Nexus for the screenshot.",
            "<b>Mod cards no longer crash when a mod declares a target language or override mode.</b> The language badge (shown for mods with <i>target_language</i> in modinfo) and the override badge (shown for mods with <i>conflict_mode: override</i>) were both built with the wrong colour-dict keys, so the moment a mod with either field rendered, the card constructor raised <i>KeyError</i> and the entire mods list failed to draw. The bug was latent until the first language / override mod imported. Fixed both badges to use the standard pill-stylesheet keys.",
            "<b>Mixed ZIPs (.asi + PAZ in one archive) now actually install the .asi half.</b> The staging path used a temporary directory that auto-deleted before the GUI handler could copy the staged plugins, AND the ASI metadata was being thrown away when a later format detector reassigned the import-result object. Result: every mixed-ZIP import silently dropped its ASI plugins even though the toast claimed success. Staging now uses a per-import UUID subdir under deltas/, the per-import dir gets cleaned up after install, stale dirs from prior crashes are swept on app start, and the GUI handler copies into bin64/ from a path that still exists.",
            "<b>The .ini filter on mixed-ZIP staging no longer steals game-data .ini files.</b> The previous build moved every .ini file out of the extract tree and into ASI staging, so any mod that ships a configuration .ini inside a numbered PAZ folder lost it. Now only .ini files whose stem matches a sibling .asi (proper ASI companion config) are staged.",
            "<b>OG_ XML full-replacement mods now create a real mod row instead of an orphan delta file.</b> The v3.2.6 detector wrote the replacement bytes to disk but never inserted into the mods table or mod_deltas, so pure OG_ mods imported as \"no recognised mod format\" with the orphan .entr sitting in deltas/. CDUMM now creates a full mod row with author / version / game version stamp and registers each OG_ entry in mod_deltas, mirroring the pipeline used for JSON-source mods. Detection is also case-insensitive on the OG_ prefix (Linux-authored ZIPs) and rejects empty XML files instead of writing a 3-byte BOM-only delta.",
            "<b>OG_ XML re-import is now atomic.</b> When updating an existing OG_ mod, mid-loop write failures used to roll back the database but leave overwritten .entr files on disk, so the restored DB rows pointed at corrupted bytes. CDUMM now writes new deltas with a .entr.new suffix during the loop and atomically swaps them into place only after the database commit succeeds. Failed renames are surfaced via the result info banner so the user knows to re-import.",
            "<b>JSON mod inline value editing now stores edits at the correct patch index.</b> When a JSON mod's config panel mixed toggles and editable spinboxes (e.g. some changes were on/off, others let you adjust a number), the apply pipeline saved the spinbox values keyed by the wrong patch slot. Result: spinbox edits silently fell back to the mod author's defaults at apply time. The config panel now tags each emitted entry with its original patch index and the apply pipeline keys custom values by that index, not by emit order.",
            "<b>conflict_mode in modinfo.json is now case-insensitive.</b> A mod author who wrote <code>\"conflict_mode\": \"Override\"</code> (capitalised) was silently downgraded to normal because the validator did exact-string comparison. CDUMM now strips and lowercases the value before checking, and logs a warning when the supplied value doesn't normalise to a known mode.",
            "<b>target_language in modinfo.json is now normalised and length-capped.</b> Authors writing \"EN\" vs \"en\" used to bucket separately, and a long value like \"english\" overflowed the language badge. The value is now stripped, lowercased, and truncated to 12 characters (covers extended BCP47 tags like <i>zh-Hant-TW</i>).",
            "<b>fix_xml_format is now idempotent.</b> Running the BOM/CRLF fixup on an XML file that already had both a BOM and an &lt;?xml?&gt; declaration left the declaration intact (the declaration check ran before the BOM was stripped) and the game's parser then rejected the \"fixed\" file. The fixup now strips any leading BOM before checking for the declaration so two passes produce the same output as one.",
        ],
    },
    {
        "version": "3.2.6",
        "date": "2026-04-29",
        "notes": [
            "<b>v3 Format 3 mods that target large data tables (like \"No Cooldown for All Items\") now apply their working patches instead of dropping the whole file.</b> When part of a Format 3 mod no longer matches the current game (e.g. Pearl Abyss already zeroed some cooldowns the mod expected to change), CDUMM was correctly applying the working entries during import, but at apply time the mount-time guard was hardcoded to skip the entire <i>iteminfo.pabgb</i> file on any mismatch. Result: \"Apply Completed with Warnings\" with 80 of 201 patches surfaced and the other 121 verified patches silently dropped. The v3.2.3 <code>allow_partial_apply</code> opt-in only worked at import; the apply-time guard now honors it too. Mods that opt in get the verified changes through, the rest are skipped with a clear log line.",
            "<b>The Character Creator mod (and other multi-folder variant zips) now show distinguishable picker labels.</b> Mods like Character Creator v4.9 ship six JSONs all internally named \"Female Animations\" (or \"Male Animations\") under different race folders (HumanFemale, OrcFemale, GoblinFemale). The picker was rendering three identical rows because it only used the JSON's internal name field. Now CDUMM detects label collisions and appends the parent folder name in parentheses, so you see \"Female Animations (HumanFemale)\", \"Female Animations (OrcFemale)\", etc. Thanks to ZapZockt on GitHub.",
            "<b>Variant mods with shared filenames no longer overwrite each other on disk.</b> When two or more variants share a basename (e.g. several FemaleAnimations.json files under different parent folders), CDUMM was copying them all to the same destination so only the last file survived. Switching variants in the cog panel did nothing because every variant's metadata pointed at the same overwritten file. CDUMM now adds a parent-folder prefix to each on-disk filename so all variants survive and the cog can actually toggle between them.",
            "<b>Mixed zips that ship an .asi plugin alongside JSON variants now install both.</b> Character Creator's zip ships <i>CharacterCreator.asi</i> next to its variant JSONs. The variant import path was extracting the zip, picking the JSON, and dropping the .asi on the floor. The ASI never showed up in the ASI Mods tab and the in-game features that depend on it never worked. CDUMM now scans the extracted archive for any .asi files after a variant import and installs them via the normal ASI manager. Thanks to ZapZockt on GitHub.",
            "<b>NattKh field-name mods that rewrite drop tables (the <i>drops</i> field on <i>dropsetinfo.pabgb</i>) now apply.</b> Mods like kori228's 100% drop rate and the original 5x drops + bags + bandits + bleed pack target a variable-length list of drop entries. Earlier CDUMM versions skipped these intents with a 'requires list writer' message because changing the list size shifts every record after it. CDUMM now ports NattKh's drop-entry layout, serializes the new list, and rides the existing cumulative-shift cascade so all 695 records in a multi-target mod apply cleanly. Thanks to kori228 and the original #41 reporter on GitHub.",
            "<b>NattKh field-name mods that rewrite item buffs / enchant data (the <i>enchant_data_list</i>, <i>equip_passive_skill_list</i>, and 16 other list fields on <i>iteminfo.pabgb</i>) now apply.</b> The example case is UnLuckyLust's Item Buffs export which sets <i>enchant_data_list</i> with nested <i>buy_price_list</i> and <i>equip_buffs</i> entries. CDUMM now bundles NattKh's verified iteminfo parser/serializer (Rust extension, MPL-2.0) and dispatches all iteminfo list-of-dict intents through it. The full 5.3 MB iteminfo table parses, mutates, and re-serializes byte-perfectly in under a second. Thanks to UnLuckyLust on GitHub.",
            "<b>NattKh field-name mods that rewrite skill data (the <i>_useResourceStatList</i> and <i>_buffLevelList</i> fields on <i>skill.pabgb</i>) now apply.</b> Test case is timuela's <i>focus_aerial_roll</i> mod. CDUMM now bundles NattKh's verified skill parser (pure Python port, MPL-2.0) so skill mods can be applied alongside item, drop, and other Format 3 mods. Thanks to timuela on GitHub.",
            "<b>Updating a disabled ASI plugin no longer creates a duplicate entry in the list.</b> When an ASI plugin was disabled (file renamed to <i>.asi.disabled</i>) and the user dropped a new version of the same mod, CDUMM wrote the new <i>.asi</i> next to the old <i>.asi.disabled</i> instead of replacing it. The plugin scanner then reported both files as separate plugins, so the ASI Plugins list showed two entries for the same name (one enabled, one disabled). CDUMM now removes the stale <i>.asi.disabled</i> sibling on update.",
            "<b>The 'Apply Completed with Warnings' message now names the specific JSON mods that produced no game changes.</b> Before, the warning just said 'X JSON mods produced no changes' with no way to tell which mods failed or which game files were targeted. Now you see each contributing mod by name plus the files it tried to patch, so you know exactly what to investigate. Thanks to Robhood19 on Nexus for the report that surfaced the missing attribution.",
            "<b>Bug report no longer shows the misleading 'vanilla_backup=no' for JSON mount-time mods.</b> JSON mods that apply at apply-time (rather than from pre-computed deltas) legitimately have no entries in the deltas table, but the bug-report's vanilla-backup check iterated those entries and always returned 'no' for them, making the field actively misleading during triage. Now those rows render as 'vanilla_backup=n/a (mount-time)' so users and triagers can tell the field doesn't apply, instead of assuming a missing backup.",
        ],
    },
    {
        "version": "3.2.5",
        "date": "2026-04-29",
        "notes": [
            "<b>Multi-file mods now show a clear \"X file(s) skipped\" warning when some files were dropped.</b> The earlier multi-file fix (one bad file no longer rejects the whole mod) was applying the working files but not telling you which ones were skipped. Now you get a yellow banner listing the skipped files so you can ask the mod author what to fix. Same warning shows for batch imports too.",
            "<b>A malformed Format 3 intent no longer crashes the whole mod import.</b> If a mod's JSON has a missing or null `field` name on any single intent, the importer now skips just that intent with a clear message. Other intents in the same mod still apply normally.",
            "<b>Bug Report stops showing fake \"missing key\" errors for normal byte-patch mods.</b> The diagnostic was flagging every change in a classic byte-offset JSON mod as \"ISSUE: missing 'entry' key\" — but that key is optional in v2 (only used by the newer entry-anchored format). The warning was a false positive that made perfectly-valid mods look broken when they weren't. Now the diagnostic only flags changes that genuinely have nowhere to write to. Thanks to lycusz and jscrump1278 on GitHub.",
            "<b>Reimporting a Format 3 mod no longer fails with \"file is being used by another process\".</b> When you right-clicked a Format 3 mod and chose Reimport from source, CDUMM was trying to copy the mod's JSON file onto itself (because the mod's stored source path points at CDUMM's own copy in the mods folder). On Windows, that fails with WinError 32 if anything is briefly holding the file open — antivirus scanning, CDUMM's apply worker, etc. Now CDUMM detects the same-source case and skips the unnecessary copy. Thanks to Matrixz on Nexus.",
            "<b>Multi-file mods no longer reject the whole mod when one file is incompatible.</b> Mods like Faster NPC Animations (Instant) ship hundreds of patch files at once. Before, if even ONE of those files had byte offsets that didn't match the current game, CDUMM rejected the entire mod — even if the other 115 files were perfectly fine. Now CDUMM applies all the working files and clearly tells you which specific files were skipped, so you get the working parts of the mod and a useful note for the author about exactly what needs updating.",
            "<b>Mods that work in older managers but failed here with \"byte patches don't match\" now apply.</b> Some mods ship a leftover \"signature\" header in their JSON that's stale — meaning the offsets in the mod are actually meant to be absolute but the signature points at a wrong spot in the current game. Before, CDUMM trusted the signature blindly and rejected the whole mod. Now CDUMM detects this case (signature-relative apply produces zero matches but absolute would work), falls back to absolute offsets, and applies the mod cleanly with a clear log line. Max Inventory Storage v1.04.02 is the test case — 10 of 10 patches apply now where 0 of 9 applied before. Thanks to ecbrown777 and jeffersonalves71 on GitHub.",
            "<b>CDUMM no longer fails to start if a previous launch crashed without cleanup.</b> Sshuvzz reported \"logo flash for 2 seconds, then nothing\" even after restarting the PC. CDUMM was checking a leftover lock file but didn't notice the recorded process was already dead — so launches kept thinking another instance was running. Now CDUMM checks if the recorded process is actually alive; if it's dead or the file is empty/corrupt, the lock is treated as stale and CDUMM acquires it cleanly. Real running instances still block as before. Thanks to Sshuvzz on Nexus.",
            "<b>NattKh-style mods with multi-word field names now apply.</b> Mods exported from CrimsonGameMods using snake-case field names like <i>gimmick_info</i> or <i>item_charge_type</i> were silently rejected because the game's internal schema uses camelCase (<i>_gimmickInfo</i>, <i>_itemChargeType</i>). CDUMM now bridges the two case styles in both the validator and the writer. Earlier short names like <i>cooltime</i> already worked; this completes the coverage. Thanks to Matrixz on Nexus.",
            "<b>Field-name mods with nested or list values get a clear \"coming in v3.3\" message.</b> Some NattKh exports include intents like <i>enchant_data_list = [{...}]</i> or <i>docking_child_data.gimmick_info_key</i>. Those need writer-side support that lands in v3.3 — CDUMM now says exactly that instead of the misleading \"add a field_schema entry\" message. Other intents in the same mod still apply normally. Thanks to UnLuckyLust on GitHub.",
            "<b>A mod with a malformed signature no longer takes down the apply.</b> If a mod's <i>signature</i> field has a typo, a leading \"0x\", or an odd number of hex characters, CDUMM now logs a clear warning and treats the mod as if no signature were present (absolute offsets). Before, this would crash the entire apply for that file before any patches even ran. Found during this round's systematic-debugging sweep.",
        ],
    },
    {
        "version": "3.2.4",
        "date": "2026-04-28",
        "notes": [
            "<b>BC7 texture mods no longer show as rainbow noise.</b> Mods that ship single-mip BC7 textures (common for UI elements, icons, HUD bars) were producing scrambled multi-coloured noise in-game after CDUMM applied them. CDUMM was writing the texture's 148-byte DX10 header correctly but flagging the entry as a layout the game reads with a 128-byte header — the 20-byte mismatch shifted every pixel by 20 bytes and the result looked like static. Now ships the texture bytes raw at the same layout vanilla BC7 textures use, so the game's loader reads them correctly. Thanks to BANDU on Nexus for the report.",
            "<b>Field-name JSON mods that target by short name (like \"iteminfo.pabgb\") now apply.</b> Some Format 3 mods, including exports from CrimsonGameMods v3 that target item buffs and equip data, were failing with \"could not extract vanilla bytes\" even on a clean game install with verified files. The mod's target is the file's short name; CDUMM's vanilla extraction was only matching against the full path the game stores (e.g. \"gamedata/iteminfo.pabgb\"). The lookup now tries both, so short-name targets resolve correctly. Thanks to Matrixz on Nexus.",
        ],
    },
    {
        "version": "3.2.3",
        "date": "2026-04-27",
        "notes": [
            "<b>Field-name JSON mods finally apply to your game.</b> The new modding format (the kind that says \"change <i>cooltime</i> to 0\" instead of \"change byte 1632 to <code>00 00 00 00</code>\") was announced in v3.2.1 and import-supported in v3.2.2 — but no schemas shipped, so dropping one of these mods got you a clean error message and not much else. This release is the breakthrough: six game tables are now covered, and a mod targeting one of them imports, applies, and works in your game.",
            "<b>What this unlocks.</b> Items (durability, stack size, drop rates, costs, item flags), mounts and horses (speed, stamina, jump, \"can call in safe zone\"), terrain and zones (field info, stage info, region info), and the part of character data mounts use. NoCooldownForALLItems is the test case — worked nowhere a week ago, works now. Skill data, NPC info, item pools, and other tables aren't covered yet — mods targeting those still get a clean \"no schema for this table yet\" message naming exactly what's missing, so anyone can add it. See <code>field_schema/README.md</code> next to CDUMM3.exe.",
            "<b>Why this matters going forward.</b> The new format survives game patches. When Pearl Abyss shifts bytes around in an update, byte-offset mods break and need every author to repush. Field-name mods just keep working — the field's meaning didn't change, only its location, and CDUMM tracks the location for you.",
            "<b>One broken mod can't kill your whole loadout anymore.</b> A single mod with a corrupt change used to abort the entire Apply — every other mod skipped, every patch undone. Now CDUMM skips just the broken change, tells you which mod and which entry was wrong, and applies the rest cleanly. Thanks to ZirconX1 on Nexus.",
            "<b>Multi-version mod packs let you pick which version you want.</b> Mods like CrimsonWings ship five strength levels (10%, 25%, 50%, 75%, infinite) as five files in one zip. Drop the zip, pick a level, done. Thanks to gleglezao on Nexus.",
            "<b>Uninstalling an ASI mod cleans up properly.</b> Companion files used to get left behind in your game folder if their names didn't match the .asi exactly. They're tracked at install now and removed cleanly on uninstall. Shared loader files (winmm.dll etc.) are intentionally left alone — those belong to the loader, not any one mod. Thanks to enowai on Nexus.",
            "<b>RAR mods import even if 7-Zip is in an unusual place.</b> Scoop, NanaZip, portable copies, custom paths — all detected via the Windows registry now, not just the standard Program Files location. Thanks to femdogga on Nexus.",
            "<b>Multi-language mod packs install correctly.</b> Mods that ship one zip per language inside a parent zip used to fail with \"no recognized format\". They unpack automatically now. Thanks to femdogga on Nexus.",
            "<b>Updates land on the right card.</b> Clicking \"Mod Manager Download\" to update a renamed mod no longer creates a duplicate card next to the old one.",
            "<b>No more confusing \"crash\" log when you close CDUMM normally.</b> Closing the app the usual way was leaving behind a crash-pre-qt.log file every time, even when nothing actually crashed. Real crashes still get logged.",
            "<b>The configuration gear only shows up when there's a real choice to make.</b> Mods with nothing to configure no longer display an empty cog when you click it. Variant pickers and preset choices still get the gear as before.",
            "<b>For mod authors: a new \"apply what fits, skip what doesn't\" flag.</b> A mod that's mostly compatible — only a handful of values drifted in a recent game patch — can now ship <code>\"allow_partial_apply\": true</code> at the top of its JSON. CDUMM applies the verified changes, skips the mismatched ones with a clear log, and tells the user. Without the flag, mismatches still reject so a half-broken mod can't crash your game silently. Thanks to XxDman10311xX on Nexus (Refinement Cost Reforged).",
            "<b>Heads up: did a Format 3 mod fail before?</b> Try reimporting it. If it targets one of the six covered tables, it'll work now. If you get a \"no schema\" message, that's honest — the table just isn't covered yet, and the message names what's missing. Existing v2 byte-offset JSON mods are unaffected.",
        ],
    },
    {
        "version": "3.2.2",
        "date": "2026-04-26",
        "notes": [
            "<b>Crimson Browser format mods stop spamming the warning bar.</b> Mods like r457 Graphics Tweaks were producing 30+ identical 'corrupt archive' warnings on every Apply, all bogus. CDUMM was trying to parse the mod's binary patch deltas as full index files and choking on the bytes. The mods themselves were never broken, just the false alarm. Both fixed: the false alarm is gone and the duplicate-warning flood is capped at one per mod. Thanks to Richardker2545, DerBambusbjoern, and Giony on Nexus for the report.",
            "<b>Update detection actually works for mods with multiple file versions on the page.</b> Some mods host several versioned files at once (Fat Stacks had v1 and v2 sitting side-by-side on its Nexus page). CDUMM was latching onto the wrong file at import and then forever after reporting 'up to date' even when a real update existed. Fully fixed end-to-end: the matcher now picks the file that matches your version, the engine self-corrects when it finds a previously-wrong file ID stored, and the database actually overwrites instead of silently keeping the bad value.",
            "<b>NattKh-style Format 3 mods import even when wrapped in a ZIP or folder.</b> The new field-name JSON format used to fail import if the .json was inside a zip or shipped in a folder. Drop them as-is now and they route through the apply pipeline correctly. The Inspect Mod dialog also recognizes them properly instead of saying 'no recognized mod format'.",
            "<b>Update check fires the moment CDUMM opens.</b> No more 5-second wait to see whether anything's outdated. The check now posts immediately and surfaces results as soon as Nexus responds.",
            "<b>The CDUMM auto-updater no longer rare-crashes on close-and-reopen.</b> A Qt threading edge case could crash the Check For Updates worker if the previous one had been cleaned up out from under it. Defensive fix added. Thanks to priston201 on GitHub for the bug report.",
            "<b>Mod-config sidebar shows long option names without cutting them off.</b> Configs like 'Insect_Collect_Cooldown' or 'Plant_Collect_Bandit' no longer get clipped to '...'. Sidebar bumped from 400 to 520 pixels. Thanks to nknwn on GitHub for the report.",
            "<b>Nexus update-check is more resilient to weird API responses.</b> When Nexus returns null timestamps, missing fields, or one bad entry mixed in with good ones, the update check now silently skips the bad entries instead of dropping the whole feed. Mostly invisible to you — its job is to keep the red 'click to update' pill working consistently.",
        ],
    },
    {
        "version": "3.2.1",
        "date": "2026-04-26",
        "notes": [
            "<b>Dark Mode Map (and any CSS mod) stops crashing the game.</b> If you had a mod modifying any <code>.css</code>, <code>.html</code>, or <code>.js</code> file — Dark Mode Map being the famous one — opening the screen that used the modded file would crash you to desktop. CDUMM was forgetting to encrypt the file when packing it back up, and the game's loader couldn't read it. Fixed. Same fix v2.1.2 originally landed and the v3.0 rewrite quietly broke. Thanks to TheUnLuckyOnes on Nexus for the bug report.",
            "<b>Your shop and inventory mods stack properly now.</b> If you had two mods touching the same shop or inventory file — one tweaks prices, the other adds stock — only one was silently winning since v3.0. You probably never knew. Both now combine like they should. Some of your mods that looked broken? They were. They're not anymore.",
            "<b>You can finally see which mod got dropped in a conflict.</b> When two mods clash and one has to lose, the warning used to say '1 mod was dropped' with no name. Now: <i>Active: ModA. Dropped: ModB.</i> Want to swap them? Drag ModB to the top of the load order. Done.",
            "<b>Apply stops gaslighting you.</b> If you hit Apply and the game looks identical to before, CDUMM now tells you exactly why — 'Mod X did nothing because the value was out of range' or 'no schema for this table yet'. No more 'I applied, why didn't anything change?' guesswork.",
            "<b>Find Culprit works again.</b> The 'when 20 mods are on and the game crashes, which one broke it?' tool was broken on click in v3.2. Fixed. Now you can bisect properly when something goes sideways.",
            "<b>The update banner stops nagging you.</b> The 'CDUMM vX.Y is available' strip at the top of the window has an X button now. Click to dismiss until the next release lands. The badge stays in the sidebar so you can find the update later if you change your mind.",
            "<b>NattKh's new mod format opens up.</b> NattKh's GameMods recently switched to a new JSON style — the kind that says 'change <i>price</i> to 100' instead of 'change byte 24 to <code>64 00 00 00</code>'. CDUMM v3.2.1 understands the format and routes these mods through the apply pipeline, including when they're zipped or in a folder. <b>Caveat:</b> for any specific mod to actually change bytes, CDUMM needs a 'field_schema' file mapping that mod's field names to byte locations. <b>None ship in v3.2.1.</b> If you drop one of these mods today, you'll get a clean 'here's exactly what's missing' message that tells the mod author what to add, instead of a silent failure or 'unsupported format' error. Once schemas start landing (community-authored, see <code>field_schema/README.md</code> next to CDUMM3.exe), more of NattKh's mods will Just Work.",
            "<b>Game-folder auto-detect is more reliable.</b> If your Crimson Desert install lives on a secondary Steam library (e.g. <code>F:/Steam/...</code>) that Steam itself never properly registered, CDUMM used to ask you to pick the folder manually. Now it scans the common Steam library locations across all your drives directly, so the folder picker only appears as a fallback. Thanks to Feikaz on GitHub for the report.",
            "<b>Used Format 3 mods on v3.2? You need to reimport them.</b> Old v3.2 imports stored a placeholder, not the real mod data. Right-click each → Reimport from source. New imports after this update are fine. Existing v2-format JSON mods are completely unaffected.",
            "<b>Quiet protections for the new format.</b> When mod authors ship buggy schemas (and they will), CDUMM refuses to guess where to write, won't spill bytes into the wrong record, and rejects malformed schema files at load time. You won't see this — it's protection that runs silently when needed.",
        ],
    },
    {
        "version": "3.2",
        "date": "2026-04-25",
        "notes": [
            "<b>NexusMods is now built in.</b> Sign in once and CDUMM "
            "keeps an eye on every mod you have installed. When an "
            "author pushes an update you see a red badge on the card. "
            "Click \"Mod Manager Download\" on a Nexus page and it "
            "lands straight in CDUMM. This is the headline feature.",
            "<b>One-click sign-in to Nexus.</b> Open Settings, hit "
            "\"Login with Nexus\", browser pops up, you confirm it is "
            "you, done. No API keys to copy and paste. CDUMM never "
            "sees your password.",
            "<b>CDUMM tells you when your mods need updating.</b> "
            "Every 30 minutes CDUMM quietly checks Nexus for new "
            "versions of the mods you have installed. Outdated mods "
            "get a bright red \"Click To Update\" badge. Up-to-date "
            "mods get a quiet green check.",
            "<b>Download from Nexus with one click.</b> On any mod's "
            "Nexus page, the \"Mod Manager Download\" button now "
            "sends the file straight to CDUMM. Premium members get "
            "one-click imports; free members get sent to the right "
            "Files tab.",
            "<b>Game updates can't break your day anymore.</b> Steam "
            "patches Crimson Desert overnight, your mods don't work "
            "in the morning? CDUMM now catches that on launch and "
            "offers a Start Recovery button. One click runs the whole "
            "repair: verify your game files, regenerate every mod, "
            "reapply them. Live progress bar. Cancel any time.",
            "<b>Apply is way faster.</b> Two engine rewrites: conflict "
            "detection is near-instant on big mod sets, and merging "
            "mod changes runs hundreds of times faster. Files that "
            "took minutes now take seconds. Real progress bar — no "
            "more frozen 95% mystery.",
            "<b>Cleaner Settings page.</b> Login is the recommended "
            "flow now and it leads. Manual API key paste is still "
            "there for power users, tucked behind an Advanced toggle.",
            "<b>Mods stop multiplying during Recovery.</b> Some mods "
            "ship multiple presets in one archive (Glider Stamina with "
            "5 options, Infinite Horse with 9). Recovery used to "
            "clone each preset into its own card. One mod is one mod "
            "again.",
            "<b>No more black command-prompt windows flashing</b> "
            "while CDUMM is working.",
            "<b>No more false \"corrupt PAMT\" warning</b> popping up "
            "after every Apply when nothing was wrong (rolled in from "
            "the unreleased v3.1.7.2 hotfix).",
            "<b>Recovery doesn't false-alarm.</b> A bug was making "
            "the Recovery banner appear on every launch right after a "
            "successful Apply. Fixed.",
            "<b>Cleaner error messages.</b> When a mod fails you see "
            "which mod failed instead of a generic placeholder.",
            "<b>ASI plugin polish.</b> Uninstalling cleans up sidecar "
            "files. If an author renames their .asi between versions, "
            "CDUMM removes the old one. Outdated badge clears the "
            "moment you update a plugin.",
        ],
    },
    {
        "version": "3.1.7.2",
        "date": "2026-04-24",
        "notes": [
            "Hotfix: v3.1.7 / v3.1.7.1 showed an \"Apply Completed "
            "with Warnings\" banner after every Apply saying your "
            "mods had corrupt PAMT files, even when the Apply itself "
            "worked. The precheck that produced that banner was "
            "examining the wrong file (the binary patch, not the "
            "reconstructed PAMT) and false-flagging every valid mod. "
            "The precheck is now disabled; the actual import and "
            "apply flows still catch truly corrupt files. The banner "
            "should not appear anymore.",
        ],
    },
    {
        "version": "3.1.7.1",
        "date": "2026-04-24",
        "notes": [
            "Hotfix: v3.1.7 was rejecting some mods at import time "
            "with a \"corrupt PAMT\" error that mentioned a weird "
            "temp-file name (e.g. invalid literal for int() with "
            "base 10: 'tmp9wk9wy2h'). The mods weren't actually "
            "corrupt — CDUMM's new import-time PAMT validator was "
            "writing to a randomly-named temp file that confused "
            "the parser into flagging valid files. Fixed by using "
            "the mod's real PAMT filename for validation. Affected "
            "mods should re-import cleanly now.",
        ],
    },
    {
        "version": "3.1.7",
        "date": "2026-04-24",
        "notes": [
            "Apply is now locked when CDUMM detects Crimson Desert "
            "has been updated but you haven't rescanned yet. Applying "
            "mods on a stale baseline is what caused most of the "
            "recent \"stuck at 2%\" reports — patches land on the "
            "wrong bytes and mods silently fail. You'll see a clear "
            "banner telling you to run Rescan Game Files; once the "
            "rescan finishes, Apply unlocks automatically.",
            "If your game is installed under Program Files, CDUMM "
            "now shows a persistent warning banner every session "
            "instead of a one-time dialog you'd forget about. "
            "Windows restricts writes under Program Files, which "
            "causes silent mod failures. Move your Steam library to "
            "a different drive (e.g. D:\\SteamLibrary) to resolve it "
            "permanently.",
            "Rescan Game Files now refuses to run when the live "
            "game files don't match your vanilla backups. Before "
            "this, if the disk was modded (from a stale backup, a "
            "silent Steam patch, etc.), rescan would happily hash "
            "the modded bytes as \"vanilla\" — and every future "
            "Revert would restore the wrong state forever. CDUMM "
            "now detects the mismatch, blocks the rescan, and tells "
            "you exactly what to do (Revert first, or Steam Verify "
            "then Fix Everything with the verified option).",
            "Fix Everything no longer overwrites a just-verified "
            "game. When you pick the \"Steam verified\" option, the "
            "revert step is now skipped — your clean Steam-repaired "
            "files stay untouched, vanilla backups are wiped, and a "
            "fresh snapshot captures the real game state. Before "
            "this fix, Fix Everything's revert would paste stale "
            "pre-update backups over Steam's freshly downloaded "
            "files, which is why \"40 files reacquired\" kept "
            "coming back every time.",
            "Right-click a mod (or multi-select several) and pick "
            "\"Reimport from source\" to regenerate patches against "
            "the current game without re-dragging each zip. After a "
            "Steam update this is the fastest way to unstick every "
            "mod that silently stopped working. The mod's priority, "
            "notes, enabled state, and everything else you set on "
            "it are preserved.",
            "Fixed the Post-Apply Verification dialog falsely "
            "reporting dozens of mods as \"imported on a different "
            "game version\". Two bugs rolled together: (1) the "
            "version fingerprint used to change every apply/revert "
            "cycle (now stable, based only on Steam build ID + game "
            "exe); (2) mods kept the fingerprint from their import "
            "moment and were never re-stamped even after working on "
            "a newer game version. Successful apply now stamps "
            "every enabled mod with the current game version, so "
            "the \"outdated\" signal self-heals after a Steam patch "
            "if your mods still work. Existing installs are "
            "auto-migrated on first launch.",
            "CDUMM no longer gets stuck when a mod's files are damaged. "
            "If a mod archive is corrupt or the apply step stops "
            "making progress, CDUMM now stops the operation and tells "
            "you which mod caused it instead of sitting frozen on a "
            "progress bar.",
            "The vanilla-backup step shows per-file progress now, so "
            "the progress bar actually moves through that phase "
            "instead of sitting at 2% for the whole run.",
            "Importing a mod now checks the mod's internal index "
            "files up front. A damaged archive gets rejected at "
            "import time with a clear message instead of silently "
            "failing every time you try to apply.",
            "Folder imports that contain both mesh/texture data and "
            "sibling .json files (e.g. Character Creator preset "
            "folders with an extra animations JSON inside) now "
            "import BOTH parts instead of only the .json. Thanks to "
            "kori228 on GitHub for the report.",
            "Compatibility improvements for mod packaging.",
            "Improved handling of edge case files in mods.",
        ],
    },
    {
        "version": "3.1.6",
        "date": "2026-04-23",
        "notes": [
            "Fixed the Program Files warning dialog that was telling users to move their Steam library to C:\\SteamLibrary. Steam only allows one library per drive, so if the game is already on C: you can't make a second library there. The dialog now says to use a different drive (e.g. D:\\SteamLibrary) and explains why Steam would reject a second folder on C:. Thanks to 1Phase1 on Nexus for catching this.",
        ],
    },
    {
        "version": "3.1.5",
        "date": "2026-04-23",
        "notes": [
            "Texture mods render correctly in game again. Icons from mods like Bigger Minimap Tweaks were being written into the overlay without a specific header the game expects, so enemies and dropped items showed as blank on the minimap. Those render again. Thanks to RoninWoof and AvariceHnt for the bug reports.",
            "Apply is about 4x faster on typical loadouts. An Apply run with 33 mods that used to take 35 seconds now finishes in about 9. CDUMM skips rewriting files already at vanilla state, and skips a small but repeated texture table write that was running on every apply even when nothing changed.",
            "Texture mods now work correctly on first install. A leftover issue from v3.0.0 was writing a garbage value into the game's texture table every apply, so some textures could fail to load cleanly. The table is now written exactly as the game expects, byte for byte.",
            "Fix Everything runs the revert step correctly instead of crashing partway through with a signal error.",
        ],
    },
    {
        "version": "3.1.4",
        "date": "2026-04-22",
        "notes": [
            "ReShade integration — new sidebar page detects whether ReShade is installed next to CrimsonDesert.exe. Distinguishes ReShade vs ReShade + Add-on Support, detects addons like RenoDX.",
            "ReShade preset picker — activate, revert last switch, import preset from disk, soft-hide presets from the list. Blocks activation while the game is running so ReShade doesn't overwrite your change on exit.",
            "ReShade preset merging — pick a main preset and layer pieces from another. Categorised UI (New / Existing / Advanced), collapsible groups, Select-all per category, tick preservation across 'Include advanced' rebuilds.",
            "Right-click 'Open source files' on any mod card opens its source folder in Explorer.",
            "Linux support via Wine (BETA — needs more testing). Bundled launcher script scripts/cdumm-linux.sh and LINUX.md install guide. Smoke-tested on Ubuntu 24.04 with Wine 11 + vcrun2022 + corefonts. Please report what works.",
            "Settings → Game → Manage ASI loader (winmm.dll) toggle. Default on. Turn off if OptiScaler or another tool already ships its own winmm proxy so CDUMM stops stomping on it.",
            "Packed JSON mod regression fix. v3.1.3 could silently drop zip-imported JSON mods when the cross-mod aggregator couldn't cover them. The ENTR delta now stays in play as a fallback so the mod applies either way.",
            "Crimson Browser handler now walks sibling Tex/ directories. CrimsonForge-generated mesh mods (Berserk Dragon Slayer, etc.) ship textures in Tex/ outside files/ — previously silently dropped.",
            "Config panel indicator moved left of label so the checkbox stays visible when the side panel gets clipped on narrow windows.",
            "ReShade merge dialog polish — scroll overflow, background bleed, category headers, Select-all button, tick preservation across toggle rebuilds, wider/taller default.",
            "15-second ReShade page startup hang fixed on installs with huge bin64 subdirectories. Flat-scan preset enumeration plus name-only process detection (10–200× faster).",
            "ReShade audit fixes — atomic preset writes, subfolder preset support, relative BasePath resolution, Windows-safe hidden state, INI key casing preserved.",
            "README clarifies 'every platform' means every store (Steam / Epic / Xbox), not every OS. macOS isn't supported natively.",
        ],
    },
    {
        "version": "3.1.3",
        "date": "2026-04-20",
        "notes": [
            "Same-file conflicts now combine. Two JSON mods editing the same file (Fat Stacks + ExtraSockets) no longer silently drop each other — all enabled JSON mods' patches feed through one patching pass so size-changing inserts work correctly across mod boundaries. After updating, uninstall and re-import any mod pair that modifies the same file.",
            "Apply no longer hangs at 7%. Thousands of per-byte-overlap INFO log lines were saturating the subprocess pipe; those are now DEBUG.",
            "Apply no longer crashes on partial-compressed textures. zlib Error -3 while reading PATHC now downgrades to a soft warning with a 'Run Fix Everything' hint instead of killing the whole apply.",
            "Bug Report stops lying. The byte-verification summary now uses the same counting as the import rejection — no more '0 mismatched' on the report while import says 'N don't match'.",
            "Silent launch crash catcher. If CDUMM dies before the main window paints, you now get crash-pre-qt.log in %LOCALAPPDATA%\\cdumm or %TEMP% to paste into a bug report.",
            "Revert button tooltip now spells out that your save games are not touched.",
            "Cross-layer PAZ + JSON merge. A PAZ-directory mod that ships its own copy of a file now layers correctly with a JSON mod patching the same file.",
            "'Cdumm Variant XXXX' name leak defense — the prettifier refuses to surface internal temp-dir stems as mod titles.",
        ],
    },
    {
        "version": "3.1.1",
        "date": "2026-04-19",
        "notes": [
            "Batch import 60% faster thanks to a Rust native module for the hot loops (PAMT parsing, sparse delta scanning, ENTR byte comparison).",
            "Nested folder-variant mods work end to end. Mods like Character Creator with Female/Male at the top and Goblin/Human/Orc inside now show a second picker, render per-axis radio groups in the cog, and install bundled ASI plugins alongside the picked variant.",
            "Kliff Wears Damiane-style mods no longer crash the game. Half-patched data tables are now refused at import with a clear 'incompatible with your game version' message.",
            "ExtraSockets and Rings/Earrings Abyss Sockets work: generic name-offset resolver for iteminfo.pabgb.",
            "New compatibility: ExtraSockets (1274), Rings and Earrings Abyss Sockets (1379), Character Creator (837), Barber Unlocked 4.0 (591), Vaxis LoD Improvements (733), Faster Vanilla Animations Trimmer (774).",
            "Cog icon moved next to the mod name, outdated summary tile, NEW badge + auto-sort for freshly imported mods, grid-grouped radio picker for multi-axis variants, follow-Windows-theme live (no restart).",
            "Brazilian Portuguese translation complete.",
            "Many smaller fixes: uninstall crash on bare-hex JSON offsets, RAR support in main import, closeEvent crash on exit, PrivateBin bundled so Bug Report uploads actually work.",
        ],
    },
    {
        "version": "3.0.4",
        "date": "2026-04-17",
        "notes": [
            "JSON mods apply reliably now. Root cause was large vanilla archives (like ~100MB 0008/0.paz) exceeding the backup size threshold — they never got saved so mount-time extraction failed silently while reporting 'apply successful'. CDUMM now dynamically discovers which archives your enabled JSON mods target and always backs those up, with hash-verified live fallback and clear error banners if neither source is usable.",
            "Texture mods finally work. Barber Unlocked, Character Creator, Stone Floor Textures Overhaul and similar mods now produce a full PAZ + PAMT + PATHC overlay instead of only patching the PATHC index.",
            "Conflicts dialog redesigned: 'Needs attention' vs 'Auto-resolved' split, in-dialog reordering, tooltips make the #1-wins rule unambiguous, resizable Fluent dialog.",
            "Import improvements: script mods in zips import without manual extraction, multi-part mods (Trust Me + Pet Abyss Gear) become separate rows, Xbox Game Pass installs under C:/XboxGames/ auto-detect, strict duplicate detection with an Update/Add-as-new/Cancel dialog, clearer 'no recognized game files' errors.",
            "Version tracking: Nexus filename parser reads versions from both Nexus-formatted names and free-form names; backfills versions for existing mods on first startup.",
            "Dark mode stays dark on Windows theme changes and wallpaper slideshows.",
            "Bug reports, mod-list exports and compatibility reports now default to Documents/CDUMM/ instead of the launch folder.",
            "Fixed startup crash on Python 3.13, Linux launch crash, Steam/Xbox protocol-handler launch.",
        ],
    },
    {
        "version": "3.0.1",
        "date": "2026-04-17",
        "notes": [
            "Settings page redesigned: Bug Report section now uses a proper Fluent card group with icons, titles, and inline descriptions — no more floating labels or empty vertical gaps.",
            "Settings status messages use theme-aware icon badges instead of hardcoded hex colors — readable in both light and dark themes.",
            "Conflicts dialog redesigned: Fluent header with count badge, cleaner tree rhythm, scannable load-order cards with rank pills.",
            "Conflicts dialog: theme-aware surface colors, no more raw Qt chrome.",
            "Broad format-support pass: DDS partial payload, PATHC hierarchical paths, PABGH fixup after inserts, XML patch/merge with identity keys, language redirect + PAMT .paloc rewrite, compiled-file byte-merge fallback, ChaCha20 overlay re-encryption.",
            "Texture mods now render correctly (Enhanced Map Icons, Barber Unlocked, Kliff Wears Damiane hair).",
            "Multi-variant JSON mods: one card per drop with cog switcher, conflict-group radio selection, title wraps correctly in dark theme.",
            "Miki990 UX fixes: rename propagation, conflict re-detect on reorder, long-name wrap, multi-drop crash-guards, dedicated conflict-order dialog.",
            "Linux startup crash fixed, Steam/Xbox launch fixed.",
        ],
    },
    {
        "version": "3.0.0",
        "date": "2026-04-16",
        "notes": [
            "New look: completely redesigned interface with card-based mod list, folder groups, and side panel.",
            "First-time welcome wizard: pick your language, theme, and game folder in a guided setup.",
            "Batch import: drop many mods at once and they all import in a single fast pass.",
            "ASI mods auto-detected when dropped alongside PAZ mods and installed to the right place.",
            "Configurable mods with multiple presets now show a picker dialog.",
            "Folder variant mods now prompt you to choose which variant to install.",
            "Mod names are automatically cleaned up for readability.",
            "ASI plugins now show version numbers from folder names and modinfo.",
            "Smaller app size: removed unused libraries, optimized build.",
            "More mods just work out of the box: better handling of sound mods, XML replacements, and hand-edited files.",
            "Override mode: mod authors can declare conflict winners in modinfo.json.",
            "Correct compression type for new files added by mods (DDS textures, soundbanks).",
            "Full app translation: all UI text can now be translated, not just the main pages.",
            "Steam, Xbox, and Epic Games auto-detection with store logos in setup.",
            "8 new languages: Italian, Polish, Russian, Turkish, Japanese, Simplified Chinese, Ukrainian, Indonesian.",
        ],
    },
    {
        "version": "2.5.0",
        "date": "2026-04-13",
        "notes": [
            "Performance: Apply is dramatically faster — core operations now run through native compiled code.",
            "JSON mod import is now instant (mount-time patching).",
            "Mods now survive game updates without needing to reimport.",
            "Per-patch toggle: right-click a JSON mod to enable/disable individual changes.",
            "Find Problem Mod now includes ASI plugins alongside PAZ mods.",
            "DDS texture mods now register in PATHC for correct in-game rendering.",
            "Added support for modinfo.json + files/ mod format (CrimsonSaveEditor exports).",
            "PABGH auto-included alongside PABGB entries in overlay.",
            "Font, audio, and video mod support.",
            "Fixed DDS repack errors, conflict detector threshold, transactional I/O edge case.",
            "6 new translations: German (by HaZt), Spanish, French, Korean, Portuguese (BR), Chinese (TW).",
        ],
    },
    {
        "version": "2.4.4",
        "date": "2026-04-13",
        "notes": [
            "Added format 2 JSON mod support: hex offsets and insert operations. Mods like Kliff Wears Damiane that use hex offsets (e.g. '12079F') and insert operations now import correctly.",
            "Fixed hex offset parsing in all code paths (import, apply, collision detection).",
            "Single instance: only one CDUMM GUI window can run at a time. Opening a second brings the existing window to front.",
        ],
    },
    {
        "version": "2.4.3",
        "date": "2026-04-12",
        "notes": [
            "Fixed KeyError on JSON mods with incomplete change entries (missing 'patched' or 'offset' fields). Changes are now skipped gracefully instead of crashing the import.",
        ],
    },
    {
        "version": "2.4.2",
        "date": "2026-04-12",
        "notes": [
            "Fixed import crash on JSON mods with string offsets (e.g. Taller Damiane). Mod authors writing \"offset\": \"107\" instead of \"offset\": 107 now works.",
            "Added crimson_sharp_mod_v1 manifest format support (e.g. Silver Fang Boss Size). Both files_dir and patches_dir are handled.",
            "Database watcher: CDUMM UI auto-refreshes when mods are toggled by external tools.",
        ],
    },
    {
        "version": "2.4.1",
        "date": "2026-04-12",
        "notes": [
            "Fixed Apply silently skipping outdated mods. All enabled mods now apply regardless of game version. The 'outdated' status label remains as an informational warning only.",
        ],
    },
    {
        "version": "2.4.0",
        "date": "2026-04-12",
        "notes": [
            "Semantic diffing and merging: PABGB binary records are now parsed at the field level using community schemas (322 tables, 3700+ fields). Mods that change different fields in the same record are automatically merged instead of conflicting.",
            "Field-level conflict detection: conflicts now show exactly which fields each mod changes (e.g. 'Mod A changes maxSlot, Mod B changes defaultSlot — compatible') instead of generic byte-range overlaps.",
            "Offset collision detection for JSON byte-patch mods: overlapping byte ranges across mods are detected and reported before Apply.",
            "Semantic merge during Apply: when multiple mods modify the same PABGB entry, CDUMM attempts a three-way merge at the field level. Falls back to byte-level for unknown formats.",
            "Conflict resolutions persisted in database: user decisions on field-level conflicts survive across sessions.",
            "Light and dark theme: toggle between warm-neutral light mode and refined dark mode in Tools. Theme preference saved.",
            "ASI panel: enable/disable now refreshes the table immediately. ASI Loader auto-updated via SHA-512 hash comparison on every refresh.",
            "Fixed startup crash (NameError on COL_NOTES) in column auto-sizing when Notes column was added.",
        ],
    },
    {
        "version": "2.3.1",
        "date": "2026-04-11",
        "notes": [
            "Outdated mod detection: mods imported for an older game version show amber 'outdated' status. Combined statuses: 'active (outdated)', 'disabled (outdated)'.",
            "Apply skips outdated mods automatically. When only 1 outdated mod is enabled, you get the option to force apply it to test.",
            "Post-update validation: after a game update, CDUMM checks each mod's entry metadata against the new game data and notes what broke.",
            "Version mismatch error: importing incompatible JSON byte-patch mods now shows a clear 'mod is incompatible' message instead of 'changes already present'.",
            "Vanilla directories (0000-0035) always use ENTR decomposition even when PAMT sizes differ after a game update. Prevents 900MB full PAZ copies.",
            "Reimport from source: right-click a mod to reimport from stored source files against the current game version.",
            "User notes: new Notes column, right-click to add/edit notes per mod. Notes shown in details dialog and as tooltips.",
            "Fixed Qt crash on startup caused by 237+ conflicts in the conflict tree view (signal storm during batch model rebuild).",
            "Revert no longer fails with 'No vanilla backups found' for overlay mods (ENTR-only mods don't need backup restoration).",
            "Auto-sized columns: all columns auto-fit to their widest content.",
            "Updated bundled ASI Loader with SHA-512 hash-based auto-update.",
        ],
    },
    {
        "version": "2.3.0",
        "date": "2026-04-10",
        "notes": [
            "Apply now blocked when game files don't match vanilla snapshot. Prevents contaminated backups that poison the restore chain. Shows a clear error with instructions to verify through Steam and Fix Everything.",
            "Fix Everything no longer deletes vanilla backups when you say No to Steam verify. Backups are only cleared after a confirmed Steam verification.",
            "Backup refresh now hash-verifies game files against the snapshot before overwriting existing backups. Prevents modded files from replacing clean backups.",
        ],
    },
    {
        "version": "2.2.9",
        "date": "2026-04-10",
        "notes": [
            "Fixed 'not applied' status for mods that modify game files without a vanilla snapshot. The status check now detects applied mods by the presence of vanilla backups. Fixes Barber Unlocked and similar mods showing 'not applied' even though they were correctly applied.",
        ],
    },
    {
        "version": "2.2.8",
        "date": "2026-04-10",
        "notes": [
            "Hotfix: restricted mixed-format import to only trigger for mods with standalone PAZ directories alongside loose files. Prevents false triggers on normal mods with README/config files.",
            "Narrowed loose file detection to .json and .xml only (removed .txt, .ini, .cfg, .csv that matched non-game files).",
            "Fixed source archive being overwritten during second import pass.",
        ],
    },
    {
        "version": "2.2.7",
        "date": "2026-04-10",
        "notes": [
            "Force in-place mode: mod authors can set \"force_inplace\": true in modinfo.json to bypass the overlay system. Fixes mods like HAWT that need to replace vanilla entries in game directories that use merge behavior (e.g., conditionalpartprefab).",
            "Mixed-format mod import: mods that ship both standalone PAZ directories and loose game files (JSON, XML) are now fully imported. Previously the loose files were silently dropped. Fixes Character Creator Female and similar mods.",
        ],
    },
    {
        "version": "2.2.6",
        "date": "2026-04-09",
        "notes": [
            "Multi-select enable/disable for PAZ mods. Select multiple mods with Ctrl+click or Shift+click, right-click to enable or disable them all at once.",
            "Multi-select enable/disable for ASI plugins. Same bulk toggle support in the ASI Plugins tab.",
            "Ctrl+A to select all mods in both PAZ and ASI tabs.",
        ],
    },
    {
        "version": "2.2.5",
        "date": "2026-04-09",
        "notes": [
            "Auto-fix XML formatting on import. Mods with broken XML (missing UTF-8 BOM, added XML declaration, LF line endings) are automatically corrected to match what the game expects. Fixes VAXIS Dynamic Ragdolls and similar mods that crashed the game.",
            "ASI mods in 7z archives now detected and installed correctly.",
        ],
    },
    {
        "version": "2.2.4",
        "date": "2026-04-09",
        "notes": [
            "Fixed standalone PAZ mods (like Proper 3rd Person Camera) being incorrectly detected as loose-file mods. Numbered directories containing 0.paz are now correctly routed to the PAZ import path.",
        ],
    },
    {
        "version": "2.2.3",
        "date": "2026-04-08",
        "notes": [
            "DX10 multi-mip DDS textures (BC7 with mipmaps) now handled correctly. Written raw to overlay without inner LZ4 compression. Fixes Enhanced Map Icons and similar mods using DX10/BC7 format.",
            "Standard DDS textures (DXT1/DXT5 single mip) continue using inner LZ4 compression. Both formats work together in the same overlay.",
            "Vanilla backup gap closed. All game files are now backed up before modification, even if they don't match the snapshot. A potentially dirty backup is better than no backup.",
            "Revert to Vanilla button restored in the action bar.",
            "DDS decompression fallback: if all LZ4 decompression attempts fail, raw DDS data is returned instead of crashing. Handles DX10 raw passthrough entries during import.",
        ],
    },
    {
        "version": "2.2.2",
        "date": "2026-04-08",
        "notes": [
            "Localization system: language selector in Tools page. English and Arabic included. Translators can contribute by copying en.json to their language code. RTL layout support for Arabic.",
            "CDUMM minimizes to taskbar when Launch Game is clicked.",
            "Fixed text clipping on Tools page header and combo boxes (descenders cut off).",
        ],
    },
    {
        "version": "2.2.1",
        "date": "2026-04-08",
        "notes": [
            "Bare numbered directory mods now recognized. Mods that ship game files directly in NNNN/ directories without a files/ wrapper or mod.json (e.g. VAXIS Blood Mod with 0010/actionchart/...) are now detected and imported correctly.",
            "Fixed PAPGT stale hash causing game crash. The PAPGT rebuild now verifies PAMT hashes against disk for small PAMTs (<2MB), catching stale hashes from previous in-place applies or other mod managers without memory issues.",
        ],
    },
    {
        "version": "2.2.0",
        "date": "2026-04-07",
        "notes": [
            "Removed auto-update download for NexusMods TOS compliance. CDUMM no longer downloads executables from the internet. Update checks still work: when a new version is found, you are prompted to open the GitHub releases page in your browser to download manually.",
        ],
    },
    {
        "version": "2.1.9",
        "date": "2026-04-07",
        "notes": [
            "Fixed overlay PAZ entries not being found by the game. The overlay PAMT was using flattened folder paths (e.g. sequencer/) instead of the full hierarchical paths the game uses for lookups (e.g. sequencer/baseseq/gamesystemfx/ui/). Full paths are now resolved from the vanilla PAMT's folder tree.",
            "Fixed overlay PAMT format: corrected folder record structure and file record prefix to match vanilla format exactly.",
            "Fixed overlay directory collision with standalone mods. Overlay now allocates a directory number that doesn't conflict with any staged mod directories.",
            "Fixed standalone PAZ mods with mod.json being incorrectly detected as loose-file mods. Mods with numbered directories containing 0.paz are now correctly routed to the PAZ import path.",
            "Fixed PAPGT crash during rebuild when reading all PAMTs from disk in the worker thread.",
        ],
    },
    {
        "version": "2.1.8",
        "date": "2026-04-07",
        "notes": [
            "Overlay PAZ system: ENTR delta mods now write to a fresh overlay directory instead of modifying original game files. Original PAZ and PAMT files stay vanilla. The game loads modded entries from the overlay, which improves stability and makes revert cleaner.",
            "DDS texture mod support: fixed type 0x01 DDS split handling. The compression check no longer relies on comp_size vs orig_size (which is always equal for DDS split). DDS headers are automatically fixed with correct flags, depth, mip sizes, and format identifier. Mods like Modern Controller Icons now work correctly.",
            "DDS decompression reads inner LZ4 size from the DDS header instead of using the full padded body. Fixes decompression failures for overlay DDS entries.",
            "Removed PATHC update for DDS mods that was causing hash collisions with existing textures.",
            "PAPGT rebuild always verifies PAMT hashes against actual files on disk instead of trusting cached values from the base. Fixes stale hash mismatches after switching from in-place to overlay.",
            "CB handler uses game directory for PAZ file paths instead of vanilla backup which may be incomplete. Fixes import failures for mods in directories with partial backups.",
            "Loose file mods without a files/ directory now recognized. Mods with game paths directly next to mod.json are resolved via PAMT lookup.",
            "Automatic migration from in-place to overlay on first apply after update. PAMTs modified by previous versions are restored to vanilla before the overlay is created.",
            "Stale overlay directories from previous applies are cleaned up automatically.",
            "Mod status correctly shows active for ENTR delta mods when overlay directory exists.",
        ],
    },
    {
        "version": "2.1.7",
        "date": "2026-04-06",
        "notes": [
            "Fixed PAMT revert overwriting active mod changes. When a disabled mod shared a PAZ directory with enabled mods, the revert would restore the vanilla PAMT on top of the ENTR-updated PAMT, corrupting all active entries in that directory. PAMT revert now skips directories where enabled mods have active entry-level deltas.",
            "Fix Everything no longer requires Steam verify. Asks if you verified, takes a snapshot only if yes, proceeds either way.",
            "Contaminated ENTR deltas from pre-v2.1.6 imports are automatically cleaned on startup. Duplicate entry_paths across mods are detected and the foreign copies removed.",
        ],
    },
    {
        "version": "2.1.6",
        "date": "2026-04-06",
        "notes": [
            "Fixed disabling a mod breaking other active mods. The import handler was copying the current (modded) game PAZ instead of the vanilla backup. This caused other mods' entry changes to be attributed to the new mod. Disabling that mod would then revert all of them. Now uses vanilla backup as the base for PAZ copies and PAMT parsing.",
        ],
    },
    {
        "version": "2.1.5",
        "date": "2026-04-06",
        "notes": [
            "Loose file mods without a files/ directory now recognized. Mods that ship game file paths (gamedata/, sequencer/, ui/) directly next to mod.json are resolved via PAMT lookup. Supports mods like Skip All Loading Scene.",
        ],
    },
    {
        "version": "2.1.4",
        "date": "2026-04-06",
        "notes": [
            "Fixed removing a mod leaving game files partially modded. When a mod with entry-level deltas was removed, the PAZ was reverted but the PAMT index was not, causing corrupted game state (wrong texture sizes, missing icons, crashes). Both PAZ and PAMT are now reverted together.",
            "Fixed vanilla backups not created for PAMT and PATHC files. These files get modified during Apply by entry-level deltas and texture mods but had no backup, making revert impossible without Steam verify. Backups are now created automatically before any modification.",
        ],
    },
    {
        "version": "2.1.3",
        "date": "2026-04-06",
        "notes": [
            "Fixed false byte_range conflicts showing for mods that use entry-level deltas. Conflict panel now compares at the game file level instead of raw byte offsets. Two mods modifying different files inside the same PAZ archive correctly show as compatible.",
            "Fixed stale PAMT byte-range deltas left behind after entry-level PAZ import. PAMT files were processed before PAZ files alphabetically, creating unnecessary deltas that caused false conflict reports.",
            "Multi-variant loose file mods now detected and show a picker dialog. Mods like VAXIS Partial LOD Fix with multiple quality options (x1/x2/x3/x4/x5 folders) are recognized even when nested several folders deep inside the archive.",
            "Fixed null-byte padding crash for mods with smaller content than vanilla. XML and CSS files that are shorter than the original (common with line ending differences) no longer get padded with null bytes that crash the game's parser. PAMT orig_size is now updated to the actual content size.",
        ],
    },
    {
        "version": "2.2.0",
        "date": "2026-04-07",
        "notes": [
            "Removed auto-update download for NexusMods TOS compliance. CDUMM no longer downloads executables from the internet. Update checks still work: when a new version is found, you are prompted to open the GitHub releases page in your browser to download manually.",
        ],
    },
    {
        "version": "2.1.2",
        "date": "2026-04-06",
        "notes": [
            "Fixed game directory not updating when Steam library is moved. CDUMM now validates the saved path has the actual game exe, and auto-detects the new location if the game was moved.",
            "Fixed loose file mods in zip archives not being detected (VAXIS LOD Fix and similar mods).",
            "Auto migration no longer triggers on every version update. Only triggers when the delta format actually changes. No more unnecessary reimports that wipe working mods.",
            "Migration never clears deltas. If a mod has no stored source, its existing deltas are kept as-is instead of being destroyed.",
            "Fix Everything button in the action bar. One click to revert, clear backups, rescan, and reimport. Recommends Steam verify first.",
            "Startup health check silently auto-fixes dirty game state (orphan directories, wrong PAPGT) when no mods are enabled.",
            "Auto migration now clears old deltas before reimport. Mods with no source get disabled instead of keeping stale wrong deltas that crash the game.",
            "Fixed configurable source leak between imports. Failed imports no longer pollute the next mod's source path or configure options.",
            "Fixed source archiving for filtered JSON mods. Original source path is preserved so reimport and Configure work correctly.",
            "Configure now shows preset picker for multi-preset mods (like Trust Me variants) and auto-applies after selection. Mod name updates to reflect current configuration.",
            "Toggle picker only shows when changes target the same game file. Mods like LET ME SLEEP that patch multiple files no longer show a confusing options dialog.",
            "Smarter configurable detection. Only mods with real options (multiple bracket groups in same file, or 10+ independent changes) show the gear icon.",
            "Bare loose file mods now detected without mod.json (files/NNNN/ structure).",
            "Notifies users on startup if mods are missing source files and need reimport.",
            "Fixed orphan mod directories not cleaned up when mods disabled via Apply.",
            "Fixed stale PAPGT backup causing modded state after revert.",
            "Bare loose file mods now detected without mod.json. Mods with files/NNNN/ structure (like Enhanced Internal Graphics) import correctly.",
            "Auto migration now runs on background thread with progress dialog instead of freezing the UI.",
            "Configure now shows preset picker for multi-preset mods (like Trust Me) and auto-applies after selection.",
            "Mod name updates to reflect current configuration after reconfiguring.",
            "JSON mods now archive source files to CDMods/sources/ for auto-reimport and Configure support.",
            "Notifies users on startup if mods are missing source files and need reimport.",
            "Configurable flag only set for mods with real configurable options (not simple labeled descriptions).",
            "Rescan now clears stale vanilla backups automatically. After Steam verify, old backups from previous modded state are wiped so reverts always use clean data.",
            "Fixed JSON mods in ZIP archives using the old FULL_COPY path instead of entry level deltas. ZIP JSON mods now compose correctly like folder and standalone JSON imports.",
            "Consolidated duplicate extraction code into single decompress_entry utility.",
            "Range backup revert now verifies result against snapshot hash and warns if reconstruction may be incomplete.",
            "Stale staging directory cleaned up on startup after crash.",
            "Database entry_path index created safely after migration for old databases.",
            "JSON patch merge metadata now scoped to specific entry instead of overwriting all entries in same directory.",
            "Empty JSON imports now show error instead of creating a mod with no data.",
            "Directory assignment ceiling raised from 200 to 9999 with proper error instead of silent collision.",
            "Assigned directory numbers cleared on startup to prevent leaks across sessions.",
            "Per patch toggle now works for ALL labeled JSON mods, not just bracket prefixed ones.",
            "Fixed mods importing with 'no data' when game files are already modded.",
            "Fixed game directory not persisting between launches. The setup dialog no longer appears every time you open CDUMM.",
            "Auto migration after update. When CDUMM updates, it offers to revert and reimport all mods automatically so they use the new internal format. Mod list, enabled state, and load order are preserved.",
            "Fixed critical PAPGT crash with standalone mods (Better Minimap, Better Trade Menu, Better Inventory UI, save editors). Mod shipped PAPGTs were being parsed incorrectly, removing all 33 vanilla directories and leaving only the mod's directory. PAPGT is now always built from vanilla base with new directories discovered from disk.",
            "Update dialog only shows once per session, no more repeated popups every 15 minutes",
            "Fixed JSON import crash — preset picker had a variable rename bug that silently killed all labeled JSON imports",
            "Detects and offers to clean up stale data from old CDUMM versions in AppData",
            "Warns when game is installed under Program Files (admin restrictions can cause mod issues)",
            "Update check interval reduced to 15 minutes (was 4 hours)",
            "Epic Games Store support — CDUMM now auto-detects Crimson Desert installed via Epic",
            "Improved Xbox Game Pass detection — scans .GamingRoot drives",
            "Partially compressed textures now supported — DDS mods with split header+body compression (type 0x01) work correctly",
            "Fixed preset picker auto-selecting first option — single-patch mods with bracket-labeled variants now show radio buttons instead of checkboxes",
            "Fixed v1.8.0 regression — encryption probe was falsely marking all large entries as encrypted, causing game crashes on every mod",
            "PAZ replacement mods now decompose into entry-level deltas — mods modifying different entries in the same PAZ no longer conflict",
            "Fixed slow/laggy UI — mod list no longer queries database on every cell paint",
            "Added database indexes for mod_deltas and conflicts tables",
            "Build spec no longer has hardcoded paths — other contributors can build from source",
            "Patches with mismatched original bytes are now skipped instead of applied blindly",
            "JSON mods no longer crash when sharing a PAZ file — Trust Me + Loot Multiplier etc. now compose correctly",
            "JSON mods produce entry-level deltas instead of copying the entire 955MB PAZ",
            "Fixed Dark Map and other CSS mods crashing the game — encrypted files were being repacked without encryption",
            "Loose file mods now supported — mods with mod.json + files/ directory (e.g. Mute Vendor Music) import correctly",
            "Fixed Revert leaving files modded — range backups now accumulate when new mods touch the same file",
            "Fixed PAPGT rebuild during Revert using stale modded hashes — now recomputes from vanilla PAMTs",
            "Revert now warns if any files couldn't be restored and advises Steam Verify",
            "Preset picker shows summary instead of hundreds of change labels",
        ],
    },
    {
        "version": "1.7.1",
        "date": "2026-04-01",
        "notes": [
            "Fixed auto-update dialog not showing — users on old versions now get prompted correctly",
            "Fixed 'too many SQL variables' crash on startup for users with many mods",
            "Persistent red update banner at the bottom — stays visible until you update",
            "Critical versions (below v1.7.0) are force-updated — no option to skip",
            "Update download applies immediately — no second confirmation dialog",
            "If automatic download fails, opens the browser to GitHub releases as fallback",
            "Re-checks for updates every 4 hours for users who leave the app open",
        ],
    },
    {
        "version": "1.7.0",
        "date": "2026-04-01",
        "notes": [
            "Mods survive game updates — auto-reimported from stored sources after rescan",
            "Database moved to CDMods/ in game directory — everything in one place",
            "File hashing 16x faster with xxh3_128",
            "Verify Game State detects in-place mods (same size, different content)",
            "Stale vanilla backups auto-cleaned after Steam verify",
            "Game-running check no longer gives false positives",
            "Duplicate mods and orphaned files cleaned up on startup",
            "About tab with update indicator and links",
            "Resizable columns, readable progress bar",
        ],
    },
    {
        "version": "1.6.3",
        "date": "2026-03-31",
        "notes": [
            "Fixed decompression error when importing JSON mods on modded game files",
            "CDUMM now retries extraction with fresh offsets when vanilla offsets don't match",
            "Mods that both use directory 0036 (like PlayStation Icons + Clean Kills) now work together",
            "Each standalone mod gets its own directory and all are added to PAPGT correctly",
            "After updating: Disable all → Apply → Re-enable all → Apply",
        ],
    },
    {
        "version": "1.6.1",
        "date": "2026-03-31",
        "notes": [
            "JSON mods no longer fail when vanilla PAZ backup doesn't exist",
            "Variant mods like Fat Stacks now show a picker to choose which option to install",
            "Mods with plain labels no longer incorrectly show the preset picker",
            "Standalone mods (Free Gliding, LET ME SLEEP, etc.) now work — new directories placed first in PAPGT",
            "New mod directories use correct flags matching what mod authors expect",
            "All columns in the mod list are now resizable by dragging",
            "After updating: Disable all → Apply → Re-enable all → Apply for changes to take effect",
        ],
    },
    {
        "version": "1.6.0",
        "date": "2026-03-31",
        "notes": [
            "Import is dramatically faster — large files use streaming comparison",
            "Apply responds instantly — removed blocking dialogs and slow process checks",
            "PAPGT integrity check only rehashes directories that changed (not all 33)",
            "Revert now guarantees ALL files return to vanilla — safety net catches orphaned files",
            "Multiple mods modifying the same PAZ compose correctly (FULL + sparse patches)",
            "Overlay mods like Helmet and Armor Hider now work (mod-shipped PAPGT preserved)",
            "JSON mods patching the same file get changes merged (e.g. Stamina + Fat Stacks)",
            ".bsdiff patches auto-detect target game file — no special naming needed",
            "PAPGT backed up before first Apply — Revert restores exact vanilla copy",
            "Xbox Game Pass game directory detection",
            "Import progress shows per-file status instead of freezing at 0%",
            "Conflicts shown in panel instead of blocking popup",
        ],
    },
    {
        "version": "1.4.0",
        "date": "2026-03-30",
        "notes": [
            # ── Mod Composition Engine (NEW) ──
            "Script mods now captured at PAMT entry level — mods that change different files in the same PAZ compose correctly",
            "Multiple script mods modifying the same PAZ no longer corrupt each other",
            "PAMT index rebuilt from entry-level changes during Apply instead of raw byte diffs",
            "Apply now processes PAZ files first, then rebuilds PAMT, then PAPGT — correct dependency order",
            # ── Conflict Detection & Safety ──
            "Dangerous byte-range overlaps shown as a blocking warning before Apply — lists every conflict and winner",
            "Apply preview — shows exactly what files will be changed before modifying anything",
            "Post-apply integrity verification — checks PAPGT hash, PAMT entries, and PAZ bounds",
            "Safety net catches orphaned modded files left by removed mods and restores them",
            # ── Game Update Detection ──
            "Game update/hotfix detection now shows Steam build ID in the notification",
            "Automatic reset and rescan when game files change (update, hotfix, or Steam verify)",
            "Mod version mismatch warnings — flags mods imported for a different game version",
            # ── Reliability Overhaul ──
            "PAPGT always rebuilt from scratch — never restored from stale backup",
            "PAPGT rebuild removes entries for deleted mod directories (fixes reinstall errors)",
            "Vanilla backups validated against snapshot before creation (rejects modded files)",
            "Snapshot refuses to run on modded files — blocks with clear error message",
            "Orphan mod directories (0036+) cleaned up automatically",
            "Corrupted vanilla backups detected and purged on startup",
            "PAMT hash always recomputed after composing multiple mod deltas",
            # ── Trust & Transparency ──
            "Verify Game State tool — scan all files and see what's vanilla vs modded",
            "Activity Log tab — persistent, color-coded history of every action across sessions",
            "No more silent snapshot refresh — always asks before rescanning",
            # ── New Formats & Import ──
            "JSON preset picker — choose which variant when a mod has labeled presets",
            "7z archive support",
            "Batch import — drop multiple mods at once, imported sequentially",
            "New mods import as disabled — must enable and Apply explicitly",
            # ── ASI Mods ──
            "ASI Loader detection recognizes version.dll, dinput8.dll, dsound.dll",
            "Bundled ASI Loader auto-install when missing",
            # ── UX Improvements ──
            "Script capture progress now shows per-file scanning status instead of freezing at 0%",
            "Configurable mods show gear icon in mod list",
            "Import date shows local time instead of UTC",
            "Leftover .bak files from mod scripts detected and offered for cleanup",
            # ── Bug Fixes ──
            "Fixed script mods leaving game files modded after capture — vanilla restored automatically",
            "Fixed CB mod content truncation — mod files are never modified or stripped",
            "Fixed FULL_COPY delta ordering — applied before SPRS patches from other mods",
            "Fixed ASI panel not showing installed plugins after loader install",
            "Fixed binary search wizard crash on round 10 (NameError in result display)",
            "Fixed uninstall not reverting game files (now disables, applies, then deletes)",
        ],
    },
    {
        "version": "1.2.0",
        "date": "2026-03-29",
        "notes": [
            "Added DDS texture mod support (PATHC format) — install texture replacement mods",
            "Added Crimson Browser mod support for game update directories (prefers latest PAZ)",
            "Fixed Hair Physics mod crash — CB handler now resolves to correct PAZ directory",
            "Added patch notes dialog — see what changed after each update",
            "Drop zone now shows hints about updating mods and right-click options",
            "Snapshot now tracks meta/0.pathc for texture mod revert support",
        ],
    },
    {
        "version": "1.1.2",
        "date": "2026-03-28",
        "notes": [
            "Fixed stale snapshot detection causing repeated reset prompts",
            "Improved game update detection using Steam build ID",
            "Silent snapshot refresh when files are stale but game version unchanged",
        ],
    },
    {
        "version": "1.1.1",
        "date": "2026-03-27",
        "notes": [
            "Fixed app freeze when importing large mods (LootMultiplier 954MB PAZ)",
            "Added FULL_COPY delta format for files >500MB with different sizes",
            "Fixed mod update detection for concatenated names",
        ],
    },
    {
        "version": "1.1.0",
        "date": "2026-03-26",
        "notes": [
            "Added game update auto-detection and reset flow",
            "Added one-time reset for users upgrading from pre-1.0.7",
            "Improved snapshot integrity — prevents dirty snapshots from modded files",
            "Fixed conflict detector capped at 200 to prevent UI freeze",
        ],
    },
    {
        "version": "1.0.9",
        "date": "2026-03-25",
        "notes": [
            "Fixed PAMT hash conflict when multiple mods modify the same PAMT",
            "Health check now uses vanilla backup for accurate validation",
            "Bug report version now reads from __version__ instead of hardcoded",
        ],
    },
    {
        "version": "1.0.0",
        "date": "2026-03-22",
        "notes": [
            "First stable release",
            "PAZ mod import from zip, folder, .bat, .py scripts",
            "JSON byte-patch mod format support",
            "Crimson Browser mod format support",
            "ASI plugin management",
            "Drag-and-drop import with auto-update detection",
            "Mod conflict detection and resolution",
            "Vanilla backup and restore system",
            "Health check with auto-fix for common mod issues",
        ],
    },
]


def get_changelog_html(versions: list[dict] | None = None) -> str:
    """Generate HTML changelog from version data.

    Body text colour is intentionally NOT hardcoded — the wrapping
    QTextBrowser sets a theme-aware foreground colour via its
    stylesheet (dark text on light theme, light text on dark theme).
    Inline ``color:`` declarations override that stylesheet, which
    is what caused the unreadable washed-out bullets in light mode.
    """
    entries = versions or CHANGELOG
    lines = ['<div style="font-family: Segoe UI, sans-serif;">']
    for entry in entries:
        lines.append(
            f'<h3 style="color: #D4A43C; margin-bottom: 4px;">'
            f'v{entry["version"]} &mdash; {entry["date"]}</h3>'
        )
        lines.append('<ul style="margin-top: 2px; margin-bottom: 16px;">')
        for note in entry["notes"]:
            lines.append(f'<li style="margin-bottom: 3px;">{note}</li>')
        lines.append('</ul>')
    lines.append('</div>')
    return "\n".join(lines)


def get_latest_notes_html() -> str:
    """Get HTML for just the latest version's notes."""
    if not CHANGELOG:
        return ""
    return get_changelog_html([CHANGELOG[0]])


class PatchNotesDialog(MessageBoxBase):
    """Dialog showing patch notes — either latest or full history."""

    def __init__(self, parent=None, latest_only: bool = False):
        super().__init__(parent)
        version = CHANGELOG[0]["version"] if CHANGELOG else "?"

        if latest_only:
            self.titleLabel = SubtitleLabel(tr("changelog.whats_new", version=version))
        else:
            self.titleLabel = SubtitleLabel(tr("changelog.patch_notes"))
        self.viewLayout.addWidget(self.titleLabel)

        if latest_only:
            header = BodyLabel(tr("changelog.updated", version=version))
            font = header.font()
            font.setPixelSize(15)
            font.setBold(True)
            header.setFont(font)
            self.viewLayout.addWidget(header)

        from qfluentwidgets import isDarkTheme
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setMinimumHeight(350)
        if isDarkTheme():
            browser.setStyleSheet(
                "QTextBrowser { background: #1C2028; color: #E2E8F0; "
                "border: 1px solid #2D3340; border-radius: 6px; padding: 8px; }")
        else:
            browser.setStyleSheet(
                "QTextBrowser { background: #FAFBFC; color: #1A202C; "
                "border: 1px solid #E2E8F0; border-radius: 6px; padding: 8px; }")
        if latest_only:
            browser.setHtml(get_latest_notes_html())
        else:
            browser.setHtml(get_changelog_html())
        self.viewLayout.addWidget(browser)

        # Override default buttons
        self.yesButton.setText(tr("main.close"))
        self.cancelButton.hide()

        self.widget.setMinimumWidth(560)
