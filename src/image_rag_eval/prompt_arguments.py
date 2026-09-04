"""Extract explicit source placeholders as literal evidence, never as instructions.

The existing double-quoted grammar and its JSON-escaped-quote form are
recognized. Unknown syntax stays in the original prompt with an offset warning.
Values are raw quoted contents: no evaluation, unescaping, or LLM inference.
"""
from __future__ import annotations

import re

VERSION = "source-argument-literal-2"
MARKER = "{argument name="
ARGUMENT = re.compile(
    r'\{argument name="(?P<name>(?:[^"\\]|\\.)*)"\s+'
    r'default="(?P<default>(?:[^"\\]|\\.)*)"\s*\}'
)
ESCAPED_ARGUMENT = re.compile(
    r'\{argument name=\\"(?P<name>(?:[^\\]|\\(?!"))*)\\"\s+'
    r'default=\\"(?P<default>(?:[^\\]|\\(?!"))*)\\"\s*\}'
)


def extract_arguments(prompt: str) -> dict:
    starts = [match.start() for match in re.finditer(re.escape(MARKER), prompt)]
    arguments = []
    matches = sorted((*ARGUMENT.finditer(prompt), *ESCAPED_ARGUMENT.finditer(prompt)), key=lambda match: match.start())
    for match in matches:
        if arguments and match.start() < arguments[-1]["end_char"]:
            continue
        arguments.append({"ordinal": len(arguments), "start_char": match.start(),
                          "end_char": match.end(), "literal": match.group(),
                          "name_raw": match["name"], "default_raw": match["default"]})
    recognized = {row["start_char"] for row in arguments}
    # An apparent marker inside a quoted value is literal content of that value.
    unparsed = [start for start in starts if start not in recognized and not any(
        row["start_char"] < start < row["end_char"] for row in arguments)]
    return {"parser_version": VERSION, "argument_count": len(arguments),
            "unparsed_marker_offsets": unparsed, "arguments": arguments}
