# 轨迹修正工具
import re
import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageTk
import cv2
import os
import numpy as np
from datetime import datetime
import json

# SFT和RL标注，直接修改就行。
# ==========================================
# 1. SOP 编辑对话框
# ==========================================
class SOPEditDialog(tk.Toplevel):
    def __init__(self, parent, title, initial_text, idx=None):
        super().__init__(parent)
        self.title(title)
        self.geometry("400x150")
        self.result = None
        self.transient(parent)

        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = (self.winfo_screenwidth() // 2) - (width // 2)
        y = (self.winfo_screenheight() // 2) - (height // 2)
        self.geometry(f'{width}x{height}+{x}+{y}')

        tk.Label(self, text="编辑SOP内容:", font=('Arial', 10, 'bold')).pack(pady=10)
        self.text_area = tk.Entry(self, font=('Arial', 10), width=40)
        self.text_area.insert(0, initial_text)
        self.text_area.pack(pady=5, padx=10, fill=tk.X)
        self.text_area.focus_set()
        self.text_area.selection_range(0, tk.END)

        btn_frame = tk.Frame(self)
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="确定 (Enter)", width=12, bg="#28a745", fg="white", command=self.on_ok).pack(
            side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="取消 (Esc)", width=12, command=self.destroy).pack(side=tk.LEFT, padx=5)
        self.grab_set()

        self.bind('<Return>', lambda e: self.on_ok())
        self.bind('<Escape>', lambda e: self.destroy())

    def on_ok(self):
        self.result = self.text_area.get().strip()
        self.destroy()


# ==========================================
# 2. 动态动作编辑面板 (Dynamic Action Palette)
# ==========================================
class ActionEditDialog(tk.Toplevel):
    def __init__(self, parent, initial_action_str, main_app):
        super().__init__(parent)
        self.title("修改 Action (动态面板)")
        self.geometry("450x250")
        self.result = None
        self.main_app = main_app
        self.transient(parent)

        self.initial_data = {}
        try:
            self.initial_data = json.loads(initial_action_str)
        except:
            self.initial_data = {"action": "click", "coordinate": [500, 500]}

        self.action_types = ["click", "long_press", "type", "open", "swipe", "system_button", "wait", "terminate",
                             "answer"]

        top_frame = tk.Frame(self)
        top_frame.pack(pady=10, fill=tk.X, padx=20)
        tk.Label(top_frame, text="动作类型 (Action):", font=('Arial', 10, 'bold')).pack(side=tk.LEFT)
        self.action_var = tk.StringVar()
        self.combo = ttk.Combobox(top_frame, textvariable=self.action_var, values=self.action_types, state="readonly",
                                  width=15)
        self.combo.pack(side=tk.LEFT, padx=10)
        self.combo.bind("<<ComboboxSelected>>", self.render_dynamic_inputs)

        self.dynamic_frame = tk.Frame(self, pady=10)
        self.dynamic_frame.pack(fill=tk.BOTH, expand=True, padx=20)

        btn_frame = tk.Frame(self)
        btn_frame.pack(pady=10)
        tk.Button(btn_frame, text="💾 保存并更新", width=15, bg="#007bff", fg="white", command=self.on_save).pack(
            side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="取消 (Esc)", width=10, command=self.destroy).pack(side=tk.LEFT, padx=5)

        self.bind('<Escape>', lambda e: self.destroy())

        init_action = self.initial_data.get("action", "click")
        if init_action not in self.action_types: init_action = "click"
        self.combo.set(init_action)
        self.input_vars = {}
        self.render_dynamic_inputs()

        self.update_idletasks()
        x = (self.winfo_screenwidth() // 2) - (self.winfo_width() // 2)
        y = (self.winfo_screenheight() // 2) - (self.winfo_height() // 2)
        self.geometry(f'+{x}+{y}')
        self.grab_set()

    def render_dynamic_inputs(self, event=None):
        for widget in self.dynamic_frame.winfo_children(): widget.destroy()
        self.input_vars = {}
        act = self.action_var.get()

        if act in ["click", "long_press"]:
            coords = self.initial_data.get("coordinate", [0, 0])
            self.input_vars['x'] = tk.IntVar(value=coords[0] if len(coords) > 0 else 0)
            self.input_vars['y'] = tk.IntVar(value=coords[1] if len(coords) > 1 else 0)
            tk.Label(self.dynamic_frame, text="X:").grid(row=0, column=0, padx=5)
            tk.Entry(self.dynamic_frame, textvariable=self.input_vars['x'], width=5).grid(row=0, column=1)
            tk.Label(self.dynamic_frame, text="Y:").grid(row=0, column=2, padx=5)
            tk.Entry(self.dynamic_frame, textvariable=self.input_vars['y'], width=5).grid(row=0, column=3)
            tk.Button(self.dynamic_frame, text="📍 在原图上取点", bg="#17a2b8", fg="white",
                      command=lambda: self.start_pick('single')).grid(row=0, column=4, padx=15)

        elif act == "swipe":
            starts = self.initial_data.get("start_coordinate", [0, 0])
            ends = self.initial_data.get("end_coordinate", [0, 0])
            self.input_vars['sx'] = tk.IntVar(value=starts[0] if len(starts) > 0 else 0)
            self.input_vars['sy'] = tk.IntVar(value=starts[1] if len(starts) > 1 else 0)
            self.input_vars['ex'] = tk.IntVar(value=ends[0] if len(ends) > 0 else 0)
            self.input_vars['ey'] = tk.IntVar(value=ends[1] if len(ends) > 1 else 0)

            tk.Label(self.dynamic_frame, text="起点 X/Y:").grid(row=0, column=0)
            tk.Entry(self.dynamic_frame, textvariable=self.input_vars['sx'], width=4).grid(row=0, column=1)
            tk.Entry(self.dynamic_frame, textvariable=self.input_vars['sy'], width=4).grid(row=0, column=2)
            tk.Button(self.dynamic_frame, text="📍 取起点", bg="#17a2b8", fg="white",
                      command=lambda: self.start_pick('start')).grid(row=0, column=3, padx=5)

            tk.Label(self.dynamic_frame, text="终点 X/Y:").grid(row=1, column=0, pady=10)
            tk.Entry(self.dynamic_frame, textvariable=self.input_vars['ex'], width=4).grid(row=1, column=1)
            tk.Entry(self.dynamic_frame, textvariable=self.input_vars['ey'], width=4).grid(row=1, column=2)
            tk.Button(self.dynamic_frame, text="📍 取终点", bg="#20c997", fg="white",
                      command=lambda: self.start_pick('end')).grid(row=1, column=3, padx=5)

        elif act in ["type", "open", "answer"]:
            lbl_txt = "输入内容 (Text):" if act in ["type", "answer"] else "应用名称 (App):"
            key = "text"
            val = self.initial_data.get(key, "")
            tk.Label(self.dynamic_frame, text=lbl_txt).pack(anchor=tk.W)
            self.input_vars[key] = tk.StringVar(value=val)
            tk.Entry(self.dynamic_frame, textvariable=self.input_vars[key], width=40).pack(pady=5)

        elif act == "system_button":
            tk.Label(self.dynamic_frame, text="按键名称:").pack(side=tk.LEFT)
            self.input_vars['button'] = tk.StringVar(value=self.initial_data.get("button", "back"))
            ttk.Combobox(self.dynamic_frame, textvariable=self.input_vars['button'],
                         values=["back", "home", "menu", "enter"], state="readonly").pack(side=tk.LEFT, padx=10)

        elif act == "terminate":
            tk.Label(self.dynamic_frame, text="终止状态:").pack(side=tk.LEFT)
            self.input_vars['status'] = tk.StringVar(value=self.initial_data.get("status", "success"))
            ttk.Combobox(self.dynamic_frame, textvariable=self.input_vars['status'],
                         values=["success", "failure"], state="readonly").pack(side=tk.LEFT, padx=10)

        elif act == "wait":
            tk.Label(self.dynamic_frame, text="Wait操作无需额外参数，点击保存即可。", fg="gray").pack(pady=10)

    def start_pick(self, pick_type):
        self.grab_release()
        self.withdraw()
        self.main_app.root.update()
        self.main_app.start_canvas_pick(self.on_picked, pick_type)

    def on_picked(self, nx, ny, pick_type):
        self.deiconify()
        self.update()
        self.lift()
        self.grab_set()
        try:
            if pick_type == 'single':
                self.input_vars['x'].set(nx)
                self.input_vars['y'].set(ny)
            elif pick_type == 'start':
                self.input_vars['sx'].set(nx)
                self.input_vars['sy'].set(ny)
            elif pick_type == 'end':
                self.input_vars['ex'].set(nx)
                self.input_vars['ey'].set(ny)
        except Exception as e:
            messagebox.showerror("取点错误", f"坐标值回填失败: {e}")
        self.focus_force()

    def on_save(self):
        act = self.action_var.get()
        new_data = {"action": act}
        try:
            if act in ["click", "long_press"]:
                new_data["coordinate"] = [self.input_vars['x'].get(), self.input_vars['y'].get()]
            elif act == "swipe":
                new_data["start_coordinate"] = [self.input_vars['sx'].get(), self.input_vars['sy'].get()]
                new_data["end_coordinate"] = [self.input_vars['ex'].get(), self.input_vars['ey'].get()]
            elif act in ["type", "open", "answer"]:
                new_data["text"] = self.input_vars['text'].get()
            elif act == "system_button":
                new_data["button"] = self.input_vars['button'].get()
            elif act == "terminate":
                new_data["status"] = self.input_vars['status'].get()

            self.result = json.dumps(new_data, ensure_ascii=False)
            self.destroy()
        except Exception as e:
            messagebox.showerror("错误", f"输入格式有误: {e}")


# ==========================================
# 3. 主应用程序
# ==========================================
class ExcelViewerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("高质量轨迹数据精修工作台 (SFT/RLHF 专版)")
        self.root.geometry("1700x950")

        self.file_path = None
        self.df = None
        self.task_groups = {}
        self.tk_image = None
        self.orig_size = (0, 0)
        self.current_size = (0, 0)

        # 状态追踪容器
        self.sop_edited = {}
        self.coord_edited = {}
        self.original_actions = {}  # 暗箱快照：存储修改前的原版错误动作
        self.listbox_mapping = []

        self.pick_callback = None
        self.pick_type = None

        self._setup_ui()

    def _setup_ui(self):
        self.outer_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.outer_paned.pack(fill=tk.BOTH, expand=True)

        self.left_container = ttk.Frame(self.outer_paned)
        self.outer_paned.add(self.left_container, weight=4)
        self.left_v_paned = ttk.PanedWindow(self.left_container, orient=tk.VERTICAL)
        self.left_v_paned.pack(fill=tk.BOTH, expand=True)

        # ================= 上方 (图片 + 任务步骤) =================
        self.upper_frame = ttk.Frame(self.left_v_paned)
        self.left_v_paned.add(self.upper_frame, weight=7)
        self.upper_h_paned = ttk.PanedWindow(self.upper_frame, orient=tk.HORIZONTAL)
        self.upper_h_paned.pack(fill=tk.BOTH, expand=True)

        self.image_outer = ttk.LabelFrame(self.upper_h_paned, text=" 截屏显示")
        self.upper_h_paned.add(self.image_outer, weight=1)
        self.pos_label = ttk.Label(self.image_outer, text="等待导入...", font=('Arial', 9))
        self.pos_label.pack(side=tk.TOP, anchor=tk.E, padx=5)
        self.image_canvas = tk.Canvas(self.image_outer, bg="#1a1a1a", highlightthickness=0)
        self.image_canvas.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        self.image_canvas.bind("<Motion>", self.update_mouse_position)
        self.image_canvas.bind("<Button-1>", self.on_canvas_click)
        self.image_canvas.bind("<Configure>", lambda e: self.on_canvas_resize())

        self.center_container = ttk.Frame(self.upper_h_paned)
        self.upper_h_paned.add(self.center_container, weight=2)

        self.full_task_text = tk.Text(self.center_container, height=4, wrap=tk.WORD, font=('Arial', 10), bg="#f4f4f4",
                                      borderwidth=1, relief="solid")
        self.full_task_text.pack(side=tk.TOP, fill=tk.X, padx=5, pady=(5, 0))
        self.full_task_text.config(state=tk.DISABLED)
        self.full_task_text.tag_configure("header", font=('Arial', 10, 'bold'), foreground="#555555")

        self.step_outer = ttk.LabelFrame(self.center_container, text=" 步骤列表 (双击 Action 动态编辑) ")
        self.step_outer.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 把原本的 cols 定义和 column 宽度设置替换为：
        cols = ('Excel行号', 'Action', 'SOP', '编辑SOP', '微观 (Micro)', '宏观 (Macro)')
        self.step_tree = ttk.Treeview(self.step_outer, columns=cols, show='headings', selectmode='browse')
        for col in cols: self.step_tree.heading(col, text=col)

        self.step_tree.column('Excel行号', width=70, minwidth=60, anchor=tk.CENTER, stretch=False)
        self.step_tree.column('Action', width=260, minwidth=150, stretch=True)
        self.step_tree.column('SOP', width=200, minwidth=150, stretch=True)
        self.step_tree.column('编辑SOP', width=70, minwidth=60, anchor=tk.CENTER, stretch=False)
        self.step_tree.column('微观 (Micro)', width=90, minwidth=70, anchor=tk.CENTER, stretch=False)
        self.step_tree.column('宏观 (Macro)', width=90, minwidth=70, anchor=tk.CENTER, stretch=False)

        tree_scroll_y = ttk.Scrollbar(self.step_outer, orient=tk.VERTICAL, command=self.step_tree.yview)
        tree_scroll_x = ttk.Scrollbar(self.step_outer, orient=tk.HORIZONTAL, command=self.step_tree.xview)
        self.step_tree.configure(yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set)
        tree_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        tree_scroll_x.pack(side=tk.BOTTOM, fill=tk.X)
        self.step_tree.pack(fill=tk.BOTH, expand=True)

        # 🌟 UI高亮配置：SFT 与 RLHF 背景色
        self.step_tree.tag_configure('bad_interval', background='#ffcccc')  # 浅红色（异常预警）
        self.step_tree.tag_configure('sft_edit', background='#fff9c4')  # 浅黄色
        self.step_tree.tag_configure('rl_edit', background='#ffe0b2')  # 浅橙色

        self.step_tree.bind('<<TreeviewSelect>>', self.on_step_selected)
        self.step_tree.bind('<Double-1>', self.on_tree_double_click)
        self.step_tree.bind('<Button-3>', self.show_context_menu)
        self.step_tree.bind('<Button-2>', self.show_context_menu)
        self.step_tree.bind('<Delete>', self.delete_selected_step)

        self.step_menu = tk.Menu(self.root, tearoff=0)
        self.step_menu.add_command(label="🗑️ 删除当前步骤 (Delete)", command=self.delete_selected_step)

        # ================= 下方 (三栏结构思考过程) =================
        self.think_outer = ttk.LabelFrame(self.left_v_paned, text=" 思考过程与 COT 展示 ")
        self.left_v_paned.add(self.think_outer, weight=3)
        self.think_h_paned = ttk.PanedWindow(self.think_outer, orient=tk.HORIZONTAL)
        self.think_h_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        left_think_v = ttk.PanedWindow(self.think_h_paned, orient=tk.VERTICAL)
        self.think_h_paned.add(left_think_v, weight=2)
        self.step_think_box = self._create_text_area(left_think_v, " 微观质检思考 (Micro)")
        self.traj_think_box = self._create_text_area(left_think_v, " 宏观质检思考 (Macro)")

        right_cot_f = ttk.Frame(self.think_h_paned)
        self.think_h_paned.add(right_cot_f, weight=3)
        self.cot_think_box = self._create_text_area(right_cot_f, " COT 原始回复 (Model Response)")

        # ================= 右侧管理区 (子任务列表) =================
        self.right_outer = ttk.LabelFrame(self.outer_paned, text=" 子任务分组 (按空格键切换状态) ")
        self.outer_paned.add(self.right_outer, weight=1)

        # 🌟 ===== 新增：顶部下拉分类筛选器 =====
        filter_frame = ttk.Frame(self.right_outer)
        filter_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)
        ttk.Label(filter_frame, text="分类筛选:").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar(value="全部任务")
        self.filter_combo = ttk.Combobox(filter_frame, textvariable=self.filter_var, state="readonly")
        self.filter_combo['values'] = ["全部任务", "🔴 需修: 完成但异常", "🔴 需修: 未完成且异常", "🟢 无需修: 完成且正常",
                                       "🟡 待定: 未完成但正常"]
        self.filter_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.filter_combo.bind("<<ComboboxSelected>>", self.apply_task_filter)
        self.current_display_mapping = []  # 记录当前过滤后展示的任务顺序

        self.task_listbox = tk.Listbox(self.right_outer, font=('Arial', 10), selectbackground="#007bff",
                                       selectforeground="white")
        list_scroll_y = ttk.Scrollbar(self.right_outer, orient=tk.VERTICAL, command=self.task_listbox.yview)
        self.task_listbox.configure(yscrollcommand=list_scroll_y.set)
        list_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.task_listbox.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.task_listbox.bind('<<ListboxSelect>>', self.on_task_selected)
        self.task_listbox.bind('<space>', self.toggle_listbox_status)

        self.btn_area = ttk.Frame(self.right_outer)
        self.btn_area.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)
        tk.Button(self.btn_area, text="导入新Excel", bg="#007bff", fg="white", font=('Arial', 10, 'bold'), height=2,
                  command=self.load_new_file).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        tk.Button(self.btn_area, text="提交分流导出", bg="#28a745", fg="white", font=('Arial', 10, 'bold'), height=2,
                  command=self.submit_data).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

    def apply_task_filter(self, event=None):
        self.task_listbox.delete(0, tk.END)
        self.current_display_mapping = []
        filter_val = self.filter_var.get()

        for gid in getattr(self, 'listbox_mapping', []):
            info = self.task_groups[gid]
            q = info['quality']

            # 判断当前任务是否符合筛选条件
            match = False
            if filter_val == "全部任务":
                match = True
            elif "完成但异常" in filter_val and q == "完成但过程有异常":
                match = True
            elif "未完成且异常" in filter_val and q == "未完成且过程有异常":
                match = True
            elif "完成且正常" in filter_val and q == "完成且过程正常":
                match = True
            elif "未完成但正常" in filter_val and q == "未完成但过程未发现明显异常":
                match = True

            if match:
                self.current_display_mapping.append(gid)
                is_ref = self.check_if_refined(gid)
                star = "*" if is_ref else ""
                exp_str = "☑️ 导出" if info['export'] else "⛔ 丢弃"
                self.task_listbox.insert(tk.END, f"[{exp_str}] {info['prefix']} {star}{info['meta_task']}")
                # 注意：这里用 tk.END 作为索引
                self.task_listbox.itemconfig(tk.END, {'bg': info['final_bg']})

        # 刷新完成后自动选中第一项
        if self.task_listbox.size() > 0:
            self.task_listbox.selection_set(0)
            self.on_task_selected(None)
        else:
            # 如果该分类下没有任务，清空中间的详情展示
            for item in self.step_tree.get_children(): self.step_tree.delete(item)
            self.full_task_text.config(state=tk.NORMAL)
            self.full_task_text.delete("1.0", tk.END)
            self.full_task_text.config(state=tk.DISABLED)
            self.image_canvas.delete("all")

    def _create_text_area(self, parent, title):
        frame = ttk.Frame(parent)
        if isinstance(parent, ttk.PanedWindow):
            parent.add(frame, weight=1)
        else:
            frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=f"■ {title}:", font=('Arial', 9, 'bold')).pack(anchor=tk.W)
        txt = tk.Text(frame, wrap=tk.WORD, font=('Consolas', 10), bg="#fdfdfd", borderwidth=1, relief="solid")
        scroll = ttk.Scrollbar(frame, command=txt.yview)
        txt.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(fill=tk.BOTH, expand=True)
        txt.config(state=tk.DISABLED)
        return txt

    def _adjust_column_widths(self):
        for col in ['Action', 'SOP']:
            max_w = 200
            for item in self.step_tree.get_children():
                val = str(self.step_tree.set(item, col))
                w = len(val) * 7
                if w > max_w: max_w = w
            if max_w > 600: max_w = 600
            self.step_tree.column(col, width=max_w)

    # ================= 数据加载与渲染 =================
    def load_new_file(self):
        f_path = filedialog.askopenfilename(filetypes=[("Excel files", "*.xlsx *.xls")])
        if not f_path: return
        self._reset_full_state()
        try:
            self.file_path = f_path
            self.df = pd.read_excel(f_path, sheet_name=0)

            if 'trajectory_quality_type' not in self.df.columns: self.df['trajectory_quality_type'] = "未知"
            if 'Bad_Interval' not in self.df.columns: self.df['Bad_Interval'] = "Normal"

            groups = []
            current_group = None
            last_task_name = None
            current_color_idx = 0
            bg_colors_normal = ['#ffffff', '#eefaf1']

            # 第一步：按任务划分组别
            for idx, row in self.df.iterrows():
                t = str(row['task'])
                mt = str(row['meta_task'])

                if current_group is None or current_group['task'] != t or current_group['meta_task'] != mt:
                    if current_group is not None: groups.append(current_group)
                    current_group = {
                        'group_id': f"group_{len(groups)}",
                        'task': t, 'meta_task': mt,
                        'indices': [idx]
                    }
                else:
                    current_group['indices'].append(idx)

            if current_group is not None: groups.append(current_group)

            # 第二步：对每个组进行底层的 0/1 扫描，动态推导四大分类
            for g in groups:
                indices = g['indices']

                # --- 1. 推导任务是否完成 ---
                # 读取该子任务第一行的 task_manual_result (兼容 pandas 读取的 1.0/0.0)
                first_row = self.df.loc[indices[0]]
                task_res = str(first_row.get('task_manual_result', '')).strip()
                if task_res in ('1', '1.0'):
                    completion_str = "完成"
                elif task_res in ('0', '0.0'):
                    completion_str = "未完成"
                else:
                    completion_str = "未知"  # 兜底防空值

                # --- 2. 推导过程是否异常 ---
                is_abnormal = False
                for idx in indices[:-1]:
                    r = self.df.loc[idx]
                    mi_man = str(r.get('micro_manual', '')).strip()
                    ma_man = str(r.get('macro_manual', '')).strip()

                    # 优先级 1：人工标注了 0
                    if mi_man in ('0', '0.0') or ma_man in ('0', '0.0'):
                        is_abnormal = True
                        break
                    # 优先级 2：人工还没标，兜底查老列名
                    elif mi_man == '' and ma_man == '':
                        if "bad_interval" in str(r.get('Bad_Interval', '')).lower():
                            is_abnormal = True
                            break

                # --- 3. 组合最终类型与 UI 配置 ---
                if completion_str == "完成":
                    quality = "完成但过程有异常" if is_abnormal else "完成且过程正常"
                elif completion_str == "未完成":
                    quality = "未完成且过程有异常" if is_abnormal else "未完成但过程未发现明显异常"
                else:
                    quality = "未知"

                g['quality'] = quality

                if quality == "完成且过程正常":
                    prefix, export, bg = "[🟢 完/正]", True, None
                elif quality == "完成但过程有异常":
                    prefix, export, bg = "[🔴 完/异常]", False, "#ffe6e6"
                elif quality == "未完成且过程有异常":
                    prefix, export, bg = "[🔴 未/异常]", False, "#ffe6e6"
                elif quality == "未完成但过程未发现明显异常":
                    prefix, export, bg = "[🟡 未/正]", False, "#fff3cd"
                else:
                    prefix, export, bg = "[⚪ 未知]", False, "#f8f9fa"

                g['prefix'] = prefix
                g['export'] = export
                g['base_bg'] = bg

            self.listbox_mapping = []
            for i, g in enumerate(groups):
                g['initial_indices_count'] = len(g['indices'])
                gid = g['group_id']
                self.task_groups[gid] = g
                self.listbox_mapping.append(gid)

                # 动态分配背景色
                if g['base_bg'] is None:
                    if g['task'] != last_task_name:
                        if last_task_name is not None: current_color_idx = 1 - current_color_idx
                        last_task_name = g['task']
                    final_bg = bg_colors_normal[current_color_idx]
                else:
                    final_bg = g['base_bg']

                g['final_bg'] = final_bg  # 存下来供后续刷新使用

            # 读取完毕后，重置下拉框并调用筛选器渲染界面
            self.filter_var.set("全部任务")
            self.apply_task_filter()

            self.pos_label.config(text=f"当前文件: {os.path.basename(self.file_path)}")
        except Exception as e:
            messagebox.showerror("错误", f"读取失败: {e}")

    # ================= 状态追踪引擎 =================
    def _get_gid_by_idx(self, idx):
        for gid, info in self.task_groups.items():
            if idx in info['indices']: return gid
        return None

    def check_if_refined(self, gid):
        info = self.task_groups[gid]
        if len(info['indices']) < info.get('initial_indices_count', 0): return True
        for idx in info['indices']:
            if idx in self.sop_edited or idx in self.coord_edited: return True
        return False

    def update_listbox_display(self, gid):
        # 换成了 current_display_mapping
        if gid not in getattr(self, 'current_display_mapping', []): return
        list_idx = self.current_display_mapping.index(gid)
        info = self.task_groups[gid]

        is_ref = self.check_if_refined(gid)
        star = "*" if is_ref else ""
        exp_str = "☑️ 导出" if info['export'] else "⛔ 丢弃"

        new_text = f"[{exp_str}] {info['prefix']} {star}{info['meta_task']}"

        is_selected = self.task_listbox.selection_includes(list_idx)
        self.task_listbox.delete(list_idx)
        self.task_listbox.insert(list_idx, new_text)
        self.task_listbox.itemconfig(list_idx, {'bg': info['final_bg']})

        if is_selected: self.task_listbox.selection_set(list_idx)

    # ================= 交互回调 =================
    def toggle_listbox_status(self, event):
        sel = self.task_listbox.curselection()
        if not sel: return "break"
        list_idx = sel[0]
        # 换成了 current_display_mapping
        group_id = self.current_display_mapping[list_idx]
        info = self.task_groups[group_id]

        info['export'] = not info['export']
        self.update_listbox_display(group_id)
        return "break"

    def on_task_selected(self, event):
        sel = self.task_listbox.curselection()
        if not sel: return

        self.pick_callback = None
        self.image_canvas.config(cursor="")
        self.pos_label.config(text="原图坐标...", foreground="black")

        list_idx = sel[0]
        # 换成了 current_display_mapping
        group_id = self.current_display_mapping[list_idx]
        group_info = self.task_groups[group_id]

        self.full_task_text.config(state=tk.NORMAL)
        self.full_task_text.delete("1.0", tk.END)
        self.full_task_text.insert(tk.END, "【主任务】: \n", "header")
        self.full_task_text.insert(tk.END, f"{group_info['task']}\n\n")
        self.full_task_text.insert(tk.END, "【子任务】: \n", "header")
        self.full_task_text.insert(tk.END, f"{group_info['meta_task']}\n")
        self.full_task_text.config(state=tk.DISABLED)

        self.show_task_details(group_id)

    def show_task_details(self, group_id):
        for item in self.step_tree.get_children(): self.step_tree.delete(item)
        if group_id not in self.task_groups: return

        info = self.task_groups[group_id]

        # 计算并分配 SFT 和 RL 的标签颜色
        edited_indices = [i for i in info['indices'] if i in self.original_actions]
        edited_indices.sort()

        for idx in info['indices']:
            row = self.df.loc[idx]
            edit_status = "已编辑" if idx in self.sop_edited else ""

            # --- 🌟 核心修改：双重优先级判定异常区间（排除最后一步） ---
            mi_man = str(row.get('micro_manual', '')).strip()
            ma_man = str(row.get('macro_manual', '')).strip()

            is_bad_interval = False
            is_last_step = (idx == info['indices'][-1])  # 判断当前行是否为该子任务的最后一步

            # 只有在【不是最后一步】的情况下，才去判断是否标红
            if not is_last_step:
                # 优先级 1：人工标注微观或宏观为 0
                if mi_man in ('0', '0.0') or ma_man in ('0', '0.0'):
                    is_bad_interval = True
                # 优先级 2：若人工没标 (为空)，兜底查老列名
                elif mi_man == '' and ma_man == '':
                    if "bad_interval" in str(row.get('Bad_Interval', '')).lower():
                        is_bad_interval = True
            # ---------------------------------------------------

            row_tags = ()
            if idx in edited_indices:
                if edited_indices.index(idx) == 0:
                    row_tags = ('sft_edit',)
                elif edited_indices.index(idx) == 1:
                    row_tags = ('rl_edit',)
            elif is_bad_interval:
                row_tags = ('bad_interval',)


            # --- 🌟 新增：微观与宏观数据提取逻辑 (人工优先，模型兜底) ---
            def get_pred_val(manual_col, pred_col):
                val = row.get(manual_col, '')
                # 如果 manual_col 是 NaN 或者是空字符串，则去取 pred_col
                if pd.isna(val) or str(val).strip() == '':
                    val = row.get(pred_col, '')
                # 如果兜底还是空，就返回 '-'
                if pd.isna(val) or str(val).strip() == '':
                    return '-'
                return str(val)

            micro_val = get_pred_val('micro_manual', 'micro_pred')
            macro_val = get_pred_val('macro_manual', 'macro_pred')

            self.step_tree.insert('', tk.END, iid=f"row_{idx}", tags=row_tags, values=(
                idx + 2, row['actions'], row.get('sop', ''), edit_status, micro_val, macro_val
            ))

        self._adjust_column_widths()
        if info['indices']: self.step_tree.selection_set(f"row_{info['indices'][0]}")

    def edit_sop_for_row(self, idx):
        if idx is None:
            sel = self.step_tree.selection()
            if not sel: return
            idx = int(sel[0][4:])

        row = self.df.loc[idx]
        current_sop = str(row.get('sop', ''))
        dialog = SOPEditDialog(self.root, f"编辑SOP (行号: {idx + 2})", current_sop, idx)
        self.root.wait_window(dialog)

        if dialog.result is not None:
            self.df.loc[idx, 'sop'] = dialog.result
            self.sop_edited[idx] = True
            self.step_tree.set(f"row_{idx}", 'SOP', dialog.result)
            self.step_tree.set(f"row_{idx}", '编辑SOP', "已编辑")
            gid = self._get_gid_by_idx(idx)
            if gid: self.update_listbox_display(gid)

    # ================= 委托取点逻辑 =================
    def start_canvas_pick(self, callback, pick_type):
        self.pick_callback = callback
        self.pick_type = pick_type
        self.image_canvas.config(cursor="crosshair")
        # 修复 fg 报错
        self.pos_label.config(text="[取点模式] 请在左侧截图中点击选择坐标...", foreground="#dc3545",
                              font=('Arial', 10, 'bold'))

    def on_canvas_click(self, event):
        if not getattr(self, 'pick_callback', None): return
        if not self.tk_image: return

        cw, ch = self.image_canvas.winfo_width(), self.image_canvas.winfo_height()
        cur_w, cur_h = self.current_size

        ix = event.x - (cw - cur_w) // 2
        iy = event.y - (ch - cur_h) // 2

        # 钳制防死区
        ix = max(0, min(cur_w, ix))
        iy = max(0, min(cur_h, iy))

        orig_x = int(ix * (self.orig_size[0] / cur_w))
        orig_y = int(iy * (self.orig_size[1] / cur_h))

        raw_width, raw_height = self.orig_size[0], self.orig_size[1]
        norm_x = int(round(orig_x * 999 / raw_width))
        norm_y = int(round(orig_y * 999 / raw_height))
        norm_x, norm_y = max(0, min(999, norm_x)), max(0, min(999, norm_y))

        cb = self.pick_callback
        pt = self.pick_type

        self.pick_callback = None
        self.image_canvas.config(cursor="")
        self.pos_label.config(text="原图坐标...", foreground="black")

        cb(norm_x, norm_y, pt)

    def on_tree_double_click(self, event):
        item = self.step_tree.identify_row(event.y)
        column = self.step_tree.identify_column(event.x)
        if item and item.startswith('row_'):
            idx = int(item[4:])
            if column in ('#3', '#4'):
                self.edit_sop_for_row(idx)
            elif column == '#2':
                action_str = str(self.df.loc[idx, 'actions'])
                dialog = ActionEditDialog(self.root, action_str, self)
                self.root.wait_window(dialog)

                if dialog.result is not None:
                    # 🌟 存入暗箱 (仅存最原始错误版本)
                    if idx not in self.original_actions:
                        self.original_actions[idx] = action_str

                    self.df.loc[idx, 'actions'] = dialog.result
                    self.step_tree.set(f"row_{idx}", 'Action', dialog.result)
                    self.coord_edited[idx] = True

                    gid = self._get_gid_by_idx(idx)
                    if gid:
                        self.update_listbox_display(gid)
                        self.show_task_details(gid)  # <--- 新增这一行：强制重绘整个表格以刷新标黄颜色

                    self.root.update_idletasks()
                    self.step_tree.selection_set(f"row_{idx}")
                    self.on_step_selected(None)

            elif column == '#7':
                self.toggle_cutoff_mark(idx)

    # ================= 核心：智能分流导出引擎 =================
    def submit_data(self):
        if self.df is None: return

        sft_list, rl_list = [], []
        unedited_pass_list, warning_list = [], []

        for gid, info in self.task_groups.items():
            # 【核心屏障】：没打勾“☑️ 导出”的，直接无视，全部丢弃
            if not info['export']: continue

            indices = info['indices'].copy()
            edited_in_group = [idx for idx in indices if idx in self.original_actions]
            edited_in_group.sort()

            # --- 0. 没做动作修改的轨迹 ---
            if len(edited_in_group) == 0:
                if indices:
                    df_un = self.df.loc[indices].copy()
                    has_sop_edit = any(idx in self.sop_edited for idx in indices)

                    if has_sop_edit:
                        # 仅改 SOP：送去 SFT
                        if 'is_refined' not in df_un.columns: df_un.insert(len(df_un.columns), 'is_refined', 0)
                        df_un['is_refined'] = [1 if i in self.sop_edited else 0 for i in indices]
                        sft_list.append(df_un)
                    else:
                        # 纯原生数据分流防污染
                        if 'is_refined' not in df_un.columns:
                            df_un.insert(len(df_un.columns), 'is_refined', 0)
                        else:
                            df_un['is_refined'] = 0

                        if info['quality'] == "完成且过程正常":
                            unedited_pass_list.append(df_un)  # 纯净底料
                        else:
                            warning_list.append(df_un)  # 异常轨迹被强行导出，进入警示隔离区！
                continue

            # --- 1. SFT 单轨分身 ---
            if len(edited_in_group) == 1:
                sft_cutoff = edited_in_group[0]
                sft_indices = indices[:indices.index(sft_cutoff) + 1]
                df_sft = self.df.loc[sft_indices].copy()
                if 'is_refined' not in df_sft.columns: df_sft.insert(len(df_sft.columns), 'is_refined', 0)
                df_sft['is_refined'] = [1 if i == sft_cutoff else 0 for i in sft_indices]
                sft_list.append(df_sft)

            # --- 2. SFT + RL 双轨魔术分身 ---
            elif len(edited_in_group) >= 2:
                sft_cutoff = edited_in_group[0]
                rl_cutoff = edited_in_group[1]

                sft_indices = indices[:indices.index(sft_cutoff) + 1]
                df_sft = self.df.loc[sft_indices].copy()
                if 'is_refined' not in df_sft.columns: df_sft.insert(len(df_sft.columns), 'is_refined', 0)
                df_sft['is_refined'] = [1 if i == sft_cutoff else 0 for i in sft_indices]
                sft_list.append(df_sft)

                rl_indices = indices[:indices.index(rl_cutoff) + 1]
                df_rl = self.df.loc[rl_indices].copy()
                df_rl.loc[sft_cutoff, 'actions'] = self.original_actions[sft_cutoff]

                if 'is_refined' not in df_rl.columns: df_rl.insert(len(df_rl.columns), 'is_refined', 0)
                df_rl['is_refined'] = [1 if i == rl_cutoff else 0 for i in rl_indices]
                rl_list.append(df_rl)

        if not any([sft_list, rl_list, unedited_pass_list, warning_list]):
            messagebox.showwarning("提示", "当前没有符合导出条件的数据（所有任务均为丢弃状态）。")
            return

        out_dir = os.path.dirname(self.file_path)
        base, ts = os.path.splitext(os.path.basename(self.file_path))[0], datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = os.path.join(out_dir, f"{base}_精修与筛查_{ts}.xlsx")

        try:
            with pd.ExcelWriter(out_path, engine='openpyxl') as writer:
                if sft_list: pd.concat(sft_list).to_excel(writer, sheet_name='SFT_人工精修', index=False)
                if rl_list: pd.concat(rl_list).to_excel(writer, sheet_name='RL_负向反思', index=False)
                if unedited_pass_list: pd.concat(unedited_pass_list).to_excel(writer, sheet_name='原生_完美通过',
                                                                              index=False)
                if warning_list: pd.concat(warning_list).to_excel(writer, sheet_name='原生_异常待处理', index=False)

            msg = (f"保存并分流导出成功！\n\n"
                   f"🎯 SFT 人工精修: {len(sft_list)} 条\n"
                   f"🧠 RL 负向反思: {len(rl_list)} 条\n"
                   f"✅ 原生 完美轨迹: {len(unedited_pass_list)} 条\n"
                   f"⚠️ 原生 异常隔离: {len(warning_list)} 条 (勾了导出但没精修的异常数据)")
            messagebox.showinfo("导出完成", msg)
            self._reset_full_state()
        except Exception as e:
            messagebox.showerror("导出错误", f"导出文件时发生错误：\n{e}")

    # ================= 杂项清理与系统刷新 =================
    def _reset_full_state(self):
        self.file_path = self.df = None
        self.task_groups = {}
        self.sop_edited = {}
        self.coord_edited = {}
        self.original_actions = {}
        self.listbox_mapping = []
        self.pick_callback = None

        self.image_canvas.delete("all")
        self.image_canvas.config(cursor="")
        self.task_listbox.delete(0, tk.END)
        self.full_task_text.config(state=tk.NORMAL)
        self.full_task_text.delete("1.0", tk.END)
        self.full_task_text.config(state=tk.DISABLED)
        for item in self.step_tree.get_children(): self.step_tree.delete(item)
        self._update_think_display("", "", "")

    def _update_think_display(self, s, t, cot):
        for box, content in [(self.step_think_box, s), (self.traj_think_box, t), (self.cot_think_box, cot)]:
            box.config(state=tk.NORMAL)
            box.delete("1.0", tk.END)
            box.insert("1.0", str(content).strip() if pd.notna(content) else "")
            box.config(state=tk.DISABLED)

    def on_step_selected(self, event):
        sel = self.step_tree.selection()
        if sel:
            item_id = sel[0]
            idx = int(item_id[4:]) if item_id.startswith('row_') else int(item_id)
            self.draw_image_step(idx)
            row = self.df.loc[idx]

            cot_text = "暂无 COT 数据"
            img_path = str(row.get('image', ''))
            if img_path:
                base_path = os.path.splitext(img_path)[0]
                txt_path = os.path.join(os.getcwd(), f"{base_path}_model_response.txt")
                if os.path.exists(txt_path):
                    try:
                        with open(txt_path, 'r', encoding='utf-8') as f:
                            cot_text = f.read()
                    except:
                        cot_text = "读取 COT 数据失败"

            self._update_think_display(row.get('mi_thought', ''), row.get('ma_thought', ''), cot_text)

    def draw_image_step(self, idx):
        try:
            row = self.df.loc[idx]
            img_path = os.path.join(os.getcwd(), str(row['image']))
            img_data = np.fromfile(img_path, dtype=np.uint8)
            image = cv2.imdecode(img_data, cv2.IMREAD_COLOR)
            self._process_cv_draw(image, str(row['actions']), str(row.get('actions_box', '')))
            img_pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
            cw, ch = self.image_canvas.winfo_width(), self.image_canvas.winfo_height()
            if cw < 10: cw, ch = 700, 500
            img_pil.thumbnail((cw, ch))
            self.orig_size, self.current_size, self.tk_image = (image.shape[1], image.shape[0]), (
            img_pil.width, img_pil.height), ImageTk.PhotoImage(img_pil)
            self.image_canvas.delete("all")
            self.image_canvas.create_image(cw // 2, ch // 2, anchor=tk.CENTER, image=self.tk_image)
        except:
            pass

    def _process_cv_draw(self, img, actions, boxes):
        raw_height, raw_width = img.shape[:2]
        action_data = {}
        try:
            action_data = json.loads(actions)
        except:
            return

        act = action_data.get("action", "")

        if act in ["click", "long_press"]:
            coords = action_data.get("coordinate", [])
            if len(coords) == 2:
                real_x = round((raw_width / 999) * float(coords[0]))
                real_y = round((raw_height / 999) * float(coords[1]))
                color = (0, 0, 255) if act == "click" else (255, 0, 255)
                cv2.circle(img, (int(real_x), int(real_y)), 25, color, -1)

            bbox = re.findall(r"\[([\d\.]+),\s*([\d\.]+),\s*([\d\.]+),\s*([\d\.]+)\]", boxes)
            if bbox:
                cv2.rectangle(img, (int(float(bbox[0][0])), int(float(bbox[0][1]))),
                              (int(float(bbox[0][2])), int(float(bbox[0][3]))), (0, 255, 0), 3)

        elif act == "swipe":
            starts = action_data.get("start_coordinate", [])
            ends = action_data.get("end_coordinate", [])
            if len(starts) == 2 and len(ends) == 2:
                real_start_x = round((raw_width / 999) * float(starts[0]))
                real_start_y = round((raw_height / 999) * float(starts[1]))
                real_end_x = round((raw_width / 999) * float(ends[0]))
                real_end_y = round((raw_height / 999) * float(ends[1]))
                cv2.arrowedLine(img, (int(real_start_x), int(real_start_y)), (int(real_end_x), int(real_end_y)),
                                (255, 0, 0), 5)

    def update_mouse_position(self, event):
        if not self.tk_image: return
        cw, ch = self.image_canvas.winfo_width(), self.image_canvas.winfo_height()
        cur_w, cur_h = self.current_size
        ix, iy = event.x - (cw - cur_w) // 2, event.y - (ch - cur_h) // 2
        if 0 <= ix <= cur_w and 0 <= iy <= cur_h:
            if not self.pick_callback:
                self.pos_label.config(
                    text=f"原图坐标: ({int(ix * (self.orig_size[0] / cur_w))}, {int(iy * (self.orig_size[1] / cur_h))})")

    def on_canvas_resize(self):
        sel = self.step_tree.selection()
        if sel:
            item_id = sel[0]
            idx = int(item_id[4:]) if item_id.startswith('row_') else int(item_id)
            self.draw_image_step(idx)

    def show_context_menu(self, event):
        iid = self.step_tree.identify_row(event.y)
        if iid:
            self.step_tree.selection_set(iid)
            self.step_menu.post(event.x_root, event.y_root)

    def delete_selected_step(self, event=None):
        sel = self.step_tree.selection()
        if not sel: return
        item_id = sel[0]
        if not item_id.startswith('row_'): return
        idx = int(item_id[4:])

        if not messagebox.askyesno("确认删除",
                                   f"确定要删除 Excel 行号为 {idx + 2} 的步骤吗？\n\n注意：这将从数据中移除该步骤。"): return

        self.df = self.df.drop(index=idx)
        for gid, info in self.task_groups.items():
            if idx in info['indices']:
                info['indices'].remove(idx)
                if len(info['indices']) == 0: messagebox.showinfo("提示",
                                                                  f"该子任务的所有步骤已被清空。\n\n导出时将自动过滤掉此任务。")
                self.update_listbox_display(gid)
                break

        self.step_tree.delete(item_id)
        self.image_canvas.delete("all")
        self._update_think_display("", "", "")
        self.pos_label.config(text="已删除当前步骤")


if __name__ == "__main__":
    root = tk.Tk()
    style = ttk.Style()
    style.configure("TLabelframe.Label", font=("Arial", 10, "bold"))


    # ===== 核心修复：Tkinter Windows 主题屏蔽 Treeview 背景色的 Bug =====
    def fixed_map(option):
        return [elm for elm in style.map('Treeview', query_opt=option) if elm[:2] != ('!disabled', '!selected')]


    style.map('Treeview', foreground=fixed_map('foreground'), background=fixed_map('background'))
    # ==============================================================

    app = ExcelViewerApp(root)
    root.mainloop()