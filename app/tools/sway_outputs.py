#!/usr/bin/env python3
"""Parse swaymsg -t get_outputs to find DSI-1's display rect at runtime.

This module provides pure functions to extract the DSI-1 output's rect
from swaymsg JSON, replacing the hardcoded DSI_Y constant in st.py.
"""


def get_output_rect(outputs_json, output_name="DSI-1"):
    """Extract a single output's rect from swaymsg -t get_outputs JSON.

    Args:
        outputs_json: parsed JSON list of outputs from swaymsg
        output_name: which output to find (default "DSI-1")

    Returns:
        dict with keys "x", "y", "width", "height"

    Raises:
        ValueError: if the output is not found in the list
    """
    if not isinstance(outputs_json, list):
        raise ValueError(f"expected list of outputs, got {type(outputs_json).__name__}")

    for output in outputs_json:
        if output.get("name") == output_name:
            rect = output.get("rect")
            if not rect:
                raise ValueError(f"{output_name} has no rect")
            return rect

    raise ValueError(f"{output_name} not found in outputs")
