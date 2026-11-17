"""A product's CI installs what the product declares (I-140, the graphcalc run's S6): the
0.9.0 python scaffold's CI ran ``pip install ruff pytest`` only, so the first feature that
imported an approved dependency failed the PR's CI at collection while every check SCIO ran
in its sandbox venv passed. The clean-venv proof of the current scaffold is in the ledger;
this pins its shape."""

from __future__ import annotations

import re
import tomllib

import pytest

from scio.product.scaffold import (
    BROWSER_INSTALL,
    OLD_PYTHON_INSTALLS,
    PYTHON_HTTP_INSTALL,
    PYTHON_INSTALL,
    python_install_lines,
    scaffold_for,
)
from scio.state.models import AppSpec, ProductFeature, ProductSpec

# `pip install` of the project itself: `.`, `-e .`, `.[extra]`
SELF_INSTALL = re.compile(r"\bpip install\b.*(?:^|\s)(?:-e\s+)?\.(?:\[[^\]]*\])?(?:\s|$)")


def _spec(kind: str) -> ProductSpec:
    return ProductSpec(
        name="shop_api",
        title="Shop API",
        vision="v",
        users=["u"],
        features=[ProductFeature(id="P1", description="skeleton", acceptance=["starts"])],
        quality_bar=["q"],
        stack="python",
        dependencies=["sympy", "requests>=2"],
        app=AppSpec(kind="http", module="shop_api", args=["serve", "--port", "{port}"])
        if kind == "http"
        else AppSpec(),
    )


@pytest.mark.parametrize("kind", ["cli", "http"])
def test_a_python_products_ci_and_develop_block_install_the_project(kind: str) -> None:
    spec = _spec(kind)
    files = scaffold_for(spec)
    ci = files[".github/workflows/ci.yml"]
    installs = [ln for ln in ci.splitlines() if "pip install" in ln]
    # an http product's line also names the browser driver (issue #169); it starts the same
    install = python_install_lines(spec)[0]
    assert install == (PYTHON_INSTALL if kind == "cli" else PYTHON_HTTP_INSTALL)
    assert install.startswith(PYTHON_INSTALL)
    assert installs == [f"      - run: {install}"]
    assert SELF_INSTALL.search(install)
    assert not any(SELF_INSTALL.search(old) for old in OLD_PYTHON_INSTALLS)  # what upgrade fixes
    develop = files["README.md"].split("## Develop", 1)[1]
    assert f"\n{install}\n" in develop
    # `pip install -e .` must not depend on setuptools guessing the packages: it refuses a
    # flat layout with a second top-level directory (`static/`, say)
    data = tomllib.loads(files["pyproject.toml"])
    assert data["build-system"]["build-backend"] == "setuptools.build_meta"
    assert data["tool"]["setuptools"]["packages"]["find"]["include"] == [
        "shop_api",
        "shop_api.*",
    ]
    assert sorted(data["project"]["dependencies"]) == ["requests>=2", "sympy"]
    assert files["pyproject.toml"].rstrip().endswith('omit = ["*/__main__.py"]')  # upgrade's split


def test_an_http_products_ci_installs_the_browser() -> None:
    """Issue #169 (I-242): graphcalc's CI read ``360 passed, 5 skipped`` because its install
    line had no browser, so the module that drives the page skipped. A python product that
    serves pages installs Playwright and Chromium in CI; a cli product is as it was."""
    http = scaffold_for(_spec("http"))
    steps = [ln for ln in http[".github/workflows/ci.yml"].splitlines() if "- run: " in ln]
    assert steps[:2] == [
        "      - run: pip install -e . ruff pytest playwright",
        "      - run: python -m playwright install --with-deps chromium",
    ]
    assert PYTHON_HTTP_INSTALL == "pip install -e . ruff pytest playwright"
    assert BROWSER_INSTALL == "python -m playwright install --with-deps chromium"
    assert SELF_INSTALL.search(PYTHON_HTTP_INSTALL)  # still installs the project (I-140)
    develop = http["README.md"].split("## Develop", 1)[1]
    assert f"\n{PYTHON_HTTP_INSTALL}\n{BROWSER_INSTALL}\n" in develop
    cli = scaffold_for(_spec("cli"))
    for text in (cli[".github/workflows/ci.yml"], cli["README.md"]):
        assert "playwright" not in text
    assert f"      - run: {PYTHON_INSTALL}\n" in cli[".github/workflows/ci.yml"]
    node = scaffold_for(_spec("http").model_copy(update={"stack": "node-ts"}))
    assert "playwright" not in node[".github/workflows/ci.yml"] + node["README.md"]
