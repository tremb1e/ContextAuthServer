"""UI surface / bounds-occupancy scale invariance + degeneracy guards — P0-1.

Regression tests for the 2026-07-04 unit-mismatch fix. The old ``_bounds_area``
normalised node area by a fixed ``1080*1920`` pixel screen, but the on-device
``bounds_grid`` is ``pixels // 24``, so every real node area was ~0.01 and
``ui_surface_like`` / ``ui_bounds_occupancy`` were a constant 0. The fix derives
a per-snapshot screen extent from the nodes themselves (scale-free) and counts
``ui_surface_like`` only for large MEDIA/CANVAS *surface* classes, so a plain
full-screen root *container* (always present in real UIs) does not trigger it.

Asserts:

* a large surface class fires ``ui_surface_like`` at BOTH pixel and ``//24`` grid
  scale (scale invariance);
* a full-screen root *container* with NO surface class does NOT fire it (the
  degeneracy the fix targets — otherwise every snapshot reads 1);
* a small surface thumbnail does NOT fire it;
* off-window sentinel / degenerate bounds are ignored without error;
* ``ui_bounds_occupancy`` stays within ``[0, 1]``.
"""

from __future__ import annotations

from typing import Any

from research.preprocessing.feature_extractors import extract_window_features

_SENTINEL = 89_478_485


def _node(cls: str, left: int, top: int, right: int, bottom: int, **flags: Any) -> dict[str, Any]:
    """Build a minimal node dict with a class name and grid bounds."""
    node = {
        "class_name": cls,
        "bounds_grid": {"left": left, "top": top, "right": right, "bottom": bottom},
        "depth": 0,
    }
    node.update(flags)
    return node


def _ui_ctx(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a single node snapshot in a minimal UI-only window context."""
    return {
        "imu_samples": None,
        "events": [],
        "nodes_snapshots": [nodes],
        "prev_snapshot": None,
        "window_duration_sec": 5.0,
    }


def _surface_like(nodes: list[dict[str, Any]]) -> float:
    """``ui_surface_like`` for a snapshot via the real extractor (ui_only mode)."""
    feats = extract_window_features(_ui_ctx(nodes), feature_mode="ui_only")
    return feats["ui_surface_like"]


def test_large_surface_fires_at_grid_scale() -> None:
    """A full-screen SurfaceView at ``//24`` grid scale sets ui_surface_like=1."""
    nodes = [
        _node("android.widget.FrameLayout", 0, 0, 45, 80),  # full-screen container
        _node("android.view.SurfaceView", 0, 0, 45, 80),    # the media surface
        _node("android.widget.TextView", 2, 2, 20, 6),
    ]
    assert _surface_like(nodes) == 1.0


def test_large_surface_fires_at_pixel_scale() -> None:
    """The SAME layout at pixel scale also fires — the test is scale invariant."""
    nodes = [
        _node("android.widget.FrameLayout", 0, 0, 1080, 1920),
        _node("android.view.SurfaceView", 0, 0, 1080, 1920),
        _node("android.widget.TextView", 40, 40, 520, 140),
    ]
    assert _surface_like(nodes) == 1.0


def test_videoview_counts_as_surface() -> None:
    """VideoView (real I0 media class) is treated as a surface even though its
    name lacks the 'surface'/'texture' substring."""
    nodes = [
        _node("android.widget.FrameLayout", 0, 0, 60, 133),
        _node("android.widget.VideoView", 0, 0, 60, 120),
    ]
    assert _surface_like(nodes) == 1.0


def test_fullscreen_container_without_surface_does_not_fire() -> None:
    """A full-screen root *container* (no surface class) must NOT fire — this is
    the degeneracy the fix targets (real UIs always nest full-screen layouts)."""
    nodes = [
        _node("android.widget.FrameLayout", 0, 0, 60, 133),      # root container
        _node("android.widget.LinearLayout", 0, 0, 60, 133),     # nested full-screen
        _node("android.widget.TextView", 2, 2, 40, 8),
        _node("android.widget.Button", 2, 10, 30, 16),
    ]
    assert _surface_like(nodes) == 0.0


def test_small_surface_thumbnail_does_not_fire() -> None:
    """A small VideoView thumbnail (<½ of the screen extent) does NOT fire."""
    nodes = [
        _node("android.widget.FrameLayout", 0, 0, 60, 133),
        _node("android.widget.VideoView", 2, 2, 20, 20),  # small
    ]
    assert _surface_like(nodes) == 0.0


def test_sentinel_and_degenerate_bounds_are_ignored() -> None:
    """Off-window sentinel and zero-area bounds are dropped without error, and do
    not by themselves create a surface hit."""
    nodes = [
        _node("android.widget.FrameLayout", 0, 0, 60, 133),
        _node("android.view.SurfaceView", -_SENTINEL, -_SENTINEL, _SENTINEL, _SENTINEL),  # sentinel
        _node("android.view.TextureView", 5, 5, 5, 5),  # zero-area (degenerate)
    ]
    # Neither the sentinel nor the zero-area surface should register.
    assert _surface_like(nodes) == 0.0


def _ui_feats(nodes: list[dict[str, Any]]) -> dict[str, float]:
    """All UI features for a single node snapshot (ui_only mode)."""
    return extract_window_features(_ui_ctx(nodes), feature_mode="ui_only")


def test_scrollview_is_doc_container_not_list() -> None:
    """ScrollView is a continuous-scroll DOC container -> ui_webview, not ui_list.

    Regression for the P1-b confound where ScrollView was bucketed as ``list``,
    setting ui_list on I4 long-form windows and pulling them into I3.
    """
    nodes = [
        _node("android.widget.FrameLayout", 0, 0, 60, 133),
        _node("android.widget.ScrollView", 0, 4, 60, 130, scrollable=True),
        _node("android.widget.TextView", 2, 6, 40, 12),
    ]
    feats = _ui_feats(nodes)
    assert feats["ui_webview"] == 1.0
    assert feats["ui_list"] == 0.0


def test_recyclerview_is_item_list() -> None:
    """A RecyclerView / ListView is a true item list -> ui_list (the I3 cue)."""
    nodes = [
        _node("android.widget.FrameLayout", 0, 0, 60, 133),
        _node("androidx.recyclerview.widget.RecyclerView", 0, 4, 60, 130, scrollable=True),
    ]
    feats = _ui_feats(nodes)
    assert feats["ui_list"] == 1.0
    assert feats["ui_webview"] == 0.0


def test_bounds_occupancy_is_bounded() -> None:
    """ui_bounds_occupancy stays within [0, 1] for a dense nested snapshot."""
    nodes = [_node("android.widget.FrameLayout", 0, 0, 60, 133)]
    nodes += [_node("android.widget.TextView", i, i, i + 40, i + 20) for i in range(0, 30, 3)]
    feats = extract_window_features(_ui_ctx(nodes), feature_mode="ui_only")
    assert 0.0 <= feats["ui_bounds_occupancy"] <= 1.0
