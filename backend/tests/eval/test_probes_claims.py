from app.eval.probes import claim_degraded


def test_real_assertions_not_degraded():
    # 此前因动词表缺词被误杀的真断言(主库实测样本)
    for s in [
        "MOS transistors continue to become faster, but at the cost of their 'analog' properties.",
        "Digital transition detector detects pulses and activates error detectors",
        "The oft-used Bode method falls short in some common systems",
        "Error signals filtered and converted to adjust VGA gain and VCO frequency",
        "The write precompensation circuitry delays the writing of the second 'one' to counter the shift",
    ]:
        assert claim_degraded(s) is False, s


def test_toc_titles_and_meta_still_degraded():
    for s in [
        "Relation Between Frequency Response and Time Response",  # 目录标题, 无动词
        "Effect of Negative Feedback on Distortion",
        "This book deals with the analysis and design of analog CMOS integrated circuits",  # 元叙述
        "Study of FinFETs",  # <4 词
    ]:
        assert claim_degraded(s) is True, s
