"""Guard: user-facing copy stays ASCII (house style: '->' not an arrow,
'...' not an ellipsis, '--' not a long dash). This is the cheap mechanical
lens the AI-tell sweep should have run from the start; baking it into a test
keeps a stray em-dash or curly quote from creeping back in unnoticed."""
import pathlib
import re

TEMPLATES = pathlib.Path(__file__).resolve().parents[1] / "src/nonnewtonian/web/templates"

# Entities that RENDER as a non-ASCII glyph. Structural escapes that render as
# ASCII (&amp; &lt; &gt;) and the non-breaking space (&nbsp;, renders as a
# space) are fine and deliberately not listed.
RENDERABLE_ENTITY = re.compile(
    r"&(mdash|ndash|rarr|larr|middot|hellip|times|deg|copy|reg|trade|frac\d+|[lr]dquo|[lr]squo);"
)


def test_templates_contain_no_nonascii_glyphs():
    offenders = []
    for f in sorted(TEMPLATES.glob("*")):
        if not f.is_file():
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for ch in line:
                if ord(ch) > 127:
                    offenders.append(f"{f.name}:{i}: literal U+{ord(ch):04X} {ch!r}")
            for m in RENDERABLE_ENTITY.finditer(line):
                offenders.append(f"{f.name}:{i}: entity {m.group(0)}")
    assert not offenders, (
        "Non-ASCII in user-facing copy (use ASCII equivalents):\n" + "\n".join(offenders)
    )
