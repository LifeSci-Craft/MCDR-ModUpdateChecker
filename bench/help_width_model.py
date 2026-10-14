"""Which width model should the help page pad with?

The help page pads its command column so the ``--`` separators line up. The padding is spaces,
so how well it can line up depends on **the font the player runs**, which the server cannot
know. Two fonts are known concretely:

* the shipped Minecraft font, from its glyph table (letters 6px at 1x, ``i``/``l``/``t``/``f``
  narrow, space 4px — two thirds of a letter);
* the one in the screenshots this ticket came with, measured off the pixels: letters 24px,
  ``i``/``l``/``t``/``f`` 18px, ``!`` 6px, and — the awkward part — **space 24px, as wide as
  a letter**.

The second font cannot be aligned perfectly with spaces at all: its smallest pad step is a
full letter. The best any padding can do there is to land every row within half a letter.
This script scans candidate models and reports, for each, the spread each font ends up with,
so the model that is shipped is the best joint answer rather than a guess.

Run with any Python (no dependencies).
"""

# Command names, in the order the help page prints them.
VERBS = ("check", "list", "summary", "info", "download", "install", "delete", "cleanup",
         "confirm", "status", "reload", "help")
PREFIX = "!!muc "

# --- the two fonts, per-character advances -------------------------------------------------
# Vanilla: units of the shipped font table (letter = 6, space = 4).
VANILLA = {"default": 6, "i": 2, "l": 3, "t": 4, "f": 5, "k": 5, " ": 4, "!": 2}
# Measured off the screenshots: letter = 24px, i/l/t/f = 18px, ! = 6px, space = 24px.
MEASURED = {"default": 24, "i": 18, "l": 18, "t": 18, "f": 18, "k": 24, " ": 24, "!": 6}


def width(text, font):
    return sum(font.get(char, font["default"]) for char in text)


def pads(model, rounding="round"):
    """The padding counts this model would print, per command."""
    widths = [width(PREFIX + verb, model) for verb in VERBS]
    target = max(widths)
    out = []
    for value in widths:
        exact = (target - value) / model[" "]
        if rounding == "round":
            pad = int(exact + 0.5)
        else:  # ceil
            pad = int(exact) + (1 if exact % 1 else 0)
        out.append(pad)
    return out


def dashes(font, pad_counts):
    """Where each ``--`` starts in this font, given those pad counts."""
    return [
        width(PREFIX + verb, font) + (pad + 1) * font[" "]
        for verb, pad in zip(VERBS, pad_counts)
    ]


def report(label, model, rounding="round"):
    counts = pads(model, rounding)
    user = dashes(MEASURED, counts)
    vanilla = dashes(VANILLA, counts)
    user_letters = (max(user) - min(user)) / MEASURED["default"]
    vanilla_letters = (max(vanilla) - min(vanilla)) / VANILLA["default"]
    print("%-34s  rounds=%-5s  user %.2f letters  vanilla %.2f letters" % (
        label, rounding, user_letters, vanilla_letters))
    return user_letters, vanilla_letters


print("Model                                          spread-of-dashes (smaller is better)")
print("-" * 96)

# The status quo: every character counts one, spaces included.
report("char count (today)", {**{c: 1 for c in "abcdefghijklmnopqrstuvwxyz"}, "default": 1,
                              " ": 1, "!": 1})

# The shipped-font model, which is what the plugin's console already uses.
report("vanilla glyph table", dict(VANILLA))

# A few joint candidates: narrow letters half to three-quarters, space near a letter.
best = None
for narrow in (0.5, 0.6, 0.7, 0.75, 0.8):
    for space in (0.6, 0.7, 0.75, 0.8, 0.9, 1.0):
        model = {**{c: 1.0 for c in "abcdefghijklmnopqrstuvwxyz"}, "default": 1.0,
                 " ": space, "!": 0.25}
        for c in "iltf":
            model[c] = narrow
        user, vanilla = report(
            "narrow=%.2f space=%.2f" % (narrow, space), model
        )
        score = max(user, vanilla)
        if best is None or score < best[0]:
            best = (score, narrow, space, user, vanilla)

print()
print("best joint: narrow=%.2f space=%.2f -> user %.2f, vanilla %.2f" % (
    best[1], best[2], best[3], best[4]))

# And for completeness, the model tuned for each font alone.
print()
for label, font in (("user only", MEASURED), ("vanilla only", VANILLA)):
    model = {**{c: 1.0 for c in "abcdefghijklmnopqrstuvwxyz"}, "default": 1.0,
             " ": 1.0, "!": 0.25}
    for c in "iltf":
        model[c] = font[c] / font["default"]
    model[" "] = font[" "] / font["default"]
    model["k"] = font["k"] / font["default"]
    report("%s (exact metrics)" % label, model)
