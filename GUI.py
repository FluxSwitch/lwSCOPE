"""
==================== GUI.py 檔案架構說明(START) ====================
本檔案為主程式入口，負責：
- DearPyGUI 介面渲染與事件處理
- 與 Logic 進程的 IPC 通信
- 數據可視化（圖表繪製）
- 用戶交互處理

主流程說明：
1. 於 __main__ 區塊執行 main()，確保 multiprocessing 支援。
2. main() 會：
    - 建立三個 multiprocessing.Queue 作為 IPC 溝通管道。
    - 建立 UIHandle 實例（UIInstance），負責所有 UI 狀態與事件。
    - 啟動 Logic 子進程（logic_main），負責資料處理與協議解析。
    - 啟動 UIInstance.loop() 進入主 UI 事件循環。
    - 結束時確保 Logic 子進程正確終止。
3. UIInstance.loop() 會持續處理 DearPyGUI 事件、資料更新與 IPC 訊息。

本檔案為多進程架構的 UI 端主控，與 Logic.py 進程協作完成資料流與互動。
==================== GUI.py 檔案架構說明(END) =====================
"""

USE_CUSTOM_WINDOW = False  # False: 使用原生 DearPyGUI 視窗，不走 3rd_party/CustomWindow

# =========================
# 除錯列印開關
# =========================
DEBUG_PRINT = False
def dprint(*args, **kwargs):
    if DEBUG_PRINT:
        print(*args, **kwargs)

# =========================
# import 區塊：標準/第三方/自訂模組導入 (型別標註於註解)
# =========================
import dearpygui.dearpygui as dpg
import time
import multiprocessing
import os
import sys
import importlib
import textwrap
import re
import csv
import math
import array as _array
import numpy as np
from bisect import bisect_left, bisect_right
try:
    import winsound  # Windows only
except Exception:
    winsound = None
try:
    import pythoncom
    import win32com.client
    import win32con
    import win32ui
except Exception:
    pythoncom = None
    win32com = None
    win32con = None
    win32ui = None
from queue import Empty
from Logic import logic_main
from IPCDataClass import HSDataSource, UIMsg
import ctypes
from ctypes import windll

# PyInstaller 凍結環境偵測
if getattr(sys, 'frozen', False):
    _BASE_DIR = sys._MEIPASS
else:
    _BASE_DIR = os.path.dirname(__file__)

# 3rd-party CustomWindow (3rd_party/CustomWindow)
_CUSTOMWINDOW_DIR = os.path.join(_BASE_DIR, "3rd_party", "CustomWindow")
if USE_CUSTOM_WINDOW and _CUSTOMWINDOW_DIR not in sys.path:
    sys.path.insert(0, _CUSTOMWINDOW_DIR)

CustomWindowClass = None
try:
    if USE_CUSTOM_WINDOW:
        _customwindow_module = importlib.import_module("CustomWindow")
        customwindow_module = _customwindow_module
        CustomWindowClass = _customwindow_module.CustomWindow
    else:
        customwindow_module = None
except Exception:
    customwindow_module = None

# 建立 CustomWindow 實例
custom_window_instance = None
if USE_CUSTOM_WINDOW and customwindow_module is not None and CustomWindowClass is not None:
    try:
        custom_window_instance = CustomWindowClass(skip_font_loading=True)
    except Exception:
        custom_window_instance = None


class SaveManager:
    """Save-related features (PNG/CSV + UI state) composed into UIHandle."""

    def __init__(self, ui_handle):
        self.ui_handle = ui_handle

        # =========================
        # PNG capture state (no thread; uses dpg.output_frame_buffer callback)
        # =========================
        self._png_capture = None
        self._png_capture_pending = None
        self._framebuffer_callback = self._output_framebuffer_callback

        # =========================
        # CSV export state (no thread; processed in handler loop)
        # =========================
        self._csv_export_active = False
        self._csv_export_filepath = None
        self._csv_export_file = None
        self._csv_export_writer = None
        self._csv_export_row_index = 0
        self._csv_export_total_rows = 0
        self._csv_export_rows_per_frame = 1000

    def _play_success_beep(self) -> None:
        """Play a short, standard Windows info/notification beep (best-effort)."""
        if winsound is None:
            return
        try:
            winsound.MessageBeep(getattr(winsound, "MB_ICONASTERISK", 0))
        except Exception:
            pass

    # --------------------
    # Save buttons UX
    # --------------------
    def update_save_buttons_enabled_state(self):
        """Update Save buttons appearance.

        We do NOT disable the widgets (to avoid DPG/ImGui disabled-style overriding colors).
        Instead:
        - Keep buttons enabled
        - Block actions in callbacks when not allowed
        - Bind a faded theme to communicate disabled state
        """
        comm_started = (getattr(self.ui_handle, "CommSts", "Stopped") == "Started")
        csv_exporting = bool(getattr(self, "_csv_export_active", False))

        png_allowed = not comm_started
        csv_allowed = (not comm_started) and (not csv_exporting)

        def _apply(tag: str, allowed: bool):
            if not dpg.does_item_exist(tag):
                return
            try:
                dpg.show_item(tag)
            except Exception:
                pass
            try:
                if allowed:
                    # Unbind item theme so it renders normally
                    try:
                        dpg.bind_item_theme(tag, None)
                    except Exception:
                        dpg.bind_item_theme(tag, 0)
                else:
                    dpg.bind_item_theme(tag, "save_buttons_disabled_theme")
            except Exception:
                pass

        _apply("save_as_png_button", png_allowed)
        _apply("save_as_csv_button", csv_allowed)

    # --------------------
    # CSV export
    # --------------------
    def start_csv_export(self, filepath: str):
        """Initialize CSV export; actual writing is done in handler loop."""
        if self._csv_export_active:
            return

        if getattr(self.ui_handle, "CommSts", "Stopped") != "Stopped":
            return

        total = 0
        try:
            for i in range(1, getattr(self.ui_handle, "max_data_sources", 16) + 1):
                ds = getattr(self.ui_handle, "signal_data", {}).get(f"ds{i}", {})
                xs = ds.get("x", []) if isinstance(ds, dict) else []
                ys = ds.get("y", []) if isinstance(ds, dict) else []
                total = max(total, len(xs), len(ys))
        except Exception:
            total = 0
        # 只匯出最新 max_data_points 筆
        _max_pts = getattr(self.ui_handle, "max_data_points", total)
        csv_offset = max(total - _max_pts, 0)
        total = min(total, _max_pts)

        try:
            f = open(filepath, "w", newline="", encoding="utf-8-sig")
            w = csv.writer(f)
        except Exception as _e:
            dprint(f"[Save CSV] open failed: {_e!r}  path={filepath!r}")
            return

        custom_headers = []
        for i in range(1, getattr(self.ui_handle, "max_data_sources", 16) + 1):
            label = None
            try:
                input_tag = f"data_source_input_{i}"
                if dpg.does_item_exist(input_tag):
                    label = dpg.get_value(input_tag)
            except Exception:
                label = None

            if not isinstance(label, str):
                label = ""
            label = label.strip()
            if not label:
                label = f"DS{i}"
            custom_headers.append(label)

        mode = getattr(self.ui_handle, "x_axis_unit_mode", "By sequence")
        x_header = "x (by time)" if mode == "By time" else "x (by sequence)"
        header = ["index", x_header] + custom_headers
        try:
            w.writerow(header)
        except Exception:
            try:
                f.close()
            except Exception:
                pass
            return

        self._csv_export_active = True
        self._csv_export_filepath = filepath
        self._csv_export_file = f
        self._csv_export_writer = w
        self._csv_export_row_index = 0
        self._csv_export_total_rows = int(total)
        self._csv_export_offset = int(csv_offset)

        try:
            if dpg.does_item_exist("csv_export_modal"):
                dpg.show_item("csv_export_modal")
                self.center_csv_export_modal()
            if dpg.does_item_exist("csv_export_progress"):
                dpg.set_value("csv_export_progress", 0.0)
            if dpg.does_item_exist("csv_export_status_text"):
                dpg.set_value(
                    "csv_export_status_text",
                    f"Saving... 0/{self._csv_export_total_rows} rows",
                )
        except Exception:
            pass

        self.update_save_buttons_enabled_state()

    def center_csv_export_modal(self):
        """Center the CSV export modal window in the viewport."""
        if not dpg.does_item_exist("csv_export_modal"):
            return

        try:
            dpg.split_frame()
        except Exception:
            pass

        try:
            vp_w = int(dpg.get_viewport_client_width())
            vp_h = int(dpg.get_viewport_client_height())
        except Exception:
            return

        modal_w = 420
        modal_h = 140
        try:
            rw, rh = dpg.get_item_rect_size("csv_export_modal")
            rw = int(rw)
            rh = int(rh)
            if rw > 0 and rh > 0:
                modal_w, modal_h = rw, rh
        except Exception:
            try:
                cfg = dpg.get_item_configuration("csv_export_modal") or {}
                if isinstance(cfg, dict):
                    mw = cfg.get("width")
                    mh = cfg.get("height")
                    if mw:
                        modal_w = int(mw)
                    if mh:
                        modal_h = int(mh)
            except Exception:
                pass

        x = max(0, int((vp_w - modal_w) / 2))
        y = max(0, int((vp_h - modal_h) / 2))
        try:
            dpg.set_item_pos("csv_export_modal", [x, y])
        except Exception:
            pass

    def _finish_csv_export(self, ok: bool, reason: str = ""):
        """Finalize CSV export, close file and hide modal."""
        try:
            if self._csv_export_file:
                try:
                    self._csv_export_file.flush()
                except Exception:
                    pass
                self._csv_export_file.close()
        except Exception:
            pass

        filepath = self._csv_export_filepath

        self._csv_export_active = False
        self._csv_export_filepath = None
        self._csv_export_file = None
        self._csv_export_writer = None
        self._csv_export_row_index = 0
        self._csv_export_total_rows = 0

        try:
            if dpg.does_item_exist("csv_export_modal"):
                dpg.hide_item("csv_export_modal")
        except Exception:
            pass

        self.update_save_buttons_enabled_state()

    def process_csv_export_every_frame(self):
        """Write CSV rows in small batches per frame; update modal progress bar."""
        if not self._csv_export_active:
            return

        if getattr(self.ui_handle, "CommSts", "Stopped") != "Stopped":
            self._finish_csv_export(False, "CommSts != Stopped")
            return

        total = int(self._csv_export_total_rows)
        if total <= 0:
            try:
                if dpg.does_item_exist("csv_export_progress"):
                    dpg.set_value("csv_export_progress", 1.0)
                if dpg.does_item_exist("csv_export_status_text"):
                    dpg.set_value("csv_export_status_text", "Done. 0/0 rows")
            except Exception:
                pass
            self._finish_csv_export(True)
            return

        remaining = total - int(self._csv_export_row_index)
        batch = min(int(self._csv_export_rows_per_frame), int(remaining))
        if batch <= 0:
            self._finish_csv_export(True)
            return

        ds1 = getattr(self.ui_handle, "signal_data", {}).get("ds1", {})
        x_ref = ds1.get("x", []) if isinstance(ds1, dict) else []
        _off = int(self._csv_export_offset)

        try:
            for _ in range(batch):
                idx = int(self._csv_export_row_index)
                raw_idx = idx + _off  # 偏移到最新資料區段

                x_val = x_ref[raw_idx] if raw_idx < len(x_ref) else idx
                row = [idx, x_val]
                for ds_id in range(1, getattr(self.ui_handle, "max_data_sources", 16) + 1):
                    ds = getattr(self.ui_handle, "signal_data", {}).get(f"ds{ds_id}", {})
                    ys = ds.get("y", []) if isinstance(ds, dict) else []
                    row.append(ys[raw_idx] if raw_idx < len(ys) else "")

                self._csv_export_writer.writerow(row)
                self._csv_export_row_index += 1
        except Exception as e:
            self._finish_csv_export(False, f"write failed: {e}")
            return

        try:
            done = int(self._csv_export_row_index)
            progress = max(0.0, min(1.0, done / total))
            if dpg.does_item_exist("csv_export_progress"):
                dpg.set_value("csv_export_progress", progress)
            if dpg.does_item_exist("csv_export_status_text"):
                dpg.set_value("csv_export_status_text", f"Saving... {done}/{total} rows")
        except Exception:
            pass

        if int(self._csv_export_row_index) >= total:
            self._finish_csv_export(True)

    # --------------------
    # PNG save
    # --------------------
    def _get_item_rect_in_viewport(self, item_tag):
        """Return (rect_min, rect_max) in viewport coordinates if possible."""
        try:
            if hasattr(dpg, "get_item_rect_min") and hasattr(dpg, "get_item_rect_max"):
                return dpg.get_item_rect_min(item_tag), dpg.get_item_rect_max(item_tag)
        except Exception:
            pass

        try:
            state = dpg.get_item_state(item_tag)
            rect_min = state.get("rect_min")
            rect_max = state.get("rect_max")
            if rect_min is not None and rect_max is not None:
                return rect_min, rect_max
        except Exception:
            pass

        try:
            x0, y0 = dpg.get_item_pos(item_tag)
            w, h = dpg.get_item_rect_size(item_tag)
            return (x0, y0), (x0 + w, y0 + h)
        except Exception:
            pass

        try:
            child_tags = []
            for slot in (0, 1, 2, 3):
                try:
                    children = dpg.get_item_children(item_tag, slot) or []
                except Exception:
                    children = []
                for c in children:
                    if c not in child_tags:
                        child_tags.append(c)

            x_min = y_min = None
            x_max = y_max = None

            for c in child_tags:
                rect_min, rect_max = None, None
                try:
                    if hasattr(dpg, "get_item_rect_min") and hasattr(dpg, "get_item_rect_max"):
                        rect_min = dpg.get_item_rect_min(c)
                        rect_max = dpg.get_item_rect_max(c)
                except Exception:
                    rect_min = rect_max = None

                if rect_min is None or rect_max is None:
                    try:
                        st = dpg.get_item_state(c)
                        rect_min = st.get("rect_min")
                        rect_max = st.get("rect_max")
                    except Exception:
                        rect_min = rect_max = None

                if rect_min is None or rect_max is None:
                    try:
                        cx0, cy0 = dpg.get_item_pos(c)
                        cw, ch = dpg.get_item_rect_size(c)
                        rect_min = (cx0, cy0)
                        rect_max = (cx0 + cw, cy0 + ch)
                    except Exception:
                        rect_min = rect_max = None

                if rect_min is None or rect_max is None:
                    continue

                cx0, cy0 = rect_min
                cx1, cy1 = rect_max
                cx0, cx1 = (cx0, cx1) if cx0 <= cx1 else (cx1, cx0)
                cy0, cy1 = (cy0, cy1) if cy0 <= cy1 else (cy1, cy0)

                x_min = cx0 if x_min is None else min(x_min, cx0)
                y_min = cy0 if y_min is None else min(y_min, cy0)
                x_max = cx1 if x_max is None else max(x_max, cx1)
                y_max = cy1 if y_max is None else max(y_max, cy1)

            if x_min is not None and y_min is not None and x_max is not None and y_max is not None:
                return (x_min, y_min), (x_max, y_max)
        except Exception:
            pass

        return None, None

    def _output_framebuffer_callback(self, sender, app_data=None, *args, **kwargs):
        """DearPyGui framebuffer callback: crop region and save as PNG."""
        cap = self._png_capture
        self._png_capture = None
        if not cap:
            return

        if app_data is None and not args and not kwargs:
            return

        fb_obj = None
        buffer_obj = None
        w = h = None
        buffer_kind = None

        def _try_pick_framebuffer(value):
            nonlocal fb_obj, buffer_obj, w, h, buffer_kind
            if value is None:
                return False

            if hasattr(value, "get_width") and hasattr(value, "get_height"):
                try:
                    w = int(value.get_width())
                    h = int(value.get_height())
                    buffer_obj = value
                    fb_obj = value
                    buffer_kind = "buffer"
                    return True
                except Exception:
                    pass

            if isinstance(value, (tuple, list)) and len(value) >= 3:
                a, b, c = value[0], value[1], value[2]
                if isinstance(a, int) and isinstance(b, int):
                    w, h = int(a), int(b)
                    buffer_obj = c
                    fb_obj = value
                    buffer_kind = "buffer"
                    return True
                if isinstance(b, int) and isinstance(c, int):
                    buffer_obj = a
                    w, h = int(b), int(c)
                    fb_obj = value
                    buffer_kind = "buffer"
                    return True

            if isinstance(value, dict):
                w_key = "width" if "width" in value else ("w" if "w" in value else None)
                h_key = "height" if "height" in value else ("h" if "h" in value else None)
                b_key = None
                for k in ("buffer", "data", "framebuffer", "frame_buffer"):
                    if k in value:
                        b_key = k
                        break
                if w_key and h_key and b_key:
                    try:
                        w = int(value[w_key])
                        h = int(value[h_key])
                        buffer_obj = value[b_key]
                        fb_obj = value
                        buffer_kind = "buffer"
                        return True
                    except Exception:
                        pass

            if isinstance(value, (tuple, list)):
                try:
                    cap_w = int(cap.get("vp_w") or 0)
                    cap_h = int(cap.get("vp_h") or 0)
                    if cap_w > 0 and cap_h > 0 and len(value) == cap_w * cap_h * 4:
                        w, h = cap_w, cap_h
                        buffer_obj = value
                        fb_obj = value
                        buffer_kind = "sequence"
                        return True
                except Exception:
                    pass

            return False

        for v in (app_data, *args):
            if _try_pick_framebuffer(v):
                break

        if fb_obj is None:
            for key in ("app_data", "user_data"):
                if _try_pick_framebuffer(kwargs.get(key)):
                    break

        if buffer_obj is None or w is None or h is None:
            return

        try:
            import numpy as np
        except Exception:
            return

        try:
            x_left = int(cap["x_left"])
            y_left = int(cap["y_left"])
            x_span = int(cap["x_span"])
            y_span = int(cap["y_span"])
            if x_span <= 0 or y_span <= 0:
                return

            if buffer_kind == "sequence":
                image = np.asarray(buffer_obj, dtype=np.float32)
            else:
                try:
                    memoryview(buffer_obj)
                except Exception:
                    return
                image = np.frombuffer(buffer_obj, dtype=np.float32, count=w * h * 4)

            image = np.reshape(image, (h, w, 4))
            image = image[y_left:y_left + y_span, x_left:x_left + x_span, :]
            image = image.flatten()
            image[:] *= 255

            target_path = cap["filepath"]
            # dpg.save_image uses a C API that cannot handle non-ASCII (CJK) paths.
            # Work-around: save to a temporary ASCII path then rename to the real target.
            try:
                target_path.encode("ascii")
                needs_rename = False
                tmp_path = target_path
            except (UnicodeEncodeError, UnicodeDecodeError):
                needs_rename = True
                import tempfile
                _fd, tmp_path = tempfile.mkstemp(suffix=".png")
                os.close(_fd)

            dpg.save_image(tmp_path, x_span, y_span, image)

            if needs_rename:
                try:
                    if os.path.isfile(tmp_path):
                        import shutil
                        shutil.move(tmp_path, target_path)
                except Exception as _mv_err:
                    dprint(f"[Save PNG] rename failed: {_mv_err!r}")

            saved = False
            try:
                saved = os.path.isfile(target_path)
            except Exception:
                saved = False

            if saved:
                self._play_success_beep()
            else:
                dprint(f"[Save PNG] file not found after save: {target_path!r}")
        except Exception as _e:
            dprint(f"[Save PNG] exception: {_e!r}")

    def process_pending_png_capture(self):
        """Called once per frame after rendering to execute queued screenshot capture."""
        if self._png_capture is not None:
            return
        if not self._png_capture_pending:
            return

        self._png_capture = self._png_capture_pending
        self._png_capture_pending = None
        try:
            dpg.output_frame_buffer(callback=self._framebuffer_callback)
        except Exception:
            self._png_capture = None

    def save_item_to_png(self, item_tag: str, filepath: str):
        """Queue a screenshot of a DPG item and save as PNG."""
        rect_min, rect_max = self._get_item_rect_in_viewport(item_tag)
        if rect_min is None or rect_max is None:
            return

        try:
            x0, y0 = rect_min
            x1, y1 = rect_max
            x_left = int(min(x0, x1))
            x_right = int(max(x0, x1))
            y_left = int(min(y0, y1))
            y_right = int(max(y0, y1))

            vp_w = int(dpg.get_viewport_client_width())
            vp_h = int(dpg.get_viewport_client_height())

            x_left = max(0, min(x_left, vp_w))
            x_right = max(0, min(x_right, vp_w))
            y_left = max(0, min(y_left, vp_h))
            y_right = max(0, min(y_right, vp_h))

            x_span = abs(x_right - x_left)
            y_span = abs(y_right - y_left)
            if x_span <= 0 or y_span <= 0:
                return

            self._png_capture_pending = {
                "filepath": filepath,
                "x_left": x_left,
                "y_left": y_left,
                "x_span": x_span,
                "y_span": y_span,
                "vp_w": vp_w,
                "vp_h": vp_h,
            }
        except Exception:
            pass

class IPC:
    """可擴展的 IPC 管理器（支援多通道），可存取 ui_handle"""
    def __init__(self, ui_handle=None):
        self.ui_handle = ui_handle
        self.channels = {}  # {channel_name: queue}

    def register_channel(self, channel_name, queue):
        self.channels[channel_name] = queue

    def send(self, channel_name, data):
        if channel_name in self.channels:
            self.channels[channel_name].put(data)

    def recv(self, channel_name, timeout=None):
        if channel_name in self.channels:
            try:
                return self.channels[channel_name].get(timeout=timeout)
            except Exception:
                return None
        return None

class FPSTracker:
    """專門負責FPS計算和顯示的組件"""
    
    def __init__(self, ui_handle):
        self.ui_handle = ui_handle  # 參考UIHandle以存取共享狀態
        self.counter = 0
        self.start_time = time.perf_counter()
        self.current = 0.0
    
    def calculate_and_update(self):
        """計算並更新FPS顯示"""
        self.counter += 1
        current_time = time.perf_counter()
        
        # Update FPS every 0.2 seconds (5 times per second for more responsive updates)
        if current_time - self.start_time >= 0.2:
            self.current = self.counter / (current_time - self.start_time)
            fps_text = f"FPS: {self.current:.1f}"
            dpg.set_value("fps_display", fps_text)
            
            # Reset counter and timer
            self.counter = 0
            self.start_time = current_time
    
    def get_current_fps(self):
        """獲取當前FPS值"""
        return self.current

class AutoFit:
    """專門負責軸自動適應的組件"""
    
    def __init__(self, ui_handle):
        self.ui_handle = ui_handle  # 參考UIHandle以存取共享狀態
        self.x_axis_autofit_enabled = True  # X軸auto-fit控制
        self.y_axis_autofit_enabled = True  # Y軸auto-fit控制
    
    def update_axis_fitting(self):
        """更新軸自動適應"""
        # 如果啟用 Y 軸 auto-fit，每 frame 更新 Y 軸範圍（基於當前數據的最大最小值）
        if self.y_axis_autofit_enabled:
            self.update_custom_y_axis_limits()
        
        # 如果啟用 X 軸 auto-fit，每 frame 更新 X 軸範圍
        # 或：若 UI 設定了「pulse」旗標，則視為暫時啟用，並在 N 幀後自動清除。
        pulse_active = False
        pulse_left = 0
        try:
            pulse_active = bool(getattr(self.ui_handle, "_x_autofit_pulse_once", False))
            pulse_left = int(getattr(self.ui_handle, "_x_autofit_pulse_frames_left", 0) or 0)
        except Exception:
            pulse_active = False
            pulse_left = 0

        if self.x_axis_autofit_enabled or (pulse_active and pulse_left > 0):
            self.update_x_axis_autofit()

        # Pulse 倒數：只在 pulse_active 時扣幀；扣到 0 才清旗標。
        if pulse_active and pulse_left > 0:
            pulse_left -= 1
            try:
                self.ui_handle._x_autofit_pulse_frames_left = pulse_left
            except Exception:
                pass
            if pulse_left <= 0:
                # Pulse ends: also run the same logic as X-axis autofit disabled.
                try:
                    for i in range(1, self.ui_handle.current_plot_count + 1):
                        x_axis_tag = f'x_axis{i}'
                        if dpg.does_item_exist(x_axis_tag):
                            dpg.set_axis_limits_auto(x_axis_tag)
                except Exception:
                    pass
                try:
                    self.ui_handle._x_autofit_pulse_once = False
                except Exception:
                    pass
    
    def update_custom_y_axis_limits(self):
        """使用 update_series_with_buffer 預算的 Y 範圍，直接套用（O(1) per plot）"""
        frame_y = getattr(self.ui_handle, '_frame_y_limits', None)
        if not frame_y:
            return
        for plot_id in range(1, self.ui_handle.current_plot_count + 1):
            limits = frame_y.get(plot_id)
            if limits is None:
                continue
            y_min_all, y_max_all = limits
            y_axis_tag = f'y_axis{plot_id}'
            if dpg.does_item_exist(y_axis_tag):
                y_range = y_max_all - y_min_all
                if y_range == 0:
                    # 平線：以數值絕對值的 10% 為 margin，若值本身也為 0 則用 1
                    margin = max(abs(y_min_all) * 0.10, 1.0)
                else:
                    margin = y_range * 0.10
                dpg.set_axis_limits(y_axis_tag, y_min_all - margin, y_max_all + margin)
    
    def update_x_axis_autofit(self):
        """更新 X 軸的 auto-fit（X 為遞增序列，直接取首尾 O(1)）"""
        for plot_id in range(1, self.ui_handle.current_plot_count + 1):
            assigned_data_sources = self.ui_handle.get_assigned_data_sources_for_plot(plot_id)
            if assigned_data_sources:
                x_axis_tag = f'x_axis{plot_id}'
                if dpg.does_item_exist(x_axis_tag):
                    x_min = None
                    x_max = None
                    for data_source_id in assigned_data_sources:
                        series_tag = f"signal_series{plot_id}_{data_source_id}"
                        if dpg.does_item_exist(series_tag):
                            if dpg.get_item_configuration(series_tag).get("show", True):
                                data_source = f'ds{data_source_id}'
                                if data_source in self.ui_handle.signal_data and len(self.ui_handle.signal_data[data_source]['x']) > 0:
                                    xs = self.ui_handle.signal_data[data_source]['x']
                                    # 只看最新 max_data_points 筆，跳過 buffer 區
                                    _offset = max(len(xs) - self.ui_handle.max_data_points, 0)
                                    first = xs[_offset]
                                    last = xs[-1]
                                    if x_min is None or first < x_min:
                                        x_min = first
                                    if x_max is None or last > x_max:
                                        x_max = last
                    if x_min is not None and x_max is not None:
                        x_range = x_max - x_min

                        # ✅ 依 X 軸模式調整最小 margin
                        if getattr(self.ui_handle, "x_axis_unit_mode", "By sequence") == "By time":
                            min_margin = max(float(getattr(self.ui_handle, "sample_period_s", 0.001) or 0.001), 1e-6)
                        else:
                            min_margin = 1

                        margin = max(x_range * 0.01, min_margin)
                        dpg.set_axis_limits(x_axis_tag, x_min - margin, x_max + margin)
                    else:
                        dpg.set_axis_limits_auto(x_axis_tag)
    
    def set_x_axis_autofit(self, enabled):
        """設定X軸auto-fit狀態 (data source version)"""
        self.x_axis_autofit_enabled = enabled
        if not enabled:
            for i in range(1, self.ui_handle.current_plot_count + 1):
                x_axis_tag = f'x_axis{i}'
                if dpg.does_item_exist(x_axis_tag):
                    dpg.set_axis_limits_auto(x_axis_tag)
    
    def set_y_axis_autofit(self, enabled):
        """設定Y軸auto-fit狀態 (data source version)"""
        self.y_axis_autofit_enabled = enabled
        if not enabled:
            for i in range(1, self.ui_handle.current_plot_count + 1):
                y_axis_tag = f'y_axis{i}'
                if dpg.does_item_exist(y_axis_tag):
                    dpg.set_axis_limits_auto(y_axis_tag)
    
    def get_x_axis_autofit_status(self):
        """獲取X軸auto-fit狀態"""
        return self.x_axis_autofit_enabled
    
    def get_y_axis_autofit_status(self):
        """獲取Y軸auto-fit狀態"""
        return self.y_axis_autofit_enabled

class AdaptiveDisplayOptimization:
    """專門負責自適應顯示優化的組件（降取樣、FPS控制）"""
    
    def __init__(self, ui_handle):
        self.ui_handle = ui_handle  # 參考UIHandle以存取共享狀態
        
        # 自適應顯示降取樣相關屬性
        self.adaptive_downsampling_enabled = False  # 自適應降取樣控制
        self.target_fps = 60.0  # 目標FPS
        self.current_downsample_rate = 1  # 當前降取樣率 (1=無降取樣, 2=每2點取1點)
        self.fps_check_interval = 10  # 每10幀檢查一次FPS並調整降取樣
        self.fps_check_counter = 0  # FPS檢查計數器
    
    def update_adaptive_downsampling(self):
        """自適應降取樣控制，根據當前FPS調整降取樣率"""
        if not self.adaptive_downsampling_enabled:
            return
            
        self.fps_check_counter += 1
        
        # 每隔指定幀數檢查一次FPS
        if self.fps_check_counter >= self.fps_check_interval:
            self.fps_check_counter = 0
            current_fps = self.ui_handle.fps_tracker.get_current_fps()
            
            # 根據當前FPS調整降取樣率
            if current_fps < self.target_fps - 10:  # FPS太低，增加降取樣
                if self.current_downsample_rate < 64:  # 最大降取樣率限制為64
                    self.current_downsample_rate += 1
                    # 更新UI顯示
                    self.update_downsampling_display()
            elif current_fps > 57:  # FPS足夠高，減少降取樣
                if self.current_downsample_rate > 1:
                    self.current_downsample_rate -= 1
                    # 更新UI顯示
                    self.update_downsampling_display()
    
    def update_downsampling_display(self):
        """更新降取樣倍率顯示"""
        if self.adaptive_downsampling_enabled and dpg.does_item_exist("downsampling_rate_text"):
            dpg.set_value("downsampling_rate_text", f"Rate: {self.current_downsample_rate}x")
    
    def apply_downsampling(self, x_list, y_list):
        """對已轉換為 list 的資料套用降取樣"""
        if self.adaptive_downsampling_enabled and self.current_downsample_rate > 1:
            return x_list[::self.current_downsample_rate], y_list[::self.current_downsample_rate]
        else:
            return x_list, y_list
    
    def set_adaptive_downsampling(self, enabled):
        """設定自適應降取樣狀態"""
        self.adaptive_downsampling_enabled = enabled
        if enabled:
            # 重置降取樣率
            self.current_downsample_rate = 1
            self.fps_check_counter = 0
            # 顯示降取樣倍率文本
            if dpg.does_item_exist("downsampling_rate_text"):
                dpg.show_item("downsampling_rate_text")
            self.update_downsampling_display()
        else:
            # 恢復正常顯示
            self.current_downsample_rate = 1
            # 隱藏降取樣倍率文本
            if dpg.does_item_exist("downsampling_rate_text"):
                dpg.hide_item("downsampling_rate_text")
    
    def get_adaptive_downsampling_status(self):
        """獲取自適應降取樣狀態"""
        return self.adaptive_downsampling_enabled
    
    def get_current_downsample_rate(self):
        """獲取當前降取樣率"""
        return self.current_downsample_rate

class LogManager:
    """專門負責Log管理的組件"""
    def __init__(self, ui_handle):
        self.ui_handle = ui_handle  # 參考UIHandle以存取共享狀態

        # 保存「原始訊息」列表，縮放時可完整重排
        self._raw_entries = []
        self._last_wrap_width = None
        self._existing_import_done = False
        
    def update_log_container_height(self):
        """根據 left_panel 剩餘高度自適應 log_text_container 高度，最小 120px"""
        try:
            if dpg.does_item_exist("left_panel") and dpg.does_item_exist("log_text_container"):
                left_panel_height = dpg.get_item_rect_size("left_panel")[1]
                spacing = 10
                left_panel_height = left_panel_height - spacing
                log_container_y = dpg.get_item_pos("log_text_container")[1]
                available_height = left_panel_height - log_container_y
                target_height = max(500, available_height)
                dpg.set_item_height("log_text_container", target_height)
        except Exception:
            pass
            
    def _normalize_message(self, message: str) -> str:
        if message is None:
            return ""
        # 單則訊息內部不保留換行；訊息之間用 entries 分隔
        return str(message).replace("\n", " ").strip()

    def _parse_existing_log_to_entries(self, text: str):
        """把目前 widget 內的文字回推為 entries（不解析時間戳；每行一則）。"""
        if not text:
            return []

        lines = [ln.rstrip("\r\n") for ln in str(text).splitlines()]
        # 每行一則（忽略空行）
        return [ln for ln in lines if ln.strip()]

    def _ensure_entries_initialized(self):
        """在第一次需要全量重排/渲染時，從現有 widget 內容做一次回填。"""
        if self._existing_import_done:
            return
        self._existing_import_done = True

        if not dpg.does_item_exist("log_text"):
            return

        try:
            current_log = dpg.get_value("log_text")
        except Exception:
            current_log = ""

        if current_log and not self._raw_entries:
            self._raw_entries = self._parse_existing_log_to_entries(current_log)

    def _update_log_height(self, rendered_text: str):
        text_size = dpg.get_text_size(rendered_text)
        if text_size:
            FRAME_PADDING = 3
            calculated_height = text_size[1] + (2 * FRAME_PADDING)
            dpg.set_item_height("log_text", calculated_height)

    def _apply_rendered_text(self, rendered_text: str):
        dpg.set_value("log_text", rendered_text)

        # 根據 auto scroll checkbox 狀態設定 tracked
        auto_scroll_enabled = True
        if dpg.does_item_exist("log_auto_scroll_checkbox"):
            auto_scroll_enabled = dpg.get_value("log_auto_scroll_checkbox")
        dpg.configure_item("log_text", tracked=auto_scroll_enabled)

        self._update_log_height(rendered_text)

    def reflow_log(self, force: bool = False):
        """依目前 log_text 寬度重排整段 log（字元級折行，不保留英文單字完整性）。"""
        if not dpg.does_item_exist("log_text"):
            return

        self._ensure_entries_initialized()
        wrap_width = self._get_log_wrap_width_chars(fallback=80)
        if (not force) and (self._last_wrap_width == wrap_width):
            return
        self._last_wrap_width = wrap_width

        wrapper = textwrap.TextWrapper(
            width=wrap_width,
            break_long_words=True,     # ✅ 允許拆英文單字（字元級）
            break_on_hyphens=False,    # ✅ 不把連字號視為特殊斷點（更接近純字元切）
            drop_whitespace=False,     # ✅ 不主動丟掉空白（可選）
            replace_whitespace=False,  # ✅ 不把各種空白全替換成單一空白（可選）
        )

        rendered_parts = []
        for entry in self._raw_entries:
            normalized = self._normalize_message(entry)
            if not normalized:
                continue
            rendered_parts.append(wrapper.fill(normalized))

        self._apply_rendered_text("\n".join(rendered_parts))

    def _get_log_wrap_width_chars(self, fallback=80):
        """依據目前 log_text 控件的可視寬度，動態換算 textwrap 的 width(字元數)。

        DearPyGui 的 item 寬度以像素為主，因此這裡用字型量測把像素寬換算成可容納的字元數。
        """
        if not dpg.does_item_exist("log_text"):
            return fallback

        try:
            rect_w, _ = dpg.get_item_rect_size("log_text")
        except Exception:
            rect_w = 0

        # rect_w 在第一次渲染前可能是 0，保留 fallback
        if not rect_w or rect_w <= 0:
            return fallback

        # 預留一些 padding，避免剛好貼邊造成視覺上溢出
        available_px = max(0, rect_w - 20)
        if available_px <= 0:
            return fallback

        # 估算「每個字」的平均寬度：同時用 ASCII 與 CJK 樣本，取較大的寬度以避免寬字溢出
        try:
            ascii_sample = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            ascii_w = dpg.get_text_size(ascii_sample)[0] / len(ascii_sample)

            cjk_sample = "測試寬度"
            cjk_w = dpg.get_text_size(cjk_sample)[0] / len(cjk_sample)

            avg_char_w = max(1.0, ascii_w, cjk_w)
        except Exception:
            return fallback

        width_chars = int(available_px / avg_char_w)

        # 避免太小導致幾乎每字都換行
        return max(20, width_chars)
    
    def update_log(self, message):
        """更新log顯示框的內容"""
        if not dpg.does_item_exist("log_text"):
            return

        # 確保已把既有內容回填成 entries（避免第一次 resize 時 entries 為空）
        self._ensure_entries_initialized()

        normalized = self._normalize_message(message)
        if not normalized:
            return

        self._raw_entries.append(normalized)
        # 新訊息進來時也重排一次（以目前寬度）
        self.reflow_log(force=True)
            
    def clear_log(self):
        """清空log顯示框"""
        if dpg.does_item_exist("log_text"):
            dpg.set_value("log_text", "")
            # 重置高度為一行
            one_line = dpg.get_text_size(" ")
            if one_line:
                FRAME_PADDING = 3
                dpg.set_item_height("log_text", one_line[1] + 2 * FRAME_PADDING)
            else:
                dpg.set_item_height("log_text", 0)

        self._raw_entries = []
        self._last_wrap_width = None
        self._existing_import_done = False
    
    def generate_demo_log(self):
        """Generate sample log messages"""
        import time
        import random
        
        # Sample log message list (English only)
        sample_messages = [
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"
        ]
        
        # 獲取當前時間戳
        current_time = time.strftime("%H:%M:%S", time.localtime())
        
        # 隨機選擇一個訊息
        message = random.choice(sample_messages)
        
        # 格式化log訊息
        log_message = f"[{current_time}] {message}"
        # Demo function kept for reference; no longer writes to log.

class CursorHandler:
    """統一管理 X cursor 和 Y cursor 的組件"""
    
    def __init__(self, ui_handle):
        self.ui_handle = ui_handle  # 參考UIHandle以存取共享狀態
        
        # Y cursor 管理
        self.y_cursor_map = {}  # {dragline_tag: (plot_id, annotation_tag, y_label)}
        self.y_cursor_dragging = False
        self._y_snap_pending = set()       # ✅旗標：需要吸附一次的 y_cursor tag
        self._y_snap_in_progress = False   # ✅避免 set_value 觸發 callback 重入
        self.y_cursor_count = 0  # 新增：Y cursor 計數器
        
        # X cursor 管理
        self.x_cursor_map = {}  # {master_tag: ([dragline_tags], annotation_tag, top_plot_id, x_label)}
        self.x_cursor_dragging = False
        self.x_cursor_count = 0  # 新增：X cursor 計數器
        
        # Cursor 主題
        self.y_cursor_color = [255, 0, 0, 255]  # 紅色
        self.x_cursor_color = [0, 255, 0, 255]  # 綠色

        # X cursor 每幀同步快取 {master_tag: (x_val, y_max_top)}
        self._last_x_cursor_state = {}

    def _snap_nearest_x(self, x_val, assigned_data_sources):
        """用 bisect O(log n) 在排序的 x 資料中找最接近 x_val 的點，零記憶體分配。"""
        nearest_x = None
        best_dx = float('inf')
        for data_source_id in assigned_data_sources:
            ds = f'ds{data_source_id}'
            if ds not in self.ui_handle.signal_data:
                continue
            xs = self.ui_handle.signal_data[ds].get('x', [])
            n = len(xs)
            if n == 0:
                continue
            idx = bisect_left(xs, x_val, 0, n)
            for j in (idx - 1, idx):
                if 0 <= j < n:
                    dx = abs(xs[j] - x_val)
                    if dx < best_dx:
                        best_dx = dx
                        nearest_x = xs[j]
        return nearest_x

    def _snap_nearest_y(self, y_val, plot_id, x_min_vis=None, x_max_vis=None):
        """用 bisect 定位可見 X 範圍，再用 NumPy 向量化找最接近 y_val 的點。"""
        assigned = self.ui_handle.get_assigned_data_sources_for_plot(plot_id)
        nearest_y = None
        best_dy = float('inf')
        for data_source_id in assigned:
            ds = f'ds{data_source_id}'
            if ds not in self.ui_handle.signal_data:
                continue
            xs = self.ui_handle.signal_data[ds].get('x', [])
            ys = self.ui_handle.signal_data[ds].get('y', [])
            if not xs or not ys:
                continue
            n = min(len(xs), len(ys))
            i0 = 0
            i1 = n
            if x_min_vis is not None and x_max_vis is not None:
                i0 = bisect_left(xs, x_min_vis, 0, n)
                i1 = bisect_right(xs, x_max_vis, 0, n)
            if i0 >= i1:
                continue
            # 使用 np.array 複製切片（非 frombuffer view），避免鎖住 array.array buffer
            # 導致後續 .append() 觸發 BufferError: cannot resize an array that is exporting buffers
            ys_slice = np.array(ys[i0:i1], dtype=np.float64)
            diffs = np.abs(ys_slice - y_val)
            idx = int(np.argmin(diffs))
            dy = float(diffs[idx])
            if dy < best_dy:
                best_dy = dy
                nearest_y = float(ys_slice[idx])
        return nearest_y

    def _format_x_cursor_value(self, x_val):
        """依 X 軸單位模式格式化 X cursor 顯示值。

        - By sequence: 顯示整數（不顯示小數點）
        - By time: 維持小數顯示
        """
        mode = getattr(self.ui_handle, "x_axis_unit_mode", "By sequence")
        if mode == "By sequence":
            try:
                return str(int(round(float(x_val))))
            except Exception:
                return str(x_val)

        try:
            return f"{float(x_val):.6f}"
        except Exception:
            return str(x_val)
    
    # ==================== Y Cursor 方法 ====================
    
    def create_y_cursor(self, plot_id):
        """在指定 plot 上創建 Y cursor（創建時自動吸附到最近數據點）"""
        y_axis_tag = f"y_axis{plot_id}"
        plot_tag = f"plot{plot_id}"
        self.y_cursor_count += 1
        y_label = f"Y{self.y_cursor_count}"
    
        # 檢查 parent 是否存在
        if not dpg.does_item_exist(y_axis_tag) or not dpg.does_item_exist(plot_tag):
            return None
    
        # 生成唯一 tag
        base_time = int(time.time() * 1000)
        y_cursor_tag = f"y_cursor_{plot_id}_{base_time}"
        annotation_tag = f"y_cursor_anno_{plot_id}_{base_time}"
        tag_offset = 0
        while dpg.does_item_exist(y_cursor_tag) or dpg.does_item_exist(annotation_tag):
            tag_offset += 1
            y_cursor_tag = f"y_cursor_{plot_id}_{base_time + tag_offset}"
            annotation_tag = f"y_cursor_anno_{plot_id}_{base_time + tag_offset}"
    
        # 取得 Y 軸範圍，預設在中間
        try:
            y_min, y_max = dpg.get_axis_limits(y_axis_tag)
            y_val = (y_min + y_max) / 2 if y_max > y_min else 0.0
        except Exception:
            y_val = 0.0
    
        # 取得所有有效 y 數據點，吸附到最近（NumPy 向量化）
        snapped = self._snap_nearest_y(y_val, plot_id)
        if snapped is not None:
            y_val = snapped
    
        # 創建 drag line
        try:
            dpg.add_drag_line(
                parent=plot_tag,
                label="Y cursor",
                tag=y_cursor_tag,
                color=self.y_cursor_color,
                thickness=0.5,
                vertical=False,
                show=True,
                show_label=False,
                callback=lambda s, a, u: self.on_y_cursor_drag(s, a, u, plot_id, y_cursor_tag),
                default_value=y_val
            )
        except Exception as e:
            return None
    
        # 創建 annotation
        try:
            x_min, x_max = dpg.get_axis_limits(f"x_axis{plot_id}")
        except Exception:
            x_max = 0
    
        try:
            y_min2, y_max2 = dpg.get_axis_limits(y_axis_tag)
            anno_y = max(min(y_val, y_max2), y_min2) if y_min2 is not None else y_val
        except Exception:
            anno_y = y_val
    
        try:
            dpg.add_plot_annotation(
                parent=plot_tag,
                default_value=(x_max, anno_y),
                offset=(0, 0),
                label=f"{y_label}={y_val:.6f}",  # 這裡加上流水號
                tag=annotation_tag,
                color=self.y_cursor_color,
                clamped=True
            )
        except Exception as e:
            dpg.delete_item(y_cursor_tag)
            return None
    
        # 綁定 dragline 與 annotation
        dpg.set_item_user_data(y_cursor_tag, annotation_tag)
    
        # 註冊到 map
        self.y_cursor_map[y_cursor_tag] = (plot_id, annotation_tag, y_label)
        
        self._y_snap_pending.add(y_cursor_tag)
    
        return y_cursor_tag
    
    def on_y_cursor_drag(self, sender, app_data, user_data, plot_id, y_cursor_tag):
        # 避免我們在 update 裡 set_value 時又回打 callback
        if self._y_snap_in_progress:
            return

        self.y_cursor_dragging = True

        annotation_tag = dpg.get_item_user_data(y_cursor_tag)
        y_val = dpg.get_value(y_cursor_tag)

        try:
            _, x_max = dpg.get_axis_limits(f"x_axis{plot_id}")
        except Exception:
            x_max = 0

        # 拖曳中更新 annotation
        dpg.configure_item(annotation_tag, label=f"Y={y_val:.6f}", default_value=(x_max, y_val))

        # ✅照你要的：callback 只負責舉旗標
        self._y_snap_pending.add(y_cursor_tag)
    
    def update_y_cursors_every_frame(self):
        """每幀更新所有 Y cursor 的 annotation 位置，並在旗標舉起且放開滑鼠時吸附一次"""
        for dragline_tag, (plot_id, annotation_tag, y_label) in list(self.y_cursor_map.items()):
            if not dpg.does_item_exist(dragline_tag) or not dpg.does_item_exist(annotation_tag):
                self.y_cursor_map.pop(dragline_tag, None)
                self._y_snap_pending.discard(dragline_tag)
                continue

            # 讀取目前 y cursor 值
            try:
                y_val = dpg.get_value(dragline_tag)
            except Exception:
                continue

            # 更新 annotation 位置（先跟著目前 y）
            try:
                _, x_max = dpg.get_axis_limits(f"x_axis{plot_id}")
            except Exception:
                x_max = 0
            dpg.configure_item(annotation_tag, label=f"{y_label}={y_val:.6f}", default_value=(x_max, y_val))

            # 沒舉旗就不吸附
            if dragline_tag not in self._y_snap_pending:
                continue

            # 滑鼠還按著就先不吸（等放開才吸一次）
            if dpg.is_mouse_button_down(dpg.mvMouseButton_Left):
                continue

            # ✅做一次就放下旗標
            self._y_snap_pending.discard(dragline_tag)
            self.y_cursor_dragging = False

            # 取得 plot 當下可視的 X 範圍
            try:
                x_min_vis, x_max_vis = dpg.get_axis_limits(f"x_axis{plot_id}")
            except Exception:
                x_min_vis, x_max_vis = None, None

            assigned = self.ui_handle.get_assigned_data_sources_for_plot(plot_id)

            # bisect 定位可見範圍 + NumPy 向量化找最近 Y 值
            nearest_y = self._snap_nearest_y(y_val, plot_id, x_min_vis, x_max_vis)

            if nearest_y is None:
                continue

            # 更新 dragline(Y) 與 annotation
            self._y_snap_in_progress = True
            try:
                dpg.set_value(dragline_tag, nearest_y)
            finally:
                self._y_snap_in_progress = False

            dpg.configure_item(annotation_tag, label=f"{y_label}={nearest_y:.6f}", default_value=(x_max, nearest_y))
    
    def remove_y_cursor(self, y_cursor_tag):
        if y_cursor_tag in self.y_cursor_map:
            plot_id, annotation_tag, y_label = self.y_cursor_map[y_cursor_tag]  # unpack 3 個
            if dpg.does_item_exist(y_cursor_tag):
                dpg.delete_item(y_cursor_tag)
            if dpg.does_item_exist(annotation_tag):
                dpg.delete_item(annotation_tag)
            self.y_cursor_map.pop(y_cursor_tag, None)
    
    # ==================== X Cursor 方法 ====================

    def create_x_cursor(self):
        """在所有 plot 上創建 X cursor（創建時自動吸附到最近數據點）"""
        base_time = int(time.time() * 1000)
        master_tag = f"x_cursor_master_{base_time}"
        self.x_cursor_count += 1
        x_label = f"X{self.x_cursor_count}"
    
        dragline_tags = []
        for pid in range(1, self.ui_handle.current_plot_count + 1):
            plot_tag = f"plot{pid}"
            x_axis_tag = f"x_axis{pid}"
    
            # 計算預設 X 位置（軸中間）
            try:
                x_min, x_max = dpg.get_axis_limits(x_axis_tag)
                x_val = (x_min + x_max) / 2 if x_max > x_min else 0.0
            except Exception:
                x_val = 0.0
    
            # 取得所有有效 x 數據點，吸附到最近（bisect O(log n)）
            assigned_data_sources = self.ui_handle.get_assigned_data_sources_for_plot(pid)
            snapped = self._snap_nearest_x(x_val, assigned_data_sources)
            if snapped is not None:
                x_val = snapped
    
            drag_tag = f"x_cursor_{pid}_{base_time}"
    
            try:
                dpg.add_drag_line(
                    parent=plot_tag,
                    label="X cursor",
                    tag=drag_tag,
                    color=self.x_cursor_color,
                    thickness=0.5,
                    vertical=True,
                    show=True,
                    show_label=False,
                    callback=self.on_x_cursor_drag,
                    user_data=master_tag,  # 傳遞 master_tag
                    default_value=x_val
                )
                dpg.set_item_user_data(drag_tag, master_tag)
                dragline_tags.append(drag_tag)
            except Exception as e:
                pass
    
        if not dragline_tags:
            return None
    
        # 在頂部 plot 創建 annotation
        top_plot = 1
        annotation_tag = f"x_cursor_anno_{base_time}"
    
        try:
            y_axis_tag_top = f"y_axis{top_plot}"
            try:
                y_min_top, y_max_top = dpg.get_axis_limits(y_axis_tag_top)
            except Exception:
                y_max_top = 0
    
            x_display = dpg.get_value(dragline_tags[0]) if dragline_tags else 0.0

            x_display_str = self._format_x_cursor_value(x_display)
    
            dpg.add_plot_annotation(
                parent=f"plot{top_plot}",
                default_value=(x_display, y_max_top),
                offset=(0, 0),
                label=f"{x_label}={x_display_str}",  # 這裡加上流水號
                tag=annotation_tag,
                color=self.x_cursor_color,
                clamped=True
            )
        except Exception as e:
            for tag in dragline_tags:
                if dpg.does_item_exist(tag):
                    dpg.delete_item(tag)
            return None
    
        # 註冊到 map
        self.x_cursor_map[master_tag] = (dragline_tags, annotation_tag, top_plot, x_label)
    
        return master_tag

    def on_x_cursor_drag(self, sender, app_data, user_data):
        self.x_cursor_dragging = True  # 拖曳時設為 True
        
        """X cursor 拖曳回調：拖曳時同步所有 dragline，放開時吸附到最近數據點"""
        drag_tag = sender
        master_tag = user_data  # 直接用 user_data
        if not master_tag or master_tag not in self.x_cursor_map:
            return
    
        dragline_tags, annotation_tag, top_plot, x_label = self.x_cursor_map[master_tag]

        # 拖曳時：同步所有 draglines
        try:
            x_val = dpg.get_value(drag_tag)
        except Exception:
            return
    
        for t in dragline_tags:
            if dpg.does_item_exist(t):
                dpg.set_value(t, x_val)
    
        # 檢查是否放開滑鼠
        if dpg.is_mouse_button_released(dpg.mvMouseButton_Left):
            # 吸附到最近的數據點（bisect O(log n)）
            assigned_data_sources = self.ui_handle.get_assigned_data_sources_for_plot(top_plot)
            nearest_x = self._snap_nearest_x(x_val, assigned_data_sources)
            if nearest_x is not None:
                for t in dragline_tags:
                    if dpg.does_item_exist(t):
                        dpg.set_value(t, nearest_x)
                x_val = nearest_x
    
        # 更新 annotation
        y_axis_tag_top = f"y_axis{top_plot}"
        try:
            y_min_top, y_max_top = dpg.get_axis_limits(y_axis_tag_top)
        except Exception:
            y_max_top = 0

        x_val_str = self._format_x_cursor_value(x_val)
        dpg.configure_item(annotation_tag, label=f"{x_label}={x_val_str}", default_value=(x_val, y_max_top))
    
    def update_x_cursors_every_frame(self):
        """每幀更新所有 X cursor 的 annotation 位置（帶快取跳過未變更的）"""
        for master_tag, (dragline_tags, annotation_tag, top_plot, x_label) in list(self.x_cursor_map.items()):
            # 清理無效的 draglines
            valid_draglines = [t for t in dragline_tags if dpg.does_item_exist(t)]
            if not valid_draglines:
                self.x_cursor_map.pop(master_tag, None)
                self._last_x_cursor_state.pop(master_tag, None)
                continue
            
            # 取得第一個 dragline 的值
            try:
                x_val = dpg.get_value(valid_draglines[0])
            except Exception:
                x_val = 0
            
            # 取得 top plot 的 y_max
            y_axis_tag_top = f"y_axis{top_plot}"
            try:
                y_min_top, y_max_top = dpg.get_axis_limits(y_axis_tag_top)
            except Exception:
                y_max_top = 0

            # 快取比對：x_val 和 y_max_top 都沒變就跳過同步與 annotation 更新
            state_key = (x_val, y_max_top)
            if self._last_x_cursor_state.get(master_tag) == state_key:
                continue
            self._last_x_cursor_state[master_tag] = state_key
            
            # 同步所有 draglines
            for t in valid_draglines:
                try:
                    dpg.set_value(t, x_val)
                except Exception:
                    pass
            
            # 更新 annotation
            try:
                x_val_str = self._format_x_cursor_value(x_val)
                dpg.configure_item(annotation_tag, label=f"{x_label}={x_val_str}", default_value=(x_val, y_max_top))
            except Exception:
                pass
                
        if self.x_cursor_dragging and not dpg.is_mouse_button_down(dpg.mvMouseButton_Left):
            self.x_cursor_dragging = False
            # 執行吸附（bisect O(log n) per DS）
            for master_tag, (dragline_tags, annotation_tag, top_plot, x_label) in list(self.x_cursor_map.items()):
                try:
                    x_val = dpg.get_value(dragline_tags[0])
                except Exception:
                    continue
                assigned_data_sources = self.ui_handle.get_assigned_data_sources_for_plot(top_plot)
                nearest_x = self._snap_nearest_x(x_val, assigned_data_sources)
                if nearest_x is not None:
                    for t in dragline_tags:
                        if dpg.does_item_exist(t):
                            dpg.set_value(t, nearest_x)
                    # 更新 annotation
                    y_axis_tag_top = f"y_axis{top_plot}"
                    try:
                        y_min_top, y_max_top = dpg.get_axis_limits(y_axis_tag_top)
                    except Exception:
                        y_max_top = 0
                    nearest_x_str = self._format_x_cursor_value(nearest_x)
                    dpg.configure_item(annotation_tag, label=f"{x_label}={nearest_x_str}", default_value=(nearest_x, y_max_top))
                    # 更新快取以反映吸附後的位置
                    self._last_x_cursor_state[master_tag] = (nearest_x, y_max_top)
    
    def remove_x_cursor(self, master_tag):
        if master_tag in self.x_cursor_map:
            dragline_tags, annotation_tag, top_plot, x_label = self.x_cursor_map[master_tag]  # unpack 4 個
            for tag in dragline_tags:
                if dpg.does_item_exist(tag):
                    dpg.delete_item(tag)
            if dpg.does_item_exist(annotation_tag):
                dpg.delete_item(annotation_tag)
            self.x_cursor_map.pop(master_tag, None)
    
    # ==================== 統一更新方法 ====================
    
    def update_all_cursors_every_frame(self):
        """每幀更新所有 cursor（Y 和 X）"""
        self.update_y_cursors_every_frame()
        self.update_x_cursors_every_frame()
    
    def clear_all_cursors(self):
        """清除所有 cursor"""
        # 清除所有 Y cursors
        for y_cursor_tag in list(self.y_cursor_map.keys()):
            self.remove_y_cursor(y_cursor_tag)
        
        # 清除所有 X cursors
        for master_tag in list(self.x_cursor_map.keys()):
            self.remove_x_cursor(master_tag)
        self.y_cursor_count = 0
        self.x_cursor_count = 0

class UIEvent:
    """統一管理所有UI事件處理的組件"""
    def __init__(self, ui_handle):
        self.ui_handle = ui_handle  # 參考UIHandle以存取共享狀態和組件

        # SaveManager is responsible for all saving logic/state.

    def on_clear_protocol_stats_clicked(self, sender, app_data, user_data):
        """清除通訊統計訊息按鈕的回調"""
        self.ui_handle.send_clear_protocol_stats_request()

    def on_save_as_png_clicked(self, sender, app_data, user_data):
        """Save screenshot of main_subplots as PNG."""
        # Rule: only allow saving while stopped
        if getattr(self.ui_handle, "CommSts", "Stopped") != "Stopped":
            return

        if not dpg.does_item_exist("main_subplots"):
            return

        # Make sure sizes/rects are up-to-date for this frame
        try:
            dpg.split_frame()
        except Exception:
            pass

        base_name = ""
        try:
            if dpg.does_item_exist("save_name_input"):
                base_name = str(dpg.get_value("save_name_input") or "").strip()
        except Exception:
            pass

        if not base_name:
            base_name = "FileName"

        # sanitize filename for Windows
        base_name = re.sub(r'[<>:"/\\|?*]+', "_", base_name).strip(" .")
        if not base_name:
            base_name = "FileName"

        # Prefer Python-side cache to avoid dpg widget CJK encoding issues
        save_dir = getattr(self.ui_handle, "_save_dir_cache", "")
        if not save_dir or not os.path.isdir(save_dir):
            try:
                if dpg.does_item_exist("Save_path"):
                    save_dir = str(dpg.get_value("Save_path") or "").strip()
            except Exception:
                save_dir = ""
        if not save_dir:
            save_dir = self.ui_handle.get_desktop_path()

        if not os.path.isdir(save_dir):
            dprint(f"[Save] save_dir does not exist: {save_dir!r}")
            return

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{base_name}_{timestamp}.png"
        filepath = os.path.join(save_dir, filename)

        try:
            self.ui_handle.save_manager.save_item_to_png("main_subplots", filepath)
        except Exception:
            pass

    def on_save_as_csv_clicked(self, sender, app_data, user_data):
        """Save cached 16 data sources as CSV (chunked in frame loop; no thread)."""
        # Rule: only allow exporting while stopped
        if getattr(self.ui_handle, "CommSts", "Stopped") != "Stopped":
            return

        # Prevent re-entry
        if getattr(getattr(self.ui_handle, "save_manager", None), "_csv_export_active", False):
            return

        base_name = ""
        try:
            if dpg.does_item_exist("save_name_input"):
                base_name = str(dpg.get_value("save_name_input") or "").strip()
        except Exception:
            pass

        if not base_name:
            base_name = "FileName"

        # sanitize filename for Windows
        base_name = re.sub(r'[<>:"/\\|?*]+', "_", base_name).strip(" .")
        if not base_name:
            base_name = "FileName"

        # Prefer Python-side cache to avoid dpg widget CJK encoding issues
        save_dir = getattr(self.ui_handle, "_save_dir_cache", "")
        if not save_dir or not os.path.isdir(save_dir):
            try:
                if dpg.does_item_exist("Save_path"):
                    save_dir = str(dpg.get_value("Save_path") or "").strip()
            except Exception:
                save_dir = ""
        if not save_dir:
            save_dir = self.ui_handle.get_desktop_path()

        if not os.path.isdir(save_dir):
            dprint(f"[Save] save_dir does not exist: {save_dir!r}")
            return

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"{base_name}_{timestamp}.csv"
        filepath = os.path.join(save_dir, filename)

        try:
            self.ui_handle.save_manager.start_csv_export(filepath)
        except Exception:
            pass

    def _show_native_dialog_unavailable_message(self):
        """Show a clear warning when native dialog backends are unavailable."""
        msg = (
            "Cannot open native file dialog.\n\n"
            "Please install pywin32:\n"
            "pip install pywin32"
        )
        try:
            ctypes.windll.user32.MessageBoxW(0, msg, "Dialog backend not available", 0x00000030)
        except Exception:
            dprint(msg)

    def _report_dialog_backend(self, dialog_kind: str, backend: str, event: str):
        """Print backend selection/result so users can verify which dialog implementation is active."""
        try:
            dprint(f"[Dialog] {dialog_kind} | backend={backend} | event={event}")
        except Exception:
            pass

    def _open_native_folder_dialog(self, title="Select folder"):
        """Open Windows native folder picker via pywin32 Shell.Application COM interface.

        Uses Shell.Application.BrowseForFolder which returns a proper Unicode string,
        avoiding ANSI/MBCS encoding issues with Chinese paths.
        """
        backend_available = False

        try:
            if pythoncom is None or win32com is None:
                raise RuntimeError("pywin32 is not available")
            pythoncom.CoInitialize()
            try:
                _shell_app = win32com.client.Dispatch("Shell.Application")
                backend_available = True
                # BIF_RETURNONLYFSDIRS=0x0001, BIF_NEWDIALOGSTYLE=0x0040 -> 0x0041
                folder_obj = _shell_app.BrowseForFolder(0, title, 0x0041, "")
                if folder_obj is None:
                    self._report_dialog_backend("folder", "pywin32", "cancelled")
                    return None
                selected_path = folder_obj.Self.Path
                if isinstance(selected_path, str) and selected_path.strip():
                    self._report_dialog_backend("folder", "pywin32", "selected")
                    return selected_path.strip()
                self._report_dialog_backend("folder", "pywin32", "cancelled")
                return None
            finally:
                pythoncom.CoUninitialize()
        except Exception as e:
            self._report_dialog_backend("folder", "pywin32", f"error:{e}")

        if not backend_available:
            self._report_dialog_backend("folder", "none", "unavailable")
            self._show_native_dialog_unavailable_message()
        return None

    def _open_native_csv_file_dialog(self, initial_dir=None, title="Select parameter CSV"):
        """Open Windows native file picker via pywin32 and return selected CSV path."""
        backend_available = False

        if win32ui is not None and win32con is not None:
            backend_available = True
            try:
                file_filter = "CSV Files (*.csv)|*.csv|All Files (*.*)|*.*||"
                dlg = win32ui.CreateFileDialog(
                    1,
                    "csv",
                    None,
                    win32con.OFN_EXPLORER | win32con.OFN_FILEMUSTEXIST | win32con.OFN_HIDEREADONLY,
                    file_filter,
                )
                try:
                    dlg.m_ofn.lpstrTitle = title
                except Exception:
                    pass
                if isinstance(initial_dir, str) and os.path.isdir(initial_dir):
                    try:
                        dlg.SetOFNInitialDir(initial_dir)
                    except Exception:
                        try:
                            dlg.m_ofn.lpstrInitialDir = initial_dir
                        except Exception:
                            pass

                if dlg.DoModal() == 1:
                    selected_path = dlg.GetPathName()
                    if isinstance(selected_path, str) and selected_path.strip():
                        self._report_dialog_backend("csv", "pywin32", "selected")
                        return selected_path.strip()
                self._report_dialog_backend("csv", "pywin32", "cancelled")
                return None
            except Exception as e:
                dprint(f"[Dialog] Failed to open pywin32 CSV dialog: {e}")

        if not backend_available:
            self._report_dialog_backend("csv", "none", "unavailable")
            self._show_native_dialog_unavailable_message()
        return None

    def on_save_path_selected(self, selected_files):
        """資料夾選擇 callback：優先取 selected_files[0]。"""
        selected_path = None

        # 1) 正常情境：使用者有點選某個資料夾/檔案項目
        if selected_files:
            try:
                selected_path = selected_files[0]
            except Exception:
                selected_path = None

        if not isinstance(selected_path, str) or not selected_path.strip():
            return

        selected_path = selected_path.strip()

        # 更新 UI 顯示
        if dpg.does_item_exist("Save_path"):
            dpg.set_value("Save_path", selected_path)

        # 記住路徑，供下一次原生對話框作為初始目錄
        try:
            self.ui_handle.native_dialog_last_dir = selected_path
        except Exception:
            pass

        # Python-side cache：確保中文路徑不被 dpg widget 截斷
        try:
            self.ui_handle._save_dir_cache = selected_path
        except Exception:
            pass

    def on_save_example_param(self, sender, app_data, user_data):
        """儲存範例參數表 CSV 檔。"""
        import csv
        desktop = self.ui_handle.get_desktop_path()
        filepath = os.path.join(desktop, "example_param_table.csv")
        example_data = [
            {"Addr": "0x0000", "Name": "U8 Example", "Type": "U8"},
            {"Addr": "0x0001", "Name": "S8 Example", "Type": "S8"},
            {"Addr": "0x0002", "Name": "U16 Example", "Type": "U16"},
            {"Addr": "0x0003", "Name": "S16 Example", "Type": "S16"},
            {"Addr": "0x0004", "Name": "U32 Example", "Type": "U32"},
            {"Addr": "0x0005", "Name": "S32 Example", "Type": "S32"},
            {"Addr": "0x0006", "Name": "Float32 Example", "Type": "Float32"},
        ]
        try:
            with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=["Addr", "Name", "Type"])
                writer.writeheader()
                writer.writerows(example_data)
            dprint(f"[Param] Example param table saved to: {filepath}")
        except Exception as e:
            dprint(f"[Param] Failed to save example param table: {e}")

    def on_read_param_clicked(self, sender, app_data, user_data):
        """讀取參數表按鈕 callback：開啟 Windows 原生 CSV 對話框。"""
        initial_dir = ""
        try:
            if dpg.does_item_exist("Save_path"):
                initial_dir = str(dpg.get_value("Save_path") or "").strip()
        except Exception:
            initial_dir = ""
        if not initial_dir:
            initial_dir = getattr(self.ui_handle, "native_dialog_last_dir", "")
        if not initial_dir or not os.path.isdir(initial_dir):
            initial_dir = self.ui_handle.get_desktop_path()

        selected_path = self._open_native_csv_file_dialog(initial_dir=initial_dir)
        if selected_path:
            self.on_load_param_file_selected([selected_path])

    def on_save_path_clicked(self, sender, app_data, user_data):
        """存檔路徑按鈕 callback：開啟 Windows 原生資料夾對話框。"""
        selected_path = self._open_native_folder_dialog(title="Select save folder")
        if selected_path:
            self.on_save_path_selected([selected_path])

    def on_load_param_file_selected(self, selected_files):
        """讀取參數表 callback：取得使用者選擇的檔案路徑。"""
        selected_path = None
        if selected_files:
            try:
                selected_path = selected_files[0]
            except Exception:
                selected_path = None
        if not isinstance(selected_path, str) or not selected_path.strip():
            return
        selected_path = selected_path.strip()
        if not selected_path.lower().endswith(".csv"):
            dprint(f"[Param] Not a CSV file: {selected_path}")
            return

        import csv
        try:
            with open(selected_path, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
        except Exception as e:
            dprint(f"[Param] Failed to read param file: {e}")
            return

        if not rows:
            dprint("[Param] Param file is empty.")
            return

        # 驗證 Addr 格式：必須為 0x0000 ~ 0xFFFF
        import re
        addr_pattern = re.compile(r'^0x[0-9A-Fa-f]{4}$')
        invalid_rows = []
        for i, row in enumerate(rows, start=1):
            addr = row.get("Addr", "").strip()
            if not addr_pattern.match(addr):
                invalid_rows.append((i, addr))
        if invalid_rows:
            for line_no, addr in invalid_rows:
                dprint(f"[Param] Row {line_no}: invalid Addr '{addr}' (expected 0x0000 ~ 0xFFFF)")
            dprint(f"[Param] Aborted loading due to {len(invalid_rows)} invalid Addr(s).")
            return

        # 清除現有資料列
        ui = self.ui_handle
        while ui._param_rw_row_count > 0:
            idx = ui._param_rw_row_count
            row_tag = f"param_rw_row_{idx}"
            if dpg.does_item_exist(row_tag):
                dpg.delete_item(row_tag)
            ui._param_rw_row_count -= 1

        # 根據 CSV 內容新增列並填入資料
        for row in rows:
            ui._param_rw_row_count += 1
            idx = ui._param_rw_row_count
            row_tag = f"param_rw_row_{idx}"
            with dpg.table_row(tag=row_tag, parent="param_rw_table"):
                with dpg.table_cell():
                    dpg.add_input_text(tag=f"param_addr_{idx}", width=-1,
                                       default_value=row.get("Addr", ""), readonly=True)
                with dpg.table_cell():
                    dpg.add_input_text(tag=f"param_name_{idx}", width=-1,
                                       default_value=row.get("Name", ""), readonly=True)
                with dpg.table_cell():
                    dpg.add_input_text(tag=f"param_type_{idx}", width=-1,
                                       default_value=row.get("Type", "U16"), readonly=True)
                with dpg.table_cell():
                    dpg.add_button(label="Dec", tag=f"param_fmt_{idx}", width=-1,
                                   callback=self.on_param_fmt_clicked, user_data=idx)
                    dpg.bind_item_theme(f"param_fmt_{idx}", "param_send_default_theme")
                with dpg.table_cell():
                    dpg.add_input_text(tag=f"param_read_{idx}", width=-1, readonly=True)
                with dpg.table_cell():
                    dpg.add_input_text(tag=f"param_write_{idx}", width=-1,
                                       hint="decimal")
                    self._bind_write_auto_format(idx)
                with dpg.table_cell():
                    dpg.add_button(label="Send", tag=f"param_send_{idx}", width=-1,
                                   callback=self.on_param_send_clicked, user_data=idx)
                    dpg.bind_item_theme(f"param_send_{idx}", "param_send_default_theme")

        self._update_param_rw_table_height()
        self._apply_pending_write_bindings()
        # 重置參數輪詢狀態
        self.ui_handle._reset_pr_poll_state()
        dprint(f"[Param] Loaded {len(rows)} params from: {selected_path}")

    _FMT_CYCLE = ["Dec", "Hex", "Bin"]
    _FMT_HINTS = {"Dec": "decimal", "Hex": "0x...", "Bin": "...b"}

    def _bind_write_auto_format(self, idx: int):
        """為 param_write_{idx} 綁定失去焦點後自動格式化（延遲到 table 外建立）"""
        self._pending_write_bindings = getattr(self, '_pending_write_bindings', [])
        self._pending_write_bindings.append(idx)

    def _apply_pending_write_bindings(self):
        """實際建立 handler registries（在 table 上下文外呼叫）"""
        pending = getattr(self, '_pending_write_bindings', [])
        for idx in pending:
            write_tag = f"param_write_{idx}"
            handler_tag = f"param_write_handler_{idx}"
            if dpg.does_item_exist(handler_tag):
                dpg.delete_item(handler_tag)
            if not dpg.does_item_exist(write_tag):
                continue
            with dpg.item_handler_registry(tag=handler_tag):
                dpg.add_item_deactivated_after_edit_handler(
                    callback=self.on_param_write_deactivated, user_data=idx)
            dpg.bind_item_handler_registry(write_tag, handler_tag)
        self._pending_write_bindings = []

    def on_param_write_deactivated(self, sender, app_data, user_data):
        """當 Write 欄位失去焦點後自動格式化輸入值"""
        idx = user_data
        write_tag = f"param_write_{idx}"
        if not dpg.does_item_exist(write_tag):
            return
        raw = dpg.get_value(write_tag).strip()
        if not raw:
            return
        fmt = self._get_row_fmt(idx)
        type_str = self._get_row_type(idx)
        val = self._parse_input(raw, fmt, type_str)
        if val is not None:
            dpg.set_value(write_tag, self._format_value(val, fmt, type_str))

    @staticmethod
    def _parse_input(s: str, fmt: str, type_str: str = "") -> int | None:
        """解析使用者輸入為原始位元 (Raw Bits)"""
        import struct
        import re
        s = s.strip().replace(" ", "")
        if not s:
            return None

        val = None
        if fmt == "Hex":
            # 限制格式：僅接受 0x 或純 hex 字符
            hex_str = s[2:] if s.lower().startswith("0x") else s
            if not re.match(r'^[0-9A-Fa-f]+$', hex_str):
                return None
            try:
                val = int(hex_str, 16)
            except ValueError:
                return None
        elif fmt == "Bin":
            # 限制格式：僅接受 0b 或 ...b 或純 binary 字符
            if s.lower().startswith("0b"):
                bin_str = s[2:]
            elif s.endswith("b") or s.endswith("B"):
                bin_str = s[:-1]
            else:
                bin_str = s
            if not re.match(r'^[01]+$', bin_str):
                return None
            try:
                val = int(bin_str, 2)
            except ValueError:
                return None
        else:  # Dec
            # 限制格式：僅接受十進位數字 (FL32/Float32 允許小數點)
            if type_str in ("FL32", "Float32"):
                if not re.match(r'^-?[0-9.]+$', s):
                    return None
                try:
                    f = float(s)
                    raw_bytes = struct.pack(">f", f)
                    return struct.unpack(">I", raw_bytes)[0]
                except (ValueError, struct.error):
                    return None
            else:
                if not re.match(r'^-?[0-9]+$', s): # Dec for integer types
                    return None
                try:
                    interpreted_val = int(s, 10)
                except ValueError:
                    return None
                # Convert interpreted_val to its raw unsigned bit representation
                try:
                    if type_str == "S8": val = struct.unpack('B', struct.pack('b', interpreted_val))[0]
                    elif type_str == "U8": val = interpreted_val & 0xFF
                    elif type_str == "S16": val = struct.unpack('H', struct.pack('h', interpreted_val))[0]
                    elif type_str == "U16": val = interpreted_val & 0xFFFF
                    elif type_str == "S32": val = struct.unpack('I', struct.pack('i', interpreted_val))[0]
                    elif type_str == "U32": val = interpreted_val & 0xFFFFFFFF
                    else: val = interpreted_val # Default for unknown types
                except (struct.error, OverflowError):
                    return None
        return val # Always return the raw unsigned integer value

    def on_param_fmt_clicked(self, sender, app_data, user_data):
        """切換該列的顯示格式 Dec -> Hex -> Bin -> Dec ..."""
        idx = user_data
        fmt_tag = f"param_fmt_{idx}"
        read_tag = f"param_read_{idx}"
        write_tag = f"param_write_{idx}"
        if not dpg.does_item_exist(fmt_tag):
            return
        cur = dpg.get_item_label(fmt_tag)
        nxt = self._FMT_CYCLE[(self._FMT_CYCLE.index(cur) + 1) % 3] if cur in self._FMT_CYCLE else "Dec"
        dpg.set_item_label(fmt_tag, nxt)
        dpg.configure_item(write_tag, hint=self._FMT_HINTS[nxt])

        ui = self.ui_handle
        type_str = self._get_row_type(idx)

        # 轉換 Read 現有值
        if dpg.does_item_exist(read_tag):
            # ✅ 優先使用存下來的原始 raw bits 進行精確顯示轉換
            raw_bits = ui._param_raw_values.get(idx)
            if raw_bits is not None:
                dpg.set_value(read_tag, self._format_value(raw_bits, nxt, type_str))
            else:
                raw = dpg.get_value(read_tag).strip()
                converted = self._convert_display(raw, cur, nxt, type_str)
                if converted is not None:
                    dpg.set_value(read_tag, converted)

        # 轉換 Write 現有值
        if dpg.does_item_exist(write_tag):
            raw_w = dpg.get_value(write_tag).strip()
            converted_w = self._convert_display(raw_w, cur, nxt, type_str)
            if converted_w is not None:
                dpg.set_value(write_tag, converted_w)

    @staticmethod
    def _parse_any_int(s: str) -> int | None:
        """嘗試解析 dec / hex(0x) / bin(b後綴) 字串為整數，允許空格分組"""
        s = s.strip()
        if not s:
            return None
        # 移除分組用的空格
        s = s.replace(" ", "")
        # 處理 binary 後綴 b
        if s.endswith("b") and not s.startswith("0b") and not s.startswith("0B"):
            s = "0b" + s[:-1]
        try:
            return int(s, 0)
        except ValueError:
            return None

    @staticmethod
    def _parse_any_number(s: str, type_str: str) -> int | None:
        """解析數值字串，若 type_str 為 FL32 且輸入為小數則轉換為 IEEE754 位元"""
        import struct
        s = s.strip().replace(" ", "")
        if not s:
            return None
        # 處理 binary 後綴 b
        if s.endswith("b") and not s.startswith("0b") and not s.startswith("0B"):
            s = "0b" + s[:-1]
        # 嘗試整數解析
        try:
            return int(s, 0)
        except ValueError:
            pass
        # FL32: 嘗試解析為浮點數，轉換為 IEEE754 位元
        if type_str == "FL32":
            try:
                f = float(s)
                raw_bytes = struct.pack(">f", f)
                return struct.unpack(">I", raw_bytes)[0]
            except (ValueError, struct.error):
                pass
        return None

    @staticmethod
    def _format_value(val: int, fmt: str, type_str: str = "") -> str:
        """將整數值依格式轉換為分組顯示字串"""
        import struct

        # 決定位元遮罩，確保負數能以位元原始形式顯示 (避免 Hex/Bin 出現負號)
        mask = 0xFFFFFFFF
        if type_str in ("S8", "U8"): mask = 0xFF
        elif type_str in ("S16", "U16"): mask = 0xFFFF

        if fmt == "Hex":
            h = f"{(val & mask):X}"
            # 補齐到4的倍數
            h = h.zfill((len(h) + 3) // 4 * 4)
            groups = [h[i:i+4] for i in range(0, len(h), 4)]
            return "0x" + " ".join(groups)
        elif fmt == "Bin":
            b = f"{(val & mask):b}"
            # 補齐到4的倍數
            b = b.zfill((len(b) + 3) // 4 * 4)
            groups = [b[i:i+4] for i in range(0, len(b), 4)]
            return " ".join(groups) + "b"
        else:  # Dec
            # 針對有號整數型別進行位元重解釋
            display_val = val
            try:
                if type_str == "S8":
                    display_val = struct.unpack('b', struct.pack('B', val & 0xFF))[0]
                elif type_str == "S16":
                    display_val = struct.unpack('h', struct.pack('H', val & 0xFFFF))[0]
                elif type_str == "S32":
                    display_val = struct.unpack('i', struct.pack('I', val & 0xFFFFFFFF))[0]
            except (struct.error, OverflowError):
                pass

            if type_str in ("FL32", "Float32"):
                # 將 32-bit 整數解釋為 IEEE754 float
                try:
                    raw_bytes = struct.pack(">I", val & 0xFFFFFFFF)
                    f = struct.unpack(">f", raw_bytes)[0]
                except struct.error:
                    return str(val)
                # 格式化浮點數，整數部分和小數部分分別加空格分組
                s = f"{f:.6f}"
                if "." in s:
                    int_part, frac_part = s.split(".", 1)
                else:
                    int_part, frac_part = s, ""
                # 處理負號
                neg = int_part.startswith("-")
                if neg:
                    int_part = int_part[1:]
                # 整數部分從後往前每3位加空格
                grp = []
                for i, ch in enumerate(reversed(int_part)):
                    if i > 0 and i % 3 == 0:
                        grp.append(" ")
                    grp.append(ch)
                int_grouped = "".join(reversed(grp))
                # 小數部分從前往後每3位加空格
                frac_groups = [frac_part[i:i+3] for i in range(0, len(frac_part), 3)] if frac_part else []
                frac_grouped = " ".join(frac_groups)
                result = f"{int_grouped}.{frac_grouped}" if frac_grouped else int_grouped
                if neg:
                    result = "-" + result
                return result
            else:
                s = str(display_val)
                # 從後往前每3位加空格
                neg = s.startswith("-")
                if neg:
                    s = s[1:]
                result = []
                for i, ch in enumerate(reversed(s)):
                    if i > 0 and i % 3 == 0:
                        result.append(" ")
                    result.append(ch)
                out = "".join(reversed(result))
                if neg:
                    out = "-" + out
                return out

    @staticmethod
    def _convert_display(raw: str, old_fmt: str, new_fmt: str, type_str: str = "") -> str | None:
        """將字串值從 old_fmt 格式轉換為 new_fmt 格式"""
        raw = raw.strip()
        if not raw:
            return None
        val = UIEvent._parse_input(raw, old_fmt, type_str)
        if val is None:
            return None
        return UIEvent._format_value(val, new_fmt, type_str)

    def _get_row_type(self, idx: int) -> str:
        """取得第 idx 列的 Type 值"""
        type_tag = f"param_type_{idx}"
        if dpg.does_item_exist(type_tag):
            return dpg.get_value(type_tag).strip()
        return ""

    def _get_row_fmt(self, idx: int) -> str:
        """取得第 idx 列目前的格式標籤"""
        fmt_tag = f"param_fmt_{idx}"
        if dpg.does_item_exist(fmt_tag):
            label = dpg.get_item_label(fmt_tag)
            if label in self._FMT_CYCLE:
                return label
        return "Dec"

    def on_param_send_clicked(self, sender, app_data, user_data):
        """參數表 Send 按鈕 callback：驗證 Write 值並發送 SET_PR_VALUE"""
        ui = self.ui_handle
        if getattr(ui, "CommSts", "Stopped") != "Started":
            return
        # 若有 in-flight 寫入請求，則不重複送出
        if getattr(ui, "_pr_write_inflight_msg_id", None) is not None:
            return
        idx = user_data  # row index
        addr_tag = f"param_addr_{idx}"
        write_tag = f"param_write_{idx}"
        if not dpg.does_item_exist(addr_tag) or not dpg.does_item_exist(write_tag):
            return
        addr = dpg.get_value(addr_tag).strip()
        write_str = dpg.get_value(write_tag).strip()
        if not write_str:
            dprint(f"[Param] Row {idx}: Write value is empty.")
            return
        fmt = self._get_row_fmt(idx)
        type_str = self._get_row_type(idx)
        write_val = self._parse_input(write_str, fmt, type_str)
        if write_val is None:
            dprint(f"[Param] Row {idx}: Write value '{write_str}' is not valid ({fmt}).")
            return
        msg_id = ui.next_msg_id()
        req = UIMsg(msg_ID=msg_id, msg_type="SET_PR_VALUE", msg_subtype="REQUEST",
                    payload=f"{addr},{type_str},{write_val}")
        ui._pr_write_inflight_msg_id = msg_id
        ui._pr_write_inflight_row_idx = idx
        ui.ipc.send("UIMsg_gui_to_logic", req)

    def on_comm_port_combo_activated(self, sender, app_data, user_data):
        # 點開下拉式選單時：向 Logic 要最新 COM port list（非阻塞）
        # CommSts=Started 時忽略（避免即使 disable 仍可點開造成 items/value 被改動）
        if getattr(self.ui_handle, "CommSts", "Stopped") != "Stopped":
            return
        self.ui_handle.send_get_com_port_list_request()

    def on_bitrate_combo_changed(self, sender, app_data, user_data):
        val = app_data
        if not isinstance(val, str):
            try:
                val = dpg.get_value("bitrate_combo") if dpg.does_item_exist("bitrate_combo") else ""
            except Exception:
                val = ""

        # Accept both old/new casing to keep behavior stable.
        show_custom = (isinstance(val, str) and val.strip().lower() == "custom")
        try:
            # Layout switch:
            # - 非 custom：下拉式選單要吃滿寬度（單欄 layout）
            # - custom：切換成雙欄 layout（下拉 + input）
            if show_custom:
                if dpg.does_item_exist("bitrate_inner_table_full"):
                    dpg.hide_item("bitrate_inner_table_full")
                if dpg.does_item_exist("bitrate_inner_table_split"):
                    dpg.show_item("bitrate_inner_table_split")

                if dpg.does_item_exist("bitrate_split_left") and dpg.does_item_exist("bitrate_combo"):
                    dpg.move_item("bitrate_combo", parent="bitrate_split_left")

                if dpg.does_item_exist("bitrate_custom_input"):
                    dpg.show_item("bitrate_custom_input")
            else:
                if dpg.does_item_exist("bitrate_custom_input"):
                    dpg.hide_item("bitrate_custom_input")

                if dpg.does_item_exist("bitrate_full_container") and dpg.does_item_exist("bitrate_combo"):
                    dpg.move_item("bitrate_combo", parent="bitrate_full_container")

                if dpg.does_item_exist("bitrate_inner_table_split"):
                    dpg.hide_item("bitrate_inner_table_split")
                if dpg.does_item_exist("bitrate_inner_table_full"):
                    dpg.show_item("bitrate_inner_table_full")
        except Exception:
            pass
        
    def on_comm_start_clicked(self, sender, app_data, user_data):
        """Start/Stop 按鈕：送 START_BUTTON/REQUEST，成功才切換狀態"""
        # Avoid spamming while waiting for Logic response
        if getattr(self.ui_handle, "_start_button_inflight_msg_id", None) is not None:
            return
        if getattr(self.ui_handle, "_set_com_port_inflight_msg_id", None) is not None:
            return

        # If COM port is None, ignore Start/Stop.
        try:
            if dpg.does_item_exist("comm_port_combo"):
                selected_port = dpg.get_value("comm_port_combo")
                if selected_port == "None":
                    return
        except Exception:
            # If we cannot read the combo value, fail safe by ignoring.
            return

        # Decide desired action based on current status
        desired_action = "SWITCH_TO_START" if self.ui_handle.CommSts == "Stopped" else "SWITCH_TO_STOP"

        # Disable button immediately to prevent double-clicks during chained IPC
        try:
            if dpg.does_item_exist("comm_start_button"):
                dpg.disable_item("comm_start_button")
        except Exception:
            pass

        # If user is starting, first set COM port, then (on SUCCESS) send SWITCH_TO_START.
        if desired_action == "SWITCH_TO_START":
            # selected_port already checked above; send SET_COM_PORT first
            set_id = self.ui_handle.send_set_com_port_request(selected_port, next_action=desired_action)
            if set_id is None:
                # Failed to dispatch; re-enable button
                try:
                    if dpg.does_item_exist("comm_start_button"):
                        dpg.enable_item("comm_start_button")
                except Exception:
                    pass
            return

        # If user is stopping, just send SWITCH_TO_STOP.
        sent_id = self.ui_handle.send_start_button_request(desired_action)
        if sent_id is None:
            # Failed to dispatch; re-enable button
            try:
                if dpg.does_item_exist("comm_start_button"):
                    dpg.enable_item("comm_start_button")
            except Exception:
                pass
        
    def on_x_axis_unit_changed(self, sender, app_data, user_data):
        """X軸單位切換：By sequence / By time"""
        new_mode = dpg.get_value("x_axis_unit_combo")
        old_mode = getattr(self.ui_handle, "x_axis_unit_mode", "By sequence")
        if new_mode == old_mode:
            return

        self.ui_handle.x_axis_unit_mode = new_mode

        # 顯示/隱藏 sample period（秒）
        if dpg.does_item_exist("sample_period_group"):
            if new_mode == "By time":
                dpg.show_item("sample_period_group")
            else:
                dpg.hide_item("sample_period_group")

        # 清除所有暫存數據點，讓新資料以新單位重新累積
        for ds in self.ui_handle.signal_data.values():
            ds['x'].clear()
            ds['y'].clear()

        self.ui_handle.update_x_axis_labels()

    def on_sample_period_changed(self, sender, app_data, user_data):
        """sample period 變更（單位：second）"""
        new_s = float(dpg.get_value("sample_period_input") or 0.001)
        if new_s <= 0:
            new_s = 0.001
            dpg.set_value("sample_period_input", new_s)

        old_s = float(getattr(self.ui_handle, "sample_period_s", 0.001) or 0.001)
        self.ui_handle.sample_period_s = new_s

        # 清除所有暫存數據點，讓新資料以新 period 重新累積
        for ds in self.ui_handle.signal_data.values():
            ds['x'].clear()
            ds['y'].clear()

        self.ui_handle.update_x_axis_labels()

    def on_plot_drop(self, sender, app_data, user_data):
        """統一 plot drop callback，根據 app_data 分流"""
        if isinstance(app_data, tuple) and len(app_data) == 2:
            # data source drop
            self.on_data_source_drop(sender, app_data, user_data)
        elif isinstance(app_data, str) and app_data == "Y_CURSOR":
            self.on_y_cursor_drop(sender, app_data, user_data)
        elif isinstance(app_data, str) and app_data == "X_CURSOR":
            self.on_x_cursor_drop(sender, app_data, user_data)
        else:
            pass

    def on_clear_all_cursors(self, sender, app_data, user_data):
        """清除所有游標的回調函數"""
        self.ui_handle.cursor_handler.clear_all_cursors()

    def on_y_cursor_drop(self, sender, app_data, user_data):
        """Y cursor 拖曳到 plot 的回調"""
        plot_id = self.extract_plot_id_from_tag(sender)
        if not (plot_id and plot_id <= self.ui_handle.current_plot_count):
            return
        
        # ✨ 使用 cursor_handler 創建
        self.ui_handle.cursor_handler.create_y_cursor(plot_id)

    def on_x_cursor_drop(self, sender, app_data, user_data):
        """X cursor 拖曳到 plot 的回調"""
        # ✨ 使用 cursor_handler 創建
        self.ui_handle.cursor_handler.create_x_cursor()

    def on_clear_all_cached_points(self, sender, app_data, user_data):
        """清除所有暫存數據點的回調函數"""
        for data_source in self.ui_handle.signal_data.values():
            data_source['x'].clear()
            data_source['y'].clear()

    def on_button_click(self):
        """Event handler for button click"""
        pass

    def on_combo_changed(self, sender, app_data, user_data):
        """Event handler for combo box selection change (data source version)"""
        selected_value = dpg.get_value("number_combo")
        new_data_source_count = int(selected_value)
        # 更新當前數據源數
        self.ui_handle.current_plot_count = new_data_source_count
        # 清空所有數據源的暫存數據
        for i in range(1, self.ui_handle.max_data_sources + 1):
            data_source = f'ds{i}'
            if data_source in self.ui_handle.signal_data:
                self.ui_handle.signal_data[data_source]['x'].clear()
                self.ui_handle.signal_data[data_source]['y'].clear()
        # 清理所有舊的series和主題（在重新創建subplots之前）
        self.cleanup_all_series_and_themes()
        # 重置數據源指定
        self.ui_handle.plot_data_source_assignments.clear()
        # 重新創建subplots以反映新的數據源數
        self.ui_handle.create_dynamic_subplots()

    def cleanup_all_series_and_themes(self):
        """清理所有series（用於畫布數量變更時）"""
        for plot_id in range(1, 17):  # 最多16個plot
            for data_source_id in range(1, 17):  # 最多16個數據源
                series_tag = f"signal_series{plot_id}_{data_source_id}"
                if dpg.does_item_exist(series_tag):
                    dpg.delete_item(series_tag)
        

    def on_max_points_changed(self, sender, app_data, user_data):
        """Event handler for max points input change"""
        new_max_points = dpg.get_value("max_points_input")
        self.ui_handle.max_data_points = new_max_points
        # 立即裁剪一次
        # 立即裁剪一次
        self.ui_handle.trim_data_to_max_points()

    def on_x_axis_autofit_changed(self, sender, app_data, user_data):
        """Event handler for X-axis auto-fit checkbox change"""
        is_enabled = dpg.get_value("x_axis_autofit_checkbox")
        self.ui_handle.auto_fit.set_x_axis_autofit(is_enabled)

    def on_y_axis_autofit_changed(self, sender, app_data, user_data):
        """Event handler for Y-axis auto-fit checkbox change"""
        is_enabled = dpg.get_value("y_axis_autofit_checkbox")
        self.ui_handle.auto_fit.set_y_axis_autofit(is_enabled)

    def on_adaptive_downsampling_changed(self, sender, app_data, user_data):
        """Event handler for adaptive downsampling checkbox change"""
        is_enabled = dpg.get_value("adaptive_downsampling_checkbox")
        self.ui_handle.adaptive_display_optimization.set_adaptive_downsampling(is_enabled)

    def on_bypass_crc_changed(self, sender, app_data, user_data):
        """Comm setting: bypass CRC check toggle (effective only when Comm is Stopped)."""
        ui = self.ui_handle
        is_enabled = bool(dpg.get_value("bypass_crc_checkbox"))

        if getattr(ui, "CommSts", "Stopped") != "Stopped":
            try:
                dpg.set_value("bypass_crc_checkbox", bool(getattr(ui, "bypass_crc_check_enabled", False)))
            except Exception:
                pass
            return

        ui.bypass_crc_check_enabled = is_enabled

    def on_show_legend_changed(self, sender, app_data, user_data):
        """Event handler for show legend checkbox change"""
        is_enabled = dpg.get_value("show_legend_checkbox")
        self.ui_handle.show_legend_enabled = is_enabled
        
        # 只遍歷數據plots，排除時間軸plot
        for i in range(1, self.ui_handle.current_plot_count + 1):  # 只到畫布數量，不包括時間軸
            legend_tag = f"legend{i}"
            if dpg.does_item_exist(legend_tag):
                dpg.configure_item(legend_tag, show=is_enabled)
    
    def on_clear_all_data_sources(self, sender, app_data, user_data):
        """清除所有數據源與資料的回調函數"""
        self.ui_handle.clear_all_data_source_assignments()
    
    def on_auto_scroll_changed(self, sender, app_data, user_data):
        """Auto Scroll checkbox改變時的回調函數"""
        if dpg.does_item_exist("log_text"):
            dpg.configure_item("log_text", tracked=app_data)

    def on_clear_log_clicked(self, sender, app_data, user_data):
        """Clear Log 按鈕的回調函數"""
        self.ui_handle.log_manager.clear_log()
    
    def on_data_source_label_changed(self, sender, app_data, user_data):
        """當數據源標籤文字被修改時的回調函數"""
        data_source_id = user_data  # data_source_id 從 user_data 傳入
        new_label = app_data    # 新的標籤文字

        # 找出所有使用這個數據源的 series 並更新它們的標籤
        for plot_id, assigned_data_sources in self.ui_handle.plot_data_source_assignments.items():
            if data_source_id in assigned_data_sources:
                series_tag = f"signal_series{plot_id}_{data_source_id}"
                if dpg.does_item_exist(series_tag):
                    # 更新 series 的標籤
                    dpg.configure_item(series_tag, label=new_label)

    def on_data_source_color_changed(self, sender, app_data, user_data):
        """當數據源顏色被修改時的回調函數"""
        data_source_id = user_data  # data_source_id 從 user_data 傳入
        new_color = app_data    # 新的顏色值
        
        # 檢查是否為浮點數格式，如果是則轉換
        if isinstance(new_color, (list, tuple)) and len(new_color) > 0:
            if isinstance(new_color[0], float) and 0 <= new_color[0] <= 1:
                color_255 = [int(c * 255) for c in new_color]
            else:
                color_255 = list(new_color)
        else:
            color_255 = [255, 255, 255, 255]  # 預設白色
        
        # 更新儲存的顏色
        self.ui_handle.data_source_colors[data_source_id] = color_255
        
        # 立即更新對應數據源的顏色
        self.update_single_data_source_color(data_source_id, color_255)
        
    def update_single_data_source_color(self, data_source_id, color):
        """更新單一數據源的顏色（使用匿名主題避免標籤管理問題）"""

        # 找到所有使用該數據源的 series 並更新顏色
        updated_count = 0
        for plot_id, assigned_data_sources in self.ui_handle.plot_data_source_assignments.items():
            if data_source_id in assigned_data_sources:
                series_tag = f"signal_series{plot_id}_{data_source_id}"
                if dpg.does_item_exist(series_tag):
                    with dpg.theme() as series_theme:
                        with dpg.theme_component(dpg.mvStairSeries):
                            dpg.add_theme_color(dpg.mvPlotCol_Line, color, category=dpg.mvThemeCat_Plots)
                    dpg.bind_item_theme(series_tag, series_theme)
                    updated_count += 1
        


    
    def global_mouse_release_handler(self, sender, app_data, user_data):
        """全域滑鼠釋放事件處理器（備用）"""
        # 這個現在主要作為備用機制
        pass
    


    def resize_window_callback(self, sender, app_data):
        if custom_window_instance is not None:
            custom_window_instance.handle_resize(sender, app_data)
        
        """視窗大小調整回調"""
        # Get the viewport width and height
        viewport_width = dpg.get_viewport_client_width()
        viewport_height = dpg.get_viewport_client_height()
        
        # Update left and right panel widths (考慮視覺分隔條寬度)
        visual_splitter_width = 3  # 視覺分隔條寬度
        spacer_width = 34  # 預留的間隔寬度 (2*resize_bar_w + 2*WindowPadding = 16+16, 再多2像素容錯)
        available_width = viewport_width - visual_splitter_width - spacer_width
        
        # 使用儲存的比例，如果沒有則使用預設值
        left_ratio = getattr(self.ui_handle, 'left_panel_ratio', 0.2)
        
        left_width = int(available_width * left_ratio)
        right_width = int(available_width * (1.0 - left_ratio))
        # 移除0.97係數，確保總寬度正確
        right_ratio = 1.0 - left_ratio
        
        # 檢查是否超出
        total_used = left_width + visual_splitter_width + right_width
        if total_used > viewport_width:
            pass

        dpg.set_item_width("left_panel", left_width)
        dpg.set_item_width("right_panel", right_width)
        
        # 更新FPS位置到右側面板的右上角
        fps_x = right_width - 80  # 距離右側面板右邊緣80像素
        fps_y = 5  # 距離頂部5像素
        dpg.set_item_pos("fps_display", [fps_x, fps_y])

        # ✅ 視窗縮放時：依 log_text 當前寬度重排整段 log
        try:
            self.ui_handle.log_manager.reflow_log(force=False)
        except Exception:
            pass

        # ✅ 視窗縮放時：更新 log 容器高度
        try:
            self.ui_handle.log_manager.update_log_container_height()
        except Exception:
            pass

        # ✅ Keep CSV export modal centered
        try:
            if getattr(getattr(self.ui_handle, "save_manager", None), "_csv_export_active", False):
                self.ui_handle.save_manager.center_csv_export_modal()
        except Exception:
            pass
    
    def on_data_source_drop(self, sender, app_data, user_data):
        """數據源拖拽到 plot 的回調函數"""
        data_source_id, input_tag = app_data
        plot_id = self.extract_plot_id_from_tag(sender)

        # 從 text input 獲取自定義標籤文字
        custom_label = dpg.get_value(input_tag) if dpg.does_item_exist(input_tag) else f"DS{data_source_id}"

        if plot_id and plot_id <= self.ui_handle.current_plot_count:
            # 將數據源指定給特定的 plot，使用自定義標籤
            self.ui_handle.assign_data_source_to_plot(plot_id, data_source_id, custom_label)

    
    def extract_plot_id_from_tag(self, tag):
        """從plot tag中提取plot ID"""
        if isinstance(tag, str) and tag.startswith("plot"):
            try:
                return int(tag.replace("plot", ""))
            except ValueError:
                return None
        return None

    def _update_param_rw_table_height(self):
        """當資料列超過 10 列時啟用 scrollY + 固定高度，否則關閉滾動"""
        ui = self.ui_handle
        if not dpg.does_item_exist("param_rw_table"):
            return
        if ui._param_rw_row_count > 10:
            dpg.configure_item("param_rw_table", scrollY=True, height=250)
        else:
            dpg.configure_item("param_rw_table", scrollY=False, height=0)

    def on_param_add_row(self, sender, app_data, user_data):
        """在 param_rw_table 新增一列"""
        ui = self.ui_handle
        ui._param_rw_row_count += 1
        idx = ui._param_rw_row_count
        row_tag = f"param_rw_row_{idx}"
        with dpg.table_row(tag=row_tag, parent="param_rw_table"):
            with dpg.table_cell():
                dpg.add_input_text(tag=f"param_addr_{idx}", width=-1, readonly=True)
            with dpg.table_cell():
                dpg.add_input_text(tag=f"param_name_{idx}", width=-1, readonly=True)
            with dpg.table_cell():
                dpg.add_input_text(tag=f"param_type_{idx}", width=-1,
                                   default_value="U16", readonly=True)
            with dpg.table_cell():
                dpg.add_button(label="Dec", tag=f"param_fmt_{idx}", width=-1,
                               callback=self.on_param_fmt_clicked, user_data=idx)
                dpg.bind_item_theme(f"param_fmt_{idx}", "param_send_default_theme")
            with dpg.table_cell():
                dpg.add_input_text(tag=f"param_read_{idx}", width=-1, readonly=True)
            with dpg.table_cell():
                dpg.add_input_text(tag=f"param_write_{idx}", width=-1,
                                   hint="decimal")
                self._bind_write_auto_format(idx)
            with dpg.table_cell():
                dpg.add_button(label="Send", tag=f"param_send_{idx}", width=-1)
                dpg.bind_item_theme(f"param_send_{idx}", "param_send_default_theme")
        self._update_param_rw_table_height()
        self._apply_pending_write_bindings()

    def on_param_remove_row(self, sender, app_data, user_data):
        """從 param_rw_table 刪除最後一列"""
        ui = self.ui_handle
        if ui._param_rw_row_count <= 0:
            return
        idx = ui._param_rw_row_count
        row_tag = f"param_rw_row_{idx}"
        if dpg.does_item_exist(row_tag):
            dpg.delete_item(row_tag)
        ui._param_rw_row_count -= 1
        self._update_param_rw_table_height()

class SplitterHandler:
    def __init__(self, ui_handle):
        self.ui_handle = ui_handle  # 參考UIHandle以存取共享狀態
        
        # 分隔條拖動相關屬性
        self.splitter_dragging = False  # 分隔條拖動狀態
        self.drag_start_mouse_x = None  # 拖動開始時的滑鼠X座標
        self.drag_start_ratio = None    # 拖動開始時的左面板比例
        
        # 分隔條限制參數
        self.min_left_ratio = 0.15      # 左面板最小比例
        self.max_left_ratio = 0.50      # 左面板最大比例
        
        # 視覺分隔條配置
        self.splitter_width = 3         # 視覺分隔條寬度
        self.spacer_width = 34          # 預留間隔寬度 (2*resize_bar_w + 2*WindowPadding = 16+16, 再多2像素容錯)
        
        # 主題物件
        self.splitter_theme = None
        self.splitter_highlight_theme = None

    def check_splitter_drag_state(self):
        """檢查分隔條拖動狀態 - 監測滑鼠互動"""
        if not dpg.does_item_exist("visual_splitter_button"):
            return
        
        button_active = dpg.is_item_active("visual_splitter_button")
        left_mouse_down = dpg.is_mouse_button_down(dpg.mvMouseButton_Left)
        currently_dragging = self.splitter_dragging
        
        # 更新按鈕主題（視覺反饋）
        self._update_button_theme(currently_dragging)
        
        # 開始拖動
        if button_active and left_mouse_down and not currently_dragging:
            mouse_pos = dpg.get_mouse_pos(local=False)
            if mouse_pos:
                self.drag_start_mouse_x = mouse_pos[0]
                self.drag_start_ratio = self.ui_handle.left_panel_ratio
                self.splitter_dragging = True
        
        # 結束拖動
        elif currently_dragging and (not left_mouse_down or not button_active):
            self.splitter_dragging = False
            self._cleanup_drag_state()

    def update_splitter_position(self):
        """更新分隔條位置和面板大小（在handler中調用）"""
        # 首先檢查拖動狀態
        self.check_splitter_drag_state()
        
        # 如果未拖動則不執行更新
        if not self.splitter_dragging:
            return
        
        # 執行拖動邏輯
        self._perform_drag_update()

    def _perform_drag_update(self):
        """執行實際的拖動更新邏輯"""
        if not dpg.does_item_exist("visual_splitter_button"):
            return
        
        # 獲取滑鼠位置
        mouse_pos = dpg.get_mouse_pos(local=False)
        if not mouse_pos:
            return
        
        mouse_x = mouse_pos[0]
        viewport_width = dpg.get_viewport_client_width()
        available_width = viewport_width - self.splitter_width - self.spacer_width
        
        # 計算新的比例
        if self.drag_start_mouse_x is not None and self.drag_start_ratio is not None:
            mouse_delta = mouse_x - self.drag_start_mouse_x
            ratio_delta = (mouse_delta / viewport_width)
            raw_ratio = self.drag_start_ratio + ratio_delta
        else:
            raw_ratio = mouse_x / available_width
        
        # 限制比例範圍
        left_ratio = self._clamp_ratio(raw_ratio)
        self.ui_handle.left_panel_ratio = left_ratio
        
        # 更新面板寬度
        self._update_panel_widths(viewport_width, available_width, left_ratio)
        
        # 更新FPS位置
        self._update_fps_position(left_ratio, available_width)

    def _clamp_ratio(self, ratio):
        """限制比例在允許範圍內"""
        return max(self.min_left_ratio, min(self.max_left_ratio, ratio))

    def _update_panel_widths(self, viewport_width, available_width, left_ratio):
        """更新左右面板寬度"""
        left_width = int(available_width * left_ratio)
        right_width = int(available_width * (1.0 - left_ratio))
        
        dpg.configure_item("left_panel", width=left_width)
        dpg.configure_item("right_panel", width=right_width)

        # ✅ 拖曳分隔條時：依 log_text 當前寬度重排整段 log
        try:
            self.ui_handle.log_manager.reflow_log(force=False)
        except Exception:
            pass

    def _update_fps_position(self, left_ratio, available_width):
        """更新FPS顯示位置到右上角"""
        right_width = int(available_width * (1.0 - left_ratio))
        fps_x = right_width - 80  # 距離右側面板右邊緣80像素
        fps_y = 5                 # 距離頂部5像素
        dpg.set_item_pos("fps_display", [fps_x, fps_y])

    def _update_button_theme(self, is_dragging):
        """更新分隔條按鈕主題（拖動中亮起，未拖動恢復正常）"""
        if not dpg.does_item_exist("visual_splitter_button"):
            return
        
        if is_dragging and self.splitter_highlight_theme:
            dpg.bind_item_theme("visual_splitter_button", self.splitter_highlight_theme)
        elif self.splitter_theme:
            dpg.bind_item_theme("visual_splitter_button", self.splitter_theme)

    def _cleanup_drag_state(self):
        """清除拖動相關的臨時狀態"""
        self.drag_start_mouse_x = None
        self.drag_start_ratio = None

    def setup_splitter_themes(self):
        """初始化分隔條主題（在create_layout中調用）"""
        # 預設主題
        with dpg.theme() as splitter_theme:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, [80, 80, 80, 255])           # 正常：深灰色
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, [120, 120, 120, 255]) # 懸停：亮灰色
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, [100, 150, 200, 255])  # 活動：藍色
                dpg.add_theme_color(dpg.mvThemeCol_Border, [0, 0, 0, 0])               # 無邊框
        self.splitter_theme = splitter_theme
        
        # 高亮主題（拖曳中）
        with dpg.theme() as highlight_theme:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, [100, 180, 255, 255])        # 高亮藍
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, [120, 200, 255, 255]) # 高亮亮藍
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, [100, 180, 255, 255])  # 高亮藍
                dpg.add_theme_color(dpg.mvThemeCol_Border, [0, 0, 0, 0])
        self.splitter_highlight_theme = highlight_theme

    def set_left_ratio_limits(self, min_ratio, max_ratio):
        """設定左面板比例的最小/最大限制"""
        self.min_left_ratio = max(0.10, min_ratio)      # 最小不低於10%
        self.max_left_ratio = min(0.60, max_ratio)      # 最大不超過60%

    def get_splitter_dragging_state(self):
        """獲取分隔條拖動狀態"""
        return self.splitter_dragging

class UIHandle:
    def apply_dark_title_bar(self):
        # 1. 獲取 DPG 視窗控制代碼 (HWND)
        #hwnd = windll.user32.FindWindowW(None, 'lwSCOPE')
        hwnd = windll.user32.GetActiveWindow()
        if not hwnd:
            return
    
        # 2. 定義 DWM 屬性代碼
        # 20 是 Win11 與新版 Win10 (20H1後) 官方標準
        # 19 是舊版 Win10 (1809 - 1909) 的屬性
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
        
        dwmapi = ctypes.WinDLL("dwmapi")
        use_dark_mode = ctypes.c_int(1) # 1 為開啟，0 為關閉
    
        # 3. 嘗試套用 (優先嘗試代碼 20)
        res = dwmapi.DwmSetWindowAttribute(
            hwnd, 
            DWMWA_USE_IMMERSIVE_DARK_MODE, 
            ctypes.byref(use_dark_mode), 
            ctypes.sizeof(use_dark_mode)
        )
    
        # 4. 如果失敗，嘗試舊版代碼 19
        if res != 0:
            dwmapi.DwmSetWindowAttribute(
                hwnd, 
                DWMWA_USE_IMMERSIVE_DARK_MODE_OLD, 
                ctypes.byref(use_dark_mode), 
                ctypes.sizeof(use_dark_mode)
            )
        
        # 強制重繪邊框以生效 (有時標題列不會立刻變色)
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)

    def get_desktop_path(self) -> str:
        """Return a reasonable Desktop path on Windows; fall back to home."""
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, "Desktop"),
            os.path.join(home, "OneDrive", "Desktop"),
        ]
        for p in candidates:
            if os.path.isdir(p):
                return p
        return home

    def __init__(self, ipc_queues):
        self.gui_initialized = False
        self.counter = 0
        self.target_interval = 1.0 / 60.0  # Targeting 60 FPS (16.67ms)

        # ...existing code...
        
        self.CommSts = "Stopped"  # "Started" | "Stopped"
        self.bypass_crc_check_enabled = False
        
        # 面板比例設定
        self.left_panel_ratio = 0.2  # 預設左面板佔 20%
        
        # 初始化組件 - 使用組合模式
        self.fps_tracker                        = FPSTracker(self)
        self.auto_fit                           = AutoFit(self)
        self.adaptive_display_optimization      = AdaptiveDisplayOptimization(self)
        self.log_manager                        = LogManager(self)
        self.save_manager                       = SaveManager(self)
        self.ui_event                           = UIEvent(self)
        self.splitter_handler                   = SplitterHandler(self)
        self.cursor_handler                     = CursorHandler(self)
        self.ipc                                = IPC(self)

        # Native dialog state
        self.save_path_dialog = None
        self.load_param_dialog = None
        self.native_dialog_last_dir = self.get_desktop_path()
        # Python-side cache for save dir (avoids dpg widget encoding issues with CJK paths)
        self._save_dir_cache = self.get_desktop_path()
        
        # ✅ UI IPC request/response 狀態
        self._ui_msg_id_counter = 0
        self.pending_requests = {}  # {msg_ID: request_type}
        
        # ✅ GET_LOG 一問一答：最多只允許 1 個 request 在路上
        self._get_log_inflight_msg_id = None
        # ✅ GET_COM_PORT_LIST 一問一答：最多只允許 1 個 request 在路上（避免重複點開狂送）
        self._get_com_port_list_inflight_msg_id = None
        # ✅ GET_PROTOCOL_STATS 一問一答
        self._get_protocol_stats_inflight_msg_id = None
        self._last_protocol_stats = None  # 最新一次收到的統計數據
        self._protocol_stats_interval = 0.1  # 每0.1秒請求一次
        self._protocol_stats_last_request_time = 0.0

        # ✅ SET_COM_PORT 一問一答：最多只允許 1 個 request 在路上
        self._set_com_port_inflight_msg_id = None
        # {msg_ID: {"port": str, "next_action": "SWITCH_TO_START"|None}}
        self._set_com_port_pending = {}

        # ✅ START_BUTTON 一問一答：最多只允許 1 個 request 在路上（避免重複點擊）
        self._start_button_inflight_msg_id = None
        self._start_button_pending_action = {}  # {msg_ID: "SWITCH_TO_START"|"SWITCH_TO_STOP"}
        
        # 註冊所有需要的 IPC 通道
        self.ipc.register_channel("HSDataSource_logic_to_gui", ipc_queues["HSDataSource_logic_to_gui"])
        self.ipc.register_channel("UIMsg_gui_to_logic", ipc_queues["UIMsg_gui_to_logic"])
        self.ipc.register_channel("UIMsg_logic_to_gui", ipc_queues["UIMsg_logic_to_gui"])
        
        # 數據源指定管理
        self.plot_data_source_assignments = {}  # {plot_id: [data_source_ids]} - 每個 plot 指定的數據源列表

        # 數據源顏色管理
        self.data_source_colors = {}  # {data_source_id: [r, g, b, a]} - 每個數據源的顏色設定（0-255整數範圍）
        # Default DS colors (DS1..DS16): fixed RGBA (0-255), top-to-bottom swatch order.
        default_colors = [
            [255, 255, 0, 255],    # DS1
            [255, 0, 128, 255],    # DS2
            [0, 255, 255, 255],    # DS3
            [0, 255, 0, 255],      # DS4
            [255, 140, 0, 255],    # DS5
            [255, 0, 0, 255],      # DS6
            [0, 90, 255, 255],     # DS7
            [0, 130, 60, 255],     # DS8
            [255, 255, 190, 255],  # DS9
            [255, 185, 205, 255],  # DS10
            [150, 80, 200, 255],   # DS11
            [140, 50, 50, 255],    # DS12
            [120, 120, 0, 255],    # DS13
            [130, 150, 110, 255],  # DS14
            [255, 255, 255, 255],  # DS15
            [110, 210, 255, 255],  # DS16
        ]
        for i in range(1, 17):
            self.data_source_colors[i] = default_colors[i-1]

        # UI 狀態變數  
        self.current_plot_count = 4  # 當前顯示的畫布（subplot/plot）數量
        self.max_data_sources = 16  # 最大支援數據源數
        self.max_data_points = 100000  # 最大顯示點數
        self.signal_data = {}  # {data_source: {'x': array.array('d'), 'y': array.array('d')}}
        for i in range(1, self.max_data_sources + 1):
            self.signal_data[f'ds{i}'] = {'x': _array.array('d'), 'y': _array.array('d')}
        
        # 圖標顯示相關變數
        self.show_legend_enabled = True  # 預設啟用，與UI checkbox一致
        
        # Row ratios 查表：為每種數據源數配置定制的顯示比例
        # 格式：{數據源數: [數據源plot比例列表, 時間軸比例]}
        # 你可以根據測試結果調整每種配置的比例值
        self.row_ratios_table = {
            1:  [15.0, 1.0],                                                                                # 1個數據源 + 時間軸
            2:  [8.0, 8.0, 1.0],                                                                            # 2個數據源 + 時間軸  
            3:  [5.0, 5.0, 5.0, 1.0],                                                                       # 3個數據源 + 時間軸
            4:  [4.0, 4.0, 4.0, 4.0, 1.0],                                                                  # 4個數據源 + 時間軸
            5:  [3.0, 3.0, 3.0, 3.0, 3.0, 1.0],                                                             # 5個數據源 + 時間軸
            6:  [2.5, 2.5, 2.5, 2.5, 2.5, 2.5, 1.0],                                                        # 6個數據源 + 時間軸
            7:  [2.3, 2.3, 2.3, 2.3, 2.3, 2.3, 2.3, 1.0],                                                   # 7個數據源 + 時間軸
            8:  [2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0, 1.0],                                              # 8個數據源 + 時間軸
            9:  [1.8, 1.8, 1.8, 1.8, 1.8, 1.8, 1.8, 1.8, 1.8, 1.0],                                         # 9個數據源 + 時間軸
            10: [1.6, 1.6, 1.6, 1.6, 1.6, 1.6, 1.6, 1.6, 1.6, 1.6, 1.0],                                    # 10個數據源 + 時間軸
            11: [1.4, 1.4, 1.4, 1.4, 1.4, 1.4, 1.4, 1.4, 1.4, 1.4, 1.4, 1.0],                               # 11個數據源 + 時間軸
            12: [1.3, 1.3, 1.3, 1.3, 1.3, 1.3, 1.3, 1.3, 1.3, 1.3, 1.3, 1.3, 1.0],                          # 12個數據源 + 時間軸
            13: [1.2, 1.2, 1.2, 1.2, 1.2, 1.2, 1.2, 1.2, 1.2, 1.2, 1.2, 1.2, 1.2, 1.0],                     # 13個數據源 + 時間軸
            14: [1.15, 1.15, 1.15, 1.15, 1.15, 1.15, 1.15, 1.15, 1.15, 1.15, 1.15, 1.15, 1.15, 1.15, 1.0],  # 14個數據源 + 時間軸
            15: [1.1, 1.1, 1.1, 1.1, 1.1, 1.1, 1.1, 1.1, 1.1, 1.1, 1.1, 1.1, 1.1, 1.1, 1.1, 1.0],           # 15個數據源 + 時間軸
            16: [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],      # 16個數據源 + 時間軸
        }
        
        self.series_last_update_length = {}
        self.series_data_buffer = {}

        # Cache for plot range-based rendering to avoid redundant set_value calls.
        # {series_tag: (x_min_vis_norm, x_max_vis_norm, downsample_rate, first_x, last_x, slice_len)}
        self._series_visible_cache = {}
        
        self.x_axis_unit_mode = "By sequence"  # "By sequence" | "By time"
        self.sample_period_s = 0.001           # ✅ 預設 0.001s (= 1ms)

        # CSV export state lives in SaveManager

        # Remember last selected COM port to keep UI stable while Started
        self._last_selected_comm_port = "None"

        # Cache the last known COM port combo items so we can restore them after
        # temporarily clearing the dropdown while CommSts == Started.
        self._comm_port_items_cached = ["None", "Demo Port"]

        # Pulse flag: after a plot double-click, make X-axis autofit run briefly.
        # The flag is evaluated inside AutoFit.update_axis_fitting alongside x_axis_autofit_enabled.
        self._x_autofit_pulse_once = False
        self._x_autofit_pulse_frames_left = 0

        # 參數讀寫表格：動態列追蹤
        self._param_rw_row_count = 0  # 目前 param_rw_table 的列數

        # 參數讀取輪詢狀態（Read/Write 分離）
        self._pr_poll_current_idx = 1          # 當前輪詢的列 index (1-based)
        self._pr_poll_last_tick = 0.0          # 上次 200ms tick 時間 (monotonic)
        self._pr_read_inflight_msg_id = None   # 當前 in-flight 的 GET_PR_VALUE msg_id
        self._pr_read_inflight_row_idx = None  # 當前 in-flight 的列 index
        self._pr_poll_last_success_row = None  # 上一次成功讀取的列 index（用於清除黃色）
        self._pr_write_inflight_msg_id = None  # 當前 in-flight 的 SET_PR_VALUE msg_id
        self._pr_write_inflight_row_idx = None # 當前 in-flight 的寫入列 index

        self._param_raw_values = {}            # {row_idx: int} 儲存最後一次成功的 raw bit 值

    def update_comm_settings_items_enabled_state(self):
        """CommSts gate for comm-setting widgets.

        Requirements:
        - CommSts == Started: widgets look faded via theme, interactions are ineffective,
          combo dropdowns open with empty lists.
        - Implementation must NOT use dpg.disable_item()/enabled=False (theme must apply).
        """
        locked = (getattr(self, "CommSts", "Stopped") == "Started")

        def _bind_theme(item_tag: str):
            try:
                if dpg.does_item_exist(item_tag):
                    dpg.bind_item_theme(
                        item_tag,
                        "comm_settings_disabled_theme" if locked else "comm_settings_normal_theme",
                    )
            except Exception:
                pass

        def _lock_combo(item_tag: str, unlocked_items: list[str] | None, locked_display_tag: str | None = None):
            if not dpg.does_item_exist(item_tag):
                return
            try:
                current = dpg.get_value(item_tag)
            except Exception:
                current = None

            try:
                if locked:
                    dpg.configure_item(item_tag, items=[])
                else:
                    dpg.configure_item(item_tag, items=(unlocked_items or []))
            except Exception:
                pass

            # Intercept clicks by swapping to a readonly display widget while locked.
            if locked_display_tag and dpg.does_item_exist(locked_display_tag):
                try:
                    if current is None:
                        current_text = ""
                    else:
                        current_text = str(current)
                    dpg.set_value(locked_display_tag, current_text)
                except Exception:
                    pass

                try:
                    if locked:
                        dpg.hide_item(item_tag)
                        dpg.show_item(locked_display_tag)
                    else:
                        dpg.show_item(item_tag)
                        dpg.hide_item(locked_display_tag)
                except Exception:
                    pass

            # Keep the displayed value stable (best-effort)
            if current is not None:
                try:
                    dpg.set_value(item_tag, current)
                except Exception:
                    pass

            _bind_theme(item_tag)
            if locked_display_tag:
                _bind_theme(locked_display_tag)

        # Combos in the red box
        _lock_combo(
            "comm_port_combo",
            getattr(self, "_comm_port_items_cached", ["None", "Demo Port"]),
            locked_display_tag="comm_port_combo_locked",
        )

        # Bitrate is special: it has two layouts (full/split). While locked, we still
        # keep the current value visible but ensure no dropdown popup can open.
        bitrate_items = [
            "Custom",
            "9600",
            "19200",
            "38400",
            "57600",
            "115200",
            "230400",
            "460800",
            "576000",
            "768000",
            "921600",
        ]
        _lock_combo("bitrate_combo", bitrate_items, locked_display_tag="bitrate_combo_locked")
        _lock_combo(
            "frame_format_combo",
            ["8N1", "8O1", "8E1", "8N2", "8O2", "8E2"],
            locked_display_tag="frame_format_combo_locked",
        )

        # Maintain bitrate layout when locked/unlocked
        try:
            current_bitrate_val = dpg.get_value("bitrate_combo") if dpg.does_item_exist("bitrate_combo") else ""
        except Exception:
            current_bitrate_val = ""
        is_custom = isinstance(current_bitrate_val, str) and current_bitrate_val.strip().lower() == "custom"

        if locked:
            # Force correct layout + move the locked display into the active container.
            if is_custom:
                if dpg.does_item_exist("bitrate_inner_table_full"):
                    dpg.hide_item("bitrate_inner_table_full")
                if dpg.does_item_exist("bitrate_inner_table_split"):
                    dpg.show_item("bitrate_inner_table_split")
                if dpg.does_item_exist("bitrate_split_left") and dpg.does_item_exist("bitrate_combo_locked"):
                    dpg.move_item("bitrate_combo_locked", parent="bitrate_split_left")
                if dpg.does_item_exist("bitrate_custom_input"):
                    dpg.show_item("bitrate_custom_input")
            else:
                if dpg.does_item_exist("bitrate_custom_input"):
                    dpg.hide_item("bitrate_custom_input")
                if dpg.does_item_exist("bitrate_inner_table_split"):
                    dpg.hide_item("bitrate_inner_table_split")
                if dpg.does_item_exist("bitrate_inner_table_full"):
                    dpg.show_item("bitrate_inner_table_full")
                if dpg.does_item_exist("bitrate_full_container") and dpg.does_item_exist("bitrate_combo_locked"):
                    dpg.move_item("bitrate_combo_locked", parent="bitrate_full_container")
        else:
            # When unlocked, let the existing callback manage layout on value changes,
            # but make sure the locked display sits in the same container as the combo.
            try:
                if is_custom:
                    if dpg.does_item_exist("bitrate_split_left") and dpg.does_item_exist("bitrate_combo_locked"):
                        dpg.move_item("bitrate_combo_locked", parent="bitrate_split_left")
                else:
                    if dpg.does_item_exist("bitrate_full_container") and dpg.does_item_exist("bitrate_combo_locked"):
                        dpg.move_item("bitrate_combo_locked", parent="bitrate_full_container")
            except Exception:
                pass

        # Custom bitrate input: keep visible state, but prevent edits
        if dpg.does_item_exist("bitrate_custom_input"):
            try:
                dpg.configure_item("bitrate_custom_input", readonly=locked)
            except Exception:
                pass
            _bind_theme("bitrate_custom_input")

        # Non-combo comm settings in the same section
        # Keep label text color unchanged even when Comm is locked.
        try:
            if dpg.does_item_exist("bypass_crc_label"):
                dpg.bind_item_theme("bypass_crc_label", "comm_settings_normal_theme")
        except Exception:
            pass
        _bind_theme("bypass_crc_checkbox")

    def send_set_com_port_request(self, port_name: str, next_action: str | None = None) -> int | None:
        """GUI -> Logic：SET_COM_PORT/REQUEST（非阻塞）

        next_action: 可選，用於串接流程（例如 SUCCESS 後再送 SWITCH_TO_START）。
        """
        if self._set_com_port_inflight_msg_id is not None:
            return None

        if not isinstance(port_name, str):
            return None
        port_name = port_name.strip()
        if not port_name or port_name == "None":
            return None

        msg_id = self.next_msg_id()
        req = UIMsg(
            msg_ID=msg_id,
            msg_type="SET_COM_PORT",
            msg_subtype="REQUEST",
            payload=port_name,
        )

        self._set_com_port_inflight_msg_id = msg_id
        self._set_com_port_pending[msg_id] = {"port": port_name, "next_action": next_action}
        self.pending_requests[msg_id] = req.msg_type
        self.ipc.send("UIMsg_gui_to_logic", req)

        return msg_id

    def send_start_button_request(self, desired_action: str) -> int | None:
        """GUI -> Logic：START_BUTTON/REQUEST（非阻塞）"""
        if self._start_button_inflight_msg_id is not None:
            return None

        if desired_action not in ("SWITCH_TO_START", "SWITCH_TO_STOP"):
            return None

        # 組装 payload
        if desired_action == "SWITCH_TO_START":
            # 取得當前 bitrate
            bitrate = "115200"
            try:
                combo_val = dpg.get_value("bitrate_combo") if dpg.does_item_exist("bitrate_combo") else "115200"
                if isinstance(combo_val, str) and combo_val.strip().lower() == "custom":
                    if dpg.does_item_exist("bitrate_custom_input"):
                        bitrate = str(dpg.get_value("bitrate_custom_input"))
                else:
                    bitrate = str(combo_val)
            except Exception:
                pass
            # 取得當前 frame format
            frame_format = "8N1"
            try:
                if dpg.does_item_exist("frame_format_combo"):
                    frame_format = dpg.get_value("frame_format_combo")
            except Exception:
                pass
            bypass_crc = bool(getattr(self, "bypass_crc_check_enabled", False))
            try:
                if dpg.does_item_exist("bypass_crc_checkbox"):
                    bypass_crc = bool(dpg.get_value("bypass_crc_checkbox"))
            except Exception:
                pass

            payload = f"SWITCH_TO_START,{bitrate},{frame_format},{1 if bypass_crc else 0}"
        else:
            payload = desired_action

        msg_id = self.next_msg_id()
        req = UIMsg(
            msg_ID=msg_id,
            msg_type="START_BUTTON",
            msg_subtype="REQUEST",
            payload=payload,
        )

        # Mark in-flight and remember action
        self._start_button_inflight_msg_id = msg_id
        self._start_button_pending_action[msg_id] = desired_action
        self.pending_requests[msg_id] = req.msg_type

        # Disable button until response arrives
        try:
            if dpg.does_item_exist("comm_start_button"):
                dpg.disable_item("comm_start_button")
        except Exception:
            pass

        self.ipc.send("UIMsg_gui_to_logic", req)

        return msg_id

    def send_get_com_port_list_request(self):
        """GUI -> Logic：GET_COM_PORT_LIST/REQUEST（非阻塞）"""
        if self._get_com_port_list_inflight_msg_id is not None:
            return  # 還在等上一筆回覆

        msg_id = self.next_msg_id()
        req = UIMsg(
            msg_ID=msg_id,
            msg_type="GET_COM_PORT_LIST",
            msg_subtype="REQUEST",
            payload=None
        )

        self._get_com_port_list_inflight_msg_id = msg_id
        self.pending_requests[msg_id] = req.msg_type
        self.ipc.send("UIMsg_gui_to_logic", req)

    def send_get_log_request(self):
        """GET_LOG 一問一答：只有沒有 in-flight 時才送下一筆，避免 queue 累積"""
        if self._get_log_inflight_msg_id is not None:
            return  # 還在等上一筆回覆

        msg_id = self.next_msg_id()
        req = UIMsg(
            msg_ID=msg_id,
            msg_type="GET_LOG",
            msg_subtype="REQUEST",
            payload=None
        )

        self._get_log_inflight_msg_id = msg_id  # ✅ 標記 in-flight
        self.pending_requests[msg_id] = req.msg_type
        self.ipc.send("UIMsg_gui_to_logic", req)

    def send_clear_protocol_stats_request(self):
        """發送清除通訊統計訊息的命令到 Logic"""
        msg_id = self.next_msg_id()
        req = UIMsg(
            msg_ID=msg_id,
            msg_type="CLEAR_PROTOCOL_STATS",
            msg_subtype="REQUEST",
            payload=None,
        )
        self.ipc.send("UIMsg_gui_to_logic", req)

    def send_get_protocol_stats_request(self):
        """GET_PROTOCOL_STATS 一問一答，每秒請求一次"""
        if self._get_protocol_stats_inflight_msg_id is not None:
            return
        now = time.monotonic()
        if now - self._protocol_stats_last_request_time < self._protocol_stats_interval:
            return
        self._protocol_stats_last_request_time = now

        msg_id = self.next_msg_id()
        req = UIMsg(
            msg_ID=msg_id,
            msg_type="GET_PROTOCOL_STATS",
            msg_subtype="REQUEST",
            payload=None,
        )
        self._get_protocol_stats_inflight_msg_id = msg_id
        self.pending_requests[msg_id] = req.msg_type
        self.ipc.send("UIMsg_gui_to_logic", req)

    def _get_comm_status_color(self):
        """依 CommSts 回傳顯示顏色（RGBA, 0~255）"""
        if self.CommSts == "Started":
            return [0, 255, 0, 255]      # 亮綠
        return [255, 0, 0, 255]          # 紅

    def set_comm_status(self, status: str):
        """更新 CommSts 並同步 UI 顯示（含顏色）"""
        if status not in ("Started", "Stopped"):
            return
        prev = self.CommSts
        self.CommSts = status

        # 通訊狀態切換時重置參數輪詢
        if status != prev:
            self._reset_pr_poll_state()

        if dpg.does_item_exist("comm_status_text"):
            if self.CommSts == "Started":
                dpg.set_value("comm_status_text", f"Status: START")
            else:
                dpg.set_value("comm_status_text", f"Status: STOP")
            dpg.configure_item("comm_status_text", color=self._get_comm_status_color())
        
        # Track current selection so it won't be lost
        if dpg.does_item_exist("comm_port_combo"):
            try:
                self._last_selected_comm_port = dpg.get_value("comm_port_combo")
            except Exception:
                pass

        # ✅ Save buttons: Started disables + fades; Stopped restores
        self.save_manager.update_save_buttons_enabled_state()

        # ✅ Comm setting widgets: Started fades + locks (without disable)
        self.update_comm_settings_items_enabled_state()
            
            
    def toggle_comm_status(self):
        """Start/Stop toggle"""
        new_status = "Started" if self.CommSts == "Stopped" else "Stopped"
        self.set_comm_status(new_status)

    def _reset_pr_poll_state(self):
        """重置參數讀取/寫入輪詢狀態並清除所有列底色"""
        self._pr_poll_current_idx = 1
        # 延遲 1 秒後才開始 polling，給 Arduino boot 時間
        self._pr_poll_last_tick = time.monotonic() + 1.0
        self._pr_read_inflight_msg_id = None
        self._pr_read_inflight_row_idx = None
        self._pr_write_inflight_msg_id = None
        self._pr_write_inflight_row_idx = None
        self._pr_poll_last_success_row = None

        self._param_raw_values.clear()

        # 清除所有列底色
        for idx in range(1, self._param_rw_row_count + 1):
            try:
                dpg.unhighlight_table_cell("param_rw_table", idx, 4)
            except Exception:
                pass

    def _set_param_row_highlight(self, row_idx: int, status: str):
        """設定參數表某列 Read 欄底色 (success=黃色, timeout=暗紅色)"""
        if status == "success":
            color = (80, 80, 0, 100)
        elif status == "timeout":
            color = (120, 20, 20, 150)
        else:
            return
        dpg.highlight_table_cell("param_rw_table", row_idx, 4, color)

    def poll_param_read_values(self):
        """每 500ms 嚴格一問一答輪詢一列的參數 read 值（在 handler 中每幀呼叫）"""
        if self.CommSts != "Started":
            return
        row_count = self._param_rw_row_count
        if row_count <= 0:
            return

        now = time.monotonic()
        if now - self._pr_poll_last_tick < 0.05:
            return

        # 嚴格一問一答：有 in-flight read request 時不送新 request
        if self._pr_read_inflight_msg_id is not None:
            return

        # 找到下一個有有效 addr 的列（跳過空欄位，最多繞一圈）
        idx = self._pr_poll_current_idx
        if idx < 1 or idx > row_count:
            idx = 1
        attempts = 0
        found = False
        while attempts < row_count:
            addr_tag = f"param_addr_{idx}"
            if not dpg.does_item_exist(addr_tag):
                return
            addr = dpg.get_value(addr_tag)
            if addr:
                found = True
                break
            idx = (idx % row_count) + 1
            attempts += 1

        if not found:
            # 所有列都沒有 addr，不發送
            self._pr_poll_current_idx = 1
            return

        # 只有在真正發送請求時才更新時間戳，確保 polling 節奏緊湊
        self._pr_poll_last_tick = now
        # 清除上一次成功讀取列的黃色底色
        if self._pr_poll_last_success_row is not None and self._pr_poll_last_success_row != idx:
            try:
                dpg.unhighlight_table_cell("param_rw_table", self._pr_poll_last_success_row, 4)
            except Exception:
                pass
            self._pr_poll_last_success_row = None

        msg_id = self.next_msg_id()
        type_str = self.ui_event._get_row_type(idx) or "U16"
        req = UIMsg(msg_ID=msg_id, msg_type="GET_PR_VALUE", msg_subtype="REQUEST", payload=f"{addr},{type_str}")
        self._pr_read_inflight_msg_id = msg_id
        self._pr_read_inflight_row_idx = idx
        self.ipc.send("UIMsg_gui_to_logic", req)


    def next_msg_id(self) -> int:
        """產生遞增 msg_ID（GUI 端唯一即可）"""
        self._ui_msg_id_counter += 1
        return self._ui_msg_id_counter

    def poll_ui_ipc_responses(self):
        """✅ GUI 端 polling 收 Logic 回應（不可阻塞）"""
        q = self.ipc.channels.get("UIMsg_logic_to_gui")
        if q is None:
            return

        while True:
            try:
                msg = q.get_nowait()
            except Empty:
                break
            except Exception:
                break

            if not isinstance(msg, UIMsg):
                continue

            # SET_COM_PORT response
            if msg.msg_type == "SET_COM_PORT" and msg.msg_subtype == "RESPONSE":
                pending = self._set_com_port_pending.pop(msg.msg_ID, None)
                if self._set_com_port_inflight_msg_id == msg.msg_ID:
                    self._set_com_port_inflight_msg_id = None
                self.pending_requests.pop(msg.msg_ID, None)

                port = None
                next_action = None
                if isinstance(pending, dict):
                    port = pending.get("port")
                    next_action = pending.get("next_action")

                result = msg.payload
                if result == "SUCCESS":
                    # Chain: after setting COM port successfully, start if requested
                    if next_action == "SWITCH_TO_START":
                        started_id = self.send_start_button_request("SWITCH_TO_START")
                        if started_id is None:
                            # Could not dispatch; re-enable button
                            try:
                                if dpg.does_item_exist("comm_start_button"):
                                    dpg.enable_item("comm_start_button")
                            except Exception:
                                pass
                        continue

                    # No chaining; re-enable the button
                    try:
                        if dpg.does_item_exist("comm_start_button"):
                            dpg.enable_item("comm_start_button")
                    except Exception:
                        pass
                else:
                    # Re-enable the button on failure
                    try:
                        if dpg.does_item_exist("comm_start_button"):
                            dpg.enable_item("comm_start_button")
                    except Exception:
                        pass
                continue

            # START_BUTTON response
            if msg.msg_type == "START_BUTTON" and msg.msg_subtype == "RESPONSE":
                pending_action = self._start_button_pending_action.pop(msg.msg_ID, None)
                if self._start_button_inflight_msg_id == msg.msg_ID:
                    self._start_button_inflight_msg_id = None

                self.pending_requests.pop(msg.msg_ID, None)

                # Re-enable the button regardless of success/fail
                try:
                    if dpg.does_item_exist("comm_start_button"):
                        dpg.enable_item("comm_start_button")
                except Exception:
                    pass

                result = msg.payload
                if result == "SUCCESS" and pending_action in ("SWITCH_TO_START", "SWITCH_TO_STOP"):
                    if pending_action == "SWITCH_TO_START":
                        # 從 STOP 轉到 START 時，清除所有暫存數據點
                        for ds in self.signal_data.values():
                            ds['x'].clear()
                            ds['y'].clear()
                        self.set_comm_status("Started")
                    else:
                        self.set_comm_status("Stopped")
                else:
                    pass
                continue

            # ✅ GET_LOG response / no_log（清掉 in-flight，允許下一次再問）
            if msg.msg_type == "GET_LOG":
                # 無論 RESPONSE/NO_LOG，只要是同一筆 in-flight 都結束它
                if self._get_log_inflight_msg_id == msg.msg_ID:
                    self._get_log_inflight_msg_id = None
                self.pending_requests.pop(msg.msg_ID, None)

                if msg.msg_subtype == "RESPONSE":
                    log_text = msg.payload
                    if isinstance(log_text, str) and log_text.strip():
                        self.log_manager.update_log(log_text)
                # msg_subtype == "NO_LOG"：不做事
                continue
                
            # ✅ GET_COM_PORT_LIST response
            if msg.msg_type == "GET_COM_PORT_LIST":
                if self._get_com_port_list_inflight_msg_id == msg.msg_ID:
                    self._get_com_port_list_inflight_msg_id = None
                self.pending_requests.pop(msg.msg_ID, None)

                if msg.msg_subtype == "RESPONSE":
                    # While Started, keep the COM port selection stable (no updates)
                    if self.CommSts == "Started":
                        continue

                    raw_ports = msg.payload
                    if not isinstance(raw_ports, list):
                        raw_ports = []

                    # 永遠固定前兩個
                    fixed = ["None", "Demo Port"]

                    # payload 從第三個開始：過濾空字串、保留字、重複
                    seen = set(fixed)
                    dynamic_ports = []
                    for p in raw_ports:
                        if not isinstance(p, str):
                            continue
                        p = p.strip()
                        if not p:
                            continue
                        if p in seen:
                            continue
                        seen.add(p)
                        dynamic_ports.append(p)

                    items = fixed + dynamic_ports

                    # Cache for later restore (we clear dropdown while Started)
                    try:
                        self._comm_port_items_cached = items
                    except Exception:
                        pass

                    if dpg.does_item_exist("comm_port_combo"):
                        current = dpg.get_value("comm_port_combo")
                        dpg.configure_item("comm_port_combo", items=items)

                        # 保留原選擇；不在清單就回到 None
                        if current in items:
                            dpg.set_value("comm_port_combo", current)
                        else:
                            dpg.set_value("comm_port_combo", "None")

                        # keep last selection in sync
                        try:
                            self._last_selected_comm_port = dpg.get_value("comm_port_combo")
                        except Exception:
                            pass

                    # No longer logging COM port list details to log_text
                continue

            # GET_PR_VALUE response

            if msg.msg_type == "GET_PR_VALUE" and msg.msg_subtype == "RESPONSE":
                # 嚴格一問一答：只處理當前 inflight 的回應
                if self._pr_read_inflight_msg_id != msg.msg_ID:
                    continue  # 不是當前 inflight 的回應，丟棄
                row_idx = self._pr_read_inflight_row_idx
                self._pr_read_inflight_msg_id = None
                self._pr_read_inflight_row_idx = None

                # 解析 payload: "addr,value"
                if isinstance(msg.payload, str) and "," in msg.payload:
                    _, value = msg.payload.split(",", 1)
                    value = value.strip()
                    if value != "ERROR" and row_idx is not None:
                        read_tag = f"param_read_{row_idx}"
                        if dpg.does_item_exist(read_tag):
                            fmt = self.ui_event._get_row_fmt(row_idx)
                            type_str = self.ui_event._get_row_type(row_idx)
                            display_val = value
                            parsed = self.ui_event._parse_any_int(display_val)
                            if parsed is not None:
                                # ✅ 存下原始位元值，以便後續切換格式時直接轉換，不需重新解析或讀取
                                self._param_raw_values[row_idx] = parsed
                                display_val = UIEvent._format_value(parsed, fmt, type_str)
                            dpg.set_value(read_tag, display_val)
                        self._set_param_row_highlight(row_idx, "success")
                        self._pr_poll_last_success_row = row_idx

                # 嚴格一問一答：收到回應後才推進到下一列
                row_count = self._param_rw_row_count
                if row_count > 0 and row_idx is not None:
                    self._pr_poll_current_idx = (row_idx % row_count) + 1
                continue


            # SET_PR_VALUE response
            if msg.msg_type == "SET_PR_VALUE" and msg.msg_subtype == "RESPONSE":
                if self._pr_write_inflight_msg_id == msg.msg_ID:
                    self._pr_write_inflight_msg_id = None
                    self._pr_write_inflight_row_idx = None
                if msg.payload == "SUCCESS":
                    dprint("[Param] SET_PR_VALUE succeeded.")
                else:
                    dprint(f"[Param] SET_PR_VALUE failed: {msg.payload}")
                continue

            # GET_PR_VALUE timeout (from Logic)
            if msg.msg_type == "GET_PR_VALUE" and msg.msg_subtype == "TIMEOUT":
                if self._pr_read_inflight_msg_id == msg.msg_ID:
                    row_idx = self._pr_read_inflight_row_idx
                    self._pr_read_inflight_msg_id = None
                    self._pr_read_inflight_row_idx = None
                    if row_idx is not None:
                        self._set_param_row_highlight(row_idx, "timeout")
                        row_count = self._param_rw_row_count
                        if row_count > 0:
                            self._pr_poll_current_idx = (row_idx % row_count) + 1
                continue

            # SET_PR_VALUE timeout (from Logic)
            if msg.msg_type == "SET_PR_VALUE" and msg.msg_subtype == "TIMEOUT":
                if self._pr_write_inflight_msg_id == msg.msg_ID:
                    self._pr_write_inflight_msg_id = None
                    self._pr_write_inflight_row_idx = None
                dprint(f"[Param] SET_PR_VALUE timeout msg_ID={msg.msg_ID}")
                continue

            # GET_PROTOCOL_STATS response
            if msg.msg_type == "GET_PROTOCOL_STATS" and msg.msg_subtype == "RESPONSE":
                if self._get_protocol_stats_inflight_msg_id == msg.msg_ID:
                    self._get_protocol_stats_inflight_msg_id = None
                self.pending_requests.pop(msg.msg_ID, None)

                if isinstance(msg.payload, dict):
                    self._last_protocol_stats = msg.payload
                    self._update_protocol_stats_ui()
                continue

    def _update_protocol_stats_ui(self):
        """更新通訊統計訊息 UI 顯示"""
        stats = self._last_protocol_stats
        if stats is None:
            return
        stat_fields = [
            ("stats_total_rx",      stats.get("total_success_received_packet", 0)),
            ("stats_total_tx",      stats.get("total_success_transmitted_packet", 0)),
            ("stats_rx_log",        stats.get("total_success_received_log_packet", 0)),
            ("stats_rx_ds",         stats.get("total_success_received_ds_packet", 0)),
            ("stats_dropped",       stats.get("dropped_packet", 0)),
            ("stats_crc_error",     stats.get("crc_error", 0)),
            ("stats_invalid",       stats.get("invalid_packet", 0)),
            ("stats_ds_seq_dropped", stats.get("ds_sequence_dropped", 0)),
            ("stats_ds_seq_ooo",    stats.get("ds_sequence_out_of_order", 0)),
        ]
        for tag, value in stat_fields:
            if dpg.does_item_exist(tag):
                dpg.set_value(tag, str(value))

    def loop(self):
        #Initialize GUI if not already done
        if not self.gui_initialized:
            self.initialize_gui()
        
        # Render and GUI handler loop
        while dpg.is_dearpygui_running():
            # 60 FPS control
            frame_start = time.perf_counter()
            
            # Handle GUI and rendering
            self.handler()
            
            # 60 FPS control
            frame_elapsed = time.perf_counter() - frame_start
            sleep_time = self.target_interval - frame_elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
        
        # If exit the loop, clean up
        dpg.destroy_context()

    def create_layout(self):
        """UI layout"""
        # Load font
        zh_font = self.load_font()
        
        # Theme initialization
        self.splitter_handler.setup_splitter_themes()
        
        # 折疊式選單主題（加強醒目程度）
        with dpg.theme() as collapsing_header_theme:
            with dpg.theme_component(dpg.mvCollapsingHeader):
                dpg.add_theme_color(dpg.mvThemeCol_Header, (76, 101, 146, 120))
                dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (56, 123, 203, 180))
                dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (0, 120, 255, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255, 255))

        # InputText 背景色主題（log_text / Save_path 共用）
        with dpg.theme() as input_text_bg_theme:
            with dpg.theme_component(dpg.mvInputText):
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (40, 44, 44, 255))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (0, 120, 255, 160))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (0, 120, 255, 200))

        # Save buttons disabled/faded theme (used when CommSts == Started)
        with dpg.theme(tag="save_buttons_disabled_theme") as save_buttons_disabled_theme:
            # Keep widgets enabled; only adjust appearance.
            with dpg.theme_component(dpg.mvAll):
                # Gray text
                dpg.add_theme_color(dpg.mvThemeCol_Text, (120, 120, 120, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, (120, 120, 120, 255))
            with dpg.theme_component(dpg.mvButton):
                # Fade button background (lower alpha)
                dpg.add_theme_color(dpg.mvThemeCol_Button, (70, 70, 70, 80))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (70, 70, 70, 80))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (70, 70, 70, 80))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (255, 255, 255, 40))

        # Theme is bound by tag string ("save_buttons_disabled_theme")

        # Comm settings disabled/faded theme (used when CommSts == Started)
        with dpg.theme(tag="comm_settings_disabled_theme"):
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_Text, (120, 120, 120, 255))
                dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, (120, 120, 120, 255))
            # Combo uses FrameBg colors
            with dpg.theme_component(dpg.mvCombo):
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (40, 44, 44, 160))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (40, 44, 44, 160))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (40, 44, 44, 160))
            with dpg.theme_component(dpg.mvInputText):
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (40, 44, 44, 160))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (40, 44, 44, 160))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (40, 44, 44, 160))
            # InputInt uses FrameBg colors too
            with dpg.theme_component(dpg.mvInputInt):
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (40, 44, 44, 160))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (40, 44, 44, 160))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (40, 44, 44, 160))

        # A no-op theme used to "unfade" without relying on disable/enabled.
        with dpg.theme(tag="comm_settings_normal_theme"):
            pass
        
        # Create horizontal layout with columns and splitter
        with dpg.group(horizontal=True, tag="main_group"):
            # Left column (20% width)
            with dpg.child_window(tag="left_panel", height=-1):  
                # Dynamic width will be set in resize callback
                # 通訊設定折疊式選單
                with dpg.collapsing_header(label="Comm", default_open=False, tag="comm_settings_header"):
                    dpg.bind_item_font("comm_settings_header", zh_font)
                    dpg.bind_item_theme("comm_settings_header", collapsing_header_theme)

                    with dpg.table(
                        header_row=False,
                        borders_innerH=False, borders_innerV=False,
                        borders_outerH=False, borders_outerV=False
                    ):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=120)
                        dpg.add_table_column()
                
                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_text(
                                    f"Status: {self.CommSts}",
                                    tag="comm_status_text",
                                    color=self._get_comm_status_color()
                                )
                                dpg.bind_item_font("comm_status_text", zh_font)
                
                            with dpg.table_cell():
                                dpg.add_button(
                                    label="Start/Stop",
                                    tag="comm_start_button",
                                    width=-1,
                                    callback=self.ui_event.on_comm_start_clicked,
                                )
                                dpg.bind_item_font("comm_start_button", zh_font)

                    # Select COM Port (水平排列)
                    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=120)
                        dpg.add_table_column()
                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_text("Select COM Port", tag="comm_port_label")
                                dpg.bind_item_font("comm_port_label", zh_font)
                            with dpg.table_cell():
                                dpg.add_combo(
                                    items=["None", "Demo Port"],  # ✅ 固定前兩個
                                    default_value="None",
                                    tag="comm_port_combo",
                                    width=-1,
                                    height_mode=dpg.mvComboHeight_Large,
                                    enabled=(self.CommSts == "Stopped"),  # ✅ 初始依狀態決定可不可選
                                )
                                dpg.bind_item_font("comm_port_combo", zh_font)

                                # Locked display (used when CommSts == Started to intercept clicks)
                                dpg.add_input_text(
                                    label="",
                                    tag="comm_port_combo_locked",
                                    default_value="",
                                    readonly=True,
                                    width=-1,
                                    show=False,
                                )
                                dpg.bind_item_font("comm_port_combo_locked", zh_font)

                                # ✅ item handler：近似「點開/關閉下拉式選單」事件
                                with dpg.item_handler_registry(tag="comm_port_combo_handlers"):
                                    dpg.add_item_activated_handler(callback=self.ui_event.on_comm_port_combo_activated)
                                dpg.bind_item_handler_registry("comm_port_combo", "comm_port_combo_handlers")

                    # Select Bitrate (水平排列)
                    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=120)
                        dpg.add_table_column()
                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_text("Select Bitrate", tag="bitrate_label")
                                dpg.bind_item_font("bitrate_label", zh_font)
                            with dpg.table_cell():
                                # Inner layout notes:
                                # - 非 custom：只顯示單欄 layout，讓下拉式選單吃滿寬度（避免出現空白欄位）
                                # - custom：切換成雙欄 layout，左下拉、右輸入

                                with dpg.table(
                                    header_row=False,
                                    borders_innerH=False,
                                    borders_innerV=False,
                                    borders_outerH=False,
                                    borders_outerV=False,
                                    tag="bitrate_inner_table_full",
                                ):
                                    dpg.add_table_column()
                                    with dpg.table_row():
                                        with dpg.table_cell():
                                            with dpg.group(tag="bitrate_full_container"):
                                                dpg.add_combo(
                                                    items=[
                                                        "Custom",
                                                        "9600",
                                                        "19200",
                                                        "38400",
                                                        "57600",
                                                        "115200",
                                                        "230400",
                                                        "460800",
                                                        "576000",
                                                        "768000",
                                                        "921600",
                                                    ],
                                                    default_value="115200",
                                                    tag="bitrate_combo",
                                                    width=-1,
                                                    height_mode=dpg.mvComboHeight_Large,
                                                    callback=self.ui_event.on_bitrate_combo_changed,
                                                )
                                                dpg.bind_item_font("bitrate_combo", zh_font)

                                                # Locked display (used when CommSts == Started to intercept clicks)
                                                dpg.add_input_text(
                                                    label="",
                                                    tag="bitrate_combo_locked",
                                                    default_value="",
                                                    readonly=True,
                                                    width=-1,
                                                    show=False,
                                                )
                                                dpg.bind_item_font("bitrate_combo_locked", zh_font)

                                with dpg.table(
                                    header_row=False,
                                    borders_innerH=False,
                                    borders_innerV=False,
                                    borders_outerH=False,
                                    borders_outerV=False,
                                    tag="bitrate_inner_table_split",
                                    show=False,
                                ):
                                    # Custom 時：下拉 / 輸入框寬度比例 5:5
                                    dpg.add_table_column(init_width_or_weight=0.50)
                                    dpg.add_table_column(init_width_or_weight=0.50)
                                    with dpg.table_row():
                                        with dpg.table_cell():
                                            with dpg.group(tag="bitrate_split_left"):
                                                pass
                                        with dpg.table_cell():
                                            with dpg.group(tag="bitrate_split_right"):
                                                dpg.add_input_int(
                                                    label="",
                                                    tag="bitrate_custom_input",
                                                    default_value=115200,
                                                    width=-1,
                                                    step=0,
                                                    step_fast=0,
                                                    show=False,
                                                )

                    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=120)
                        dpg.add_table_column()
                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_text("Select Frame Format", tag="frame_format_label")
                                dpg.bind_item_font("frame_format_label", zh_font)
                            with dpg.table_cell():
                                dpg.add_combo(
                                    items=["8N1", "8O1", "8E1", "8N2", "8O2", "8E2"],
                                    default_value="8N1",
                                    tag="frame_format_combo",
                                    width=-1,
                                    height_mode=dpg.mvComboHeight_Large,
                                )
                                dpg.bind_item_font("frame_format_combo", zh_font)

                                # Locked display (used when CommSts == Started to intercept clicks)
                                dpg.add_input_text(
                                    label="",
                                    tag="frame_format_combo_locked",
                                    default_value="",
                                    readonly=True,
                                    width=-1,
                                    show=False,
                                )
                                dpg.bind_item_font("frame_format_combo_locked", zh_font)

                    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=120)
                        dpg.add_table_column()
                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_text("Bypass CRC Check", tag="bypass_crc_label")
                                dpg.bind_item_font("bypass_crc_label", zh_font)
                            with dpg.table_cell():
                                dpg.add_checkbox(
                                    label="",
                                    tag="bypass_crc_checkbox",
                                    default_value=False,
                                    callback=self.ui_event.on_bypass_crc_changed,
                                )
                                dpg.bind_item_font("bypass_crc_checkbox", zh_font)

                # 通訊統計訊息折疊式選單
                with dpg.collapsing_header(label="Stats", default_open=False, tag="comm_stats_header"):
                    dpg.bind_item_font("comm_stats_header", zh_font)
                    dpg.bind_item_theme("comm_stats_header", collapsing_header_theme)

                    dpg.add_button(
                        label="Clear",
                        tag="clear_protocol_stats_button",
                        width=-1,
                        callback=self.ui_event.on_clear_protocol_stats_clicked,
                    )
                    dpg.bind_item_font("clear_protocol_stats_button", zh_font)
                    dpg.add_separator()

                    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=120)
                        dpg.add_table_column()

                        for label_text, tag_name in [ 
                            ("RX Total", "stats_total_rx"),
                            ("TX Total", "stats_total_tx"),
                            ("RX Log", "stats_rx_log"),
                            ("RX DS", "stats_rx_ds"),
                            ("Dropped", "stats_dropped"),
                            ("CRC Err", "stats_crc_error"),
                            ("Invalid", "stats_invalid"),
                            ("DS Lost", "stats_ds_seq_dropped"),
                            ("DS OOO", "stats_ds_seq_ooo"),
                        ]:
                            with dpg.table_row():
                                with dpg.table_cell():
                                    t = dpg.add_text(label_text)
                                    dpg.bind_item_font(t, zh_font)
                                with dpg.table_cell():
                                    dpg.add_input_text(
                                        tag=tag_name,
                                        default_value="0",
                                        readonly=True,
                                        width=-1,
                                    )
                                    dpg.bind_item_font(tag_name, zh_font)

                # 暫存設定折疊式選單
                with dpg.collapsing_header(label="Cache", default_open=False, tag="cache_settings_header"):
                    dpg.bind_item_font("cache_settings_header", zh_font)
                    dpg.bind_item_theme("cache_settings_header", collapsing_header_theme)

                    # 最大儲存點數設定 (水平排列，填滿寬度)
                    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=120)  # 標籤欄
                        dpg.add_table_column()  # 控制項欄，自動填滿剩餘寬度
                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_text("Max Points:", tag="max_points_label")
                                dpg.bind_item_font("max_points_label", zh_font)
                            with dpg.table_cell():
                                # 最大點數輸入欄
                                dpg.add_input_int(
                                    label="",
                                    tag="max_points_input",
                                    default_value=100000,
                                    min_value=1000,
                                    max_value=1000000,
                                    min_clamped=True,
                                    max_clamped=True,
                                    step=10000,
                                    callback=self.ui_event.on_max_points_changed,
                                    width=-1,
                                )
                                dpg.bind_item_font("max_points_input", zh_font)

                    # 已儲存點數顯示 + 清除暫存數據點按鈕 (水平排列，填滿寬度)
                    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=120)  # 文字欄
                        dpg.add_table_column()  # 按鈕欄，自動填滿剩餘寬度
                        with dpg.table_row():
                            with dpg.table_cell():
                                # 當前已儲存點數顯示
                                dpg.add_text("Saved: 0", tag="current_points_display")
                                dpg.bind_item_font("current_points_display", zh_font)
                            with dpg.table_cell():
                                # 新增清除暫存數據點按鈕
                                dpg.add_button(
                                    label="Clear Cache",
                                    tag="clear_all_cached_points_button",
                                    callback=self.ui_event.on_clear_all_cached_points,
                                    width=-1,
                                )
                                dpg.bind_item_font("clear_all_cached_points_button", zh_font)

                # 存檔設定折疊式選單
                with dpg.collapsing_header(label="Save", default_open=False, tag="save_settings_header"):
                    dpg.bind_item_font("save_settings_header", zh_font)
                    dpg.bind_item_theme("save_settings_header", collapsing_header_theme)

                    # 存檔路徑按鈕 + 路徑顯示（水平排列）
                    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=60)
                        dpg.add_table_column()
                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_button(
                                    label="Path",
                                    tag="select_save_path_button",
                                    width=-1,
                                    callback=self.ui_event.on_save_path_clicked,
                                )
                                dpg.bind_item_font("select_save_path_button", zh_font)
                            with dpg.table_cell():
                                dpg.add_input_text(
                                    label="",
                                    default_value=self.get_desktop_path(),
                                    tag="Save_path",
                                    width=-1,
                                    readonly=True,
                                )
                                dpg.bind_item_font("Save_path", zh_font)
                                dpg.bind_item_theme("Save_path", input_text_bg_theme)

                    # 存檔名稱（水平排列）
                    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=60)
                        dpg.add_table_column()
                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_button(
                                    label="Name",
                                    tag="save_name_label_button",
                                    width=-1,
                                )
                                with dpg.tooltip(dpg.last_item()):
                                    dpg.add_text("FileName_TimeStamp.csv")
                                    dpg.add_text("FileName_TimeStamp.png")
                                
                                dpg.bind_item_font("save_name_label_button", zh_font)
                                with dpg.theme() as save_name_label_button_theme:
                                    with dpg.theme_component(dpg.mvButton):
                                        dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
                                        dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (0, 0, 0, 0))
                                        dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (0, 0, 0, 0))
                                        dpg.add_theme_color(dpg.mvThemeCol_Border, (0, 0, 0, 0))
                                dpg.bind_item_theme("save_name_label_button", save_name_label_button_theme)
                            with dpg.table_cell():
                                dpg.add_input_text(
                                    label="",
                                    default_value="Save",
                                    tag="save_name_input",
                                    width=-1,
                                )
                                dpg.bind_item_font("save_name_input", zh_font)
                                dpg.bind_item_theme("save_name_input", input_text_bg_theme)
                    dpg.add_separator()

                    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                        dpg.add_table_column()
                        dpg.add_table_column()
                        with dpg.table_row():
                            with dpg.table_cell():
                                # Enabled button
                                dpg.add_button(
                                    label="Save as png",
                                    tag="save_as_png_button",
                                    width=-1,
                                    callback=self.ui_event.on_save_as_png_clicked,
                                )
                                dpg.bind_item_font("save_as_png_button", zh_font)
                            with dpg.table_cell():
                                # Enabled button
                                dpg.add_button(
                                    label="Save as csv",
                                    tag="save_as_csv_button",
                                    width=-1,
                                    callback=self.ui_event.on_save_as_csv_clicked,
                                )
                                dpg.bind_item_font("save_as_csv_button", zh_font)

                    # Apply initial enabled/disabled state (in case CommSts starts as Started)
                    self.save_manager.update_save_buttons_enabled_state()
                # 分隔線
                #dpg.add_separator()
                # 顯示設定折疊式選單
                with dpg.collapsing_header(label="Display", default_open=False, tag="display_settings_header"):
                    dpg.bind_item_font("display_settings_header", zh_font)
                    dpg.bind_item_theme("display_settings_header", collapsing_header_theme)

                    # 畫布數量設定 (水平排列，填滿寬度)
                    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=120)  # 標籤欄
                        dpg.add_table_column()  # 控制項欄，自動填滿剩餘寬度
                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_text("Canvas:", tag="canvas_count_label")
                                dpg.bind_item_font("canvas_count_label", zh_font)
                            with dpg.table_cell():
                                # 下拉式選單 (支援1-16個畫布)
                                dpg.add_combo(items=[str(i) for i in range(1, 17)],
                                             tag="number_combo", default_value="4",
                                             callback=self.ui_event.on_combo_changed, width=-1,
                                             height_mode=dpg.mvComboHeight_Large)
                                dpg.bind_item_font("number_combo", zh_font)

                    # ✅ X軸單位設定（水平排列，填滿寬度）
                    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=120)  # 標籤欄
                        dpg.add_table_column()  # 控制項欄，自動填滿剩餘寬度
                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_text("X Unit:", tag="x_axis_unit_label")
                                dpg.bind_item_font("x_axis_unit_label", zh_font)
                            with dpg.table_cell():
                                dpg.add_combo(
                                    items=["By sequence", "By time"],
                                    tag="x_axis_unit_combo",
                                    default_value="By sequence",
                                    width=-1,
                                    height_mode=dpg.mvComboHeight_Large,
                                    callback=self.ui_event.on_x_axis_unit_changed,
                                )
                                dpg.bind_item_font("x_axis_unit_combo", zh_font)

                    # ✅ by time 才顯示 sample period（秒）
                    with dpg.group(tag="sample_period_group", show=False):
                        with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                            dpg.add_table_column(width_fixed=True, init_width_or_weight=120)  # 標籤欄
                            dpg.add_table_column()  # 控制項欄，自動填滿剩餘寬度
                            with dpg.table_row():
                                with dpg.table_cell():
                                    dpg.add_text("Sample period(s):", tag="sample_period_label")
                                    dpg.bind_item_font("sample_period_label", zh_font)
                                with dpg.table_cell():
                                    dpg.add_input_float(
                                        label="",
                                        tag="sample_period_input",
                                        default_value=0.001,   # ✅ 預設 0.001s (= 1ms)
                                        min_value=0.000001,
                                        min_clamped=True,
                                        step=0.001,
                                        step_fast=0.01,
                                        format="%.6f",
                                        width=-1,
                                        callback=self.ui_event.on_sample_period_changed,
                                    )
                                    dpg.bind_item_font("sample_period_input", zh_font)

                    # Display toggles: align like Comm/Bypass row style (label left, checkbox right)
                    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=120)
                        dpg.add_table_column()

                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_text("X Axis Auto-Fit")
                                dpg.bind_item_font(dpg.last_item(), zh_font)
                            with dpg.table_cell():
                                dpg.add_checkbox(label="", tag="x_axis_autofit_checkbox", default_value=True,
                                               callback=self.ui_event.on_x_axis_autofit_changed)
                                dpg.bind_item_font("x_axis_autofit_checkbox", zh_font)

                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_text("Y Axis Auto-Fit")
                                dpg.bind_item_font(dpg.last_item(), zh_font)
                            with dpg.table_cell():
                                dpg.add_checkbox(label="", tag="y_axis_autofit_checkbox", default_value=True,
                                               callback=self.ui_event.on_y_axis_autofit_changed)
                                dpg.bind_item_font("y_axis_autofit_checkbox", zh_font)

                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_text("Downsample")
                                dpg.bind_item_font(dpg.last_item(), zh_font)
                            with dpg.table_cell():
                                with dpg.group(horizontal=True):
                                    dpg.add_checkbox(label="", tag="adaptive_downsampling_checkbox", default_value=False,
                                                   callback=self.ui_event.on_adaptive_downsampling_changed)
                                    dpg.bind_item_font("adaptive_downsampling_checkbox", zh_font)

                                    dpg.add_text("Rate: 1x", tag="downsampling_rate_text", show=False, color=(255, 80, 80))
                                    dpg.bind_item_font("downsampling_rate_text", zh_font)

                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_text("Legend")
                                dpg.bind_item_font(dpg.last_item(), zh_font)
                            with dpg.table_cell():
                                dpg.add_checkbox(label="", tag="show_legend_checkbox", default_value=True,
                                               callback=self.ui_event.on_show_legend_changed)
                                dpg.bind_item_font("show_legend_checkbox", zh_font)
                # 分隔線
                #dpg.add_separator()
                # 數據源指定（折疊式清單）
                with dpg.collapsing_header(label="Source", tag="data_source_assignment_header", default_open=False):
                    dpg.bind_item_font("data_source_assignment_header", zh_font)
                    dpg.bind_item_theme("data_source_assignment_header", collapsing_header_theme)
                    with dpg.theme() as cursor_drag_btn_theme:
                        with dpg.theme_component(dpg.mvButton):
                            dpg.add_theme_color(dpg.mvThemeCol_Button, (51, 51, 55, 255))         # 一般顏色
                            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (0, 120, 255, 255))    # 懸停顏色
                            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (0, 80, 180, 255))      # 按下顏色

                    # Make DS InputText background transparent so row bg highlight shows through.
                    # (Do NOT change text color; keep default theme text colors.)
                    with dpg.theme(tag="ds_row_input_transparent_theme"):
                        with dpg.theme_component(dpg.mvInputText):
                            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (0, 0, 0, 0))
                            dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (0, 0, 0, 0))
                            dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (0, 0, 0, 0))

                    # Data source assignment table: keep row_background enabled but disable
                    # alternating visuals by making the two row background colors identical.
                    with dpg.theme(tag="ds_assignment_table_no_alt_theme"):
                        with dpg.theme_component(dpg.mvTable):
                            try:
                                dpg.add_theme_color(dpg.mvThemeCol_TableRowBg, (0, 0, 0, 0))
                                dpg.add_theme_color(dpg.mvThemeCol_TableRowBgAlt, (0, 0, 0, 0))
                            except Exception:
                                pass
                    # 新增 Y cursor 拖曳按鈕（放在 DS1 上方）
                    dpg.add_button(label="Y Cursor →", tag="y_cursor_drag_button", width=-1)
                    dpg.bind_item_theme("y_cursor_drag_button", cursor_drag_btn_theme)
                    dpg.bind_item_font("y_cursor_drag_button", "symbol_font")
                    with dpg.drag_payload(parent="y_cursor_drag_button", drag_data="Y_CURSOR", payload_type="any"):
                        dpg.add_text("Drag Y Cursor")
                    # 新增 X cursor 拖曳按鈕（放在 Y Cursor 下方）
                    dpg.add_button(label="X Cursor →", tag="x_cursor_drag_button", width=-1)
                    dpg.bind_item_theme("x_cursor_drag_button", cursor_drag_btn_theme)
                    dpg.bind_item_font("x_cursor_drag_button", "symbol_font")
                    with dpg.drag_payload(parent="x_cursor_drag_button", drag_data="X_CURSOR", payload_type="any"):
                        dpg.add_text("Drag X Cursor")
                    dpg.bind_item_font("data_source_assignment_header", zh_font)
                    # 16個數據源輸入框 - 支援拖拽 (使用表格佈局)
                    with dpg.table(tag="data_source_assignment_table", header_row=False, 
                                  borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False):
                        # Keep table row_background always enabled.
                        try:
                            dpg.configure_item("data_source_assignment_table", row_background=True)
                        except Exception:
                            pass
                        
                        try:
                            dpg.bind_item_theme("data_source_assignment_table", "ds_assignment_table_no_alt_theme")
                        except Exception:
                            pass
                        
                        dpg.add_table_column(label="Data Source", width_fixed=True, init_width_or_weight=40)  # 數據源標示欄
                        dpg.add_table_column(label="Label", width_stretch=True)  # 標籤輸入欄 (自動伸縮)
                        dpg.add_table_column(label="Color", width_fixed=True, init_width_or_weight=25)   # 顏色編輯器欄
                        dpg.add_table_column(label="Drag", width_fixed=True, init_width_or_weight=45)   # 拖拽按鈕欄

                        # Track DS row items for hover highlight updates
                        self._ds_row_items = {}
                        self._ds_row_hovered = None

                        for i in range(1, 17):
                            data_source_name = f"DS{i}"
                            input_tag = f"data_source_input_{i}"
                            drag_button_tag = f"data_source_drag_{i}"
                            color_editor_tag = f"data_source_color_{i}"
                            row_tag = f"data_source_row_{i}"
                            with dpg.table_row(tag=row_tag):
                                # 數據源固定標示
                                cell_label_tag = f"data_source_cell_label_{i}"
                                with dpg.table_cell(tag=cell_label_tag):
                                    data_source_label_tag = f"data_source_label_{i}"
                                    dpg.add_text(f"DS{i}:", tag=data_source_label_tag, color=[200, 200, 200])
                                    dpg.bind_item_font(data_source_label_tag, zh_font)
                                # 文字輸入框
                                cell_input_tag = f"data_source_cell_input_{i}"
                                with dpg.table_cell(tag=cell_input_tag):
                                    dpg.add_input_text(default_value=data_source_name, tag=input_tag, width=-1,
                                                     callback=self.ui_event.on_data_source_label_changed, 
                                                     user_data=i)
                                    dpg.bind_item_font(input_tag, zh_font)
                                # 顏色編輯器
                                cell_color_tag = f"data_source_cell_color_{i}"
                                with dpg.table_cell(tag=cell_color_tag):
                                    default_color = self.data_source_colors[i]
                                    if isinstance(default_color, (list, tuple)) and len(default_color) >= 3:
                                        default_color = tuple(int(c) for c in default_color)
                                    color_editor = dpg.add_color_edit(default_value=default_color, 
                                                     tag=color_editor_tag, 
                                                     width=40, height=20,
                                                     no_alpha=True, no_inputs=True, no_label=True, no_drag_drop=True,
                                                     callback=self.ui_event.on_data_source_color_changed,
                                                     user_data=i)
                                    dpg.configure_item(color_editor, display_type=dpg.mvColorEdit_uint8)
                                # 拖拽按鈕
                                cell_drag_tag = f"data_source_cell_drag_{i}"
                                with dpg.table_cell(tag=cell_drag_tag):
                                    with dpg.theme() as drag_btn_theme:
                                        with dpg.theme_component(dpg.mvButton):
                                            dpg.add_theme_color(dpg.mvThemeCol_Button, (51, 51, 55, 255))         # 一般顏色
                                            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (0, 120, 255, 255))    # 懸停顏色
                                            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (0, 80, 180, 255))      # 按下顏色
                                    dpg.add_button(label="→", tag=drag_button_tag, width=-1)
                                    dpg.bind_item_theme(drag_button_tag, drag_btn_theme)
                                    dpg.bind_item_font(drag_button_tag, "symbol_font")
                                    with dpg.drag_payload(parent=drag_button_tag, 
                                                        drag_data=(i, input_tag), 
                                                        payload_type="any"):
                                        dpg.add_text(f"Drag {data_source_name} to canvas")
                                        dpg.bind_item_font(dpg.last_item(), zh_font)

                                # Cache tags for hover highlight
                                self._ds_row_items[i] = {
                                    "row": row_tag,
                                    "cell_label": cell_label_tag,
                                    "cell_input": cell_input_tag,
                                    "cell_color": cell_color_tag,
                                    "cell_drag": cell_drag_tag,
                                    "label": data_source_label_tag,
                                    "input": input_tag,
                                    "color": color_editor_tag,
                                    "drag": drag_button_tag,
                                }
                # 參數讀寫（折疊式清單）
                with dpg.collapsing_header(label="Param", tag="param_rw_header", default_open=False):
                    dpg.bind_item_font("param_rw_header", zh_font)
                    dpg.bind_item_theme("param_rw_header", collapsing_header_theme)
                    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False,
                                   borders_outerH=False, borders_outerV=False):
                        dpg.add_table_column(width_stretch=True)
                        dpg.add_table_column(width_stretch=True)
                        with dpg.table_row():
                            with dpg.table_cell():
                                dpg.add_button(label="Read Param", tag="read_param_button", width=-1,
                                               callback=self.ui_event.on_read_param_clicked)
                                dpg.bind_item_font("read_param_button", zh_font)
                            with dpg.table_cell():
                                dpg.add_button(label="Export Example", tag="save_example_param_button", width=-1,
                                               callback=self.ui_event.on_save_example_param)
                                dpg.bind_item_font("save_example_param_button", zh_font)

                    dpg.add_separator()

                    # param_rw_table（第一列為模擬 header，置中文字）
                    with dpg.table(tag="param_rw_table", header_row=False,
                                   borders_innerH=True, borders_innerV=True,
                                   borders_outerH=True, borders_outerV=True,
                                   pad_outerX=False, no_pad_innerX=True, row_background=True,
                                   freeze_rows=1):
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=50)
                        dpg.add_table_column(init_width_or_weight=1.0)
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=50)
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=35)
                        dpg.add_table_column(init_width_or_weight=1.0)
                        dpg.add_table_column(init_width_or_weight=1.0)
                        dpg.add_table_column(width_fixed=True, init_width_or_weight=40)
                        # 模擬 header 列（固定第一列，不可被 - 刪除）
                        with dpg.table_row(tag="param_rw_header_row"):
                            for hdr in ("Addr", "Name", "Type", "Fmt", "Read", "Write", "Send"):
                                with dpg.table_cell():
                                    b = dpg.add_button(label=hdr, width=-1)
                                    dpg.bind_item_font(b, zh_font)
                    # 用 highlight_table_cell 將 header 各 cell 底色塗滿（與按鈕同色，保留垂直內邊線）
                    dpg.highlight_table_cell("param_rw_table", 0, 0, (60, 70, 90, 255))
                    dpg.highlight_table_cell("param_rw_table", 0, 1, (60, 70, 90, 255))
                    dpg.highlight_table_cell("param_rw_table", 0, 2, (60, 70, 90, 255))
                    dpg.highlight_table_cell("param_rw_table", 0, 3, (60, 70, 90, 255))
                    dpg.highlight_table_cell("param_rw_table", 0, 4, (60, 70, 90, 255))
                    dpg.highlight_table_cell("param_rw_table", 0, 5, (60, 70, 90, 255))
                    dpg.highlight_table_cell("param_rw_table", 0, 6, (60, 70, 90, 255))

                    # param_rw_table 主題
                    with dpg.theme(tag="param_rw_table_theme"):
                        with dpg.theme_component(dpg.mvAll):
                            dpg.add_theme_style(dpg.mvStyleVar_CellPadding, 0, 1)
                            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 0, 0)
                        with dpg.theme_component(dpg.mvTable):
                            dpg.add_theme_color(dpg.mvThemeCol_TableBorderLight, (90, 90, 95, 255))
                            dpg.add_theme_color(dpg.mvThemeCol_TableBorderStrong, (90, 90, 95, 255))
                            dpg.add_theme_color(dpg.mvThemeCol_Border, (90, 90, 95, 255))
                            dpg.add_theme_color(dpg.mvThemeCol_BorderShadow, (0, 0, 0, 0))
                        with dpg.theme_component(dpg.mvButton):
                            dpg.add_theme_color(dpg.mvThemeCol_Button, (60, 70, 90, 255))
                            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (60, 70, 90, 255))
                            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (60, 70, 90, 255))
                            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 0, 4)
                        with dpg.theme_component(dpg.mvInputText):
                            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (0, 0, 0, 0))
                            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 2, 2)
                    dpg.bind_item_theme("param_rw_table", "param_rw_table_theme")

                    # Send 按鈕預設風格主題（覆蓋表格的 mvButton 主題）
                    with dpg.theme(tag="param_send_default_theme"):
                        with dpg.theme_component(dpg.mvButton):
                            dpg.add_theme_color(dpg.mvThemeCol_Button, (51, 51, 55, 255))
                            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (66, 150, 250, 102))
                            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (66, 150, 250, 171))
                            dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 4, 3)

                # 分隔線
                dpg.add_separator()
                
                # Log標籤和自動滾動控制 (水平排列)
                with dpg.group(horizontal=True):
                    dpg.add_text("Log:", tag="log_label")
                    dpg.bind_item_font("log_label", zh_font)
                    
                    dpg.add_checkbox(label="Auto Scroll", tag="log_auto_scroll_checkbox", default_value=True, 
                                   callback=self.ui_event.on_auto_scroll_changed)
                    dpg.bind_item_font("log_auto_scroll_checkbox", zh_font)
                    dpg.add_button(label=" Clear log ", tag="clear_log_button",
                                   callback=self.ui_event.on_clear_log_clicked,
                                   width=-1)
                    dpg.bind_item_font("clear_log_button", zh_font)
                
                # Log文字顯示 (使用autoscroll.py的成功公式)
                with dpg.child_window(tag="log_text_container"):
                    dpg.add_input_text(
                        tag="log_text",
                        multiline=True,
                        readonly=True,
                        tracked=True,
                        track_offset=1,
                        width=-1,
                        height=-1
                    )
                    dpg.bind_item_theme("log_text", input_text_bg_theme)
                    # 使用預設字體，不綁定中文字體
            
            # Visual splitter border
            with dpg.child_window(tag="visual_splitter", width=3, height=-1, no_scrollbar=True, border=False):
                dpg.add_button(label="", tag="visual_splitter_button", width=-1, height=-1)
                # ✨ 使用 splitter_handler 中的主題
                dpg.bind_item_theme("visual_splitter_button", self.splitter_handler.splitter_theme)
            
            # Right column (80% width)  
            with dpg.child_window(tag="right_panel", height=-1):
                # 控制按鈕區域
                with dpg.group(tag="control_buttons", horizontal=True):
                    dpg.add_button(label="Clear Sources", tag="clear_all_data_sources_button", 
                                 callback=self.ui_event.on_clear_all_data_sources)
                    dpg.bind_item_font("clear_all_data_sources_button", zh_font)
                
                    dpg.add_button(label="Clear Cursors", tag="clear_all_cursors_button", 
                        callback=self.ui_event.on_clear_all_cursors)
                    dpg.bind_item_font("clear_all_cursors_button", zh_font)
                
                # FPS 顯示區域 (使用絕對定位到右上角)
                with dpg.group(tag="fps_container"):
                    dpg.add_text("FPS: 0.0", tag="fps_display", pos=[0, 0])
                    dpg.bind_item_font("fps_display", "fps_font")
                
                # 數據源顯示區域 (使用 group)
                with dpg.group(tag="plots_panel"):
                    self.create_dynamic_subplots()

        # Modal window for CSV export progress
        with dpg.window(
            tag="csv_export_modal",
            label="CSV exporting...",
            modal=True,
            show=False,
            no_close=True,
            no_resize=True,
            no_move=True,
            width=420,
            height=140,
        ):
            dpg.add_text("Saving cached data sources to CSV...")
            dpg.bind_item_font(dpg.last_item(), zh_font)
            dpg.add_spacer(height=6)
            dpg.add_progress_bar(default_value=0.0, tag="csv_export_progress", width=-1)
            dpg.add_text("Preparing...", tag="csv_export_status_text")
            dpg.bind_item_font("csv_export_status_text", zh_font)

    def initialize_gui(self):
        if self.gui_initialized:
            return
            
            
        # DPI 意識設定（以相容性為優先，System-DPI aware）
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        
        # Initialize Dear PyGui context and viewport
        dpg.create_context()
        
        DPG_DEV_MODE = False
        
        if DPG_DEV_MODE == True:
            dpg.show_documentation()
            dpg.show_style_editor()
            dpg.show_debug()
            dpg.show_about()
            dpg.show_metrics()
            dpg.show_font_manager()
            dpg.show_item_registry()
        
        # 計算螢幕置中位置
        try:
            _rect = ctypes.wintypes.RECT()
            ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(_rect), 0)
            _sw, _sh = _rect.right - _rect.left, _rect.bottom - _rect.top
            _init_x = _rect.left + (_sw - 800) // 2
            _init_y = _rect.top + (_sh - 600) // 2
        except Exception:
            _init_x, _init_y = 100, 100
        
        dpg.create_viewport(title='lwSCOPE', width=800, height=600, min_width=800, min_height=600, decorated=(not USE_CUSTOM_WINDOW),
                            x_pos=_init_x, y_pos=_init_y)

        # 設置全域滑鼠處理器
        with dpg.handler_registry():
            dpg.add_mouse_release_handler(callback=self.ui_event.global_mouse_release_handler)
        
        '''
        # Create main window
        with dpg.window(
            label="", no_title_bar=True, no_move=True, no_scroll_with_mouse=False, menubar=False, no_resize=True, no_scrollbar=False,
            pos=(0, 0), width=-1, height=-1, tag="main_window"
        ):
            # Create layout
            self.create_layout()
        '''
        if custom_window_instance is not None:
            custom_window_instance.initialize_gui(title_text="lwSCOPE")
            custom_window_instance.register_create_layout(self.create_layout)
            custom_window_instance.create_layout()
        else:
            # Fallback: 不使用 CustomWindow 時，以原生 DPG 視窗承載主版面
            with dpg.window(
                label="", no_title_bar=True, no_move=True, no_scroll_with_mouse=False,
                menubar=False, no_resize=True, no_scrollbar=False,
                pos=(0, 0), width=-1, height=-1, tag="main_window"
            ):
                self.create_layout()

        # 綁定較大字體到 title bar 文字
        if dpg.does_item_exist("title_text_btn") and dpg.does_item_exist("title_font"):
            dpg.bind_item_font("title_text_btn", "title_font")
        
        # Set initial panel sizes (考慮視覺分隔條寬度)
        initial_width = 800  # Initial viewport width
        visual_splitter_width = 3  # 視覺分隔條寬度
        spacer_width = 34  # 預留的間隔寬度 (2*resize_bar_w + 2*WindowPadding = 16+16, 再多2像素容錯)
        available_width = initial_width - visual_splitter_width - spacer_width
        left_width = int(available_width * self.left_panel_ratio)
        right_width = int(available_width * (1.0 - self.left_panel_ratio))

        dpg.set_item_width("left_panel", left_width)
        dpg.set_item_width("right_panel", right_width)

        # 設定初始FPS位置到右側面板的右上角
        fps_x = right_width - 80  # 距離右側面板右邊緣80像素
        fps_y = 5  # 距離頂部5像素
        dpg.set_item_pos("fps_display", [fps_x, fps_y])
        
        # Setup
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("main_window", True)
        dpg.set_viewport_resize_callback(self.ui_event.resize_window_callback)
        
        #self.apply_dark_title_bar()
        
        #  Maximize viewport if supported
        #if hasattr(dpg, "maximize_viewport"):
        #    dpg.maximize_viewport()
        
        self.set_comm_status(self.CommSts)
        
        self.gui_initialized = True

    def handler(self):
        self.counter += 1
        
        if self.gui_initialized:
            # ========== Frame Profiling ==========
            _prof = getattr(self, '_frame_profiler', None)
            if _prof is None:
                self._frame_profiler = {
                    'enabled': True,
                    'interval': 300,   # 每 300 幀輸出一次
                    'frame_count': 0,
                    'accum': {},       # {label: total_seconds}
                }
                _prof = self._frame_profiler
            
            _do_prof = _prof['enabled']
            if _do_prof:
                _prof['frame_count'] += 1
                _t0 = time.perf_counter()
                def _mark(label):
                    nonlocal _t0
                    _t1 = time.perf_counter()
                    _prof['accum'][label] = _prof['accum'].get(label, 0.0) + (_t1 - _t0)
                    _t0 = _t1
            else:
                def _mark(label): pass

            # Custom window handler
            if custom_window_instance is not None:
                custom_window_instance.handler()
            _mark('custom_window')
            
            # UI splitter handler update and check in each frame
            self.splitter_handler.update_splitter_position()
            _mark('splitter')
            
            # ✅ 每幀送 GET_LOG 請求
            self.send_get_log_request()
            _mark('send_log_req')

            # ✅ 定期請求通訊統計
            self.send_get_protocol_stats_request()
            
            # ✅ 每幀 polling UI IPC 回應（不阻塞）
            self.poll_ui_ipc_responses()
            _mark('poll_ipc')

            # ✅ 每 200ms 輪詢一列參數 read 值
            self.poll_param_read_values()
            _mark('poll_pr')
            
            # ✅ 動態加速消費 queue，根據 queue 長度自動決定本幀 pop 幾筆
            q = self.ipc.channels.get("HSDataSource_logic_to_gui")
            if q is not None:
                qsize = 0
                try:
                    qsize = q.qsize()
                except Exception:
                    pass
                # 根據 queue 長度動態決定本幀最多消耗幾筆（平滑顯示+防爆）
                max_consume = max(2, min(qsize // 10, 2000)) if qsize > 0 else 2
                for _ in range(max_consume):
                    try:
                        data = q.get_nowait()
                    except Empty:
                        break

                    if isinstance(data, HSDataSource):
                        # ✅ X 值來源切換
                        if self.x_axis_unit_mode == "By time":
                            x_val = data.sequence_num * float(self.sample_period_s)  # 單位：second
                        else:
                            x_val = data.sequence_num

                        for i in range(1, 17):
                            ds_name = f"ds{i}"
                            signal_key = f"HSDataSource_{i}"
                            value = data.signals.get(signal_key, 0.0)
                            self.signal_data[ds_name]["x"].append(x_val)
                            self.signal_data[ds_name]["y"].append(value)
            # 動態調整 log_text_container 高度
            self.log_manager.update_log_container_height()
            _mark('log_height')
            
            # 每幀統一裁剪
            self.trim_data_to_max_points()
            _mark('trim')
            
            # Update current points display
            self.update_current_points_display()
            _mark('points_display')
            
            # Update series with buffered data
            self.update_series_with_buffer()
            _mark('update_series')

            # DS assignment hover highlight (row highlight when drag button is hovered)
            self.update_data_source_assignment_hover_highlight()
            _mark('ds_hover')
            
            # Axis auto fit handling
            self.auto_fit.update_axis_fitting()
            _mark('axis_autofit')
            
            # Update all cursors
            self.cursor_handler.update_all_cursors_every_frame()
            _mark('cursors')

            # CSV export (chunked) + progress update in frame loop
            self.save_manager.process_csv_export_every_frame()
            _mark('csv_export')

            # Keep Save button state/theme consistent (guards against accidental overrides)
            try:
                self.save_manager.update_save_buttons_enabled_state()
            except Exception:
                pass
            _mark('save_btn_state')
            
            # Calculate FPS value and update
            self.fps_tracker.calculate_and_update()
            _mark('fps_calc')
            
            # Render GUI
            dpg.render_dearpygui_frame()
            _mark('render_frame')

            # 在 render 之後設定游標，避免被 DPG 渲染覆蓋導致閃爍
            if custom_window_instance is not None:
                custom_window_instance.post_render()
            _mark('post_render_cursor')

            # Execute queued screenshot capture after rendering
            try:
                self.save_manager.process_pending_png_capture()
            except Exception:
                pass

            # ...existing code...


    def trim_data_to_max_points(self):
        """批次攤銷裁剪：允許資料量成長至 max_data_points * 1.2，
        超過門檻時一次裁剪回 max_data_points，攤銷後每幀近似 O(0)。
        """
        max_pts = self.max_data_points
        threshold = max_pts + max(max_pts // 5, 1)  # +20% 容忍區
        for i in range(1, self.max_data_sources + 1):
            ds = self.signal_data.get(f'ds{i}')
            if ds:
                cur_len = len(ds['x'])
                if cur_len > threshold:
                    excess = cur_len - max_pts
                    del ds['x'][:excess]
                    del ds['y'][:excess]

    def update_current_points_display(self):
        """更新當前已儲存點數顯示 (data source version)"""
        if self.current_plot_count > 0:
            # 獲取第一個活躍數據源的數據點數作為代表
            data_source = f'ds1'
            if data_source in self.signal_data:
                current_points = min(len(self.signal_data[data_source]['x']), self.max_data_points)
                display_text = f"Saved: {current_points}"
                if dpg.does_item_exist("current_points_display"):
                    dpg.set_value("current_points_display", display_text)

    def update_data_source_assignment_hover_highlight(self):
        """Hover on DS drag button: adjust row widgets (best-effort)."""
        items = getattr(self, "_ds_row_items", None)
        if not isinstance(items, dict) or not items:
            return

        hovered_ds = None
        try:
            for ds_id, t in items.items():
                drag_tag = t.get("drag")
                if drag_tag and dpg.does_item_exist(drag_tag) and dpg.is_item_hovered(drag_tag):
                    hovered_ds = ds_id
                    break
        except Exception:
            return

        if hovered_ds == getattr(self, "_ds_row_hovered", None):
            return

        table_tag = "data_source_assignment_table"

        # Clear previous highlight
        prev = getattr(self, "_ds_row_hovered", None)
        if isinstance(prev, int) and prev in items:
            t = items[prev]
            # Restore row background via table-row API (preferred)
            try:
                if hasattr(dpg, "set_table_row_color") and dpg.does_item_exist(table_tag):
                    row_index = int(prev) - 1
                    if row_index >= 0:
                        dpg.set_table_row_color(table_tag, row_index, [0, 0, 0, 0])
            except Exception:
                pass

            # Fallback: restore row and cell bg_color via configure_item (best-effort)
            row_tag = t.get("row")
            if row_tag and dpg.does_item_exist(row_tag):
                try:
                    dpg.configure_item(row_tag, bg_color=(0, 0, 0, 0))
                except Exception:
                    pass

            for key in ("cell_label", "cell_input", "cell_color", "cell_drag"):
                cell_tag = t.get(key)
                if cell_tag and dpg.does_item_exist(cell_tag):
                    try:
                        dpg.configure_item(cell_tag, bg_color=(0, 0, 0, 0))
                    except Exception:
                        pass
            # Restore InputText theme (back to default look)
            input_tag = t.get("input")
            if input_tag and dpg.does_item_exist(input_tag):
                try:
                    dpg.bind_item_theme(input_tag, 0)
                except Exception:
                    pass

        self._ds_row_hovered = hovered_ds

        # Apply new highlight
        if isinstance(hovered_ds, int) and hovered_ds in items:
            t = items[hovered_ds]

            # On hover: set the table row color using the dedicated API (demo-style).
            try:
                if hasattr(dpg, "set_table_row_color") and dpg.does_item_exist(table_tag):
                    row_index = int(hovered_ds) - 1
                    if row_index >= 0:
                        dpg.set_table_row_color(table_tag, row_index, [76, 96, 129, 120])
            except Exception:
                pass

            # Fallback: also try to adjust row/cell bg_color directly (best-effort).
            row_tag = t.get("row")
            if row_tag and dpg.does_item_exist(row_tag):
                try:
                    dpg.configure_item(row_tag, bg_color=(150, 170, 190, 255))
                except Exception:
                    pass
            '''
            for key in ("cell_label", "cell_input", "cell_color", "cell_drag"):
                cell_tag = t.get(key)
                if cell_tag and dpg.does_item_exist(cell_tag):
                    try:
                        dpg.configure_item(cell_tag, bg_color=(150, 170, 190, 255))
                    except Exception:
                        pass
            '''

            # On hover: make InputText background transparent so row bg shows through.
            input_tag = t.get("input")
            if input_tag and dpg.does_item_exist(input_tag):
                try:
                    dpg.bind_item_theme(input_tag, "ds_row_input_transparent_theme")
                except Exception:
                    pass

    def assign_data_source_to_plot(self, plot_id, data_source_id, data_source_name):
        """將數據源指定給plot（創建series時初始化緩衝）"""
        if plot_id not in self.plot_data_source_assignments:
            self.plot_data_source_assignments[plot_id] = []
        
        if data_source_id not in self.plot_data_source_assignments[plot_id]:
            self.plot_data_source_assignments[plot_id].append(data_source_id)
            
            y_axis_tag = f"y_axis{plot_id}"
            series_tag = f"signal_series{plot_id}_{data_source_id}"
            
            if dpg.does_item_exist(y_axis_tag):
                # 創建空 series
                dpg.add_stair_series([], [], label=data_source_name, tag=series_tag, parent=y_axis_tag)
                
                # ✨ 在緩衝中初始化空列表
                self.series_data_buffer[series_tag] = [[], []]
                
                # 應用顏色
                if data_source_id in self.data_source_colors:
                    color_int = self.data_source_colors[data_source_id]
                    with dpg.theme() as line_theme:
                        with dpg.theme_component(dpg.mvStairSeries):
                            dpg.add_theme_color(dpg.mvPlotCol_Line, color_int, category=dpg.mvThemeCat_Plots)
                    dpg.bind_item_theme(series_tag, line_theme)

    def get_assigned_data_sources_for_plot(self, plot_id):
        """獲取指定給plot的數據源ID列表"""
        return self.plot_data_source_assignments.get(plot_id, [])

    def get_assigned_data_source_for_plot(self, plot_id):
        """獲取指定給plot的第一個數據源ID（為了向後相容）"""
        data_sources = self.get_assigned_data_sources_for_plot(plot_id)
        return data_sources[0] if data_sources else None

    def clear_all_data_source_assignments(self):
        """清除所有數據源指定"""
        # 清除所有series（主題會自動清理，因為我們使用匿名主題）
        for plot_id in range(1, self.current_plot_count + 1):
            assigned_data_sources = self.get_assigned_data_sources_for_plot(plot_id)
            for data_source_id in assigned_data_sources:
                series_tag = f"signal_series{plot_id}_{data_source_id}"
                if dpg.does_item_exist(series_tag):
                    dpg.delete_item(series_tag)
        # 清除數據源指定記錄
        self.plot_data_source_assignments.clear()
        # 重置所有plot的X軸和Y軸範圍
        for plot_id in range(1, self.current_plot_count + 1):
            y_axis_tag = f"y_axis{plot_id}"
            if dpg.does_item_exist(y_axis_tag):
                dpg.set_axis_limits_auto(y_axis_tag)
            x_axis_tag = f"x_axis{plot_id}"
            if dpg.does_item_exist(x_axis_tag):
                dpg.set_axis_limits_auto(x_axis_tag)
        

    def _on_plot_double_click_autofit_x(self, sender, app_data, user_data):
        """Double-click callback: run X-axis autofit once.

        IMPORTANT: Do not modify the X-axis autofit feature itself; we only invoke
        the same function that is normally called per-frame in the render loop.
        """
        # Flag-based pulse: keep it active for 2 frames, then auto-clear.
        self._x_autofit_pulse_once = True
        self._x_autofit_pulse_frames_left = 2

    def _bind_plot_double_click_handlers(self):
        """Bind double-click handlers to all plots (including the time-axis plot)."""
        total_plots = int(getattr(self, "current_plot_count", 0) or 0) + 1
        for plot_id in range(1, total_plots + 1):
            plot_tag = f"plot{plot_id}"
            if not dpg.does_item_exist(plot_tag):
                continue

            handler_tag = f"plot_double_click_handler_{plot_id}"
            if dpg.does_item_exist(handler_tag):
                try:
                    dpg.delete_item(handler_tag)
                except Exception:
                    pass

            with dpg.item_handler_registry(tag=handler_tag):
                dpg.add_item_double_clicked_handler(
                    callback=self._on_plot_double_click_autofit_x,
                    user_data=plot_id,
                )

            try:
                dpg.bind_item_handler_registry(plot_tag, handler_tag)
            except Exception:
                pass

    def create_dynamic_subplots(self):
        """動態創建subplot，根據當前通道數"""
        # 刪除舊的subplot（如果存在）
        if dpg.does_item_exist("main_subplots"):
            dpg.delete_item("main_subplots")
        
        # 從查表中獲取row_ratios配置（畫布數）
        if self.current_plot_count in self.row_ratios_table:
            row_ratios = self.row_ratios_table[self.current_plot_count]
        else:
            # 備用方案：如果查表中沒有對應配置，使用動態計算
            total_plots = self.current_plot_count + 1
            row_ratios = [4.0] * self.current_plot_count + [1.0]

        total_plots = self.current_plot_count + 1
        
        # 在 plots_panel 中創建新的subplots，填滿數據源顯示區域
        with dpg.subplots(total_plots, 1, label="", width=-1, height=-1, tag="main_subplots", 
                        no_resize=True, no_title=True, link_all_x=True, 
                        row_ratios=row_ratios, parent="plots_panel"):
            # 創建數據源plots（Plot1到PlotN）- 支援拖拽放置
            for i in range(1, self.current_plot_count + 1):
                # 所有數據源plot都不顯示X軸標籤，只有時間軸plot才顯示
                with dpg.plot(tag=f"plot{i}", horizontal_mod=dpg.mvKey_ModCtrl, no_title=True, no_menus=True,
                              drop_callback=self.ui_event.on_plot_drop, payload_type="any", anti_aliased = False):
                    # 添加plot legend with tag
                    dpg.add_plot_legend(tag=f"legend{i}")
                    dpg.add_plot_axis(dpg.mvXAxis, label="", tag=f"x_axis{i}", 
                                    no_tick_labels=True, no_tick_marks=True, no_highlight=True)
                    with dpg.plot_axis(dpg.mvYAxis, label="", tag=f"y_axis{i}"):
                        pass
            
            # 創建時間軸plot（最後一個）
            time_plot_num = self.current_plot_count + 1
            with dpg.plot(tag=f"plot{time_plot_num}", no_title=True, no_menus=True, no_mouse_pos=True):
                # ✅ label 依模式決定
                x_label = "Time (s)" if self.x_axis_unit_mode == "By time" else "Sequence"
                dpg.add_plot_axis(dpg.mvXAxis, label=x_label, tag=f"x_axis{time_plot_num}", no_tick_marks=True)
                with dpg.plot_axis(dpg.mvYAxis, label="", tag=f"y_axis{time_plot_num}",
                                 no_tick_labels=True, no_tick_marks=True, no_highlight=True,
                                 no_gridlines=True, no_menus=True):
                    pass
        
        self.update_x_axis_labels()

        # Bind per-plot double-click => one-shot X-axis autofit.
        self._bind_plot_double_click_handlers()
        
        # 應用主題
        self.apply_plot_themes()
        
        # 同步legend顯示狀態與checkbox狀態
        if dpg.does_item_exist("show_legend_checkbox"):
            is_enabled = dpg.get_value("show_legend_checkbox")
            for i in range(1, self.current_plot_count + 1):
                legend_tag = f"legend{i}"
                if dpg.does_item_exist(legend_tag):
                    dpg.configure_item(legend_tag, show=is_enabled)

    def apply_plot_themes(self):
        """應用plot主題"""
        
        # Create shared themes for all plots
        with dpg.theme() as plots_theme:
            with dpg.theme_component(dpg.mvPlot):
                dpg.add_theme_color(dpg.mvPlotCol_PlotBg, (31, 31, 31, 255), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_PlotBorder, (31, 31, 31, 255), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_AxisGrid, (134, 134, 134, 255), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_Crosshairs, (255, 255, 255, 255), category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvPlotCol_AxisText, (255, 255, 255, 255), category=dpg.mvThemeCat_Plots)
        
        # 應用主題到所有數據plots
        for i in range(1, self.current_plot_count + 1):
            if dpg.does_item_exist(f"plot{i}"):
                dpg.bind_item_theme(f"plot{i}", plots_theme)
        
        # 注意：series 的顏色現在在創建時直接應用，不在這裡處理
        
        # Create special theme for time axis plot
        with dpg.theme() as time_plot_theme:
            with dpg.theme_component(dpg.mvPlot):
                dpg.add_theme_color(dpg.mvPlotCol_PlotBg, (51, 51, 55, 0), category=dpg.mvThemeCat_Plots)  # 半透明背景
                dpg.add_theme_color(dpg.mvPlotCol_PlotBorder, (51, 51, 55, 0), category=dpg.mvThemeCat_Plots)  # 半透明邊框
                dpg.add_theme_color(dpg.mvPlotCol_AxisGrid, (51, 51, 55, 0), category=dpg.mvThemeCat_Plots)  # 半透明網格
                dpg.add_theme_color(dpg.mvPlotCol_Crosshairs, (51, 51, 55, 0), category=dpg.mvThemeCat_Plots)  # 半透明十字線
                dpg.add_theme_color(dpg.mvPlotCol_AxisText, (255, 255, 255, 255), category=dpg.mvThemeCat_Plots)
        
        # 應用到時間軸plot
        time_plot_num = self.current_plot_count + 1
        if dpg.does_item_exist(f"plot{time_plot_num}"):
            dpg.bind_item_theme(f"plot{time_plot_num}", time_plot_theme)

    def load_font(self):
        """載入字體並返回字體物件"""
        zh_font_path = os.path.join(_BASE_DIR, "font", "NotoSansTC-Regular.ttf")
        symbol_font_path = os.path.join(_BASE_DIR, "font", "seguiemj.ttf")

        with dpg.font_registry():
            # 主要UI字體
            try:
                if os.path.exists(zh_font_path):
                    with dpg.font(zh_font_path, 18, tag="zh_font") as zh_font:
                        dpg.add_font_range_hint(dpg.mvFontRangeHint_Default)
                        dpg.add_font_range_hint(dpg.mvFontRangeHint_Chinese_Full)
                else:
                    zh_font = dpg.add_font_default(tag="zh_font")
            except Exception:
                zh_font = dpg.add_font_default(tag="zh_font")

            # FPS顯示專用字體
            try:
                if os.path.exists(zh_font_path):
                    with dpg.font(zh_font_path, 16, tag="fps_font") as _fps_font:
                        dpg.add_font_range_hint(dpg.mvFontRangeHint_Default)
                else:
                    dpg.add_font_default(tag="fps_font")
            except Exception:
                dpg.add_font_default(tag="fps_font")

            # Special symbol font
            try:
                if os.path.exists(symbol_font_path):
                    with dpg.font(symbol_font_path, 18, tag="symbol_font") as _symbol_font:
                        dpg.add_font_range_hint(dpg.mvFontRangeHint_Default)
                        dpg.add_font_range(0x2190, 0x21FF)  # 箭頭符號區
                        dpg.add_font_range(0x27A0, 0x27BF)  # 補充箭頭符號區
                else:
                    dpg.add_font_default(tag="symbol_font")
            except Exception:
                dpg.add_font_default(tag="symbol_font")

            # Window title bar 專用字體（較大）
            try:
                if os.path.exists(zh_font_path):
                    with dpg.font(zh_font_path, 22, tag="title_font") as _title_font:
                        dpg.add_font_range_hint(dpg.mvFontRangeHint_Default)
                        dpg.add_font_range_hint(dpg.mvFontRangeHint_Chinese_Full)
                else:
                    dpg.add_font_default(tag="title_font")
            except Exception:
                dpg.add_font_default(tag="title_font")

            return zh_font

    def update_series_with_buffer(self):
        is_time_mode = getattr(self, "x_axis_unit_mode", "By sequence") == "By time"
        need_y_limits = self.auto_fit.y_axis_autofit_enabled
        MAX_DISPLAY_PTS = 2000  # 超過此數自動 min-max 降取樣送 DPG

        def _normalize_xlim(x_min_vis, x_max_vis):
            if x_min_vis is None or x_max_vis is None:
                return None, None
            try:
                x_min_vis = float(x_min_vis)
                x_max_vis = float(x_max_vis)
            except Exception:
                return None, None
            if x_max_vis < x_min_vis:
                x_min_vis, x_max_vis = x_max_vis, x_min_vis
            if is_time_mode:
                return round(x_min_vis, 6), round(x_max_vis, 6)
            return int(round(x_min_vis)), int(round(x_max_vis))

        ado = self.adaptive_display_optimization
        downsample_active = ado.adaptive_downsampling_enabled and ado.current_downsample_rate > 1
        downsample_rate = ado.current_downsample_rate if downsample_active else 1
        extra_pts = getattr(self, "visible_range_extra_points", 5) or 0

        # ===== Per-frame cache: DS data (跨 plot 復用) =====
        _ds_cache = {}  # {data_source: (xs, ys, n)}

        _max_pts = self.max_data_points

        def _get_ds(data_source):
            cached = _ds_cache.get(data_source)
            if cached is not None:
                return cached
            ds_data = self.signal_data.get(data_source)
            if not ds_data:
                _ds_cache[data_source] = (None, None, 0)
                return _ds_cache[data_source]
            raw_x = ds_data['x']
            raw_y = ds_data['y']
            n = min(len(raw_x), len(raw_y))
            if n <= 0:
                _ds_cache[data_source] = (None, None, 0)
                return _ds_cache[data_source]
            # 只取最後 max_data_points 筆，多出的 20% buffer 不顯示
            offset = max(n - _max_pts, 0)
            if downsample_active:
                xs = raw_x[offset:n:downsample_rate]
                ys = raw_y[offset:n:downsample_rate]
                n = len(xs)
            else:
                if offset == 0 and len(raw_x) == len(raw_y):
                    xs = raw_x
                    ys = raw_y
                else:
                    # memoryview: O(1) 零複製視圖，避免整個 array.array 複製
                    xs = memoryview(raw_x)[offset:n]
                    ys = memoryview(raw_y)[offset:n]
                    n = len(xs)
            _ds_cache[data_source] = (xs, ys, n)
            return _ds_cache[data_source]

        # ===== Per-frame Y limits =====
        frame_y_limits = {}  # {plot_id: (y_min, y_max)}
        # Y limits cache per series (reuse when cache_hit)
        _ylim_cache = getattr(self, '_series_ylim_cache', {})

        for plot_id in range(1, self.current_plot_count + 1):
            assigned_data_sources = self.get_assigned_data_sources_for_plot(plot_id)
            if not assigned_data_sources:
                continue

            x_axis_tag = f"x_axis{plot_id}"
            x_min_vis = None
            x_max_vis = None
            if dpg.does_item_exist(x_axis_tag):
                try:
                    x_min_vis, x_max_vis = dpg.get_axis_limits(x_axis_tag)
                except Exception:
                    pass

            x_min_vis_norm, x_max_vis_norm = _normalize_xlim(x_min_vis, x_max_vis)

            plot_y_min = None
            plot_y_max = None

            for data_source_id in assigned_data_sources:
                series_tag = f"signal_series{plot_id}_{data_source_id}"
                if not dpg.does_item_exist(series_tag):
                    continue

                data_source = f"ds{data_source_id}"
                xs, ys, n = _get_ds(data_source)

                if n <= 0:
                    cache_key = (x_min_vis_norm, x_max_vis_norm, downsample_rate, None, None, 0)
                    if self._series_visible_cache.get(series_tag) != cache_key:
                        dpg.set_value(series_tag, [[], []])
                        self._series_visible_cache[series_tag] = cache_key
                    continue

                # bisect O(log n)
                i0 = 0
                i1 = n
                # 記錄 extra_pts 展開前的真實可見範圍（用於 Y limits）
                vis_i0 = 0
                vis_i1 = n
                if x_min_vis_norm is not None and x_max_vis_norm is not None:
                    i0 = bisect_left(xs, x_min_vis_norm, 0, n)
                    i1 = bisect_right(xs, x_max_vis_norm, 0, n)
                    vis_i0 = i0
                    vis_i1 = i1

                if extra_pts > 0:
                    ep = extra_pts
                    if downsample_active:
                        ep = int(math.ceil(extra_pts / float(downsample_rate)))
                    i0 = max(0, i0 - ep)
                    i1 = min(n, i1 + ep)
                if i1 < i0:
                    i0, i1 = 0, n

                # 真實可見範圍內是否有點（不含 extra_pts 展開的）
                vis_count = vis_i1 - vis_i0

                first_x = xs[i0] if i0 < n else None
                last_x = xs[i1 - 1] if i1 > 0 and i1 <= n else None
                slice_len = i1 - i0

                cache_key = (x_min_vis_norm, x_max_vis_norm, downsample_rate, first_x, last_x, slice_len)
                cache_hit = (self._series_visible_cache.get(series_tag) == cache_key)

                # Cache hit → 復用上幀 Y limits，跳過 set_value
                if cache_hit:
                    if need_y_limits:
                        prev = _ylim_cache.get(series_tag)
                        if prev is not None:
                            s_ymin, s_ymax = prev
                            if plot_y_min is None or s_ymin < plot_y_min:
                                plot_y_min = s_ymin
                            if plot_y_max is None or s_ymax > plot_y_max:
                                plot_y_max = s_ymax
                    continue

                # ===== 準備顯示資料（NumPy 向量化 min-max decimation）=====
                if slice_len > MAX_DISPLAY_PTS:
                    n_buckets = MAX_DISPLAY_PTS // 2  # 500 buckets × 2(min+max) = 1000 pts
                    # 一次 C 層級 list→ndarray（比 500 次 Python slice+min+max+index 快 5x+）
                    # array.array('d') → numpy: 零複製 O(1)，不需逐元素拆箱
                    xs_np = np.frombuffer(xs, dtype=np.float64)[i0:i1]
                    ys_np = np.frombuffer(ys, dtype=np.float64)[i0:i1]
                    total = len(ys_np)
                    usable = (total // n_buckets) * n_buckets
                    ys_2d = ys_np[:usable].reshape(n_buckets, -1)
                    xs_2d = xs_np[:usable].reshape(n_buckets, -1)
                    # 全向量化：min/max/argmin/argmax 各一次 C/SIMD 掃描
                    mn_vals = ys_2d.min(axis=1)
                    mx_vals = ys_2d.max(axis=1)
                    mn_local = ys_2d.argmin(axis=1)
                    mx_local = ys_2d.argmax(axis=1)
                    rng = np.arange(n_buckets)
                    mn_xs = xs_2d[rng, mn_local]
                    mx_xs = xs_2d[rng, mx_local]
                    # 按 X 順序交錯輸出 min/max（保持時序，無鋸齒）
                    mn_first = mn_local <= mx_local
                    x_out = np.empty(n_buckets * 2, dtype=np.float64)
                    y_out = np.empty(n_buckets * 2, dtype=np.float64)
                    x_out[0::2] = np.where(mn_first, mn_xs, mx_xs)
                    y_out[0::2] = np.where(mn_first, mn_vals, mx_vals)
                    x_out[1::2] = np.where(mn_first, mx_xs, mn_xs)
                    y_out[1::2] = np.where(mn_first, mx_vals, mn_vals)
                    # 尾端不滿一個 bucket 的點直接原樣附加（避免最新資料被截斷）
                    if usable < total:
                        x_disp = x_out.tolist() + xs_np[usable:].tolist()
                        y_disp = y_out.tolist() + ys_np[usable:].tolist()
                    else:
                        x_disp = x_out.tolist()
                        y_disp = y_out.tolist()
                else:
                    x_disp = list(xs[i0:i1])
                    y_disp = list(ys[i0:i1])

                # ===== Y min/max 從顯示資料計算（最多 2000 點，快速）=====
                # 只在真實可見範圍有點時才更新 Y limits，避免 extra_pts 邊緣點導致亂跳
                if need_y_limits and y_disp and vis_count > 0:
                    s_ymin = min(y_disp)
                    s_ymax = max(y_disp)
                    _ylim_cache[series_tag] = (s_ymin, s_ymax)
                    if plot_y_min is None or s_ymin < plot_y_min:
                        plot_y_min = s_ymin
                    if plot_y_max is None or s_ymax > plot_y_max:
                        plot_y_max = s_ymax

                dpg.set_value(series_tag, [x_disp, y_disp])
                self._series_visible_cache[series_tag] = cache_key

            if plot_y_min is not None:
                frame_y_limits[plot_id] = (plot_y_min, plot_y_max)

        self._frame_y_limits = frame_y_limits
        self._series_ylim_cache = _ylim_cache
        ado.update_adaptive_downsampling()
        
    def update_x_axis_labels(self):
        """依照 X 軸單位更新時間軸 plot 的 X label"""
        time_plot_num = self.current_plot_count + 1
        x_axis_tag = f"x_axis{time_plot_num}"
        if dpg.does_item_exist(x_axis_tag):
            if self.x_axis_unit_mode == "By time":
                dpg.configure_item(x_axis_tag, label="Time (s)")
            else:
                dpg.configure_item(x_axis_tag, label="Sequence")
#####################################################################################
# 建立三個 queue
ipc_queues = {
    "HSDataSource_logic_to_gui": multiprocessing.Queue(),
    "UIMsg_gui_to_logic": multiprocessing.Queue(),
    "UIMsg_logic_to_gui": multiprocessing.Queue(),
}

# Create a single global UI instance
UIInstance = UIHandle(ipc_queues)

def main():
    dprint(f"Main(UI) process PID: {os.getpid()} started")
    
    # Start Logic process
    dprint("Starting Logic process")
    logic_process = multiprocessing.Process(
        target=logic_main, 
        args=(ipc_queues,),
        name="Logic_Process"
    )
    logic_process.start()
    
    try:
        # Start UI loop
        dprint(f"UI loop started")
        UIInstance.loop()
        
    except KeyboardInterrupt:
        pass
    
    finally:
        # Make sure to stop the logic process
        if logic_process.is_alive():
            dprint("Terminating Logic process")
            logic_process.terminate()
            logic_process.join()
    
    dprint("Main process endded")

"""main entry point"""
if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()