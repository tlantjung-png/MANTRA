"""Console theme tokens: monochrome stone with one muted crimson accent.

Codes are bare SGR parameters (e.g. "38;5;245") meant to be wrapped in
"\033[<code>m...\033[0m" via Style._wrap / the _ansi helpers.
"""

# --- monochrome core (layered stone) --------------------------------
BONE  = "38;5;255"        # primary text: headings, titles, strong words
ASH   = "38;5;250"        # secondary text: labels, meta, plain values
FAINT = "38;5;246"        # tertiary text: hints, comments, timestamps
HAIR  = "38;5;244"        # chrome: borders, rules, walls, hairlines

# Weighted variants of the core tones.
BONE_BOLD = "1;38;5;255"  # strongest emphasis (H1, bold inline, keywords)
ASH_ITAL  = "3;38;5;250"  # italics when the terminal honours SGR 3

# --- the single gothic accent ----------------------------------------
# Brighter blood-crimson. Used only where identity or intent matters:
# the wordmark, the ENCHANTER signature, selection, links, the spinner.
BLOOD      = "38;5;167"
BLOOD_BOLD = "1;38;5;167"

# --- semantic hues, deliberately quiet ------------------------------
SAGE  = "38;5;114"        # additions, success, strings — moss green
EMBER = "38;5;203"        # errors, deletions — bright red, distinct from the accent
WARN  = "38;5;185"        # warnings — soft ochre

# Diff text colours — no backgrounds. Removed lines run in a soft dusty
# red and added lines in a soft sage green, so a before/after pane reads
# clearly without any band or wash on the terminal. Aliases of the
# semantic tones above, kept so a future split (e.g. brighter diff text)
# changes in one place.
DIFF_REMOVE = EMBER  # soft dusty red
DIFF_ADD    = SAGE   # soft sage green
