import json
import sys
from pathlib import Path

import pytest


def test_personas_produce_artifacts_before_the_decision(tmp_path, monkeypatch, capsys):
    from reviews.persona import __main__ as persona
    from core.loop.config import profile_dir
    from core.loop.runtime import run

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    def fixture_runner(argv, cwd, cancel, timeout, *, role, policy, stdin):
        assert role == "persona" and policy["env"] == {}
        return run(argv, cwd, cancel, timeout, prompt=stdin, env={"LANG": "C.UTF-8"})

    monkeypatch.setattr(persona, "execute", fixture_runner)
    agent = tmp_path / "agent.py"
    agent.write_text(
        """
import json
from pathlib import Path
import sys

role = sys.argv[1]
prompt = sys.stdin.read()
assert not Path("private").exists()
Path("private").write_text(role, encoding="utf-8")
assert "Build a local dashboard" in prompt
if role == "alpha":
    assert "BRAVO ARTIFACT" not in prompt
    print("# ALPHA ARTIFACT")
elif role == "bravo":
    assert "ALPHA ARTIFACT" not in prompt
    print("# BRAVO ARTIFACT")
elif role == "use":
    artifact = Path("artifact.md").read_text().strip()
    assert artifact in ("# ALPHA ARTIFACT", "# BRAVO ARTIFACT")
    assert artifact in prompt
    assert ("ALPHA ARTIFACT" in prompt) != ("BRAVO ARTIFACT" in prompt)
    print("USE OBSERVATION " + artifact)
elif role == "adversarial":
    assert "ALPHA ARTIFACT" in prompt and "BRAVO ARTIFACT" in prompt
    assert "USE OBSERVATION" in prompt and "ADVERSARIAL OBSERVATION" not in prompt
    print(role.upper() + " OBSERVATION")
else:
    assert "ALPHA ARTIFACT" in prompt and "BRAVO ARTIFACT" in prompt
    assert "USE OBSERVATION" in prompt and "ADVERSARIAL OBSERVATION" in prompt
    print(json.dumps({"synthesis": "# Findings", "question": "Which layout should ship?"}))
""",
        encoding="utf-8",
    )

    def command(role):
        return json.dumps([sys.executable, str(agent), role])

    config = profile_dir() / "personas.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        f"[alternatives]\nalpha = {command('alpha')}\nbravo = {command('bravo')}\n"
        f"[review]\nuse = {command('use')}\nadversarial = {command('adversarial')}\n"
        f"synthesis = {command('synthesis')}\n",
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("Build a local dashboard", encoding="utf-8")
    output = tmp_path / "evidence"

    args = ["persona", "--config", str(config), "--brief", str(brief), "--output"]
    monkeypatch.setattr(sys, "argv", [*args, str(output)])
    assert persona.main() == 0
    stdout = capsys.readouterr().out

    assert (output / "alternatives" / "alpha.md").read_text() == "# ALPHA ARTIFACT\n"
    assert (output / "alternatives" / "bravo.md").read_text() == "# BRAVO ARTIFACT\n"
    assert (output / "trials" / "alpha.md").read_text() == "USE OBSERVATION # ALPHA ARTIFACT\n"
    assert (output / "trials" / "bravo.md").read_text() == "USE OBSERVATION # BRAVO ARTIFACT\n"
    assert "## alpha" in (output / "use.md").read_text()
    assert (output / "adversarial.md").read_text() == "ADVERSARIAL OBSERVATION\n"
    assert (output / "synthesis.md").read_text() == "# Findings\n"
    assert (output / "question.md").read_text() == "Which layout should ship?\n"
    assert stdout.index("# Findings") < stdout.index("Which layout should ship?")

    agent.write_text(agent.read_text().replace('"synthesis": "# Findings"', '"synthesis": ""'))
    failed_output = tmp_path / "invalid"
    monkeypatch.setattr(sys, "argv", [*args, str(failed_output)])
    assert persona.main() == 2
    assert "Which layout should ship?" not in capsys.readouterr().out
    assert not (failed_output / "question.md").exists()


def test_persona_rejects_a_local_repository_profile(tmp_path, monkeypatch):
    from reviews.persona.__main__ import _load_config

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "trusted"))
    profile = Path(tmp_path / "agent-commands.toml")
    profile.write_text('[alternatives]\na = ["candidate-command"]\n')
    with pytest.raises(ValueError, match="trusted profiles"):
        _load_config(profile)
