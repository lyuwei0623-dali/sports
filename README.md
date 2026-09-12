# SPORTS QUANT V3

這是取代舊版單體 `app.py` 的新版 APP。它只使用模組化的 Core、MLB、Football 與會員發布服務；舊資料表與舊發布頁不在此專案中。

## 資料與責任邊界

| 路徑 | 允許行為 |
| --- | --- |
| 會員頁 | 只讀 MLB／Football 已儲存快照；台灣 19:30 前由 Core 顯示封鎖訊息 |
| 後台 | 自動快照、人工盤口校正、重新計算與發布 |
| 後台「自動存取快照」按鈕 | 管理員手動抓取、運算並保存自動快照 |

人工發布永遠優先於自動快照。MLB 的自動快照是 The Odds API 基準盤，並不冒充已人工 SUPER 校正；Football 使用既有標準亞洲盤計算。

## 首次啟動

1. 將 `.env.example` 複製成 `.env`，填入密碼與 API Key。`.env` 不可提交至 GitHub。
2. 安裝：`pip install -r requirements.txt`
3. 啟動：`streamlit run app.py`

Docker 方式：`docker compose up -d --build`。

## 快照建立方式

目前 Streamlit 部署採管理員手動方式：登入管理員後台後，按 MLB 或 Football 的「立即執行自動快照」。成功保存後，會員端立即讀取該份保存的快照；會員端本身絕不抓取或重新運算資料。

## 手動盤口上傳格式

MLB：

```json
{"12345":[{"market_type":"spread","side":"home","raw_line":"-1+65","price":0.94}]}
```

Football：

```json
{"98765":[{"market_type":"spread","side":"home","line":-0.75,"decimal_price":1.94}]}
```

Football 的人工發布需涵蓋該日期所有已保存賽事，這是 Football 模組的既有完整校正門檻。
