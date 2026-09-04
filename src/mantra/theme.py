"""Console theme tokens: monochrome stone with one muted crimson accent.

Codes are bare SGR parameters (e.g. "38;5;245") meant to be wrapped in
"\033[<code>m...\033[0m" via Style._wrap / the _ansi helpers.
"""

# --- monochrome core (layered stone) --------------------------------
BONE  = "38;5;253"        # primary text: headings, titles, strong words
ASH   = "38;5;245"        # secondary text: labels, meta, plain values
FAINT = "38;5;240"        # tertiary text: hints, comments, timestamps
HAIR  = "38;5;238"        # chrome: borders, rules, walls, hairlines

# Weighted variants of the core tones.
BONE_BOLD = "1;38;5;253"  # strongest emphasis (H1, bold inline, keywords)
ASH_ITAL  = "3;38;5;245"  # italics when the terminal honours SGR 3

# --- the single gothic accent ----------------------------------------
# Muted blood-crimson. Used only where identity or intent matters:
# the wordmark, the ENCHANTER signature, selection, links, the spinner.
BLOOD      = "38;5;131"
BLOOD_BOLD = "1;38;5;131"

# --- semantic hues, deliberately quiet ------------------------------
SAGE  = "38;5;108"        # additions, success, strings — muted moss
EMBER = "38;5;167"        # errors, deletions — dusty red
WARN  = "38;5;179"        # warnings — soft ochre

# Diff text colours — no backgrounds. Removed lines run in a soft dusty
# red and added lines in a soft sage green, so a before/after pane reads
# clearly without any band or wash on the terminal. Aliases of the
# semantic tones above, kept so a future split (e.g. brighter diff text)
# changes in one place.
DIFF_REMOVE = EMBER  # soft dusty red
DIFF_ADD    = SAGE   # soft sage green
