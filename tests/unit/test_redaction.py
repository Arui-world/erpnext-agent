from erpnext_agent.observability.redaction import redact


def test_redaction_recurses_without_changing_regular_data() -> None:
    value = {
        "authorization": "Bearer secret",
        "nested": {"access_token": "secret", "name": "SO-1"},
    }
    assert redact(value) == {
        "authorization": "[REDACTED]",
        "nested": {"access_token": "[REDACTED]", "name": "SO-1"},
    }

