def test_upstream_modules_import():
    import app.call_loop  # noqa: F401
    import app.call_manager  # noqa: F401
    import app.handler.voicelive_media_handler  # noqa: F401
    import app.providers.acs.media_handler  # noqa: F401
