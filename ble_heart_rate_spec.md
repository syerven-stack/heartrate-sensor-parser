# BLE 0x2A37 心率协议 XOSS专用
## Flags 第1字节
- bit0：0=uint8 心率 (1 字节),1=uint16 心率
- bit1-bit2：传感器接触状态
- bit4：1 = 报文携带 RR 间期（每 2 字节小端，单位 1/1024s）
- XOSS 设备固定 Flags=0x10：8 位心率、携带 RR，无能耗字段
## 报文结构
- len=2：[Flags, HR] 无 RR
- len=4：[Flags, HR, RR_L, RR_H] 1 组 RR
- len=6：[Flags, HR, RR1_L, RR1_H, RR2_L, RR2_H] 2 组 RR
- len=8：[Flags, HR, RR1, RR2, RR3] 3 组 RR,RR 数值为小端序，单位 1/1024 秒
