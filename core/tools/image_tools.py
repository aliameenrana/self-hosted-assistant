"""Image edit tools, bound to one request's uploaded images.

Same shape as memory_tools.build(): a factory closing over state that must
not leak across requests. Here that state is the raw bytes of images
uploaded in this turn, keyed by attachment id, so the model can only ever
operate on an image this user actually uploaded, never guess another
session's attachment id and reach someone else's file.
"""
from typing import Any

from . import images


def build(get_image_bytes) -> dict:
    from .registry import Tool, _obj

    def _bytes(attachment_id: str) -> bytes:
        data = get_image_bytes(attachment_id)
        if data is None:
            raise images.ImageError(
                "no uploaded image with that id in this conversation. "
                "Ask the user to attach one first.")
        return data

    def crop_image(attachment_id: str, left: int, top: int, right: int,
                  bottom: int, format: str = "png") -> dict[str, Any]:
        return images.crop(_bytes(attachment_id), left, top, right, bottom, format)

    def resize_image(attachment_id: str, width: int, height: int,
                     format: str = "png") -> dict[str, Any]:
        return images.resize(_bytes(attachment_id), width, height, format)

    def convert_image_format(attachment_id: str, format: str) -> dict[str, Any]:
        return images.convert_format(_bytes(attachment_id), format)

    def set_image_transparency(attachment_id: str, alpha: float) -> dict[str, Any]:
        return images.set_transparency(_bytes(attachment_id), alpha)

    id_prop = {"attachment_id": {
        "type": "string",
        "description": "The id returned when the image was uploaded."}}

    return {t.name: t for t in [
        Tool("crop_image",
             "Crop an uploaded image to a rectangular region and return a "
             "link to the result. Coordinates are pixels from the top-left "
             "corner. Use only on an image the user has attached in this "
             "conversation.",
             _obj({**id_prop,
                   "left": {"type": "integer"}, "top": {"type": "integer"},
                   "right": {"type": "integer"}, "bottom": {"type": "integer"},
                   "format": {"type": "string",
                             "description": "png, jpeg, or webp. Default png."}},
                  ["attachment_id", "left", "top", "right", "bottom"]),
             crop_image),
        Tool("resize_image",
             "Resize an uploaded image to an exact pixel width and height "
             "and return a link to the result. Use only on an image the "
             "user has attached in this conversation.",
             _obj({**id_prop,
                   "width": {"type": "integer"}, "height": {"type": "integer"},
                   "format": {"type": "string",
                             "description": "png, jpeg, or webp. Default png."}},
                  ["attachment_id", "width", "height"]),
             resize_image),
        Tool("convert_image_format",
             "Convert an uploaded image to a different file format (e.g. "
             "PNG to JPEG) and return a link to the result. Use only on an "
             "image the user has attached in this conversation.",
             _obj({**id_prop,
                   "format": {"type": "string",
                             "description": "Target format: png, jpeg, or webp."}},
                  ["attachment_id", "format"]),
             convert_image_format),
        Tool("set_image_transparency",
             "Set the uniform opacity of an uploaded image (e.g. make it "
             "look faded or semi-transparent) and return a link to the "
             "result. Always saves as PNG, the only format here that "
             "supports transparency. Use only on an image the user has "
             "attached in this conversation.",
             _obj({**id_prop,
                   "alpha": {"type": "number",
                            "description": "0.0 (invisible) to 1.0 (fully "
                                           "opaque)."}},
                  ["attachment_id", "alpha"]),
             set_image_transparency),
    ]}
