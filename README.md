# lwSCOPE
A lightweight oscilloscope/data visualizer made with DearPyGui  
一個以 DearPyGui 製作的輕量級示波器／資料視覺化工具  

## Demo
https://github.com/user-attachments/assets/db268672-d38c-46f9-9661-d5a76764633a



## Features
- Data exchange over UART with customizable baud rate and frame format.  
	以 UART 通訊進行數據交換，通訊速率與格式可自定義。
- Communication protocol with CRC validation (includes Arduino demo code).  
	採用含 CRC 校驗的通訊協定（附帶 Arduino 的 demo code）。  
	For protocol definitions, see the Draw.io file in the Protocols folder.  
	通訊協定定義請見Protocols folder中定義的drawio檔
- Communication health statistics for monitoring.  
	提供通訊健康程度的統計訊息供監看。
- Adjustable waveform buffer length (up to 16 channels, 1,000,000 points each).  
	可調整的波形數據暫存長度（最大 16 通道，各 1,000,000 點）。
- Save waveforms as PNG or CSV files.  
	波形可存成 PNG 檔或 CSV 檔。
- Multi-canvas display.  
	多畫布顯示。
- High refresh-rate rendering at 60 fps.  
	60fps 高刷新率顯示。
- Quickly drag specific waveform channels and cursors onto canvases.  
	快速拖曳指定波形通道與 cursor 至畫布。
- Customizable waveform colors.  
	波形顏色可自定義。
- Flexible X/Y axis controls: auto-fit, free panning, and box zoom.  
	彈性的 X 軸與 Y 軸：auto-fit、自由拖曳、自由框選放大。
- Configurable target parameters for field tuning and command dispatch.  
	可規劃受監控者的參數（用於現場調試、命令下達）。
- ASCII string log (similar to print output).  
	ASCII string log（相當於 print）。

## Why
The design philosophy is smooth interaction, simplicity, and practicality above all.  
設計理念是流暢的操作、簡單與實用性至上  


## Current state
Version 1.0 is now released, and the source code is available under the MIT license. Feel free to fork it.  
1.0 版現已發布，原始碼以 MIT 授權公開，請隨意 fork。

This is purely a hobby project, and it follows an **“extreme function-oriented development”** style.  
這純粹是一個業餘項目，並且是 **「極致的功能導向開發」**。

⚠ **Note!!! → My personal preference > Functionality > Maintainability**  
⚠ **注意!!! → 我的個人喜好 > 功能性 > 維護性**

This project is built using *vibe coding* techniques in my spare time.  
此專案是我在閒暇之餘使用 vibe coding 技巧建構的。

Therefore, the code quality is extremely messy (with limited time, refactoring may be far, far away).  
因此該專案的程式碼極度混亂（在有限的時間內，重構大概遙遙無期）。

For now, I can only review the architecture-level constraints as much as possible.  
目前我只能盡可能地基於架構面的約束進行審查。

The remaining parts will be extended by AI agents during development (based on the architecture I defined).  
剩下的部分會在開發過程中由 AI agent 自行擴充（基於我定義的軟體架構之下）。

If you like this software, feel free to leave your thoughts via Issues.  
如果你喜歡此軟體，可以透過 issue 寫下您的想法。

You may make feature requests — but I cannot promise anything.  
您可以許願，但我無法給予任何保證。

(Life is short. Enjoy it. Spend more time with your family.)  
（人生苦短，請及時行樂，多陪伴家人。）  
