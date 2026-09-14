# 維大力體育APP V13 更新

## 會員專屬連結

後台不再需要反覆輸入 APP 網址。請在 Streamlit Secrets 一次設定：

```toml
APP_PUBLIC_URL = "https://你的APP名稱.streamlit.app"
APP_MEMBER_LINK_SECRET = "請自行設定一段長且不公開的隨機文字"
```

之後只要輸入會員帳號，例如 `dali_001`，就能產生專屬連結。連結以簽章防止帳號被手動替換，但不包含密碼；會員開啟後仍須輸入會員共用密碼。若 APP 在 Streamlit Cloud 仍為私人，外部會員會先被 Streamlit 擋住，必須將 APP 設為公開。

## 足球預估比分

目前版本保留既有 Football 模組的 Base Model。已確認大量 `1:1` 是現有預估比分顯示採用「主、客各自最可能進球數」的結果；修正為聯合比分分布最高的比分，及加強球隊統計／ClubElo 資料品質，必須在 Football 模組內進行，不會在 Core／會員連結層偷偷改寫。
