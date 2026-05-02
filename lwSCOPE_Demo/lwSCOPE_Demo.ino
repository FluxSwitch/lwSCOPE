volatile unsigned long msCounter = 0;
volatile bool sendLogFlag = false;
volatile bool sendDsFlag = false;

#define LOG_INTERVAL_MS 1000
#define DS_INTERVAL_MS  1

// ====== 新協議：兩階段 CRC 封包格式 ======
// [0] 0x5A  [1] 0xA5  [2] PayloadLen  [3] PacketType
// [4] HeaderCRC16_H  [5] HeaderCRC16_L
// [6 .. 6+PayloadLen-1] Payload
// [6+PayloadLen] PacketCRC16_H  [6+PayloadLen+1] PacketCRC16_L
// Header CRC: bytes 0~3 | Packet CRC: bytes 0~(5+PayloadLen)

#define PKT_SYNC_0        0x5A
#define PKT_SYNC_1        0xA5
#define PKT_HEADER_LEN    6    // sync(2) + len(1) + type(1) + headerCRC(2)
#define PKT_CRC_LEN       2    // packet CRC at end

// Packet types
#define PKT_TYPE_LOG      0x00
#define PKT_TYPE_DS       0x01
#define PKT_TYPE_PARAM    0x02

// DS 封包定義
#define DS_NUM_CHANNELS  8

// DS 資料型態
#define DS_TYPE_U8      0
#define DS_TYPE_S8      1
#define DS_TYPE_U16     2
#define DS_TYPE_S16     3
#define DS_TYPE_U32     4
#define DS_TYPE_S32     5
#define DS_TYPE_FLOAT32 6

uint8_t dsSequence = 0;  // 封包序號 0~255

// DS 通道數據類型配置：DS1~DS8 分別對應 U8~FLOAT32
uint8_t dsChannelTypes[DS_NUM_CHANNELS] = {
  DS_TYPE_U8,      // DS1: U8
  DS_TYPE_S8,      // DS2: S8
  DS_TYPE_U16,     // DS3: U16
  DS_TYPE_S16,     // DS4: S16
  DS_TYPE_U32,     // DS5: U32
  DS_TYPE_S32,     // DS6: S32
  DS_TYPE_FLOAT32, // DS7: FLOAT32
  DS_TYPE_FLOAT32  // DS8: FLOAT32
};

// ====== Parameter Packet 定義 ======
// Payload (固定 8 bytes):
// [0] FuncCode [1] ParamType [2] AddrH [3] AddrL
// [4] Data3(MSB) [5] Data2 [6] Data1 [7] Data0(LSB)
#define PR_PAYLOAD_LEN    8
#define PR_PACKET_SIZE    (PKT_HEADER_LEN + PR_PAYLOAD_LEN + PKT_CRC_LEN)  // 16 bytes

// Function codes
#define PR_FUNC_READ_REQ   0x00
#define PR_FUNC_READ_RESP  0xFF
#define PR_FUNC_WRITE_REQ  0x55
#define PR_FUNC_WRITE_RESP 0xAA
#define PR_FUNC_READ_FAIL  0x11
#define PR_FUNC_WRITE_FAIL 0x22

// Parameter types (same as DS types)
#define PR_TYPE_U8      0
#define PR_TYPE_S8      1
#define PR_TYPE_U16     2
#define PR_TYPE_S16     3
#define PR_TYPE_U32     4
#define PR_TYPE_S32     5
#define PR_TYPE_FLOAT32 6

// ====== 範例參數表 ======
// 參數以 uint32_t 儲存（不論 type 均以 4 bytes 傳輸）
#define PARAM_TABLE_SIZE 7

struct ParamEntry {
  uint8_t  type;   // PR_TYPE_xxx
  uint32_t value;  // stored value
};

// 範例參數表（addr 0x0000 ~ 0x0006）
ParamEntry paramTable[PARAM_TABLE_SIZE] = {
  { PR_TYPE_U8,      100       },  // 0x0000: U8  範例
  { PR_TYPE_S8,      200       },  // 0x0001: S8  範例
  { PR_TYPE_U16,     1000      },  // 0x0002: U16 範例
  { PR_TYPE_S16,     2000      },  // 0x0003: S16 範例
  { PR_TYPE_U32,     100000    },  // 0x0004: U32 範例
  { PR_TYPE_S32,     50000     },  // 0x0005: S32 範例
  { PR_TYPE_FLOAT32, 0x4048F5C3},  // 0x0006: Float32 範例 (3.14)
};

// 串口接收緩衝區（新格式最大封包 = header(6) + payload(255) + CRC(2) = 263）
// 但目前只接收 Param 封包，大小固定 16 bytes
static uint8_t rxBuf[PR_PACKET_SIZE];
static uint8_t rxIdx = 0;
static uint8_t rxExpectedLen = 0;      // 從 header 解析出的預期封包總長
static unsigned long rxLastByteMs = 0;
#define PR_RX_TIMEOUT_MS 50

// ====== TX 發送佇列 ======
// 所有封包（DS / Log / Param）先入隊，txDrain() 逐包消化
// 每次發送前確認 UART TX buffer 有足夠空間，保證封包不交錯
#define TX_QUEUE_SLOTS    16       // 佇列深度
#define TX_MAX_PKT_SIZE   48       // slot 最大封包長度 (DS=34, PR=16, Log≈18)

struct TxPacket {
  uint8_t data[TX_MAX_PKT_SIZE];
  uint8_t len;
};

static TxPacket txQueue[TX_QUEUE_SLOTS];
static uint8_t txHead  = 0;   // 下一個要發送的索引
static uint8_t txTail  = 0;   // 下一個要寫入的索引
static uint8_t txCount = 0;   // 佇列中的封包數
static unsigned long txOverflowCount = 0;  // TX 佇列溢出次數
static unsigned long prRxCount = 0;  // 成功收下的 parameter read request 次數
static unsigned long prTxCount = 0;  // 成功發出的 parameter read response 次數
static unsigned long prCrcFail = 0;  // RX 封包 CRC 驗證失敗次數

// 將封包加入發送佇列
bool txEnqueue(const uint8_t *pkt, uint8_t len) {
  if (len == 0 || len > TX_MAX_PKT_SIZE) return false;
  if (txCount >= TX_QUEUE_SLOTS) { txOverflowCount++; return false; }
  memcpy(txQueue[txTail].data, pkt, len);
  txQueue[txTail].len = len;
  txTail = (txTail + 1) % TX_QUEUE_SLOTS;
  txCount++;
  return true;
}

// 消化佇列：盡量多送，每包確認 TX buffer 足夠才發送
void txDrain() {
  while (txCount > 0) {
    uint8_t len = txQueue[txHead].len;
    // 確認 TX buffer 可容納整包（前一包資料已發送完畢）
    if (Serial.availableForWrite() < len) return;
    Serial.write(txQueue[txHead].data, len);
    txHead = (txHead + 1) % TX_QUEUE_SLOTS;
    txCount--;
  }
}

// 波形產生
#define WAVE_PERIOD   1000  // 1000 步 * 1ms = 1 秒一個週期
#define WAVE_AMP      511
#define WAVE_OFFSET   512
uint16_t waveStep = 0;

// Modbus CRC16 (多項式 0xA001, 初始值 0xFFFF)
uint16_t crc16(const uint8_t *data, uint8_t len) {
  uint16_t crc = 0xFFFF;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t j = 0; j < 8; j++) {
      if (crc & 0x0001)
        crc = (crc >> 1) ^ 0xA001;
      else
        crc = crc >> 1;
    }
  }
  return crc;
}

// ====== 通用封包建構 ======
// 將 payload 包裝為新協議格式，寫入 outBuf，回傳封包總長
// outBuf 大小需 >= PKT_HEADER_LEN + payloadLen + PKT_CRC_LEN
uint8_t buildPacket(uint8_t *outBuf, uint8_t pktType, const uint8_t *payload, uint8_t payloadLen) {
  // Header: sync(2) + length(1) + type(1)
  outBuf[0] = PKT_SYNC_0;
  outBuf[1] = PKT_SYNC_1;
  outBuf[2] = payloadLen;
  outBuf[3] = pktType;

  // Header CRC16: bytes 0~3
  uint16_t hCrc = crc16(outBuf, 4);
  outBuf[4] = (uint8_t)(hCrc >> 8);
  outBuf[5] = (uint8_t)(hCrc & 0xFF);

  // Payload
  if (payloadLen > 0) {
    memcpy(&outBuf[PKT_HEADER_LEN], payload, payloadLen);
  }

  // Packet CRC16: bytes 0 ~ (5 + payloadLen)
  uint8_t totalBeforeCrc = PKT_HEADER_LEN + payloadLen;
  uint16_t pCrc = crc16(outBuf, totalBeforeCrc);
  outBuf[totalBeforeCrc]     = (uint8_t)(pCrc >> 8);
  outBuf[totalBeforeCrc + 1] = (uint8_t)(pCrc & 0xFF);

  return totalBeforeCrc + PKT_CRC_LEN;
}

// 發送 Log 封包 (PacketType=0x00, payload=ASCII string)
#define LOG_MAX_PAYLOAD  (TX_MAX_PKT_SIZE - PKT_HEADER_LEN - PKT_CRC_LEN)  // 可用 payload 上限

bool sendLogPacket(const char *logStr) {
  uint8_t logLen = strlen(logStr);
  if (logLen > 255) logLen = 255;
  if (logLen > LOG_MAX_PAYLOAD) logLen = LOG_MAX_PAYLOAD;

  uint8_t packet[TX_MAX_PKT_SIZE];
  uint8_t pktLen = buildPacket(packet, PKT_TYPE_LOG, (const uint8_t *)logStr, logLen);
  return txEnqueue(packet, pktLen);
}

// 發送參數封包回應 (PacketType=0x02, payload=8 bytes)
bool sendParamPacket(uint8_t funcCode, uint8_t paramType, uint16_t addr, uint32_t data) {
  uint8_t payload[PR_PAYLOAD_LEN];
  payload[0] = funcCode;
  payload[1] = paramType;
  payload[2] = (uint8_t)(addr >> 8);
  payload[3] = (uint8_t)(addr & 0xFF);
  payload[4] = (uint8_t)(data >> 24);
  payload[5] = (uint8_t)(data >> 16);
  payload[6] = (uint8_t)(data >> 8);
  payload[7] = (uint8_t)(data & 0xFF);

  uint8_t packet[PR_PACKET_SIZE];
  uint8_t pktLen = buildPacket(packet, PKT_TYPE_PARAM, payload, PR_PAYLOAD_LEN);
  if (txEnqueue(packet, pktLen)) return true;
  // 佇列滿：先 drain 再重試一次
  txDrain();
  return txEnqueue(packet, pktLen);
}

// 處理收到的參數請求封包（payload 為 8 bytes）
void handleParamRequest(const uint8_t *payload) {
  uint8_t funcCode  = payload[0];
  uint8_t paramType = payload[1];
  uint16_t addr = ((uint16_t)payload[2] << 8) | payload[3];
  uint32_t data = ((uint32_t)payload[4] << 24) | ((uint32_t)payload[5] << 16)
                | ((uint32_t)payload[6] << 8)  | payload[7];

  if (funcCode == PR_FUNC_READ_REQ) {
    prRxCount++;
    if (addr < PARAM_TABLE_SIZE) {
      if (sendParamPacket(PR_FUNC_READ_RESP, paramTable[addr].type, addr, paramTable[addr].value)) {
        prTxCount++;
      }
    } else {
      sendParamPacket(PR_FUNC_READ_FAIL, paramType, addr, 0);
    }
  } else if (funcCode == PR_FUNC_WRITE_REQ) {
    if (addr < PARAM_TABLE_SIZE) {
      paramTable[addr].value = data;
      sendParamPacket(PR_FUNC_WRITE_RESP, paramTable[addr].type, addr, paramTable[addr].value);
    } else {
      sendParamPacket(PR_FUNC_WRITE_FAIL, paramType, addr, 0);
    }
  }
}

// 處理串口接收資料，解析兩階段 CRC 封包（目前只處理 Param 封包）
void pollSerialForParamPackets() {
  // 超時重置
  if (rxIdx > 0 && (millis() - rxLastByteMs) > PR_RX_TIMEOUT_MS) {
    rxIdx = 0;
    rxExpectedLen = 0;
  }

  while (Serial.available()) {
    uint8_t b = Serial.read();
    rxLastByteMs = millis();

    if (rxIdx == 0) {
      // 等待第一個 sync byte
      if (b == PKT_SYNC_0) {
        rxBuf[rxIdx++] = b;
      }
      continue;
    }

    if (rxIdx == 1) {
      // 第二個 byte 必須是 0xA5
      if (b == PKT_SYNC_1) {
        rxBuf[rxIdx++] = b;
      } else if (b != PKT_SYNC_0) {
        rxIdx = 0;  // 不是 sync pair，重置
      }
      // 如果 b == PKT_SYNC_0，保持 rxIdx=1（re-sync，等下一個 0xA5）
      continue;
    }

    rxBuf[rxIdx++] = b;

    // 收到 4 bytes 後可讀取 PayloadLen
    if (rxIdx == 4) {
      uint8_t payloadLen = rxBuf[2];
      rxExpectedLen = PKT_HEADER_LEN + payloadLen + PKT_CRC_LEN;
      // 只接受 Param 封包（固定 16 bytes），其他封包忽略
      if (rxExpectedLen > PR_PACKET_SIZE) {
        rxIdx = 0;
        rxExpectedLen = 0;
        continue;
      }
    }

    // 收到 6 bytes 時做 Stage 1: Header CRC 驗證
    if (rxIdx == PKT_HEADER_LEN) {
      uint16_t hCrcRecv = ((uint16_t)rxBuf[4] << 8) | rxBuf[5];
      uint16_t hCrcCalc = crc16(rxBuf, 4);
      if (hCrcCalc != hCrcRecv) {
        prCrcFail++;
        rxIdx = 0;
        rxExpectedLen = 0;
        continue;
      }
    }

    if (rxExpectedLen == 0 || rxIdx < rxExpectedLen) {
      continue;  // 還沒收滿
    }

    // 收滿 → Stage 2: Packet CRC 驗證
    uint16_t pCrcRecv = ((uint16_t)rxBuf[rxExpectedLen - 2] << 8) | rxBuf[rxExpectedLen - 1];
    uint16_t pCrcCalc = crc16(rxBuf, rxExpectedLen - 2);

    if (pCrcCalc == pCrcRecv) {
      uint8_t pktType = rxBuf[3];
      if (pktType == PKT_TYPE_PARAM) {
        // payload 起始於 rxBuf[6]，長度 PR_PAYLOAD_LEN
        handleParamRequest(&rxBuf[PKT_HEADER_LEN]);
      }
      // 其他 packet type 在 Arduino 端忽略
    } else {
      prCrcFail++;
    }

    rxIdx = 0;
    rxExpectedLen = 0;
    break;  // 一問一答：處理完一包就跳出
  }
}

// DS 值的聯合體，支持所有數據類型
union DsValue {
  uint8_t u8;
  int8_t s8;
  uint16_t u16;
  int16_t s16;
  uint32_t u32;
  int32_t s32;
  float f32;
};

// 發送 DS 封包 (PacketType=0x01)
// Payload: Seq(1)+NumDS(1)+8*(Type(1)+Data(variable)) = variable bytes
// 每個通道的數據長度根據類型而定：U8/S8=1, U16/S16=2, U32/S32/FLOAT32=4
#define DS_PACKET_SIZE   (TX_MAX_PKT_SIZE)  // 使用最大緩衝區

void sendDsPacket(const union DsValue *values, uint8_t numCh) {
  uint8_t payload[TX_MAX_PKT_SIZE - PKT_HEADER_LEN - PKT_CRC_LEN];
  uint8_t idx = 0;

  payload[idx++] = dsSequence++;  // Packet sequence (auto wrap 0~255)
  payload[idx++] = numCh;        // Numbers of DS

  for (uint8_t i = 0; i < numCh; i++) {
    uint8_t type = dsChannelTypes[i];
    payload[idx++] = type;

    // 根據類型序列化數據（Big-Endian）
    switch (type) {
      case DS_TYPE_U8:
        payload[idx++] = values[i].u8;
        break;
      case DS_TYPE_S8:
        payload[idx++] = (uint8_t)values[i].s8;
        break;
      case DS_TYPE_U16:
        payload[idx++] = (uint8_t)(values[i].u16 >> 8);
        payload[idx++] = (uint8_t)(values[i].u16 & 0xFF);
        break;
      case DS_TYPE_S16:
        payload[idx++] = (uint8_t)(values[i].s16 >> 8);
        payload[idx++] = (uint8_t)(values[i].s16 & 0xFF);
        break;
      case DS_TYPE_U32:
        payload[idx++] = (uint8_t)(values[i].u32 >> 24);
        payload[idx++] = (uint8_t)(values[i].u32 >> 16);
        payload[idx++] = (uint8_t)(values[i].u32 >> 8);
        payload[idx++] = (uint8_t)(values[i].u32 & 0xFF);
        break;
      case DS_TYPE_S32:
        payload[idx++] = (uint8_t)(values[i].s32 >> 24);
        payload[idx++] = (uint8_t)(values[i].s32 >> 16);
        payload[idx++] = (uint8_t)(values[i].s32 >> 8);
        payload[idx++] = (uint8_t)(values[i].s32 & 0xFF);
        break;
      case DS_TYPE_FLOAT32:
      {
        // 用位複製的方式將 float 轉為 uint32_t
        uint32_t bits = *(uint32_t *)&values[i].f32;
        payload[idx++] = (uint8_t)(bits >> 24);
        payload[idx++] = (uint8_t)(bits >> 16);
        payload[idx++] = (uint8_t)(bits >> 8);
        payload[idx++] = (uint8_t)(bits & 0xFF);
        break;
      }
    }
  }

  uint8_t packet[TX_MAX_PKT_SIZE];
  uint8_t pktLen = buildPacket(packet, PKT_TYPE_DS, payload, idx);
  txEnqueue(packet, pktLen);
}

void setup() {
  Serial.begin(1000000);
  randomSeed(analogRead(A0));

  // 設定 Timer1 為 CTC 模式，每 1ms 觸發一次中斷
  noInterrupts();
  TCCR1A = 0;
  TCCR1B = 0;
  TCNT1 = 0;
  // OCR1A = (F_CPU / (prescaler * 頻率)) - 1
  // = (16000000 / (64 * 1000)) - 1 = 249
  OCR1A = 249;
  TCCR1B |= (1 << WGM12);             // CTC 模式
  TCCR1B |= (1 << CS11) | (1 << CS10); // prescaler 64
  TIMSK1 |= (1 << OCIE1A);            // 啟用 Compare Match A 中斷
  interrupts();
}

void loop() {
  // 消化 TX 佇列（確認上一包發送完成才送下一包）
  txDrain();

  // 每次迴圈都檢查串口是否有參數請求封包
  pollSerialForParamPackets();

  // 每 1ms 發送 DS 封包
  if (sendDsFlag) {
    sendDsFlag = false;

    union DsValue values[DS_NUM_CHANNELS];
    uint16_t pos = waveStep % WAVE_PERIOD;
    uint16_t half = WAVE_PERIOD / 2;
    float phaseRad = 2.0 * PI * pos / WAVE_PERIOD;

    // DS1 (U8, 0~255): 正弦波，offset=127, amp=127
    values[0].u8 = (uint8_t)(127 + 127 * sin(phaseRad));

    // DS2 (S8, -128~127): 正弦波，offset=0, amp=127
    values[1].s8 = (int8_t)(127 * sin(phaseRad));

    // DS3 (U16, 0~65535): 正弦波，offset=32767.5, amp=32767.5
    values[2].u16 = (uint16_t)(32768 + 32767 * sin(phaseRad));

    // DS4 (S16, -32768~32767): 正弦波，offset=0, amp=32767
    values[3].s16 = (int16_t)(32767 * sin(phaseRad));

    // DS5 (U32, 0~1000000): 三角波，offset=500000, amp=500000
    {
      uint32_t v;
      if (pos < half)
        v = (uint32_t)((uint64_t)1000000 * pos / half);
      else
        v = (uint32_t)((uint64_t)1000000 * (WAVE_PERIOD - pos) / half);
      values[4].u32 = v;
    }

    // DS6 (S32, -500000~500000): 三角波，offset=0, amp=500000
    {
      int32_t v;
      if (pos < half)
        v = (int32_t)((uint64_t)1000000 * pos / half) - 500000;
      else
        v = (int32_t)((uint64_t)1000000 * (WAVE_PERIOD - pos) / half) - 500000;
      values[5].s32 = v;
    }

    // DS7 (FLOAT32, -1~1): 正弦波浮點版本
    values[6].f32 = sin(phaseRad);

    // DS8 (FLOAT32, 0~1): 三角波浮點版本
    {
      float v;
      if (pos < half)
        v = (float)pos / half;
      else
        v = (float)(WAVE_PERIOD - pos) / half;
      values[7].f32 = v;
    }

    waveStep++;
    if (waveStep >= WAVE_PERIOD) waveStep = 0;

    sendDsPacket(values, DS_NUM_CHANNELS);
  }

  // DS 處理後再次檢查串口，縮短 RX 空窗期
  pollSerialForParamPackets();

  // 每 1000ms 發送 Log 封包
  if (sendLogFlag) {
    // 組合 log 字串，報告計數器
    char logBuf[48];
    snprintf(logBuf, sizeof(logBuf), "ovf:%lu Rx:%lu Tx:%lu crc:%lu", txOverflowCount, prRxCount, prTxCount, prCrcFail);
    if (sendLogPacket(logBuf)) {
      sendLogFlag = false;  // 入隊成功才清除 flag
    }
    // 入隊失敗則 sendLogFlag 保持 true，下次 loop 重試
  }
}

// Timer1 Compare Match A 中斷，每 1ms 執行一次
ISR(TIMER1_COMPA_vect) {
  msCounter++;
  // 每 1ms 觸發一次 DS 發送
  if (msCounter % DS_INTERVAL_MS == 0) {
    sendDsFlag = true;
  }
  // 每 1000ms 觸發一次 log 發送
  if (msCounter % LOG_INTERVAL_MS == 0) {
    sendLogFlag = true;
  }
}
