import logging

from src.log import build_logger, redact


def test_redact_replaces_password_substring():
    redacted = redact("login attempt user=alice pw=hunter2 done", "hunter2")
    assert "hunter2" not in redacted
    assert "***" in redacted


def test_redact_handles_empty_secret():
    assert redact("nothing to redact", "") == "nothing to redact"


def test_redact_replaces_all_occurrences():
    out = redact("hunter2 then hunter2 again", "hunter2")
    assert "hunter2" not in out
    assert out.count("***") == 2


def test_logger_redacts_password_in_messages(caplog):
    logger = build_logger("test", secret="hunter2")
    with caplog.at_level(logging.INFO, logger="test"):
        logger.info("submitting password=hunter2 to server")
    record = caplog.records[-1]
    assert "hunter2" not in record.getMessage()
    assert "***" in record.getMessage()


def test_logger_writes_to_stderr(capsys):
    logger = build_logger("test2", secret="x")
    logger.info("hello")
    captured = capsys.readouterr()
    assert "hello" in captured.err
