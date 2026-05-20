from src.parallel_runner import BookingResult, artifact_path


def test_booking_result_has_expected_members():
    assert BookingResult.SUCCESS.name == "SUCCESS"
    assert BookingResult.OCCUPIED.name == "OCCUPIED"
    assert BookingResult.ERROR.name == "ERROR"


def test_artifact_path_namespaces_by_session():
    assert artifact_path("pre_submit", "s0").name == "pre_submit_s0.png"
    assert artifact_path("post_submit", "s2").name == "post_submit_s2.png"
