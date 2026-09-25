def test_upstream_modules_import():
    import app.call_loop  # noqa: F401
    import app.call_manager  # noqa: F401
    import app.handler.voicelive_media_handler  # noqa: F401
    import app.providers.acs.media_handler  # noqa: F401


def test_load_server_returns_the_quart_app(load_server):
    server = load_server()
    assert hasattr(server, "app")


def test_load_server_with_acs_env(load_server):
    from tests.helpers import acs_env

    server = load_server(**acs_env())
    assert hasattr(server, "app")
