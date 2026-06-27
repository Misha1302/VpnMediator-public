from vpn_access_bot.support_security import contains_secret_material


def test_detects_subscription_and_admin_secrets() -> None:
    assert contains_secret_material("https://vpn.example/sub/abc/devices/def?token=secret")
    assert contains_secret_material("X-Admin-Token: abc")
    assert contains_secret_material("claim=secret")


def test_allows_normal_support_text() -> None:
    assert not contains_secret_material("Happ не обновляет список серверов после нажатия обновить")


def test_detects_happ_deep_link_with_encoded_subscription_url() -> None:
    assert contains_secret_material(
        "happ://add?url=https%3A%2F%2Fvpn.example%2Fsub%2Fabc%3Ftoken%3Dsecret"
    )


def test_detects_double_encoded_subscription_url() -> None:
    assert contains_secret_material(
        "happ://add?url=https%253A%252F%252Fvpn.example%252Fsub%252Fabc%253Ftoken%253Dsecret"
    )


def test_detects_encoded_secret_in_fragment() -> None:
    assert contains_secret_material(
        "happ://add#url=https%3A%2F%2Fvpn.example%2Fsub%2Fabc%3Fclaim%3Dsecret"
    )


def test_allows_happ_application_link_without_subscription_secret() -> None:
    assert not contains_secret_material("happ://open?screen=settings")
