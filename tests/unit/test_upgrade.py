"""``scio upgrade`` (issue #58): a product bootstrapped before M11 is brought to the current
product bar additively, from the current scaffold, and nothing the product made its own is
touched. The round trip strips a fresh scaffold back to its pre-M11 shape and expects the
current scaffold's files back."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scio.aci.sandbox.local import LocalSandbox
from scio.product.release_checks import RUN_END, RUN_START, readme_run_commands, release_report
from scio.product.scaffold import README_RUN_STEP, scaffold_for
from scio.product.spec import render_product_md
from scio.product.upgrade import MARKER, UpgradeRefused, upgrade_product
from scio.state.models import AppSpec, ProductFeature, ProductSpec
from scio.toolchain import get_toolchain


def _spec(stack: str) -> ProductSpec:
    return ProductSpec(
        name="shop_api",
        title="Shop API",
        vision="v",
        users=["u"],
        features=[ProductFeature(id="P1", description="skeleton", acceptance=["starts"])],
        quality_bar=["q"],
        stack=stack,
        app=AppSpec(
            kind="http",
            module="shop_api" if stack == "python" else None,
            args=["serve", "--port", "{port}"],
        ),
    )


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@x", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _pre_m11_product(root: Path, stack: str) -> dict[str, str]:
    """A fresh scaffold with M11's additions taken out again, committed."""
    spec = _spec(stack)
    fresh = scaffold_for(spec)
    command = get_toolchain(stack).skeleton_command(spec.name)
    old = dict(fresh)
    del old["Dockerfile"], old[".dockerignore"]
    old["README.md"] = old["README.md"].replace(
        f"{RUN_START}\n```\n{command}\n```\n{RUN_END}\n", f"```\n{command}\n```\n"
    )
    old[".github/workflows/ci.yml"] = old[".github/workflows/ci.yml"].replace(
        README_RUN_STEP, f"      - run: {command}\n"
    )
    if stack == "python":
        old["pyproject.toml"] = old["pyproject.toml"].split("\n[tool.coverage.run]")[0] + "\n"
    old["PRODUCT.md"] = render_product_md(spec)
    for rel, text in old.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "chore: bootstrap (pre-M11)")
    return fresh


@pytest.mark.parametrize("stack", ["python", "node-ts"])
def test_upgrade_brings_a_pre_m11_product_to_the_current_scaffold(
    tmp_path: Path, stack: str
) -> None:
    fresh = _pre_m11_product(tmp_path, stack)
    sb = LocalSandbox(tmp_path)
    report = upgrade_product(sb)
    expected = [
        "Dockerfile",
        ".dockerignore",
        "README run block",
        ".github/workflows/ci.yml README run step",
    ]
    if stack == "python":
        expected.insert(3, "[tool.coverage.run] in pyproject.toml")
    assert report.added == expected and report.commit
    for rel in ("Dockerfile", ".dockerignore", ".github/workflows/ci.yml"):
        assert (tmp_path / rel).read_text(encoding="utf-8") == fresh[rel], rel
    if stack == "python":
        assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == fresh["pyproject.toml"]
    assert MARKER in _git(tmp_path, "log", "-1", "--format=%s")
    assert _git(tmp_path, "status", "--porcelain") == ""
    lines, _ = release_report(sb, get_toolchain(stack))
    assert lines[1].startswith("[release files] present")
    # the block holds exactly the skeleton command (whether it runs needs the product's env)
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert readme_run_commands(readme) == [get_toolchain(stack).skeleton_command("shop_api")]
    again = upgrade_product(sb)
    assert again.added == [] and again.commit is None


def test_upgrade_keeps_what_the_product_made_its_own(tmp_path: Path) -> None:
    _pre_m11_product(tmp_path, "python")
    (tmp_path / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8", newline="\n")
    ci = tmp_path / ".github" / "workflows" / "ci.yml"
    ci.write_text(ci.read_text(encoding="utf-8") + "      - run: make deploy\n", encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text("# Shop API\n\nOur own README.\n", encoding="utf-8", newline="\n")
    _git(tmp_path, "commit", "-q", "-am", "docs: our own", "--allow-empty")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "chore: our own files")
    report = upgrade_product(LocalSandbox(tmp_path))
    assert "Dockerfile (exists)" in report.kept
    assert any("the product's own" in k for k in report.kept)
    assert (tmp_path / "Dockerfile").read_text(encoding="utf-8") == "FROM busybox\n"
    assert "make deploy" in ci.read_text(encoding="utf-8")
    text = readme.read_text(encoding="utf-8")
    assert text.startswith("# Shop API\n\nOur own README.\n\n## Run\n\n" + RUN_START)


def test_upgrade_refuses_a_dirty_tree_and_a_directory_without_product_md(tmp_path: Path) -> None:
    _pre_m11_product(tmp_path, "python")
    (tmp_path / "scratch.txt").write_text("wip\n", encoding="utf-8")
    with pytest.raises(UpgradeRefused, match="uncommitted"):
        upgrade_product(LocalSandbox(tmp_path))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(UpgradeRefused, match="not a SCIO product"):
        upgrade_product(LocalSandbox(empty))


def test_upgrade_refuses_a_product_md_that_does_not_parse(tmp_path: Path) -> None:
    """I-148's sweep: `scio upgrade` parsed PRODUCT.md unguarded, so a file that no longer
    parsed ended the command in a ValidationError traceback instead of its refusal."""
    _pre_m11_product(tmp_path, "python")
    md = tmp_path / "PRODUCT.md"
    text = md.read_text(encoding="utf-8")
    head, sep, rest = text.partition("\n### P1: ")
    assert sep, "the fixture has a P1 section"
    md.write_text(head + sep + "x" * 500 + rest, encoding="utf-8", newline="\n")
    _git(tmp_path, "commit", "-qam", "edit")  # a clean tree, so only the parse can refuse
    with pytest.raises(UpgradeRefused, match=r"PRODUCT\.md does not parse"):
        upgrade_product(LocalSandbox(tmp_path))


def _v090_python_product(
    root: Path, *, kind: str = "http", install: str = "pip install ruff pytest"
) -> dict[str, str]:
    """A python product as the 0.9.0 scaffold wrote it: CI and README install only the tools,
    never the project, and pyproject.toml has no build system (the graphcalc run's S6).
    ``install`` is the one install line it has; no scaffold before #169 installed a browser."""
    from scio.product.scaffold import (
        PYTHON_BUILD_SYSTEM,
        python_install_lines,
        python_packages_find,
    )

    spec = _spec("python").model_copy(update={"dependencies": ["sympy"]})
    if kind == "cli":
        spec = spec.model_copy(update={"app": AppSpec()})
    fresh = scaffold_for(spec)
    old = dict(fresh)
    first, *later = python_install_lines(spec)
    for rel, prefix in ((".github/workflows/ci.yml", "      - run: "), ("README.md", "")):
        for line in later:
            assert f"{prefix}{line}\n" in old[rel]
            old[rel] = old[rel].replace(f"{prefix}{line}\n", "")
        assert f"{prefix}{first}\n" in old[rel]
        old[rel] = old[rel].replace(f"{prefix}{first}\n", f"{prefix}{install}\n")
    old["pyproject.toml"] = (
        old["pyproject.toml"]
        .replace(PYTHON_BUILD_SYSTEM + "\n", "")
        .replace(python_packages_find(spec.name) + "\n", "")
    )
    old["PRODUCT.md"] = render_product_md(spec)
    for rel, text in old.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "chore: bootstrap (0.9.0)")
    _git(root, "checkout", "-q", "-b", "milestone/1")
    return fresh


def test_upgrade_makes_an_old_python_products_ci_install_the_project(tmp_path: Path) -> None:
    """I-140, I-142: the 0.9.0 scaffold's CI never installed the project, so a declared
    dependency never reached the tests; upgrade rewrites exactly that line in the CI and the
    README, gives pyproject.toml a build system so ``pip install -e .`` works, and commits
    on the branch that is checked out (a milestone branch at a merge stop)."""
    import tomllib

    from scio.product.scaffold import PYTHON_INSTALL

    # a cli product: an http one also gets the browser (issue #169), in the test after this
    fresh = _v090_python_product(tmp_path, kind="cli")
    report = upgrade_product(LocalSandbox(tmp_path))
    assert report.added == [
        "[build-system] and the package list in pyproject.toml",
        f".github/workflows/ci.yml: {PYTHON_INSTALL!r}, which installs the project",
        f"README.md: {PYTHON_INSTALL!r}, which installs the project",
    ]
    ci = (tmp_path / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert ci == fresh[".github/workflows/ci.yml"] and f"- run: {PYTHON_INSTALL}\n" in ci
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert readme == fresh["README.md"]
    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert tomllib.loads(pyproject) == tomllib.loads(fresh["pyproject.toml"])
    assert _git(tmp_path, "rev-parse", "--abbrev-ref", "HEAD") == "milestone/1"
    assert MARKER in _git(tmp_path, "log", "-1", "--format=%s")
    assert _git(tmp_path, "log", "-1", "--format=%an") == "scio"
    assert upgrade_product(LocalSandbox(tmp_path)).added == []


@pytest.mark.parametrize("install", ["pip install -e . ruff pytest", "pip install ruff pytest"])
def test_upgrade_adds_the_browser_to_an_http_products_ci_once(tmp_path: Path, install: str) -> None:
    """Issue #169 (I-242): graphcalc's CI installs ``pip install -e . ruff pytest``, so its
    browser tests skip there. Upgrade gives a python product that serves pages the install
    line with Playwright and one browser step after it, with the step's indentation, in the
    CI and the README; a second upgrade changes nothing."""
    from scio.product.scaffold import BROWSER_INSTALL, PYTHON_HTTP_INSTALL

    fresh = _v090_python_product(tmp_path, install=install)
    ci_path = tmp_path / ".github/workflows/ci.yml"
    assert "playwright" not in ci_path.read_text(encoding="utf-8")
    report = upgrade_product(LocalSandbox(tmp_path))
    change = f"{PYTHON_HTTP_INSTALL!r} and {BROWSER_INSTALL!r}, so the browser tests run"
    assert report.added[-2:] == [f".github/workflows/ci.yml: {change}", f"README.md: {change}"]
    ci = ci_path.read_text(encoding="utf-8")
    assert ci == fresh[".github/workflows/ci.yml"]
    assert (
        "      - run: pip install -e . ruff pytest playwright\n"
        "      - run: python -m playwright install --with-deps chromium\n"
    ) in ci
    assert ci.count("playwright install") == 1 and ci.count("pip install") == 1
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == fresh["README.md"]
    head = _git(tmp_path, "rev-parse", "HEAD")
    again = upgrade_product(LocalSandbox(tmp_path))
    assert again.added == [] and again.commit is None
    assert ".github/workflows/ci.yml browser install (exists)" in again.kept
    assert _git(tmp_path, "rev-parse", "HEAD") == head and _git(tmp_path, "status", "-s") == ""
    assert ci_path.read_text(encoding="utf-8") == ci


def test_upgrade_names_an_http_ci_it_cannot_give_a_browser(tmp_path: Path) -> None:
    """An install line the product changed is its own (I-142): the file is left as it is,
    and the report says what to add by hand."""
    _v090_python_product(tmp_path, install="pip install ruff pytest sympy")
    ci_path = tmp_path / ".github/workflows/ci.yml"
    before = ci_path.read_text(encoding="utf-8")
    report = upgrade_product(LocalSandbox(tmp_path))
    assert ci_path.read_text(encoding="utf-8") == before
    assert [a for a in report.added if a.startswith((".github", "README.md:"))] == []
    assert any(
        k.startswith(".github/workflows/ci.yml (no scaffold install line; add ")
        for k in report.kept
    )


def test_upgrade_keeps_an_install_line_and_build_system_the_product_changed(
    tmp_path: Path,
) -> None:
    """I-142: only the old scaffold's exact line is SCIO's; a line the product changed (here
    it names sympy) and a build system of its own (graphcalc's) are left alone."""
    _v090_python_product(tmp_path)
    ci = tmp_path / ".github" / "workflows" / "ci.yml"
    ci.write_text(
        ci.read_text(encoding="utf-8").replace(
            "pip install ruff pytest\n", "pip install ruff pytest sympy\n"
        ),
        encoding="utf-8",
        newline="\n",
    )
    pyproject = tmp_path / "pyproject.toml"
    own = '[build-system]\nrequires = ["flit_core>=3.9"]\nbuild-backend = "flit_core.buildapi"\n'
    pyproject.write_text(own + pyproject.read_text(encoding="utf-8"), encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-am", "chore: our own install line and build system")
    report = upgrade_product(LocalSandbox(tmp_path))
    assert "[build-system] (the product's own)" in report.kept
    assert [a for a in report.added if a.startswith(".github")] == []
    assert "      - run: pip install ruff pytest sympy\n" in ci.read_text(encoding="utf-8")
    assert pyproject.read_text(encoding="utf-8").startswith(own)
