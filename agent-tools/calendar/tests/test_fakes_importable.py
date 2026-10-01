"""Regression guard for `pythonpath = ["."]` (needed with --import-mode=importlib)."""

import importlib


def test_tests_fakes_and_tools_import():
    assert importlib.import_module("tests.fakes.clock").FakeClock
    assert importlib.import_module("tools.render_openapi").render
