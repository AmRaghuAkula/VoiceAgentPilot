"""Smoke test: the package imports."""


def test_package_imports():
    import calendar_tools

    assert calendar_tools.__name__ == "calendar_tools"
