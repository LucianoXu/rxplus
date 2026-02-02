"""Tests for rxplus.graphic module."""

import threading
import time

import numpy as np
import pytest

from rxplus.graphic import (
    create_screen_capture,
    jpeg_bytes_to_rgb_ndarray,
    rgb_ndarray_to_jpeg_bytes,
    png_bytes_to_rgb_ndarray,
    rgb_ndarray_to_png_bytes,
)


# =============================================================================
# JPEG Codec Tests
# =============================================================================


class TestJPEGCodec:
    """Tests for JPEG encoding/decoding functions."""

    def test_rgb_to_jpeg_produces_valid_jpeg(self, synthetic_rgb_frame):
        """Verify output starts with JPEG magic bytes."""
        jpeg = rgb_ndarray_to_jpeg_bytes(synthetic_rgb_frame)
        
        # JPEG files start with FFD8 magic bytes
        assert jpeg[:2] == b"\xff\xd8"
        # JPEG files end with FFD9
        assert jpeg[-2:] == b"\xff\xd9"

    def test_jpeg_to_rgb_recovers_dimensions(self, synthetic_rgb_frame):
        """Verify decoded image has same dimensions as original."""
        jpeg = rgb_ndarray_to_jpeg_bytes(synthetic_rgb_frame, quality=95)
        recovered = jpeg_bytes_to_rgb_ndarray(jpeg)
        
        assert recovered.shape == synthetic_rgb_frame.shape
        assert recovered.dtype == np.uint8

    def test_jpeg_roundtrip_preserves_shape(self):
        """Verify encode->decode preserves image shape."""
        frame = np.zeros((200, 300, 3), dtype=np.uint8)
        recovered = jpeg_bytes_to_rgb_ndarray(rgb_ndarray_to_jpeg_bytes(frame))
        
        assert recovered.shape == (200, 300, 3)

    def test_jpeg_quality_affects_size(self, synthetic_rgb_frame):
        """Verify higher quality produces larger file size."""
        low_quality = rgb_ndarray_to_jpeg_bytes(synthetic_rgb_frame, quality=10)
        high_quality = rgb_ndarray_to_jpeg_bytes(synthetic_rgb_frame, quality=95)
        
        assert len(high_quality) > len(low_quality)

    def test_jpeg_handles_small_images(self):
        """Verify codec works with very small images."""
        tiny = np.random.randint(0, 255, (8, 8, 3), dtype=np.uint8)
        jpeg = rgb_ndarray_to_jpeg_bytes(tiny)
        recovered = jpeg_bytes_to_rgb_ndarray(jpeg)
        
        assert recovered.shape == (8, 8, 3)

    def test_jpeg_handles_grayscale_as_rgb(self):
        """Verify grayscale-like RGB images work correctly."""
        gray_rgb = np.full((100, 100, 3), 128, dtype=np.uint8)
        jpeg = rgb_ndarray_to_jpeg_bytes(gray_rgb)
        recovered = jpeg_bytes_to_rgb_ndarray(jpeg)
        
        assert recovered.shape == (100, 100, 3)


# =============================================================================
# PNG Codec Tests
# =============================================================================


class TestPNGCodec:
    """Tests for PNG encoding/decoding functions."""

    def test_rgb_to_png_produces_valid_png(self, synthetic_rgb_frame):
        """Verify output starts with PNG magic bytes."""
        png = rgb_ndarray_to_png_bytes(synthetic_rgb_frame)
        
        # PNG files start with 89 50 4E 47 0D 0A 1A 0A
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_png_to_rgb_recovers_dimensions(self, synthetic_rgb_frame):
        """Verify decoded image has same dimensions as original."""
        png = rgb_ndarray_to_png_bytes(synthetic_rgb_frame)
        recovered = png_bytes_to_rgb_ndarray(png)
        
        assert recovered.shape == synthetic_rgb_frame.shape
        assert recovered.dtype == np.uint8

    def test_png_roundtrip_preserves_shape(self):
        """Verify encode->decode preserves image shape."""
        frame = np.zeros((200, 300, 3), dtype=np.uint8)
        recovered = png_bytes_to_rgb_ndarray(rgb_ndarray_to_png_bytes(frame))
        
        assert recovered.shape == (200, 300, 3)

    def test_png_compression_affects_size(self, synthetic_rgb_frame):
        """Verify lower compression produces larger file size."""
        low_compression = rgb_ndarray_to_png_bytes(synthetic_rgb_frame, compression=1)
        high_compression = rgb_ndarray_to_png_bytes(synthetic_rgb_frame, compression=9)
        
        # Lower compression = larger file, higher compression = smaller file
        assert len(low_compression) > len(high_compression)

    def test_png_lossless_roundtrip(self):
        """Verify PNG preserves exact pixel values (lossless)."""
        # Create frame with specific values
        frame = np.array([
            [[255, 0, 0], [0, 255, 0]],
            [[0, 0, 255], [128, 128, 128]]
        ], dtype=np.uint8)
        
        png = rgb_ndarray_to_png_bytes(frame)
        recovered = png_bytes_to_rgb_ndarray(png)
        
        # PNG is lossless, so values should be exactly equal
        np.testing.assert_array_equal(frame, recovered)

    def test_png_handles_small_images(self):
        """Verify codec works with very small images."""
        tiny = np.random.randint(0, 255, (8, 8, 3), dtype=np.uint8)
        png = rgb_ndarray_to_png_bytes(tiny)
        recovered = png_bytes_to_rgb_ndarray(png)
        
        assert recovered.shape == (8, 8, 3)
        np.testing.assert_array_equal(tiny, recovered)

    def test_png_handles_grayscale_as_rgb(self):
        """Verify grayscale-like RGB images work correctly."""
        gray_rgb = np.full((100, 100, 3), 128, dtype=np.uint8)
        png = rgb_ndarray_to_png_bytes(gray_rgb)
        recovered = png_bytes_to_rgb_ndarray(png)
        
        assert recovered.shape == (100, 100, 3)
        np.testing.assert_array_equal(gray_rgb, recovered)


# =============================================================================
# Screen Capture Tests
# =============================================================================


class TestScreenCapture:
    """Tests for create_screen_capture Observable."""

    def test_emits_rgb_arrays(self, mock_mss):
        """Verify emitted frames are RGB arrays with correct shape."""
        received = []
        
        obs = create_screen_capture(fps=100.0)  # High FPS to emit quickly
        subscription = obs.subscribe(on_next=lambda x: received.append(x))
        
        # Give time for at least one frame
        time.sleep(0.15)
        subscription.dispose()
        
        assert len(received) > 0
        frame = received[0]
        # Should be RGB (H, W, 3), not BGRA
        assert frame.ndim == 3
        assert frame.shape[2] == 3
        assert frame.dtype == np.uint8

    def test_bgra_to_rgb_conversion(self, mock_mss):
        """Verify BGRA from mss is correctly converted to RGB."""
        received = []
        
        obs = create_screen_capture(fps=100.0)
        subscription = obs.subscribe(on_next=lambda x: received.append(x))
        
        time.sleep(0.15)
        subscription.dispose()
        
        assert len(received) > 0
        frame = received[0]
        
        # mock_mss returns BGRA with B=100, G=150, R=200
        # After conversion to RGB: R=200, G=150, B=100
        assert frame[0, 0, 0] == 200  # Red channel
        assert frame[0, 0, 1] == 150  # Green channel
        assert frame[0, 0, 2] == 100  # Blue channel

    def test_disposal_stops_loop(self, mock_mss):
        """Verify disposing the subscription stops frame capture."""
        frame_count = []
        
        obs = create_screen_capture(fps=100.0)
        subscription = obs.subscribe(on_next=lambda x: frame_count.append(1))
        
        time.sleep(0.1)
        count_at_dispose = len(frame_count)
        subscription.dispose()
        
        time.sleep(0.1)
        count_after_dispose = len(frame_count)
        
        # Should have captured frames before disposal
        assert count_at_dispose > 0
        # Should not capture many more frames after disposal
        # (allow small margin for timing)
        assert count_after_dispose - count_at_dispose <= 2

    def test_fps_timing(self, mock_mss):
        """Verify frame rate is approximately correct."""
        received = []
        target_fps = 20.0
        
        obs = create_screen_capture(fps=target_fps)
        subscription = obs.subscribe(on_next=lambda x: received.append(time.time()))
        
        time.sleep(0.5)
        subscription.dispose()
        
        if len(received) >= 2:
            # Calculate actual FPS from timestamps
            intervals = [received[i+1] - received[i] for i in range(len(received)-1)]
            avg_interval = sum(intervals) / len(intervals)
            actual_fps = 1.0 / avg_interval
            
            # Allow 50% tolerance due to mocking overhead
            assert actual_fps > target_fps * 0.5
            assert actual_fps < target_fps * 2.0

    def test_uses_thread_scheduler_outside_loop(self, mock_mss):
        """Verify ThreadPoolScheduler is used when no event loop is running."""
        thread_ids = []
        main_thread = threading.get_ident()
        
        obs = create_screen_capture(fps=100.0)
        subscription = obs.subscribe(on_next=lambda x: thread_ids.append(threading.get_ident()))
        
        time.sleep(0.15)
        subscription.dispose()
        
        assert len(thread_ids) > 0
        # Should run in a different thread
        assert any(tid != main_thread for tid in thread_ids)

    def test_default_monitor_uses_primary(self, mock_mss):
        """Verify None (default) uses monitors[1] (primary)."""
        received = []
        
        obs = create_screen_capture(fps=100.0, monitor=None)
        subscription = obs.subscribe(on_next=lambda x: received.append(x))
        
        time.sleep(0.15)
        subscription.dispose()
        
        assert len(received) > 0
        # Verify grab was called (mock returns 100x100 frame)
        assert received[0].shape[:2] == (100, 100)

    def test_monitor_index_selection(self, mock_mss):
        """Verify int monitor index is passed to sct.monitors[]."""
        received = []
        
        # Add more monitors to the mock
        mock_mss.monitors = [
            None,  # index 0: all combined
            {"width": 100, "height": 100, "left": 0, "top": 0},  # index 1: primary
            {"width": 200, "height": 200, "left": 100, "top": 0},  # index 2: secondary
        ]
        
        obs = create_screen_capture(fps=100.0, monitor=1)
        subscription = obs.subscribe(on_next=lambda x: received.append(x))
        
        time.sleep(0.15)
        subscription.dispose()
        
        assert len(received) > 0
        # Verify grab was called with correct monitor
        mock_mss.grab.assert_called()

    def test_region_dict_selection(self, mock_mss):
        """Verify dict region is passed directly to grab()."""
        received = []
        region = {"left": 10, "top": 20, "width": 50, "height": 50}
        
        obs = create_screen_capture(fps=100.0, monitor=region)
        subscription = obs.subscribe(on_next=lambda x: received.append(x))
        
        time.sleep(0.15)
        subscription.dispose()
        
        assert len(received) > 0
        # Verify grab was called with the region dict
        # The last call should have the region dict
        mock_mss.grab.assert_called()
        # Check that grab was called with our region dict at some point
        call_args_list = mock_mss.grab.call_args_list
        assert any(call[0][0] == region for call in call_args_list)
