from erpnext_agent.actions.gateway import canonicalize_arguments


def test_action_argument_hash_is_order_independent() -> None:
    first, first_hash = canonicalize_arguments(
        {"doctype": "Sales Order", "payload": {"b": 2, "a": 1}}
    )
    second, second_hash = canonicalize_arguments(
        {"payload": {"a": 1, "b": 2}, "doctype": "Sales Order"}
    )
    assert first == second
    assert first_hash == second_hash


def test_action_argument_hash_changes_with_parameters() -> None:
    _, first_hash = canonicalize_arguments({"name": "SO-1"})
    _, second_hash = canonicalize_arguments({"name": "SO-2"})
    assert first_hash != second_hash
