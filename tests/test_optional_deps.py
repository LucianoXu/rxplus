"""Tests for optional dependency handling.

These tests verify that the package can be imported correctly depending on
which optional dependencies are installed.
"""

import sys


def test_basic_imports():
    """Test that basic imports work without optional dependencies."""
    import rxplus

    # Core exports should always be available
    assert hasattr(rxplus, "RxException")
    assert hasattr(rxplus, "TaggedData")
    assert hasattr(rxplus, "tag")
    assert hasattr(rxplus, "untag")
    assert hasattr(rxplus, "FPSMonitor")
    assert hasattr(rxplus, "BandwidthMonitor")

    # Logging exports
    assert hasattr(rxplus, "LogItem")
    assert hasattr(rxplus, "LOG_LEVEL")
    assert hasattr(rxplus, "Logger")

    # WebSocket exports
    assert hasattr(rxplus, "RxWSServer")
    assert hasattr(rxplus, "RxWSClient")
    assert hasattr(rxplus, "RxWSClientGroup")

    # Duplex exports
    assert hasattr(rxplus, "Duplex")
    assert hasattr(rxplus, "make_duplex")
    assert hasattr(rxplus, "connect_adapter")

    # CLI exports
    assert hasattr(rxplus, "from_cli")

    # Opt exports
    assert hasattr(rxplus, "redirect_to")
    assert hasattr(rxplus, "stream_print_out")


def test_audio_feature_flags():
    """Test audio feature detection flags."""
    import rxplus

    # _HAS_AUDIO should be defined
    assert hasattr(rxplus, "_HAS_AUDIO")

    if rxplus._HAS_AUDIO:
        # If audio is available, these should exist
        assert hasattr(rxplus, "PCMFormat")
        assert hasattr(rxplus, "RxMicrophone")
        assert hasattr(rxplus, "RxSpeaker")
        assert hasattr(rxplus, "create_wavfile")
        assert hasattr(rxplus, "save_wavfile")
    else:
        # If audio is not available, these should NOT exist
        assert not hasattr(rxplus, "PCMFormat")
        assert not hasattr(rxplus, "RxMicrophone")


def test_video_feature_flags():
    """Test video feature detection flags."""
    import rxplus

    # _HAS_VIDEO should be defined
    assert hasattr(rxplus, "_HAS_VIDEO")

    if rxplus._HAS_VIDEO:
        # If video is available, these should exist
        assert hasattr(rxplus, "create_screen_capture")
        assert hasattr(rxplus, "rgb_ndarray_to_jpeg_bytes")
        assert hasattr(rxplus, "jpeg_bytes_to_rgb_ndarray")
    else:
        # If video is not available, these should NOT exist
        assert not hasattr(rxplus, "create_screen_capture")
        assert not hasattr(rxplus, "rgb_ndarray_to_jpeg_bytes")


def test_all_exports():
    """Test that __all__ is properly defined."""
    import rxplus

    # Basic exports should always be in __all__
    basic_exports = [
        "RxException",
        "TaggedData",
        "tag",
        "untag",
        "FPSMonitor",
        "BandwidthMonitor",
        "LogItem",
        "LOG_LEVEL",
        "Logger",
        "RxWSServer",
        "RxWSClient",
        "RxWSClientGroup",
        "Duplex",
        "make_duplex",
        "connect_adapter",
        "from_cli",
    ]

    for export in basic_exports:
        assert export in rxplus.__all__, f"{export} not in __all__"


def run_installation_test():
    """Run a quick installation test and print results."""
    import rxplus

    print("=== rxplus Installation Test ===")
    print()
    print("Basic imports: OK")
    print(f"  - rxplus.RxException: {rxplus.RxException}")
    print(f"  - rxplus.RxWSClient: {rxplus.RxWSClient}")
    print(f"  - rxplus.Duplex: {rxplus.Duplex}")
    print()
    print(f"Audio support: {'AVAILABLE' if rxplus._HAS_AUDIO else 'NOT AVAILABLE'}")
    if rxplus._HAS_AUDIO:
        print(f"  - rxplus.RxMicrophone: {rxplus.RxMicrophone}")
        print(f"  - rxplus.RxSpeaker: {rxplus.RxSpeaker}")
    print()
    print(f"Video support: {'AVAILABLE' if rxplus._HAS_VIDEO else 'NOT AVAILABLE'}")
    if rxplus._HAS_VIDEO:
        print(f"  - rxplus.create_screen_capture: {rxplus.create_screen_capture}")
    print()
    print("Installation test PASSED!")


if __name__ == "__main__":
    run_installation_test()
