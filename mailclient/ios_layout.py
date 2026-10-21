"""iOS-inspired desktop settings: grouped surfaces, switches and calm status UI."""
from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
import customtkinter as ctk

from . import __version__
from .ios_widgets import BG, BLUE, COLORS, FIELD, GREEN, INK, LINE, MUTED, WHITE, PageHost, ScrollableTab, icon


class IOSLayout:
    def font(self, size=13, bold=False):
        return ctk.CTkFont(family=self.font_family, size=size, weight="bold" if bold else "normal")

    def _style(self):
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        self.root.title(f"邮件转发助手  ·  {__version__}")
        self.root.geometry(f"{min(1200, max(960, self.root.winfo_screenwidth()-80))}x{min(880, max(680, self.root.winfo_screenheight()-100))}")
        self.root.minsize(960, 680)
        self.root.configure(bg=BG)
        available = {name.casefold(): name for name in tkfont.families(self.root)}
        if "customtkinter_shapes_font" not in available:
            # Some Linux Tk builds cannot see a privately loaded icon font.
            # Draw real curves instead of depending on missing font glyphs.
            from customtkinter.windows.widgets.core_rendering import DrawEngine
            DrawEngine.preferred_drawing_method = "polygon_shapes"
        candidates = ("Microsoft YaHei UI", "Microsoft YaHei") if sys.platform == "win32" else (
            "Noto Sans CJK SC", "WenQuanYi Bitmap Song", "WenQuanYi Zen Hei", "DejaVu Sans")
        self.font_family = next((available[x.casefold()] for x in candidates if x.casefold() in available),
                                tkfont.nametofont("TkDefaultFont").actual("family"))
        if sys.platform != "win32":
            probe = tkfont.Font(root=self.root, family=self.font_family, size=-14)
            if probe.actual("family").casefold() == "fixed" and "wenquanyi bitmap song" in available:
                self.font_family = available["wenquanyi bitmap song"]
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=self.font_family, size=10)
        self.icons = {}
        for name, color in (("mail", BLUE), ("route", BLUE), ("sliders", BLUE), ("log", BLUE),
                            ("shield", GREEN), ("chat", GREEN), ("folder", MUTED), ("save", BLUE)):
            self.icons[name] = icon(name, color)
        self.icons["app"] = icon("mail", WHITE, 26)
        self.inputs = {}

    def label(self, parent, text="", size=13, color=INK, bold=False, **kwargs):
        kwargs.setdefault("anchor", "w")
        return ctk.CTkLabel(parent, text=text, font=self.font(size, bold), text_color=color, **kwargs)

    def button(self, parent, text, command, primary=False, width=116, **kwargs):
        return ctk.CTkButton(parent, text=text, command=command, width=width, height=40, corner_radius=11,
                             font=self.font(13, True), fg_color=BLUE if primary else "#EDF2F9",
                             hover_color="#0069DE" if primary else "#E3EAF4",
                             text_color=WHITE if primary else INK, text_color_disabled="#AFB9CB", **kwargs)

    def _resize_status_text(self, event, label):
        # Tk Configure widths are physical pixels; CTk wraplength is unscaled.
        # Leave a small inset and update only on width changes to avoid cycles.
        width = max(40, int(event.width / label._get_widget_scaling()) - 8)
        if label.cget("wraplength") != width:
            label.configure(wraplength=width)

    def card(self, parent, row, title, description="", symbol="mail", color=BLUE):
        card = ctk.CTkFrame(parent, fg_color=WHITE, corner_radius=20, border_width=1, border_color="#EAEFF6")
        card.grid(row=row, column=0, sticky="ew", padx=2, pady=(0, 14))
        card.columnconfigure(0, weight=1)
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 12))
        head.columnconfigure(1, weight=1)
        tile = ctk.CTkFrame(head, width=38, height=38, corner_radius=12, fg_color="#EBF3FF" if color == BLUE else "#E8F8F1")
        tile.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        self.label(tile, "", image=self.icons[symbol], width=24, height=24, anchor="center").pack(padx=7, pady=7)
        self.label(head, title, 15, bold=True).grid(row=0, column=1, sticky="w")
        if description:
            self.label(head, description, 11, MUTED).grid(row=1, column=1, sticky="w")
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 18))
        body.columnconfigure(0, weight=1)
        return card, head, body

    def _entry(self, parent, row, label, key, col=0, width=None, secret=False):
        line = ctk.CTkFrame(parent, fg_color="transparent")
        line.grid(row=row, column=col, sticky="ew", pady=6)
        line.columnconfigure(1, weight=1)
        self.label(line, label, color="#596579", width=112).grid(row=0, column=0, padx=(0, 12), sticky="w")
        entry = ctk.CTkEntry(line, textvariable=self.vars[key], show="•" if secret else "", width=width or 260,
                             height=40, corner_radius=10, fg_color=FIELD, border_width=1, border_color=LINE,
                             text_color=INK, font=self.font(13), placeholder_text_color=MUTED)
        entry.grid(row=0, column=1, sticky="ew")
        entry.bind("<FocusIn>", lambda event: entry.configure(border_color="#A5CAFF"), add="+")
        entry.bind("<FocusOut>", lambda event: entry.configure(border_color=LINE), add="+")
        self.controls.append((entry, "normal"))
        self.inputs[key] = entry
        if secret:
            visible = tk.BooleanVar(value=False)
            def toggle():
                visible.set(not visible.get())
                entry.configure(show="" if visible.get() else "•")
                show.configure(text="隐藏" if visible.get() else "显示")
            show = ctk.CTkButton(line, text="显示", command=toggle, width=48, height=34, corner_radius=9,
                                 fg_color="transparent", hover_color="#EBF3FF", text_color=BLUE, font=self.font(12))
            show.grid(row=0, column=2, padx=(5, 0))
            self.controls.append((show, "normal"))
        return entry

    def _combo(self, parent, row, label, key, values):
        line = ctk.CTkFrame(parent, fg_color="transparent")
        line.grid(row=row, column=0, sticky="ew", pady=6)
        line.columnconfigure(1, weight=1)
        self.label(line, label, color="#596579", width=112).grid(row=0, column=0, padx=(0, 12), sticky="w")
        combo = ctk.CTkOptionMenu(line, variable=self.vars[key], values=values, width=220, height=40,
                                 corner_radius=10, fg_color=FIELD, button_color="#EBEFF6", button_hover_color="#DFE7F4",
                                 text_color=INK, font=self.font(13), dropdown_font=self.font(13),
                                 dropdown_fg_color=WHITE, dropdown_text_color=INK, dropdown_hover_color="#EBF3FF")
        combo.grid(row=0, column=1, sticky="ew")
        self.controls.append((combo, "normal"))
        self.inputs[key] = combo
        return combo

    def switch(self, parent, row, title, key, description="", command=None):
        line = ctk.CTkFrame(parent, fg_color="transparent")
        line.grid(row=row, column=0, sticky="ew", pady=9)
        line.columnconfigure(0, weight=1)
        self.label(line, title, 13).grid(row=0, column=0, sticky="w")
        if description:
            self.label(line, description, 11, MUTED).grid(row=1, column=0, sticky="w", pady=(2, 0))
        toggle = ctk.CTkSwitch(line, text="", variable=self.vars[key], width=44, switch_width=42, switch_height=24,
                              button_length=0, progress_color=GREEN, fg_color="#D8DEE9", button_color=WHITE,
                              button_hover_color="#F7FAFF", command=command)
        toggle.grid(row=0, column=1, rowspan=2, padx=(16, 0), sticky="e")
        state = "disabled" if key == "remember_credentials" and sys.platform != "win32" else "normal"
        toggle.configure(state=state)
        self.controls.append((toggle, state))
        self.inputs[key] = toggle
        return toggle

    def note(self, parent, text, row=0):
        frame = ctk.CTkFrame(parent, fg_color="#EAF2FF", corner_radius=13)
        frame.grid(row=row, column=0, sticky="ew", padx=2, pady=(0, 12))
        item = self.label(frame, text, 12, "#6683B1", wraplength=620, justify="left")
        item.pack(fill="x", padx=17, pady=13)
        item.bind("<Configure>", lambda event: item.configure(wraplength=max(150, event.width)), add="+")

    def _build(self):
        shell = ctk.CTkFrame(self.root, fg_color=BG, corner_radius=0)
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(0, weight=1)
        sidebar = ctk.CTkFrame(shell, fg_color="#FAFBFD", width=205, corner_radius=0)
        sidebar.grid(row=0, column=0, sticky="ns")
        sidebar.grid_propagate(False)
        sidebar.columnconfigure(0, weight=1)
        sidebar.rowconfigure(8, weight=1)
        brand = ctk.CTkFrame(sidebar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=22, pady=(28, 24))
        app = ctk.CTkFrame(brand, width=48, height=48, fg_color=BLUE, corner_radius=15)
        app.pack(anchor="w")
        self.label(app, "", image=self.icons["app"], width=30, height=30, anchor="center").pack(padx=9, pady=9)
        self.label(brand, "Mail Relay", 20, bold=True).pack(anchor="w", pady=(13, 0))
        self.label(brand, "邮件转发助手", 12, MUTED).pack(anchor="w", pady=(2, 0))
        self.label(sidebar, "工作空间", 11, "#A1AABA").grid(row=1, column=0, sticky="w", padx=24, pady=(0, 10))
        names = ["邮箱账户", "转发通道", "运行策略", "运行日志"]
        symbols = ["mail", "route", "sliders", "log"]
        self.nav_buttons = []
        for index, (name, symbol) in enumerate(zip(names, symbols)):
            button = ctk.CTkButton(sidebar, text=name, image=self.icons[symbol], compound="left", anchor="w",
                                   command=lambda i=index: self.notebook.select(i), width=166, height=45,
                                   corner_radius=12, fg_color="transparent", hover_color="#F0F4FA",
                                   text_color="#69758C", font=self.font(13), border_spacing=14)
            button.grid(row=index+2, column=0, padx=17, pady=4, sticky="ew")
            self.nav_buttons.append(button)
        tip = ctk.CTkFrame(sidebar, fg_color="#F0F4FA", corner_radius=15)
        tip.grid(row=9, column=0, sticky="ew", padx=16, pady=14)
        self.label(tip, "本地运行 · 私密保存", 11, "#7C8DA8").pack(anchor="w", padx=13, pady=(13, 3))
        self.label(tip, "关闭窗口将停止转发", 11, MUTED).pack(anchor="w", padx=13, pady=(0, 13))
        ctk.CTkButton(sidebar, text="配置与日志目录", image=self.icons["folder"], command=self.open_folder,
                       fg_color="transparent", hover_color="#F0F4FA", text_color=MUTED,
                       font=self.font(11), height=34, width=170).grid(row=10, column=0, padx=16)
        self.label(sidebar, f"VERSION  {__version__}", 10, "#B0B8C6").grid(row=11, column=0, sticky="w", padx=25, pady=(14, 22))

        main = ctk.CTkFrame(shell, fg_color=BG, corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew", padx=(26, 22), pady=(24, 16))
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)
        head = ctk.CTkFrame(main, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 20))
        head.columnconfigure(0, weight=1)
        self.page_title = self.label(head, "邮箱账户", 27, bold=True)
        self.page_title.grid(row=0, column=0, sticky="w")
        self.page_subtitle = self.label(head, "连接你的工作邮箱，让重要信息及时抵达。", 12, MUTED)
        self.page_subtitle.grid(row=1, column=0, sticky="w", pady=(3, 0))
        self.phase_var = tk.StringVar(value="等待登录")
        self.phase_pill = ctk.CTkFrame(head, fg_color="#E8EDF5", corner_radius=13)
        self.phase_pill.grid(row=0, column=1, padx=(12, 12), sticky="e")
        self.phase_label = self.label(self.phase_pill, "●  等待连接", 11, "#8190A7")
        self.phase_label.pack(padx=12, pady=6)
        self.save_button = self.button(head, "保存配置", self.save, width=106, image=self.icons["save"])
        self.save_button.grid(row=0, column=2)

        statuses = self.status_panel = ctk.CTkFrame(main, fg_color=WHITE, corner_radius=18,
                                                  border_width=1, border_color="#EBEFF6")
        statuses.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        # Let the wrapped labels determine height; a fixed 84px clips longer
        # diagnostic messages and scaled Windows fonts.
        self.status_dots = {}
        for index, (key, name) in enumerate((("network", "邮箱网络"), ("login", "账号登录"), ("mailbox", "邮件读取"),
                                             ("wecom", "企业微信"), ("smtp", "SMTP 邮箱"))):
            statuses.columnconfigure(index, weight=1, uniform="status")
            part = ctk.CTkFrame(statuses, fg_color="transparent", corner_radius=0)
            part.grid(row=0, column=index, sticky="nsew", padx=(17 if index == 0 else 8, 5), pady=11)
            title = ctk.CTkFrame(part, fg_color="transparent")
            title.pack(fill="x")
            dot = self.label(title, "●", 8, "#CFD6E1", width=12)
            dot.pack(side="left")
            self.label(title, name, 11, MUTED).pack(side="left", padx=(3, 0))
            variable = tk.StringVar(value="尚未检测")
            label = self.label(part, "", 12, "#7E8CA4", textvariable=variable,
                               wraplength=125, justify="left", height=0)
            label.pack(fill="x", pady=(5, 0))
            tk.Frame.bind(part, "<Configure>", lambda event, item=label: self._resize_status_text(event, item), add="+")
            self.status_vars[key], self.status_labels[key], self.status_dots[key] = variable, label, dot
        for key in ("wecom", "smtp"):
            if not self.vars[f"{key}_enabled"].get():
                self._status(key, "off", "未启用")

        self.notebook = PageHost(main, self._select_page)
        self.notebook.grid(row=2, column=0, sticky="nsew")
        self.tabs = [ScrollableTab(self.notebook) for _ in names]
        for tab, name in zip(self.tabs, names):
            self.notebook.add(tab, text=name)
        company, channels, policy, logs = [page.content for page in self.tabs]
        _, _, account = self.card(company, 0, "邮箱账号", "用于登录已开通 POP3 服务的工作邮箱")
        self._entry(account, 0, "邮箱账号", "pop_user")
        self._entry(account, 1, "登录密码", "pop_password", secret=True)
        self.switch(account, 2, "记住登录信息", "remember_credentials", "下次自动回填；Windows 版加密保存密码及授权信息")
        _, _, server = self.card(company, 1, "连接设置", "填写邮箱服务提供的 POP3 连接参数", "sliders")
        self._entry(server, 0, "POP3 服务器", "pop_host")
        self._entry(server, 1, "服务端口", "pop_port")
        self._combo(server, 2, "连接加密", "pop_security", ["SSL/TLS", "STARTTLS", "不加密"])
        self.switch(server, 3, "验证服务器证书", "pop_verify_tls", "推荐开启；公司自签名证书可在下方导入")
        self._entry(server, 4, "CA 证书", "pop_ca_file")
        browse = self.button(server, "选择证书文件", self._browse_ca, width=120)
        browse.grid(row=5, column=0, sticky="e", pady=(4, 0))
        self.controls.append((browse, "normal"))
        self.note(company, "公司邮箱仅内网可达时，请先连接 VPN，再进行登录检测。", 2)

        _, wecom_head, wecom = self.card(channels, 0, "企业微信群", "新邮件通过机器人推送到工作群", "chat", GREEN)
        self._channel_switch(wecom_head, "wecom_enabled")
        self._entry(wecom, 0, "Webhook", "wecom_webhook", secret=True)
        self._combo(wecom, 1, "消息格式", "wecom_type", ["markdown", "text"])
        self._entry(wecom, 2, "提醒成员 UserID", "wecom_userids")
        self.label(wecom, "填写企微账号 UserID，如 zhangsan,lisi；Markdown 和 text 均支持，显示昵称不能自动匹配。",
                   11, MUTED, wraplength=550, justify="left").grid(row=3, column=0, sticky="w", pady=(6, 4))
        self._entry(wecom, 4, "提醒手机号", "wecom_mobiles")
        self.label(wecom, "手机号用逗号分隔，@all 提醒全员；仅 text 格式支持提醒。", 11, MUTED,
                   wraplength=550, justify="left").grid(row=5, column=0, sticky="w", pady=(8, 0))
        _, smtp_head, smtp = self.card(channels, 1, "邮箱转发", "支持 QQ 邮箱及其他 SMTP 邮箱", "mail")
        self._channel_switch(smtp_head, "smtp_enabled")
        self._entry(smtp, 0, "发件邮箱", "smtp_user")
        self._entry(smtp, 1, "授权码 / 密码", "smtp_password", secret=True)
        self._entry(smtp, 2, "收件邮箱", "smtp_to")
        self._entry(smtp, 3, "SMTP 服务器", "smtp_host")
        self._entry(smtp, 4, "服务端口", "smtp_port")
        self._combo(smtp, 5, "连接加密", "smtp_security", ["SSL/TLS", "STARTTLS", "不加密"])
        self.label(smtp, "收件邮箱留空则发给自己；QQ 邮箱请填写 SMTP 授权码。", 11, MUTED,
                   wraplength=550, justify="left").grid(row=6, column=0, sticky="w", pady=(8, 0))

        _, _, rules = self.card(policy, 0, "检查与过滤", "按你的节奏接收需要的邮件", "sliders")
        self._entry(rules, 0, "检查间隔 / 秒", "poll_seconds")
        self._entry(rules, 1, "连接超时 / 秒", "timeout_seconds")
        self._entry(rules, 2, "过滤关键词", "keywords")
        self.label(rules, "多个关键词用逗号分隔；留空将转发全部新邮件。", 11, MUTED,
                   wraplength=550).grid(row=3, column=0, sticky="w", pady=(8, 4))
        self.switch(rules, 4, "首次测试最后一封邮件", "test_latest_first_run", "仍按关键词过滤；之后重启不重复转发历史邮件")
        self.note(policy, "邮件记录按账号保存，两个通道分别重试；本客户端不会删除原邮箱邮件。", 1)

        _, _, log_body = self.card(logs, 0, "实时运行记录", "查看连接、登录、通道检测和邮件投递结果", "log")
        log_body.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_body, height=18, wrap="word", state="disabled", relief="flat", borderwidth=0,
                                highlightthickness=0, bg=WHITE, fg="#60718B", font=(self.font_family, 10),
                                padx=2, pady=6, spacing1=3, spacing3=4, selectbackground="#D9E9FF")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ctk.CTkScrollbar(log_body, command=self.log_text.yview, width=10, fg_color=WHITE,
                                  button_color="#D4DAE5", button_hover_color="#B6C3D6")
        scroll.grid(row=0, column=1, sticky="ns", padx=(7, 0))
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.tag_configure("ERROR", foreground=COLORS["error"])
        self.log_text.tag_configure("WARNING", foreground="#D99B36")
        self.note(logs, "检测会向已启用通道发送测试通知；接口接受后，请在群聊或收件箱确认。", 1)

        actions = ctk.CTkFrame(main, fg_color=WHITE, corner_radius=18, border_width=1, border_color="#EBEFF6")
        actions.grid(row=3, column=0, sticky="ew", pady=(16, 0))
        buttons = ctk.CTkFrame(actions, fg_color="transparent")
        buttons.pack(anchor="w", padx=17, pady=(14, 8))
        self.check_button = self.button(buttons, "登录并检测", lambda: self.launch("check"), primary=True, width=138)
        self.check_button.pack(side="left", padx=(0, 8))
        self.start_button = self.button(buttons, "开始转发", lambda: self.launch("start"), width=118)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ctk.CTkButton(buttons, text="停止", command=self.stop, state="disabled", width=72,
                                        height=40, corner_radius=11, fg_color="#FFF0F1", hover_color="#FFE3E6",
                                        text_color="#E26A7C", text_color_disabled="#DDB7BF", font=self.font(13))
        self.stop_button.pack(side="left", padx=(0, 8))
        self.mail_test_button = self.button(buttons, "仅检测邮箱", lambda: self.launch("mailbox"), width=116)
        self.mail_test_button.pack(side="left")
        self.label(actions, "检测会发送测试通知；开始转发前会验证尚未检测的配置。", 11, MUTED).pack(anchor="w", padx=19, pady=(0, 13))
        bottom = ctk.CTkFrame(main, fg_color="transparent")
        bottom.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.stats_var = tk.StringVar()
        self.check_time_var = tk.StringVar()
        self.last_check = "尚未检查"
        self.label(bottom, "", 10, "#9AA6B9", textvariable=self.stats_var).pack(side="left")
        self.label(bottom, "", 10, "#9AA6B9", textvariable=self.check_time_var).pack(side="right")
        self._update_stats()
        self.phase_var.trace_add("write", lambda *args: self._render_phase())
        self.notebook.select(0)

    def _channel_switch(self, head, key):
        toggle = ctk.CTkSwitch(head, text="", variable=self.vars[key], command=self._channel_toggle,
                              width=44, switch_width=42, switch_height=24, button_length=0,
                              progress_color=GREEN, fg_color="#D8DEE9", button_color=WHITE, button_hover_color="#F7FAFF")
        toggle.grid(row=0, column=2, rowspan=2, padx=(20, 0))
        self.controls.append((toggle, "normal"))
        self.inputs[key] = toggle

    def _select_page(self, index):
        titles = ["邮箱账户", "转发通道", "运行策略", "运行日志"]
        subtitles = ["连接你的工作邮箱，让重要信息及时抵达。", "选择新邮件的抵达方式。", "按需过滤，按时检查，持续守候。", "每一次连接与投递，都有迹可循。"]
        self.page_title.configure(text=titles[index])
        self.page_subtitle.configure(text=subtitles[index])
        for number, button in enumerate(self.nav_buttons):
            button.configure(fg_color="#E8F1FF" if number == index else "transparent",
                             text_color=BLUE if number == index else "#69758C")

    def _render_phase(self):
        text = self.phase_var.get()
        if "正在转发" in text:
            title, fg, color = "●  转发中", "#E6F7EF", GREEN
        elif "失败" in text or "异常" in text or "未通过" in text:
            title, fg, color = "●  需要检查", "#FFF0F1", COLORS["error"]
        elif "通过" in text or "完成" in text:
            title, fg, color = "●  检测完成", "#E6F7EF", GREEN
        elif "正在" in text:
            title, fg, color = "●  处理中", "#E8F1FF", BLUE
        else:
            title, fg, color = "●  " + ("已停止" if "停止" in text else "等待连接"), "#E8EDF5", "#8190A7"
        self.phase_pill.configure(fg_color=fg)
        self.phase_label.configure(text=title, text_color=color)
