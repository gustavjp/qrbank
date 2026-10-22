from app.security import (
    generate_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)


def test_password_hash_is_not_plaintext_and_verifies() -> None:
    password = "correct horse battery staple"
    encoded = hash_password(password)

    assert encoded != password
    assert verify_password(password, encoded)
    assert not verify_password("wrong password", encoded)


def test_session_tokens_are_random_and_hash_deterministically() -> None:
    first = generate_session_token()
    second = generate_session_token()

    assert first != second
    assert hash_session_token(first) == hash_session_token(first)
    assert hash_session_token(first) != hash_session_token(second)
    assert len(hash_session_token(first)) == 32
