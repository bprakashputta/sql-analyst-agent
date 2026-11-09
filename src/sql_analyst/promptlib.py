"""Versioned prompt files.

Each prompt lives in `prompts/<name>.md`:

    ---
    version: 3
    ---
    ## system
    ...instructions with {{placeholders}}...
    ## user
    ...

The version is part of the LLM cache key and of every logged call, so a prompt edit never reuses a
stale cached answer and every eval result can be traced to the prompt text that produced it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

PROMPT_DIR = Path(__file__).parent / "prompts"
_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    system: str
    user: str

    @property
    def id(self) -> str:
        return f"{self.name}@v{self.version}"

    def render(self, **variables: Any) -> tuple[str, str]:
        def fill(text: str) -> str:
            def sub(m: re.Match[str]) -> str:
                key = m.group(1)
                if key not in variables:
                    raise KeyError(f"prompt {self.id} needs {{{{{key}}}}}")
                return str(variables[key])

            return _VAR.sub(sub, text).strip()

        return fill(self.system), fill(self.user)


@cache
def load(name: str) -> Prompt:
    text = (PROMPT_DIR / f"{name}.md").read_text()
    m = re.match(r"---\s*\nversion:\s*(\d+)\s*\n---\s*\n(.*)", text, re.S)
    if not m:
        raise ValueError(f"prompt {name} has no version header")
    body = m.group(2)
    parts = re.split(r"^## (system|user)\s*$", body, flags=re.M)
    sections = {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}
    return Prompt(
        name=name, version=int(m.group(1)), system=sections["system"], user=sections["user"]
    )
