"""Rounded native desktop widgets and small original line icons."""
from __future__ import annotations

import tkinter as tk
import customtkinter as ctk
from PIL import Image, ImageDraw


BG = "#F3F5F9"
WHITE = "#FFFFFF"
INK = "#1C2434"
MUTED = "#8690A2"
BLUE = "#007AFF"
GREEN = "#26B77A"
LINE = "#E9EDF4"
FIELD = "#F5F7FB"
COLORS = {"idle": MUTED, "off": "#A6ADBA", "busy": BLUE, "ok": GREEN, "error": "#ED6574"}


def icon(name: str, color: str, size: int = 20) -> ctk.CTkImage:
    scale = size * 4 / 20
    image = Image.new("RGBA", (size * 4, size * 4))
    draw = ImageDraw.Draw(image)
    def line(points, width=1.7):
        draw.line([(int(x * scale), int(y * scale)) for x, y in points], fill=color,
                  width=max(1, int(width * scale)), joint="curve")
    def box(bounds, radius=3):
        draw.rounded_rectangle(tuple(int(x * scale) for x in bounds), radius=int(radius * scale),
                               outline=color, width=int(1.6 * scale))
    if name == "mail":
        box((2, 4, 18, 16))
        line([(3, 5), (10, 11), (17, 5)])
    elif name == "route":
        line([(5, 3), (5, 11), (14, 11), (14, 17)])
        line([(10, 14), (14, 18), (18, 14)])
        box((2, 1, 8, 6), 2)
    elif name == "sliders":
        for x, y in ((5, 7), (10, 13), (15, 6)):
            line([(x, 3), (x, 17)])
            draw.ellipse(((x-2.2)*scale, (y-2.2)*scale, (x+2.2)*scale, (y+2.2)*scale),
                         fill="white", outline=color, width=int(1.5*scale))
    elif name == "log":
        box((3, 2, 17, 18))
        for y in (6, 10, 14):
            line([(6, y), (14 if y != 14 else 11, y)], 1.4)
    elif name == "shield":
        line([(10, 2), (17, 5), (16, 12), (13, 16), (10, 18), (7, 16), (4, 12), (3, 5), (10, 2)])
        line([(7, 10), (9, 12), (13, 8)])
    elif name == "chat":
        box((2, 3, 18, 15), 5)
        line([(6, 14), (5, 18), (11, 15)])
        for x in (6, 10, 14):
            draw.ellipse(((x-.7)*scale, 8*scale, (x+.7)*scale, 9.4*scale), fill=color)
    elif name == "folder":
        line([(2, 6), (2, 16), (18, 16), (18, 6), (10, 6), (8, 3), (2, 3), (2, 6)])
    elif name == "play":
        draw.polygon([(6*scale, 3*scale), (17*scale, 10*scale), (6*scale, 17*scale)], fill=color)
    elif name == "save":
        box((3, 2, 17, 18), 2)
        line([(6, 3), (6, 8), (14, 8), (14, 3)])
        box((6, 12, 14, 18), 1)
    else:
        box((3, 3, 17, 17), 6)
    return ctk.CTkImage(light_image=image, dark_image=image, size=(size, size))


class ScrollableTab(ctk.CTkFrame):
    """Keep scroll position per settings page and support all child controls."""
    def __init__(self, parent):
        super().__init__(parent, fg_color=BG, corner_radius=0)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, bg=BG, borderwidth=0, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ctk.CTkScrollbar(self, command=self.canvas.yview, width=10,
                                         fg_color=BG, button_color="#D4DAE5", button_hover_color="#B9C3D5")
        self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(5, 0))
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.content = ctk.CTkFrame(self.canvas, fg_color=BG, corner_radius=0)
        self.content.columnconfigure(0, weight=1)
        self.window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        tk.Frame.bind(self.content, "<Configure>", self._resize, add="+")
        self.canvas.bind("<Configure>", self._resize)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.winfo_toplevel().bind_all(sequence, self._wheel, add="+")

    def _resize(self, event=None):
        width = max(1, self.canvas.winfo_width())
        height = max(self.canvas.winfo_height(), self.content.winfo_reqheight())
        self.canvas.itemconfigure(self.window_id, width=width, height=height)
        self.canvas.configure(scrollregion=(0, 0, width, height))

    def _wheel(self, event):
        target = self.winfo_containing(event.x_root, event.y_root)
        while target is not None and target is not self:
            target = getattr(target, "master", None)
        if target is not self or self.canvas.yview() == (0.0, 1.0):
            return
        if getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        else:
            delta = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
        self.canvas.yview_scroll(delta, "units")
        return "break"


class PageHost(ctk.CTkFrame):
    def __init__(self, parent, on_select):
        super().__init__(parent, fg_color=BG, corner_radius=0)
        self.pages = []
        self.names = []
        self.on_select = on_select
        self.current = 0

    def add(self, page, text):
        self.pages.append(page)
        self.names.append(text)

    def tabs(self):
        return tuple(self.names)

    def select(self, index):
        index = self.names.index(index) if isinstance(index, str) else int(index)
        for page in self.pages:
            page.pack_forget()
        self.current = index
        self.pages[index].pack(fill="both", expand=True)
        self.on_select(index)
