"""
Live thermal image viewer for FLIR cameras.

Requires the 'gui' extra: pip install pyflir[gui]

Can be used from code::

    with Camera() as cam:
        cam.load_xml("camera_xxx.xml")
        cam.live_view()

Features:
  - Real-time thermal display with colormap selection
  - Colorbar showing raw-count scale
  - Cursor value readout in status bar
  - Click to place persistent markers with DN value
  - Min/Max/Mean stats in status bar
"""

import contextlib
import time
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .camera import Camera

try:
    import tkinter as tk
    from tkinter import ttk

    import matplotlib
    from PIL import Image, ImageDraw, ImageTk

    HAS_GUI_DEPS = True
except ImportError:
    HAS_GUI_DEPS = False

COLORMAP_CHOICES = ["inferno", "hot", "plasma", "magma", "viridis", "gray"]

COLORBAR_WIDTH = 60


def _check_gui_deps():
    if not HAS_GUI_DEPS:
        raise ImportError("GUI dependencies not installed. Run:\n  pip install pyflir[gui]")


def build_lut(cmap_name: str) -> np.ndarray:
    """Build a 65536-entry RGB LUT from a matplotlib colormap."""
    _check_gui_deps()
    cmap = matplotlib.colormaps[cmap_name]
    return (cmap(np.linspace(0, 1, 65536))[:, :3] * 255).astype(np.uint8)


class LiveView:
    """Tkinter-based live thermal image viewer for FLIR cameras.

    Displays raw 16-bit sensor counts (DN) with colormap normalization.
    Streaming must not be active when this is called; LiveView manages
    the stream lifecycle itself.

    Args:
        camera: Connected Camera instance with XML loaded.
        colormap: Initial matplotlib colormap name.
        scale: Display upscale factor (1 = native resolution).
    """

    def __init__(self, camera: "Camera", colormap: str = "inferno", scale: int = 2):
        _check_gui_deps()

        self.cam = camera
        self.width = camera.width or 0
        self.height = camera.height or 0
        if self.width == 0 or self.height == 0:
            raise RuntimeError("Image dimensions unknown; call cam.load_xml() before live_view().")

        self.cmap_name = colormap
        self.lut = build_lut(self.cmap_name)
        self.scale = scale
        self.disp_w = self.width * scale
        self.disp_h = self.height * scale

        # Current frame for cursor readout
        self._current_frame: np.ndarray | None = None
        self._vmin = 0.0
        self._vmax = 1.0

        # Markers: list of (img_x, img_y)
        self._markers: list[tuple[int, int]] = []

        # Mouse position in image coordinates
        self._mouse_img_x = -1
        self._mouse_img_y = -1

        # Start streaming
        self.cam.start_stream()

        # Build GUI
        self.root = tk.Tk()
        self.root.title("pyFlir Live View")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Main frame: image + colorbar
        main = tk.Frame(self.root)
        main.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(main, width=self.disp_w, height=self.disp_h, bg="black")
        self.canvas.pack(side="left")

        self.cbar_canvas = tk.Canvas(main, width=COLORBAR_WIDTH, height=self.disp_h, bg="black")
        self.cbar_canvas.pack(side="left", fill="y")

        # Bottom bar: status + controls
        bottom = tk.Frame(self.root)
        bottom.pack(fill="x", padx=5, pady=2)

        self.status_var = tk.StringVar(value="Starting…")
        self.status = tk.Label(
            bottom, textvariable=self.status_var, font=("Consolas", 10), anchor="w"
        )
        self.status.pack(side="left", fill="x", expand=True)

        self.cursor_var = tk.StringVar(value="")
        self.cursor_label = tk.Label(
            bottom,
            textvariable=self.cursor_var,
            font=("Consolas", 10),
            anchor="e",
            fg="yellow",
            bg="black",
            padx=5,
        )
        self.cursor_label.pack(side="right")

        self.cmap_var = tk.StringVar(value=self.cmap_name)
        cmap_menu = ttk.Combobox(
            bottom, textvariable=self.cmap_var, values=COLORMAP_CHOICES, width=10, state="readonly"
        )
        cmap_menu.pack(side="right")
        cmap_menu.bind("<<ComboboxSelected>>", self._on_cmap_change)

        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Button-3>", self._on_right_click)

        self.photo = None
        self.cbar_photo = None
        self.frame_count = 0
        self.fps_time = time.monotonic()
        self.fps = 0.0
        self.running = True

        self.root.after(1, self.update)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_cmap_change(self, event=None):
        self.cmap_name = self.cmap_var.get()
        self.lut = build_lut(self.cmap_name)

    def _on_mouse_move(self, event):
        self._mouse_img_x = event.x // self.scale
        self._mouse_img_y = event.y // self.scale

    def _on_click(self, event):
        ix = event.x // self.scale
        iy = event.y // self.scale
        if 0 <= ix < self.width and 0 <= iy < self.height:
            self._markers.append((ix, iy))

    def _on_right_click(self, event):
        self._markers.clear()

    def _dn_at(self, ix: int, iy: int):
        """Return the raw DN value at image coordinates, or None."""
        if (
            self._current_frame is not None
            and 0 <= iy < self._current_frame.shape[0]
            and 0 <= ix < self._current_frame.shape[1]
        ):
            return int(self._current_frame[iy, ix])
        return None

    # ------------------------------------------------------------------
    # Update loop
    # ------------------------------------------------------------------

    def update(self):
        if not self.running:
            return

        # Pull latest frame (non-blocking drain → freshest image)
        frame = None
        with contextlib.suppress(Exception):
            frame = self.cam.read(timeout=0.05, latest=True)

        if frame is not None and frame.size > 0:
            self._current_frame = frame.astype(np.float32)

            # Percentile normalization
            vmin = float(np.percentile(self._current_frame, 1))
            vmax = float(np.percentile(self._current_frame, 99))
            self._vmin = vmin
            self._vmax = vmax

            if vmax > vmin:
                flt = (self._current_frame - vmin) / (vmax - vmin)
                np.clip(flt, 0, 1, out=flt)
                img16 = (flt * 65535).astype(np.uint16)
            else:
                img16 = np.zeros((self.height, self.width), dtype=np.uint16)

            colored = self.lut[img16.ravel()].reshape(self.height, self.width, 3)
            pil_img = Image.fromarray(colored)
            pil_img = pil_img.resize((self.disp_w, self.disp_h), Image.NEAREST)

            # Draw markers
            if self._markers:
                draw = ImageDraw.Draw(pil_img)
                for mx, my in self._markers:
                    dx, dy = mx * self.scale, my * self.scale
                    r = 4
                    draw.ellipse([dx - r, dy - r, dx + r, dy + r], outline="white", width=2)
                    dn = self._dn_at(mx, my)
                    if dn is not None:
                        draw.text((dx + r + 2, dy - 8), str(dn), fill="white")

            self.photo = ImageTk.PhotoImage(pil_img)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=self.photo)

            self._draw_colorbar(vmin, vmax)

            # FPS counter
            self.frame_count += 1
            now = time.monotonic()
            elapsed = now - self.fps_time
            if elapsed >= 1.0:
                self.fps = self.frame_count / elapsed
                self.frame_count = 0
                self.fps_time = now

            mean_dn = float(self._current_frame.mean())
            self.status_var.set(
                f"{self.width}×{self.height}  |  "
                f"{self.fps:.1f} fps  |  "
                f"min={vmin:.0f}  max={vmax:.0f}  mean={mean_dn:.0f} DN"
            )

            dn = self._dn_at(self._mouse_img_x, self._mouse_img_y)
            if dn is not None:
                self.cursor_var.set(f"({self._mouse_img_x},{self._mouse_img_y})  {dn} DN")
            else:
                self.cursor_var.set("")

        self.root.after(1, self.update)

    # ------------------------------------------------------------------
    # Colorbar
    # ------------------------------------------------------------------

    def _draw_colorbar(self, vmin: float, vmax: float):
        bar_w = 20
        bar_h = self.disp_h
        margin_left = 5
        pad_top = 10
        pad_bot = 14
        usable_h = max(bar_h - pad_top - pad_bot, 1)
        n_ticks = 5

        gradient = np.linspace(65535, 0, usable_h, dtype=np.uint16)
        bar_rgb = self.lut[gradient]
        bar_img = np.repeat(bar_rgb[:, np.newaxis, :], bar_w, axis=1)

        pil_bar = Image.new("RGB", (COLORBAR_WIDTH, bar_h), (0, 0, 0))
        pil_bar.paste(Image.fromarray(bar_img), (margin_left, pad_top))

        draw = ImageDraw.Draw(pil_bar)
        for i in range(n_ticks + 1):
            frac = i / n_ticks
            y = pad_top + int(frac * usable_h)
            val = vmax - frac * (vmax - vmin)
            label = f"{val:.0f}"
            if i == n_ticks:
                label += " DN"
            draw.text((margin_left + bar_w + 3, y - 6), label, fill="white")

        self.cbar_photo = ImageTk.PhotoImage(pil_bar)
        self.cbar_canvas.delete("all")
        self.cbar_canvas.create_image(0, 0, anchor="nw", image=self.cbar_photo)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_close(self):
        self.running = False
        self.cam.stop_stream()
        self.root.destroy()

    def run(self):
        """Run the viewer. Blocks until the window is closed."""
        self.root.mainloop()
