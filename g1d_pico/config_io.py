"""Read project JSONC configuration (JSON with // line comments), without dependencies."""
import json
from pathlib import Path


def load_config(path: Path):
    """Keep quoted strings and error locations intact; use strict JSON otherwise.

    Ordinary JSON remains supported. Only // outside a quoted string starts a
    comment; URLs, escaped quotes and backslashes inside strings are untouched.
    Block comments and trailing commas are intentionally not accepted.
    """
    text = Path(path).read_text(encoding='utf-8')
    cleaned = list(text)
    in_string = False
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == '/' and text[index:index + 2] == '//':
            while index < len(text) and text[index] not in '\r\n':
                cleaned[index] = ' '
                index += 1
            continue
        index += 1
    return json.loads(''.join(cleaned))
