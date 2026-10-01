"""Two guards in main.App, on the real app (no SDL): a frame with no pixel buffer is skipped and repainted
instead of crashing, and the YouTube Firefox is stopped at exit instead of being left holding the strip."""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
import main  # noqa: E402
from test_charge_stuck_app import Case  # noqa: E402


class RenderWithoutPixels(Case):
    def app_ready_to_paint(self, pixels):
        app = self.build()
        app.sdl = mock.MagicMock()
        app.canvas = mock.MagicMock()
        app.canvas.pixels.return_value = pixels
        app.tex = app.ren = object()
        app.layer.presented = mock.Mock()
        app.ui.root.paint = mock.Mock()
        app.ui.root.add_damage((0, 0, 40, 20))
        return app

    def test_a_missing_pixel_buffer_skips_the_frame_and_repaints_next_time(self):
        app = self.app_ready_to_paint((None, 0))
        with self.assertLogs(level="WARNING") as logs:
            app._render()
        app.sdl.UpdateTexture.assert_not_called()
        app.sdl.RenderPresent.assert_not_called()
        self.assertIsNotNone(app.ui.root.peek_damage(), "the damage was not put back")
        self.assertEqual(len([r for r in logs.records if "no pixel data" in r.getMessage()]), 1)

    def test_it_warns_once_not_every_frame(self):
        app = self.app_ready_to_paint((None, 0))
        with self.assertLogs(level="WARNING") as logs:
            app._render()
            app.ui.root.add_damage((0, 0, 40, 20))
            app._render()
        self.assertEqual(len([r for r in logs.records if "no pixel data" in r.getMessage()]), 1)

    def test_with_pixels_the_same_setup_presents_a_frame(self):
        app = self.app_ready_to_paint((1000, 4))
        with mock.patch.object(main, "byref", lambda x: x):
            app._render()
        app.sdl.UpdateTexture.assert_called_once()
        app.sdl.RenderPresent.assert_called_once()


class YtAppStopsAtExit(Case):
    def test_shutdown_stops_the_youtube_firefox(self):
        app = self.build()
        app.ytapp = mock.Mock()
        app.shutdown()
        app.ytapp.shutdown.assert_called_once_with()

    def test_a_stuck_youtube_stop_does_not_block_the_rest(self):
        app = self.build()
        app.ytapp = mock.Mock()
        app.ytapp.shutdown.side_effect = RuntimeError("stuck")
        app.web = mock.Mock()
        with self.assertLogs(level="ERROR"):
            app.shutdown()
        app.web.shutdown.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
