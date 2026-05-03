import time
import os
import math
import random
import struct

from queue import Empty
from collections import deque
from IPCDataClass import HSDataSource, UIMsg

# =========================
# Protocol log 控制開關
# =========================
ENABLE_PROTOCOL_LOG = False

# =========================
# 除錯列印開關
# =========================
DEBUG_PRINT = False
def dprint(*args, **kwargs):
    if DEBUG_PRINT:
        print(*args, **kwargs)

try:
    import serial  # pyserial
    from serial.tools import list_ports
except Exception:
    serial = None
    list_ports = None

class IPC:
    """可擴展的 IPC 管理器（支援多通道），可存取 logic_handle"""
    def __init__(self, logic_handle=None):
        self.logic_handle = logic_handle
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

class DemoSignalHandler:
    def __init__(self, logic_handle):
        self.logic_handle = logic_handle
        # 不再依賴系統時間；改用 loop 次數推進相位
        self.sequence_num = 0

    def get_instant_signals(self):
        """回傳 16 個正弦波的瞬時值（與系統時間無關）。

        每個 loop 計算一次：
        - 第 1 個波：每個 loop 相位遞增 1 度
        - 第 2 個波：每個 loop 相位遞增 2 度
        - ...
        - 第 16 個波：每個 loop 相位遞增 16 度
        """
        self.sequence_num += 1
        signals = {}
        for i in range(16):
            step_deg = i + 1  # 1..16 度/loop
            phase_deg = (self.sequence_num * step_deg) % 360.0
            value = math.sin(math.radians(phase_deg))
            signals[f"HSDataSource_{step_deg}"] = value
        return signals


class ProtocolStats:
    """簡單的協定統計類別，紀錄封包收發與錯誤計數。

    以組合方式掛在 LogicHandle 上，不包含任何 I/O 行為，
    僅作為純資料容器與輔助方法。
    """

    def __init__(self) -> None:
        # 1. 成功接收到的封包總數（含各種協定類型）
        self.total_success_received_packet: int = 0
        # 2. 成功送出的封包總數
        self.total_success_transmitted_packet: int = 0
        # 3. 成功接收到的 Log 封包總數
        self.total_success_received_log_packet: int = 0
        # 4. 成功接收到的 DS 封包總數
        self.total_success_received_ds_packet: int = 0
        # 5. 被丟棄的封包數（各種原因綜合統計）
        self.dropped_packet: int = 0
        # 6. CRC 檢查失敗次數
        self.crc_error: int = 0
        # 7. 協定格式錯誤或長度非法等「無效封包」次數
        self.invalid_packet: int = 0
        # 8. DS sequence 跳號推定遺失的封包數
        self.ds_sequence_dropped: int = 0
        # 9. DS sequence 亂序或重複而被丟棄的封包數
        self.ds_sequence_out_of_order: int = 0

    def reset(self) -> None:
        """將所有計數歸零。"""
        self.total_success_received_packet = 0
        self.total_success_transmitted_packet = 0
        self.total_success_received_log_packet = 0
        self.total_success_received_ds_packet = 0
        self.dropped_packet = 0
        self.crc_error = 0
        self.invalid_packet = 0
        self.ds_sequence_dropped = 0
        self.ds_sequence_out_of_order = 0

class LogicHandle:
    def __init__(self, ipc_queues):
        self.counter = 0
        self.target_interval = 0.01  # 10ms interval for logic processing
        self.demo_signal_handler = DemoSignalHandler(self)
        self.ipc = IPC(self)

        # 協定/封包統計（純計數，供 UI 或診斷查詢使用）
        self.protocol_stats = ProtocolStats()

        # ✅ 用於隨機產生假 COM port（每次 request 都會變）
        self._rng = random.Random()

        # ✅ Comm status (for START_BUTTON request handling)
        self.comm_status = "Stopped"  # "Started" | "Stopped"

        # ✅ Currently selected COM port (via SET_COM_PORT)
        self.selected_com_port = None  # str | None

        # ✅ Real serial port handle (pyserial)
        self._serial = None
        self._serial_port_name = None

        # ✅ Desired serial buffer size (bytes) for both TX/RX
        self._serial_buffer_size = 50 * 1024 * 1024  # 50MB

        # ✅ Serial settings (updated via START_BUTTON payload)
        self._serial_baudrate = 115200
        self._serial_frame_format = "8N1"
        self._bypass_crc_check = False

        # ✅ Protocol buffer (application-level stream buffer for packet parsing)
        self._protocol_buffer = bytearray()
        self._protocol_buffer_max = 50 * 1024 * 1024  # 50MB cap to avoid unbounded growth
        self._serial_read_max_per_tick = 64 * 1024     # 64KB per handler tick

        # ✅ DS sequence unwrap: 將封包中 0-255 循環的 sequence 展開為持續累加值
        self._ds_last_raw_seq = None   # 上一次收到的原始 sequence (0-255)
        self._ds_cumulative_seq = 0    # 持續累加的 sequence

        # ✅ Log buffer：GUI 用 GET_LOG 來拉取
        self._log_buffer = deque(maxlen=1000)
        self._last_demo_log_ts = time.monotonic()

        # ✅ 診斷統計：每秒打印一次
        self._diag_last_ts = time.perf_counter()
        self._diag_tick_count = 0
        self._diag_ds_count = 0
        self._diag_bytes_read = 0

        # ✅ Parameter read/write: 分開追蹤 read/write request
        self._pending_pr_read_request = None   # {"msg_ID": ..., "msg_type": ..., "ts": ...} or None
        self._pending_pr_write_request = None  # {"msg_ID": ..., "msg_type": ..., "ts": ...} or None
        self._pr_timeout_sec = 0.1      # Parameter 請求超時時間 (秒)
        
        # 註冊所有需要的 IPC 通道
        self.ipc.register_channel("HSDataSource_logic_to_gui", ipc_queues["HSDataSource_logic_to_gui"])
        self.ipc.register_channel("UIMsg_gui_to_logic", ipc_queues["UIMsg_gui_to_logic"])
        self.ipc.register_channel("UIMsg_logic_to_gui", ipc_queues["UIMsg_logic_to_gui"])

    def _list_real_com_ports(self) -> list[str]:
        """Return a list of real COM port names via pyserial (best-effort)."""
        if list_ports is None:
            return []
        ports = []
        try:
            for info in list_ports.comports():
                dev = getattr(info, "device", None)
                if isinstance(dev, str) and dev.strip():
                    ports.append(dev.strip())
        except Exception:
            return []

        def _sort_key(name: str):
            # Prefer numeric sort for COMx
            try:
                s = name.upper().strip()
                if s.startswith("COM"):
                    return (0, int(s[3:]))
            except Exception:
                pass
            return (1, name)

        ports = sorted(set(ports), key=_sort_key)
        return ports

    def _close_serial(self):
        """Close current serial port if open (best-effort)."""
        try:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
        finally:
            self._serial = None
            self._serial_port_name = None
            self._protocol_buffer = bytearray()

    def _open_serial(self, port_name: str) -> tuple[bool, str]:
        """Open a serial port; returns (ok, reason)."""
        if serial is None:
            return False, "pyserial not available"
        if not isinstance(port_name, str) or not port_name.strip():
            return False, "invalid port"

        port_name = port_name.strip()

        # Always close previous handle before opening another
        self._close_serial()
        try:
            # 解析 frame format (e.g. "8N1" -> bytesize=8, parity=N, stopbits=1)
            ff = self._serial_frame_format
            bytesize = int(ff[0]) if len(ff) >= 1 and ff[0].isdigit() else 8
            parity_char = ff[1].upper() if len(ff) >= 2 else "N"
            stopbits_char = ff[2] if len(ff) >= 3 else "1"

            parity_map = {"N": serial.PARITY_NONE, "O": serial.PARITY_ODD, "E": serial.PARITY_EVEN}
            stopbits_map = {"1": serial.STOPBITS_ONE, "2": serial.STOPBITS_TWO}
            bytesize_map = {5: serial.FIVEBITS, 6: serial.SIXBITS, 7: serial.SEVENBITS, 8: serial.EIGHTBITS}

            self._serial = serial.Serial(
                port=port_name,
                baudrate=self._serial_baudrate,
                bytesize=bytesize_map.get(bytesize, serial.EIGHTBITS),
                parity=parity_map.get(parity_char, serial.PARITY_NONE),
                stopbits=stopbits_map.get(stopbits_char, serial.STOPBITS_ONE),
                timeout=0,
                write_timeout=1,
                xonxoff=False,   # 關閉軟體流控
                rtscts=False,    # 關閉硬體 RTS/CTS 流控
                dsrdtr=False,    # 關閉硬體 DSR/DTR 流控
            )

            # 主動清空串口輸入緩衝區，避免第一次開啟時殘留雜訊導致封包解析失敗
            try:
                if hasattr(self._serial, "reset_input_buffer"):
                    self._serial.reset_input_buffer()
                elif hasattr(self._serial, "flushInput"):
                    self._serial.flushInput()
            except Exception:
                pass

            # After opening, attempt to enlarge TX buffer (Windows best-effort).
            try:
                if hasattr(self._serial, "set_buffer_size"):
                    try:
                        self._serial.set_buffer_size(
                            rx_size=int(self._serial_buffer_size),
                            tx_size=int(self._serial_buffer_size),
                        )
                    except TypeError:
                        # Some versions/platforms may not accept both keywords.
                        try:
                            self._serial.set_buffer_size(int(self._serial_buffer_size), int(self._serial_buffer_size))
                        except Exception:
                            pass
            except Exception:
                pass

            # Reset protocol buffer on (re)open
            self._protocol_buffer = bytearray()
            self._serial_port_name = port_name
            return True, ""
        except Exception as e:
            self._close_serial()
            return False, str(e)

    def _crc16_modbus(self, data: bytes) -> int:
        """Compute Modbus CRC16 (poly=0xA001, init=0xFFFF, refin/refout=True)."""
        crc = 0xFFFF
        for b in data:
            crc ^= b
            for _ in range(8):
                if crc & 1:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        return crc & 0xFFFF

    def _append_serial_bytes(self, chunk: bytes) -> None:
        if not chunk:
            return
        try:
            self._protocol_buffer.extend(chunk)
        except Exception:
            return

        # Prevent unbounded growth
        if len(self._protocol_buffer) > int(self._protocol_buffer_max):
            overflow = len(self._protocol_buffer) - int(self._protocol_buffer_max)
            if overflow > 0:
                del self._protocol_buffer[:overflow]
                try:
                    dprint(f"[Logic] Protocol buffer overflow: dropped {overflow} bytes")
                except Exception:
                    pass

    # DS type → byte size mapping
    _DS_TYPE_SIZE = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4}
    # DS type → struct format (Big-Endian)
    _DS_TYPE_FMT = {0: '>B', 1: '>b', 2: '>H', 3: '>h', 4: '>I', 5: '>i', 6: '>f'}

    # Parameter type string → protocol type code
    _PR_TYPE_STR_TO_CODE = {"U8": 0, "S8": 1, "U16": 2, "S16": 3, "U32": 4, "S32": 5, "Float32": 6}

    def _send_serial_bytes(self, data: bytes) -> bool:
        """Send raw bytes over the open serial port (best-effort)."""
        if self._serial is None:
            return False
        try:
            written = self._serial.write(data)
            if written != len(data):
                if ENABLE_PROTOCOL_LOG:
                    ts = time.strftime('%H:%M:%S', time.localtime())
                    self.push_log(f"[{ts}][PR] Serial write incomplete: {written}/{len(data)} bytes")
                return False
            self._serial.flush()
            return True
        except Exception as e:
            try:
                dprint(f"[Logic] Serial write failed: {e}")
            except Exception:
                pass
            return False

    # Packet type constants (new two-stage CRC protocol)
    _PKT_TYPE_LOG   = 0x00
    _PKT_TYPE_DS    = 0x01
    _PKT_TYPE_PARAM = 0x02
    _PKT_SYNC       = bytes([0x5A, 0xA5])
    _PKT_HEADER_LEN = 6   # sync(2) + length(1) + type(1) + header_crc(2)

    def _build_packet(self, pkt_type: int, payload: bytes) -> bytes:
        """Build a packet with the new two-stage CRC protocol.

        Packet format:
          [0]    Sync byte 1     = 0x5A
          [1]    Sync byte 2     = 0xA5
          [2]    Length           = len(payload) (1~255)
          [3]    Packet type      (0x00=Log, 0x01=DS, 0x02=Param)
          [4]    Header CRC16 high (CRC of bytes 0~3)
          [5]    Header CRC16 low
          [6 .. 6+Length-1] Payload
          [6+Length]   Packet CRC16 high (CRC of bytes 0~5 + payload)
          [6+Length+1] Packet CRC16 low
        """
        header_part = bytes([0x5A, 0xA5, len(payload) & 0xFF, pkt_type & 0xFF])
        header_crc = self._crc16_modbus(header_part)
        header = header_part + bytes([(header_crc >> 8) & 0xFF, header_crc & 0xFF])
        pkt_crc = self._crc16_modbus(header + payload)
        return header + payload + bytes([(pkt_crc >> 8) & 0xFF, pkt_crc & 0xFF])

    def _build_param_packet(self, func_code: int, type_code: int, addr: int, data_bytes: bytes) -> bytes:
        """Build a parameter packet (type=0x02, payload=8 bytes).

        Payload format (8 bytes):
          [0] Function code  [1] Parameter type
          [2] Address high   [3] Address low
          [4] Data byte 3    [5] Data byte 2    [6] Data byte 1    [7] Data byte 0
        """
        payload = bytes([
            func_code & 0xFF,
            type_code & 0xFF,
            (addr >> 8) & 0xFF,
            addr & 0xFF,
            data_bytes[0], data_bytes[1], data_bytes[2], data_bytes[3],
        ])
        return self._build_packet(self._PKT_TYPE_PARAM, payload)

    def _poll_serial_and_parse_packets(self):
        """Non-blocking poll serial input and parse packets (two-stage CRC protocol)."""
        if self._serial is None:
            dprint("[DEBUG] _serial is None, skip polling")
            return

        # Read available bytes (non-blocking)
        try:
            waiting = int(getattr(self._serial, "in_waiting", 0) or 0)
        except Exception:
            waiting = 0

        to_read = min(waiting, int(self._serial_read_max_per_tick))
        if to_read > 0:
            try:
                data = self._serial.read(to_read)
            except Exception as e:
                try:
                    dprint(f"[Logic] Serial read failed: {e}")
                except Exception:
                    pass
                return
            dprint(f"[DEBUG] Serial read {len(data)} bytes (waiting={waiting}), buffer={len(self._protocol_buffer)} bytes: {data.hex()}")
            self._diag_bytes_read += len(data)
            self._append_serial_bytes(data)

        self._parse_packets_from_buffer()

    def _parse_packets_from_buffer(self):
        """Parse packets using the two-stage CRC protocol.

        General packet format:
          [0]  0x5A  Sync byte 1
          [1]  0xA5  Sync byte 2
          [2]  Length of payload (1~255)
          [3]  Packet type (0x00=Log, 0x01=DS, 0x02=Param)
          [4-5]  Header CRC16 (network order) — checked range: bytes 0~3
          [6 .. 6+Length-1]  Payload
          [6+Length .. 6+Length+1]  Packet CRC16 (network order) — checked range: bytes 0~5 + payload

        Two-stage validation:
          Stage 1: verify Header CRC16 (only need 6 bytes)
          Stage 2: verify Packet CRC16 (need full packet)
        """
        buf = self._protocol_buffer
        if not buf:
            dprint(f"[DEBUG] Buffer empty, nothing to parse.")
            return

        while len(buf) >= self._PKT_HEADER_LEN:
            # Find sync pair 0x5A 0xA5
            sync_pos = -1
            for i in range(len(buf) - 1):
                if buf[i] == 0x5A and buf[i + 1] == 0xA5:
                    sync_pos = i
                    break

            if sync_pos < 0:
                # No sync pair found — keep last byte in case it's 0x5A
                if buf[-1] == 0x5A:
                    del buf[:-1]
                else:
                    buf.clear()
                dprint(f"[DEBUG] No sync pair found, buffer cleared.")
                break

            # Discard bytes before sync
            if sync_pos > 0:
                dprint(f"[DEBUG] Discarding {sync_pos} bytes before sync: {buf[:sync_pos].hex()}")
                del buf[:sync_pos]

            if len(buf) < self._PKT_HEADER_LEN:
                dprint(f"[DEBUG] Not enough data for header (need 6), have {len(buf)}")
                break

            # ── Stage 1: Header CRC16 check (bytes 0~3) ──
            header_data = bytes(buf[0:4])  # sync(2) + length(1) + type(1)
            header_crc_recv = (buf[4] << 8) | buf[5]
            header_crc_calc = self._crc16_modbus(header_data)

            if (not self._bypass_crc_check) and header_crc_calc != header_crc_recv:
                dprint(f"[DEBUG] Header CRC mismatch: recv=0x{header_crc_recv:04X}, calc=0x{header_crc_calc:04X}")
                self.protocol_stats.crc_error += 1
                self.protocol_stats.dropped_packet += 1
                del buf[0:1]  # skip one byte, re-sync
                continue

            payload_len = buf[2]
            pkt_type = buf[3]
            total_len = payload_len + 8  # header(6) + payload + packet_crc(2)

            if len(buf) < total_len:
                dprint(f"[DEBUG] Incomplete packet: type=0x{pkt_type:02X}, need {total_len} bytes, have {len(buf)}")
                break  # wait for more data

            # ── Stage 2: Packet CRC16 check (bytes 0 ~ 5+payload_len) ──
            pkt = bytes(buf[:total_len])
            pkt_crc_recv = (pkt[-2] << 8) | pkt[-1]
            pkt_crc_calc = self._crc16_modbus(pkt[:-2])

            if (not self._bypass_crc_check) and pkt_crc_calc != pkt_crc_recv:
                dprint(f"[DEBUG] Packet CRC mismatch: recv=0x{pkt_crc_recv:04X}, calc=0x{pkt_crc_calc:04X}, pkt={pkt.hex()}")
                self.protocol_stats.crc_error += 1
                self.protocol_stats.dropped_packet += 1
                del buf[0:1]  # skip one byte, re-sync
                continue

            # CRC OK — extract payload and dispatch
            payload = pkt[6:6 + payload_len]

            if pkt_type == self._PKT_TYPE_LOG:
                dprint(f"[DEBUG] Parse LOG packet: {pkt.hex()}")
                self._try_parse_log_packet(payload)
            elif pkt_type == self._PKT_TYPE_DS:
                dprint(f"[DEBUG] Parse DS packet: {pkt.hex()}")
                self._try_parse_ds_packet(payload)
            elif pkt_type == self._PKT_TYPE_PARAM:
                dprint(f"[DEBUG] Parse PARAM packet: {pkt.hex()}")
                self._try_parse_param_packet(payload)
            else:
                dprint(f"[DEBUG] Unknown packet type 0x{pkt_type:02X}, discarding.")
                self.protocol_stats.invalid_packet += 1
                self.protocol_stats.dropped_packet += 1

            del buf[:total_len]

    def _try_parse_log_packet(self, payload: bytes):
        """Decode a Log packet payload (ASCII string)."""
        try:
            text = payload.decode("latin-1", errors="strict")
        except Exception:
            text = payload.decode("latin-1", errors="replace")

        if "\x00" in text:
            text = text.replace("\x00", "\\x00")

        self.push_log(text)
        self.protocol_stats.total_success_received_packet += 1
        self.protocol_stats.total_success_received_log_packet += 1

    def _try_parse_ds_packet(self, payload: bytes):
        """Decode a DS packet payload.

        Payload format:
          [0]  Packet sequence (0~255)
          [1]  Numbers of DS (1~16)
          [2..] DS1_type(1B) + DS1_data(variable) + ... + DSn_type + DSn_data
        """
        if len(payload) < 2:
            self.protocol_stats.invalid_packet += 1
            self.protocol_stats.dropped_packet += 1
            return

        sequence = payload[0]
        num_ds = payload[1]

        if num_ds < 1 or num_ds > 16:
            self.protocol_stats.invalid_packet += 1
            self.protocol_stats.dropped_packet += 1
            return

        signals = {}
        offset = 2
        data_end = len(payload)

        for i in range(num_ds):
            if offset >= data_end:
                self.protocol_stats.invalid_packet += 1
                self.protocol_stats.dropped_packet += 1
                return

            ds_type = payload[offset]
            offset += 1

            size = self._DS_TYPE_SIZE.get(ds_type)
            fmt = self._DS_TYPE_FMT.get(ds_type)
            if size is None or fmt is None:
                self.protocol_stats.invalid_packet += 1
                self.protocol_stats.dropped_packet += 1
                return

            if offset + size > data_end:
                self.protocol_stats.invalid_packet += 1
                self.protocol_stats.dropped_packet += 1
                return

            value = struct.unpack(fmt, payload[offset:offset + size])[0]
            offset += size
            signals[f"HSDataSource_{i + 1}"] = float(value)

        # Sequence 順序檢查 + unwrap（0-255 循環展開為持續累加值）
        if self._ds_last_raw_seq is not None:
            delta = (sequence - self._ds_last_raw_seq) & 0xFF
            if delta == 0:
                dprint(f"[DEBUG] DS duplicate seq={sequence}, discarding.")
                self.protocol_stats.ds_sequence_out_of_order += 1
                self.protocol_stats.dropped_packet += 1
                return
            elif delta > 128:
                dprint(f"[DEBUG] DS old seq={sequence} (last={self._ds_last_raw_seq}), discarding.")
                self.protocol_stats.ds_sequence_out_of_order += 1
                self.protocol_stats.dropped_packet += 1
                return
            else:
                if delta > 1:
                    lost = delta - 1
                    dprint(f"[DEBUG] DS seq gap: expected {(self._ds_last_raw_seq + 1) & 0xFF}, got {sequence}, lost {lost} packet(s).")
                    self.protocol_stats.ds_sequence_dropped += lost
                    self.protocol_stats.dropped_packet += lost
                    na_signals = {f"HSDataSource_{i + 1}": float('nan') for i in range(num_ds)}
                    for k in range(1, delta):
                        self._ds_cumulative_seq += 1
                        self.ipc.send(
                            "HSDataSource_logic_to_gui",
                            HSDataSource(signals=dict(na_signals), sequence_num=self._ds_cumulative_seq),
                        )
                        dprint(f"[DEBUG] DS NA placeholder sent: cumul_seq={self._ds_cumulative_seq}")
                self._ds_cumulative_seq += 1
        else:
            self._ds_cumulative_seq = sequence
        self._ds_last_raw_seq = sequence

        # Send parsed DS to GUI
        self.ipc.send(
            "HSDataSource_logic_to_gui",
            HSDataSource(signals=signals, sequence_num=self._ds_cumulative_seq),
        )
        self.protocol_stats.total_success_received_packet += 1
        self.protocol_stats.total_success_received_ds_packet += 1
        self._diag_ds_count += 1
        dprint(f"[DEBUG] DS OK: raw_seq={sequence}, cumul_seq={self._ds_cumulative_seq}, num_ds={num_ds}, signals={signals}")

    def _try_parse_param_packet(self, payload: bytes):
        """Decode a Parameter packet payload (fixed 8 bytes).

        Payload format:
          [0] Function code  [1] Parameter type
          [2] Address high   [3] Address low
          [4] Data byte 3    [5] Data byte 2    [6] Data byte 1    [7] Data byte 0
        """
        if len(payload) != 8:
            self.protocol_stats.invalid_packet += 1
            self.protocol_stats.dropped_packet += 1
            return

        func_code = payload[0]
        param_type = payload[1]
        addr = (payload[2] << 8) | payload[3]
        data_bytes = payload[4:8]

        # Decode data according to param_type
        raw_value = struct.unpack('>I', data_bytes)[0]
        if param_type == 6:    # Float32
            float_value = struct.unpack('>f', data_bytes)[0]
        else:
            float_value = None
        addr_str = f"0x{addr:04X}"

        if float_value is not None:
            dprint(f"[DEBUG] Param OK: func=0x{func_code:02X}, type={param_type}, addr={addr_str}, raw={raw_value}, float={float_value}")
            if ENABLE_PROTOCOL_LOG:
                ts = time.strftime('%H:%M:%S', time.localtime())
                self.push_log(f"[{ts}][PR] RESP func=0x{func_code:02X} addr={addr_str} raw={raw_value} float={float_value}")
        else:
            dprint(f"[DEBUG] Param OK: func=0x{func_code:02X}, type={param_type}, addr={addr_str}, raw={raw_value}")
            if ENABLE_PROTOCOL_LOG:
                ts = time.strftime('%H:%M:%S', time.localtime())
                self.push_log(f"[{ts}][PR] RESP func=0x{func_code:02X} addr={addr_str} raw={raw_value}")

        # 根據 func_code 判斷是 read 還是 write response
        if func_code in (0xFF, 0x11):
            pending = self._pending_pr_read_request
            if pending is None:
                dprint(f"[DEBUG] Param read response but no pending read request, discarding.")
                self.protocol_stats.total_success_received_packet += 1
                return
            # 驗證 addr 是否與 pending 中的 addr 一致，防止接受 stale 延遲回應
            if addr != pending.get("addr_int"):
                ts = time.strftime('%H:%M:%S', time.localtime())
                if ENABLE_PROTOCOL_LOG:
                    self.push_log(f"[{ts}][PR] Stale read response discarded: resp_addr={addr_str}, pending_addr=0x{pending.get('addr_int', 0):04X}")
                self.protocol_stats.dropped_packet += 1
                return
            self._pending_pr_read_request = None
            if func_code == 0xFF:
                # Read response (success)
                resp = UIMsg(
                    msg_ID=pending["msg_ID"],
                    msg_type="GET_PR_VALUE",
                    msg_subtype="RESPONSE",
                    payload=f"{addr_str},{raw_value}",
                )
                self.ipc.send("UIMsg_logic_to_gui", resp)
            elif func_code == 0x11:
                # Read exception
                resp = UIMsg(
                    msg_ID=pending["msg_ID"],
                    msg_type="GET_PR_VALUE",
                    msg_subtype="RESPONSE",
                    payload=f"{addr_str},ERROR",
                )
                self.ipc.send("UIMsg_logic_to_gui", resp)
        elif func_code in (0xAA, 0x22):
            pending = self._pending_pr_write_request
            if pending is None:
                dprint(f"[DEBUG] Param write response but no pending write request, discarding.")
                self.protocol_stats.total_success_received_packet += 1
                return
            # 驗證 addr 是否與 pending 中的 addr 一致，防止接受 stale 延遲回應
            if addr != pending.get("addr_int"):
                ts = time.strftime('%H:%M:%S', time.localtime())
                if ENABLE_PROTOCOL_LOG:
                    self.push_log(f"[{ts}][PR] Stale write response discarded: resp_addr={addr_str}, pending_addr=0x{pending.get('addr_int', 0):04X}")
                self.protocol_stats.dropped_packet += 1
                return
            self._pending_pr_write_request = None
            if func_code == 0xAA:
                # Write response (success)
                resp = UIMsg(
                    msg_ID=pending["msg_ID"],
                    msg_type="SET_PR_VALUE",
                    msg_subtype="RESPONSE",
                    payload="SUCCESS",
                )
                self.ipc.send("UIMsg_logic_to_gui", resp)
            elif func_code == 0x22:
                # Write exception
                resp = UIMsg(
                    msg_ID=pending["msg_ID"],
                    msg_type="SET_PR_VALUE",
                    msg_subtype="RESPONSE",
                    payload="FAIL",
                )
                self.ipc.send("UIMsg_logic_to_gui", resp)
        else:
            dprint(f"[DEBUG] Param unknown func_code=0x{func_code:02X}, discarding.")

        self.protocol_stats.total_success_received_packet += 1
        
    def poll_ui_requests(self):
        """✅ 非阻塞處理 GUI->Logic 的 UIMsg requests（request/response）"""
        q_in = self.ipc.channels.get("UIMsg_gui_to_logic")
        if q_in is None:
            return

        while True:
            try:
                msg = q_in.get_nowait()
            except Empty:
                break
            except Exception:
                break

            if not isinstance(msg, UIMsg):
                continue

            # SET_COM_PORT
            if msg.msg_type == "SET_COM_PORT" and msg.msg_subtype == "REQUEST":
                port = msg.payload
                ok = False
                reason = ""
                if isinstance(port, str):
                    port = port.strip()
                    if port == "Demo Port":
                        # Demo mode: ensure real COM is closed
                        self._close_serial()
                        self.selected_com_port = port
                        ok = True
                    elif port == "None" or not port:
                        # Treat as close
                        self._close_serial()
                        self.selected_com_port = None
                        ok = True
                    else:
                        ok, reason = self._open_serial(port)
                        if ok:
                            self.selected_com_port = port

                resp = UIMsg(
                    msg_ID=msg.msg_ID,
                    msg_type="SET_COM_PORT",
                    msg_subtype="RESPONSE",
                    payload=("SUCCESS" if ok else "FAIL"),
                )
                self.ipc.send("UIMsg_logic_to_gui", resp)

                # optional trace log for GUI
                try:
                    extra = f" reason={reason}" if (not ok and reason) else ""
                    dprint(
                        f"[Logic] SET_COM_PORT port={port} result={resp.payload} selected={self.selected_com_port}{extra}"
                    )
                except Exception:
                    pass
                continue

            # START_BUTTON
            if msg.msg_type == "START_BUTTON" and msg.msg_subtype == "REQUEST":
                parts = msg.payload.split(",") if isinstance(msg.payload, str) else [msg.payload]
                action = parts[0]
                ok = False
                if action == "SWITCH_TO_START":
                    # 解析 bitrate 和 frame format
                    if len(parts) >= 2:
                        try:
                            self._serial_baudrate = int(parts[1])
                        except (ValueError, TypeError):
                            self._serial_baudrate = 115200
                    if len(parts) >= 3:
                        self._serial_frame_format = parts[2]
                    if len(parts) >= 4:
                        token = str(parts[3]).strip().lower()
                        self._bypass_crc_check = token in ("1", "true", "yes", "on")
                    else:
                        self._bypass_crc_check = False

                    # 取得目前選擇的COM port
                    port = self.selected_com_port
                    # open -> close -> open
                    if port and port != "Demo Port" and port != "None":
                        self._open_serial(port)
                        self._close_serial()
                        self._open_serial(port)

                    # 主動清空串口輸入緩衝區，確保每次開始都從乾淨狀態解析
                    if self._serial is not None:
                        try:
                            if hasattr(self._serial, "reset_input_buffer"):
                                self._serial.reset_input_buffer()
                            elif hasattr(self._serial, "flushInput"):
                                self._serial.flushInput()
                        except Exception:
                            pass

                    self.comm_status = "Started"
                    # 重置 DS 累加 sequence
                    self._ds_last_raw_seq = None
                    self._ds_cumulative_seq = 0
                    ok = True
                elif action == "SWITCH_TO_STOP":
                    self.comm_status = "Stopped"
                    # Close real COM when stopping (best-effort)
                    self._close_serial()
                    # Clear pending parameter requests to avoid msg_ID mismatch on restart
                    self._pending_pr_read_request = None
                    self._pending_pr_write_request = None
                    ok = True

                resp = UIMsg(
                    msg_ID=msg.msg_ID,
                    msg_type="START_BUTTON",
                    msg_subtype="RESPONSE",
                    payload=("SUCCESS" if ok else "FAIL"),
                )
                self.ipc.send("UIMsg_logic_to_gui", resp)

                # optional trace log for GUI
                try:
                    dprint(f"[Logic] START_BUTTON action={action} result={resp.payload} comm_status={self.comm_status}")
                except Exception:
                    pass
                continue

            # ✅ GET_LOG
            if msg.msg_type == "GET_LOG" and msg.msg_subtype == "REQUEST":
                if self._log_buffer:
                    payload = self._log_buffer.popleft()
                    resp = UIMsg(msg_ID=msg.msg_ID, msg_type="GET_LOG", msg_subtype="RESPONSE", payload=payload)
                else:
                    resp = UIMsg(msg_ID=msg.msg_ID, msg_type="GET_LOG", msg_subtype="NO_LOG", payload=None)

                self.ipc.send("UIMsg_logic_to_gui", resp)
                continue
                
            # ✅ GET_COM_PORT_LIST
            if msg.msg_type == "GET_COM_PORT_LIST" and msg.msg_subtype == "REQUEST":
                if list_ports is not None:
                    ports = self._list_real_com_ports()
                else:
                    ports = []
                    
                resp = UIMsg(
                    msg_ID=msg.msg_ID,
                    msg_type="GET_COM_PORT_LIST",
                    msg_subtype="RESPONSE",
                    payload=ports
                )
                self.ipc.send("UIMsg_logic_to_gui", resp)
                continue
            # GET_PR_VALUE
            if msg.msg_type == "GET_PR_VALUE" and msg.msg_subtype == "REQUEST":
                # payload: "addr,type" e.g. "0x0001,U16"
                parts = msg.payload.split(",") if isinstance(msg.payload, str) else []
                addr = parts[0] if len(parts) >= 1 else str(msg.payload)
                type_str = parts[1] if len(parts) >= 2 else "U16"
                # 防守性保護：已有 pending read request 時不應再收到新請求。
                # 在現有展稷下 (GUI 單執行緒一問一答) 此情況不應發生，若發生則為上層 bug。
                if self._pending_pr_read_request is not None:
                    ts = time.strftime('%H:%M:%S', time.localtime())
                    self.push_log(f"[{ts}][PR][ERROR] GET_PR_VALUE REQUEST received while read already pending (msg_ID={msg.msg_ID}), discarding.")
                    continue
                if self.selected_com_port == "Demo Port" and self.comm_status == "Started":
                    # Demo: 隨機讓某些請求不回應（模擬 timeout）
                    if self._rng.random() < 0.3:
                        # 30% 機率不回應，讓 GUI 觸發 timeout
                        resp = UIMsg(msg_ID=msg.msg_ID, msg_type="GET_PR_VALUE", msg_subtype="TIMEOUT", payload=None)
                        self.ipc.send("UIMsg_logic_to_gui", resp)
                        continue
                    value = str(self._rng.randint(0, 65535))
                    resp = UIMsg(
                        msg_ID=msg.msg_ID,
                        msg_type="GET_PR_VALUE",
                        msg_subtype="RESPONSE",
                        payload=f"{addr},{value}",
                    )
                    self.ipc.send("UIMsg_logic_to_gui", resp)
                elif self.comm_status == "Started" and self._serial is not None:
                    # Real serial: send read request packet
                    type_code = self._PR_TYPE_STR_TO_CODE.get(type_str, 2)
                    try:
                        addr_int = int(addr, 16) if isinstance(addr, str) else int(addr)
                    except (ValueError, TypeError):
                        addr_int = 0
                    pkt = self._build_param_packet(0x00, type_code, addr_int, b'\x00\x00\x00\x00')
                    sent = self._send_serial_bytes(pkt)
                    if sent:
                        self._pending_pr_read_request = {"msg_ID": msg.msg_ID, "msg_type": "GET_PR_VALUE", "ts": time.perf_counter(), "pkt": pkt, "retries": 0, "addr_int": addr_int}
                        self.protocol_stats.total_success_transmitted_packet += 1
                        ts = time.strftime('%H:%M:%S', time.localtime())
                        if ENABLE_PROTOCOL_LOG:
                            self.push_log(f"[{ts}][PR] READ REQ addr={addr} type={type_str} pkt={pkt.hex()}")
                    else:
                        ts = time.strftime('%H:%M:%S', time.localtime())
                        if ENABLE_PROTOCOL_LOG:
                            self.push_log(f"[{ts}][PR] READ REQ SEND FAILED addr={addr}")
                        resp = UIMsg(msg_ID=msg.msg_ID, msg_type="GET_PR_VALUE", msg_subtype="TIMEOUT", payload=None)
                        self.ipc.send("UIMsg_logic_to_gui", resp)
                continue

            # SET_PR_VALUE
            if msg.msg_type == "SET_PR_VALUE" and msg.msg_subtype == "REQUEST":
                # payload: "addr,type,value" e.g. "0x0001,U16,12345"
                parts = msg.payload.split(",") if isinstance(msg.payload, str) else []
                addr = parts[0] if len(parts) >= 1 else ""
                type_str = parts[1] if len(parts) >= 2 else "U16"
                value_str = parts[2] if len(parts) >= 3 else "0"
                # 防守性保護：已有 pending write request 時不應再收到新請求。
                # 在現有展稷下 (GUI 單執行緒一問一答) 此情況不應發生，若發生則為上層 bug。
                if self._pending_pr_write_request is not None:
                    ts = time.strftime('%H:%M:%S', time.localtime())
                    self.push_log(f"[{ts}][PR][ERROR] SET_PR_VALUE REQUEST received while write already pending (msg_ID={msg.msg_ID}), discarding.")
                    continue
                if self.selected_com_port == "Demo Port" and self.comm_status == "Started":
                    try:
                        dprint(f"[Logic] SET_PR_VALUE received: {msg.payload}")
                    except Exception:
                        pass
                    resp = UIMsg(
                        msg_ID=msg.msg_ID,
                        msg_type="SET_PR_VALUE",
                        msg_subtype="RESPONSE",
                        payload="SUCCESS",
                    )
                    self.ipc.send("UIMsg_logic_to_gui", resp)
                elif self.comm_status == "Started" and self._serial is not None:
                    # Real serial: send write request packet
                    type_code = self._PR_TYPE_STR_TO_CODE.get(type_str, 2)
                    try:
                        addr_int = int(addr, 16) if isinstance(addr, str) else int(addr)
                    except (ValueError, TypeError):
                        addr_int = 0
                    try:
                        value_int = int(value_str)
                    except (ValueError, TypeError):
                        value_int = 0
                    # Encode value: payload always carries raw uint32 (GUI already converts float to IEEE754 int)
                    data_bytes = struct.pack('>I', value_int & 0xFFFFFFFF)
                    pkt = self._build_param_packet(0x55, type_code, addr_int, data_bytes)
                    sent = self._send_serial_bytes(pkt)
                    if sent:
                        self._pending_pr_write_request = {"msg_ID": msg.msg_ID, "msg_type": "SET_PR_VALUE", "ts": time.perf_counter(), "pkt": pkt, "retries": 0, "addr_int": addr_int}
                        self.protocol_stats.total_success_transmitted_packet += 1
                        ts = time.strftime('%H:%M:%S', time.localtime())
                        if ENABLE_PROTOCOL_LOG:
                            self.push_log(f"[{ts}][PR] WRITE REQ addr={addr} type={type_str} val={value_int} pkt={pkt.hex()}")
                    else:
                        ts = time.strftime('%H:%M:%S', time.localtime())
                        if ENABLE_PROTOCOL_LOG:
                            self.push_log(f"[{ts}][PR] WRITE REQ SEND FAILED addr={addr}")
                        resp = UIMsg(msg_ID=msg.msg_ID, msg_type="SET_PR_VALUE", msg_subtype="TIMEOUT", payload=None)
                        self.ipc.send("UIMsg_logic_to_gui", resp)
                continue

            # GET_PROTOCOL_STATS
            if msg.msg_type == "GET_PROTOCOL_STATS" and msg.msg_subtype == "REQUEST":
                stats = self.protocol_stats
                payload = {
                    "total_success_received_packet": stats.total_success_received_packet,
                    "total_success_transmitted_packet": stats.total_success_transmitted_packet,
                    "total_success_received_log_packet": stats.total_success_received_log_packet,
                    "total_success_received_ds_packet": stats.total_success_received_ds_packet,
                    "dropped_packet": stats.dropped_packet,
                    "crc_error": stats.crc_error,
                    "invalid_packet": stats.invalid_packet,
                    "ds_sequence_dropped": stats.ds_sequence_dropped,
                    "ds_sequence_out_of_order": stats.ds_sequence_out_of_order,
                }
                resp = UIMsg(
                    msg_ID=msg.msg_ID,
                    msg_type="GET_PROTOCOL_STATS",
                    msg_subtype="RESPONSE",
                    payload=payload,
                )
                self.ipc.send("UIMsg_logic_to_gui", resp)
                continue

            # CLEAR_PROTOCOL_STATS
            if msg.msg_type == "CLEAR_PROTOCOL_STATS" and msg.msg_subtype == "REQUEST":
                self.protocol_stats.reset()
                continue
            
    def _demo_log_tick(self):
        """每秒塞一筆 demo log（僅在 selected_com_port=Demo Port 時）"""
        if self.selected_com_port != "Demo Port":
            return
        if self.comm_status != "Started":
            return
        now = time.monotonic()
        if now - self._last_demo_log_ts >= 1.0:
            self._last_demo_log_ts = now
            ts = time.strftime("%H:%M:%S", time.localtime())
            self.push_log(f"[{ts}] [Demo] logic_counter={self.counter}")
    
    def push_log(self, text: str):
        """將 log 放入 buffer，供 GUI 用 GET_LOG 拉取"""
        if text is None:
            return
        s = str(text).strip()
        if not s:
            return
        self._log_buffer.append(s)

    def _check_pr_timeout(self):
        """檢查 pending parameter read/write request 是否超時，超時自動重試一次再通知 GUI"""
        # Read timeout
        pending = self._pending_pr_read_request
        if pending is not None:
            elapsed = time.perf_counter() - pending["ts"]
            if elapsed >= self._pr_timeout_sec:
                # 超時 → 若尚有重試機會，重送封包
                if pending.get("retries", 0) < 5 and pending.get("pkt") is not None:
                    pending["retries"] = pending.get("retries", 0) + 1
                    pending["ts"] = time.perf_counter()
                    sent = self._send_serial_bytes(pending["pkt"])
                    ts = time.strftime('%H:%M:%S', time.localtime())
                    if ENABLE_PROTOCOL_LOG:
                        self.push_log(f"[{ts}][PR] RETRY #{pending['retries']} msg_ID={pending['msg_ID']} sent={sent}")
                    if sent:
                        self.protocol_stats.total_success_transmitted_packet += 1
                else:
                    # 重試用盡 → 通知 GUI
                    msg_type = pending["msg_type"]
                    msg_id = pending["msg_ID"]
                    self._pending_pr_read_request = None
                    ts = time.strftime('%H:%M:%S', time.localtime())
                    if ENABLE_PROTOCOL_LOG:
                        self.push_log(f"[{ts}][PR] TIMEOUT msg_ID={msg_id} type={msg_type}")
                    if msg_type == "GET_PR_VALUE":
                        resp = UIMsg(
                            msg_ID=msg_id,
                            msg_type="GET_PR_VALUE",
                            msg_subtype="TIMEOUT",
                            payload=None,
                        )
                        self.ipc.send("UIMsg_logic_to_gui", resp)
        # Write timeout
        pending = self._pending_pr_write_request
        if pending is not None:
            elapsed = time.perf_counter() - pending["ts"]
            if elapsed >= self._pr_timeout_sec:
                # 超時 → 若尚有重試機會，重送封包
                if pending.get("retries", 0) < 5 and pending.get("pkt") is not None:
                    pending["retries"] = pending.get("retries", 0) + 1
                    pending["ts"] = time.perf_counter()
                    sent = self._send_serial_bytes(pending["pkt"])
                    ts = time.strftime('%H:%M:%S', time.localtime())
                    if ENABLE_PROTOCOL_LOG:
                        self.push_log(f"[{ts}][PR] RETRY #{pending['retries']} msg_ID={pending['msg_ID']} sent={sent}")
                    if sent:
                        self.protocol_stats.total_success_transmitted_packet += 1
                else:
                    # 重試用盡 → 通知 GUI
                    msg_type = pending["msg_type"]
                    msg_id = pending["msg_ID"]
                    self._pending_pr_write_request = None
                    ts = time.strftime('%H:%M:%S', time.localtime())
                    if ENABLE_PROTOCOL_LOG:
                        self.push_log(f"[{ts}][PR] TIMEOUT msg_ID={msg_id} type={msg_type}")
                    if msg_type == "SET_PR_VALUE":
                        resp = UIMsg(
                            msg_ID=msg_id,
                            msg_type="SET_PR_VALUE",
                            msg_subtype="TIMEOUT",
                            payload=None,
                        )
                        self.ipc.send("UIMsg_logic_to_gui", resp)

    def handler(self):
        # Count logic process executions
        self.counter += 1

        # ✅ 每秒診斷統計
        now = time.perf_counter()
        self._diag_tick_count += 1
        if now - self._diag_last_ts >= 1.0:
            self._diag_tick_count = 0
            self._diag_ds_count = 0
            self._diag_bytes_read = 0
            self._diag_last_ts = now
        
        self.poll_ui_requests()

        # ✅ Real serial mode: poll and parse packets (Log 0xA5 + DS 0x5A)
        if self.selected_com_port not in (None, "Demo Port") and self.comm_status == "Started":
            self._poll_serial_and_parse_packets()

        # ✅ Check parameter request timeout (must be AFTER serial read to avoid false timeout)
        self._check_pr_timeout()
        
        # 僅在選到合法 Demo Port 且 comm_status=Started 後，才發送 demo log 與 demo signals
        if self.selected_com_port == "Demo Port" and self.comm_status == "Started":
            # 每秒新增 demo log
            self._demo_log_tick()

            LOOP_TIME = 100
            
            for timer in range(LOOP_TIME):
                # 取得瞬時 demo 訊號
                instant_signals = self.demo_signal_handler.get_instant_signals()
    
                # 傳送 demo signals 到 GUI
                self.ipc.send(
                    "HSDataSource_logic_to_gui",
                    HSDataSource(signals=instant_signals, sequence_num=self.demo_signal_handler.sequence_num),
                )
    
    def loop(self):        
        while 1:            
            # Get logic loop start time
            current_time = time.perf_counter()
            
            # Execute logic handler
            self.handler()
            
            # Calculate elapsed time
            elapsed = time.perf_counter() - current_time

            # Calculate sleep time to maintain 10ms cycle
            sleep_time = self.target_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)


#####################################################################################
def logic_main(ipc_queues):
    # Create a single global Logic instance
    LogicInstance = LogicHandle(ipc_queues)
    LogicInstance.loop()
