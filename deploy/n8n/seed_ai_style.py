"""Build a repeatable PostgreSQL seed transaction from checked owner assets.

INSERT ... ON CONFLICT DO NOTHING preserves owner edits and review decisions.
Only the bundled source files are accepted as SQL input.
"""

from __future__ import annotations

import hashlib
import re

from deploy.n8n.ai_assets import read_asset, style_seeds


def _literal(value: str) -> str:
    if "\x00" in value:
        raise ValueError("SQL text contains NUL")
    return "'" + value.replace("'", "''") + "'"


def _rule_sections(agent: str) -> dict[str, str]:
    text = read_asset("chat_dna" if agent == "chat" else "serious_dna")
    pattern = r"^## (?:\d+\. )?`([a-z_]+)`[^\n]*$"
    matches = list(re.finditer(pattern, text, flags=re.M))
    sections: dict[str, str] = {}
    for match in matches:
        next_heading = re.search(r"^## ", text[match.end():], flags=re.M)
        end = match.end() + next_heading.start() if next_heading else len(text)
        sections[match.group(1)] = text[match.end():end].strip()
    return sections


def seed_sql() -> str:
    seeds = style_seeds()
    lines = ["BEGIN;"]
    for agent in ("chat", "serious"):
        sections = _rule_sections(agent)
        for rule in seeds[agent]["rules"]:
            key = rule["key"]
            if key not in sections:
                raise ValueError(f"Missing {agent} style rule: {key}")
            body = sections[key]
            digest = hashlib.sha256(body.encode()).hexdigest()
            values = (agent, key, body, digest, "bundled_default")
            lines.append("INSERT INTO mika_style_rules "
                         "(agent_type,rule_key,body,source_hash,provenance) VALUES (" +
                         ",".join(map(_literal, values)) + ") ON CONFLICT DO NOTHING;")
        for row in seeds[agent]["examples"]:
            values = (row["id"], agent, row["category"], row["audience"],
                      row["user_example"], row["assistant_example"],
                      hashlib.sha256((row["user_example"] + "\n" +
                          row["assistant_example"]).encode()).hexdigest(),
                      "bundled_default")
            lines.append("INSERT INTO mika_style_examples "
                         "(example_id,agent_type,category,audience,user_example,"
                         "assistant_example,source_hash,provenance) VALUES (" +
                         ",".join(map(_literal, values)) + ") ON CONFLICT DO NOTHING;")
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(seed_sql(), end="")
