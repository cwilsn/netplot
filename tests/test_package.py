from netplot import __version__
from netplot.web import create_app


def test_package_has_version() -> None:
    assert __version__ == "0.1.0"


def test_web_app_can_be_created() -> None:
    app = create_app()

    assert app.title == "netplot"
