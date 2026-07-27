from dreeve_garmin_connector import __version__


def test_the_package_is_importable_and_versioned() -> None:
    # Guards the container wiring: the source lives on a bind mount while the venv
    # lives outside it, so an importable package is not a given.
    assert __version__
