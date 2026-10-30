"""Hermes's text cleaner writes units and symbols as English words; Czech voices must not say them."""
import importlib


def czech(text: str) -> str:
    """Hermes's real cleaner, then the plugin's Czech fix-up (what a Czech voice gets)."""
    from tools.tts_text_normalize import prepare_spoken_text
    piper_stream = importlib.import_module("hermes_plugins.hermes_3310.piper_stream")
    return piper_stream.czech_spoken(prepare_spoken_text(text, max_chars=None))


def test_units_currencies_and_symbols_are_read_in_czech(hermes):
    hermes.load()
    spoken = czech("Venku je 20 °C, zítra 11-17 °C, vlhkost 80 %, vítr 50 km/h, sníh 5 cm, "
                   "lístek stojí €5 nebo $4, parkování 5/den, R&D tým to zvládne ~ za hodinu.")

    for english in ("degrees", "Celsius", "percent", "kilometres", "per", "euros", "dollars", "centimetres",
                    " and ", "about"):
        assert english not in spoken, f"{english!r} left in: {spoken}"
    for word in ("20 stupňů Celsia", "11 až 17 stupňů Celsia", "80 procent", "50 kilometrů za hodinu",
                 "5 centimetrů", "5 eur", "4 dolarů", "5 za den", "R a D"):
        assert word in spoken, f"{word!r} missing in: {spoken}"


def test_plain_czech_is_left_alone(hermes):
    hermes.load()
    text = "Dobrý den, zítra bude v Praze polojasno a odpoledne může sprchnout."

    assert czech(text) == text
