def test_package_imports():
    import hub
    import hub.secrets

    assert hub.secrets.TELEGRAM_BOT_TOKEN
