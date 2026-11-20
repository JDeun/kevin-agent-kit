import json
import subprocess
import sys


def test_personas_produce_artifacts_before_the_decision(tmp_path):
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

    config = tmp_path / "personas.toml"
    config.write_text(
        f"[alternatives]\nalpha = {command('alpha')}\nbravo = {command('bravo')}\n"
        f"[review]\nuse = {command('use')}\nadversarial = {command('adversarial')}\n"
        f"synthesis = {command('synthesis')}\n",
        encoding="utf-8",
    )
    brief = tmp_path / "brief.md"
    brief.write_text("Build a local dashboard", encoding="utf-8")
    output = tmp_path / "evidence"

    result = subprocess.run(
        [sys.executable, "-m", "reviews.persona", "--config", str(config),
         "--brief", str(brief), "--output", str(output)],
        capture_output=True,
        text=True,
        check=True,
    )

    assert (output / "alternatives" / "alpha.md").read_text() == "# ALPHA ARTIFACT\n"
    assert (output / "alternatives" / "bravo.md").read_text() == "# BRAVO ARTIFACT\n"
    assert (output / "trials" / "alpha.md").read_text() == "USE OBSERVATION # ALPHA ARTIFACT\n"
    assert (output / "trials" / "bravo.md").read_text() == "USE OBSERVATION # BRAVO ARTIFACT\n"
    assert "## alpha" in (output / "use.md").read_text()
    assert (output / "adversarial.md").read_text() == "ADVERSARIAL OBSERVATION\n"
    assert (output / "synthesis.md").read_text() == "# Findings\n"
    assert (output / "question.md").read_text() == "Which layout should ship?\n"
    assert result.stdout.index("# Findings") < result.stdout.index("Which layout should ship?")

    agent.write_text(agent.read_text().replace('"synthesis": "# Findings"', '"synthesis": ""'))
    failed_output = tmp_path / "invalid"
    failed = subprocess.run(
        [sys.executable, "-m", "reviews.persona", "--config", str(config),
         "--brief", str(brief), "--output", str(failed_output)],
        capture_output=True,
        text=True,
    )
    assert failed.returncode == 2
    assert "Which layout should ship?" not in failed.stdout
    assert not (failed_output / "question.md").exists()
