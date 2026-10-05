"""Installed skills must have valid metadata and reachable local references."""
from pathlib import Path
import re

import pytest
import yaml

SKILLS = Path(__file__).parents[1] / "skills"


@pytest.mark.parametrize("skill", sorted(p for p in SKILLS.iterdir() if p.is_dir()))
def test_skill_metadata_and_links(skill):
    entry = skill / "SKILL.md"
    text = entry.read_text(encoding="utf-8")
    head = re.match(r"\A---\s*\n(.*?)\n---", text, re.S)
    assert head, f"Missing metadata in {entry}"
    metadata = yaml.safe_load(head.group(1))
    assert metadata["name"] == skill.name
    assert isinstance(metadata.get("description"), str) and metadata["description"].strip()
    interface = yaml.safe_load((skill / "agents/openai.yaml").read_text(encoding="utf-8"))
    assert "$" + skill.name in interface["interface"]["default_prompt"]
    for doc in skill.rglob("*.md"):
        body = doc.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^\s)]+)\)", body):
            if "://" in target or target.startswith(("#", "mailto:")):
                continue
            local = target.split("#", 1)[0]
            assert (doc.parent / local).exists(), f"Broken skill reference: {doc} -> {target}"
