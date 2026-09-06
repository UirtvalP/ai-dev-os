from __future__ import annotations

import ast
import json
import re
import tomllib
from itertools import pairwise, product
from pathlib import Path

from workspace_orchestrator.phase_gate import source_fingerprint


def test_pytest_parallelism_is_fixed_four_workers_with_lpac_seed_groups() -> None:
    root = Path(__file__).parents[1]
    configuration = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert configuration["tool"]["pytest"]["ini_options"]["addopts"] == "-n 4 --dist=loadgroup"

    source = ast.parse((root / "tests" / "test_legacy_verification.py").read_text(encoding="utf-8"))
    group_members: dict[str, str] = {}
    parameterized: set[str] = set()
    test_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(source):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("test_"):
            test_functions[node.name] = node
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Attribute)
                and isinstance(decorator.func.value.value, ast.Name)
                and decorator.func.value.value.id == "pytest"
                and decorator.func.value.attr == "mark"
                and decorator.func.attr == "xdist_group"
            ):
                assert (
                    len(decorator.args) == 1
                    and isinstance(decorator.args[0], ast.Constant)
                    and isinstance(decorator.args[0].value, str)
                )
                group_members[node.name] = decorator.args[0].value
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Attribute)
                and isinstance(decorator.func.value.value, ast.Name)
                and decorator.func.value.value.id == "pytest"
                and decorator.func.value.attr == "mark"
                and decorator.func.attr == "parametrize"
            ):
                parameterized.add(node.name)

    lpac_fixture_consumers = {
        name
        for name, node in test_functions.items()
        if "lpac_test_infrastructure"
        in {
            argument.arg
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
                *((node.args.vararg,) if node.args.vararg is not None else ()),
                *((node.args.kwarg,) if node.args.kwarg is not None else ()),
            )
        }
    }

    assert group_members == {
        "test_lpac_runtime_seed_clones_are_physical_isolated_and_tamper_evident": "lpac-seeded-a",
        "test_default_adapter_rejects_candidate_pytest_impersonation_before_launch": "lpac-seeded-a",
        "test_real_lpac_trusted_pytest_really_executes_a_failing_candidate_test": "lpac-seeded-b",
        "test_real_lpac_candidate_package_cannot_be_shadowed_by_old_installed_package": "lpac-seeded-b",
        "test_real_lpac_private_package_and_bytecode_injection_cannot_reach_next_command": "lpac-seeded-a",
        "test_default_backend_real_timeout_kills_descendants": "lpac-seeded-b",
        "test_existing_python_test_tools_run_inside_private_candidate_directory": "lpac-seeded-a",
        "test_private_python_ignores_existing_pth_and_cannot_write_dependency_source": "lpac-seeded-b",
    }
    assert set(group_members) == lpac_fixture_consumers
    assert "test_real_lpac_candidate_package_cannot_be_shadowed_by_old_installed_package" in parameterized
    raw_cold_test = "test_default_backend_real_candidate_isolated_raw_output_and_cleanup"
    assert raw_cold_test not in lpac_fixture_consumers
    assert raw_cold_test not in group_members


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
        expected_kinds = (
            ["github-attestation", "github-attestation"]
            if phase == 4
            else ["command", "github-actions"]
        )
        assert [suite["kind"] for suite in suites] == expected_kinds
        if phase == 4:
            assert [suite["attested_kind"] for suite in suites] == ["command", "github-actions"]
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
        github_suite = next(suite for suite in definition["verification_suites"] if (
            suite["kind"] == "github-actions" or suite.get("attested_kind") == "github-actions"
        ))
        assert github_suite["workflow"] == "ci.yml"
        assert set(github_suite["required_jobs"]) == expected_jobs
    for program, option in product(("ai-dev-os", "workspace"), ("--help", "--version")):
        assert f"uv tool run --no-cache --isolated --from . {program} {option}\n" in workflow
