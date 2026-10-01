"""Reply-language rule: quick coverage (REPLY_LANGUAGE, message_language, mode_reply, stop_reply).
Not a full rebuild of the earlier A/B/E session's suite - just enough to catch a regression fast."""
import sys
from unittest import mock

from _helpers import jc, run_module_tests


def test_reply_language_defaults_to_english_always():
    assert jc.REPLY_LANGUAGE == "en"
    for text in ["ola, tudo bem?", "hello", "modo serio", "", "oi\n\n[Reply in Portuguese.]"]:
        assert jc.message_language(text) == "en", text


def test_message_language_mirrors_when_reply_language_is_none():
    with mock.patch.object(jc, "REPLY_LANGUAGE", None):
        assert jc.message_language("ola, tudo bem?") == "pt"
        assert jc.message_language("hello there") == "en"
        assert jc.message_language("oi\n\n[Reply in Portuguese.]") == "pt"
        assert jc.message_language("hi\n\n[Reply in English.]") == "en"


def test_mode_reply_follows_reply_language():
    assert jc.mode_reply(("serious", "pt")) == "Serious mode activating."
    assert jc.mode_reply(("normal", "pt")) == "Back to normal."
    with mock.patch.object(jc, "REPLY_LANGUAGE", None):
        assert jc.mode_reply(("serious", "pt")) == "Modo sério ativado."
        assert jc.mode_reply(("normal", "pt")) == "Modo normal."


def test_stop_reply_follows_reply_language():
    assert jc.stop_reply("pt", True) == "Cancelled." and jc.stop_reply("pt", False) == "Nothing to cancel."
    with mock.patch.object(jc, "REPLY_LANGUAGE", None):
        assert jc.stop_reply("pt", True) == "Cancelado." and jc.stop_reply("en", False) == "Nothing to cancel."


if __name__ == "__main__":
    run_module_tests(sys.modules[__name__])
