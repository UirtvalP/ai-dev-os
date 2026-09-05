from __future__ import annotations

import json
import re
from itertools import pairwise, product
from pathlib import Path

from workspace_orchestrator.phase_gate import source_fingerprint


def test_v2_plan_and_complete_gate_definition_chain_are_one_contract() -> None:
    root = Path(__file__).parents[1]
    plan = (root / "V2实施主计划.md").read_text(encoding="utf-8")
    acceptance = {
        match.group(1): match.group(2)
        for match in re.finditer(
            r"^- \[[ x]\] `(P[0-6]-AC-\d+)` (.+)$",
            plan,
            re.MULTILINE,
        )
    }
    definition_paths = sorted(
        (root / ".ai-dev-os" / "gate-definitions" / "REQ-020").glob(
            "phase-*.json"
        )
    )
    definitions = [
        json.loads(path.read_text(encoding="utf-8")) for path in definition_paths
    ]

    assert [item["phase"] for item in definitions] == list(range(7))
    assert len({item["task_id"] for item in definitions}) == len(definitions)
    assert all(
        current["next_task_id"] == following["task_id"]
        for current, following in pairwise(definitions)
    )
    assert definitions[-1]["next_task_id"] is None
    assert {
        item["id"]: item["description"]
        for definition in definitions
        for item in definition["acceptance"]
    } == acceptance
    assert len({item["plan_source_path"] for item in definitions}) == len(definitions)

    for phase, definition in enumerate(definitions):
        assert definition["plan_source_path"] == f".ai-dev-os/plans/REQ-020/phase-{phase}.md"
        source = (root / definition["plan_source_path"]).read_text(encoding="utf-8")
        assert definition["plan_source_fingerprint"] == source_fingerprint(source)
        master_section = re.search(
            rf"^## {6 + phase}\. Phase {phase} .*?(?=^## \d+\.)",
            plan,
            re.MULTILINE | re.DOTALL,
        )
        assert master_section is not None
        # 总路线可显示实时进度，已签发的范围/验收快照不因勾选被重写。
        assert re.sub(r"^- \[[ x]\]", "- [ ]", source.strip(), flags=re.MULTILINE) == re.sub(
            r"^- \[[ x]\]", "- [ ]", master_section.group().strip(), flags=re.MULTILINE
        )
        suites = definition["verification_suites"]
        assert [suite["id"] for suite in suites] == [
            f"p{phase}-local-common-quality",
            f"p{phase}-github-ci-matrix",
        ]
        assert [suite["kind"] for suite in suites] == ["command", "github-actions"]
        commands = suites[0]["commands"]
        for program, option in product(("ai-dev-os", "workspace"), ("--help", "--version")):
            assert [
                "uv", "tool", "run", "--no-cache", "--isolated", "--from", ".", program, option
            ] in commands


def test_ci_matrix_job_names_and_installed_smokes_match_all_phase_gates() -> None:
    root = Path(__file__).parents[1]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    candidate_sha = "${{ github.event.pull_request.head.sha || github.sha }}"
    assert f"          ref: {candidate_sha}\n" in workflow
    assert (
        "      - name: Verify tested commit\n"
        "        shell: bash\n"
        "        env:\n"
        f"          EXPECTED_SHA: {candidate_sha}\n"
        '        run: test "$(git rev-parse HEAD)" = "$EXPECTED_SHA"\n'
    ) in workflow
    assert "    name: ${{ matrix.os }} / Python ${{ matrix.python-version }}\n" in workflow
    matrix_match = re.search(r"^      matrix:\n(.+?)\n    steps:", workflow, re.MULTILINE | re.DOTALL)
    assert matrix_match is not None
    matrix = {}
    for key, values in re.findall(
        r"^        ([\w-]+):\n((?:          - .+\n?)+)",
        matrix_match.group(1),
        re.MULTILINE,
    ):
        matrix[key] = [value.strip().removeprefix("- ").strip('"') for value in values.splitlines()]
    assert set(matrix) == {"os", "python-version"}
    assert set(matrix["os"]) == {"ubuntu-latest", "windows-latest"}
    assert set(matrix["python-version"]) == {"3.11", "3.14"}
    expected_jobs = {
        f"{operating_system} / Python {version}"
        for operating_system, version in product(matrix["os"], matrix["python-version"])
    }
    for path in (root / ".ai-dev-os" / "gate-definitions" / "REQ-020").glob("phase-*.json"):
        definition = json.loads(path.read_text(encoding="utf-8"))
        github_suite = next(
            suite for suite in definition["verification_suites"] if suite["kind"] == "github-actions"
        )
        assert github_suite["workflow"] == "ci.yml"
        assert set(github_suite["required_jobs"]) == expected_jobs
    for program, option in product(("ai-dev-os", "workspace"), ("--help", "--version")):
        assert f"uv tool run --no-cache --isolated --from . {program} {option}\n" in workflow
