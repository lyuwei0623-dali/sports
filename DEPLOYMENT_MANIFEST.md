# V12 上傳清單（請整包覆蓋）

V12 包含會員連結、足球聯賽選擇與人工輸入修正，必須與以下清單一起上傳。

請把本資料夾中的下列檔案全部上傳到 GitHub 專案根目錄；不要混用舊版同名檔案，也不要只挑其中幾個 `.py` 檔。

```text
app.py
app_services.py
admin_snapshot_ui.py
core_shared_ui.py
football_display.py
football_module.py
integration_adapter.py
live_calculator.py
live_ui.py
member_experience.py
member_release_service.py
manual_odds.py
member_links.py
mlb_pre_release_module.py
source_health.py
logo.png
requirements.txt
README.md
V12_UPDATE.md
REGRESSION_TESTS.md
DEPLOYMENT_MANIFEST.md
```

以下兩個檔案只在 Docker／VPS 部署時需要；使用 Streamlit Cloud 可以一起上傳，但不會被 APP 執行：

```text
Dockerfile
docker-compose.yml
```

`tests/` 是開發測試檔，不必上傳到 Streamlit Cloud。`jobs/` 是舊的背景排程入口，本版採管理員手動建立快照，不必上傳。

本次請務必以此 V12 資料夾內的所有清單檔案覆蓋舊版，避免新舊模組混用。
