# v7 可追溯回歸測試

執行日期：2026-09-12T08:36:33.927356+00:00

全部21項通過；大部分為受控資料及錯誤注入，非真實付費API測試。

| ID | 日期 | 運動 | 快照類型 | 結果 | 錯誤分類／測試情境 | 會員可見結果 |
|---|---|---|---|---|---|---|
| T01 | 2026-09-12 | 共用 | automatic／展示 | PASS | test_admin_snapshot_ui.AdminSnapshotUiTest.test_failure_never_echoes_raw_error | PASS／保留已存資料 |
| T02 | 2026-09-12 | Football | automatic／展示 | PASS | test_admin_snapshot_ui.AdminSnapshotUiTest.test_football_runner_result_is_a_safe_success_summary | 已存快照表格／安全摘要 |
| T03 | 2026-09-12 | MLB | automatic／展示 | PASS | test_admin_snapshot_ui.AdminSnapshotUiTest.test_mlb_runner_result_is_a_safe_success_summary | 已存快照表格／安全摘要 |
| T04 | 2026-09-12 | Football | automatic／展示 | PASS | test_football_member_metadata.FootballMemberMetadataTest.test_admin_preview_uses_member_table_without_release_time_gate | 已存快照表格／安全摘要 |
| T05 | 2026-09-12 | Football | automatic／展示 | PASS | test_football_member_metadata.FootballMemberMetadataTest.test_auto_provenance_and_stale_warning_reach_core | 已存快照表格／安全摘要 |
| T06 | 2026-09-12 | Football | automatic／展示 | PASS | test_football_member_metadata.FootballMemberMetadataTest.test_invalid_kickoff_is_preserved_without_crashing | 已存快照表格／安全摘要 |
| T07 | 2026-09-12 | Football | automatic／展示 | PASS | test_football_member_metadata.FootballMemberMetadataTest.test_no_snapshot_uses_required_message | 無快照更新提示 |
| T08 | 2026-09-12 | Football | automatic／展示 | PASS | test_football_member_metadata.FootballMemberMetadataTest.test_utc_z_kickoff_is_displayed_as_taiwan_month_day_time | 已存快照表格／安全摘要 |
| T09 | 2026-09-12 | Football | automatic／展示 | PASS | test_football_schedule_fallback.FootballScheduleFallbackTest.test_api_football_failure_falls_back_to_espn | PASS／保留已存資料 |
| T10 | 2026-09-12 | Football | automatic／展示 | PASS | test_football_schedule_fallback.FootballScheduleFallbackTest.test_games_without_odds_remain_in_complete_schedule | PASS／保留已存資料 |
| T11 | 2026-09-12 | MLB | automatic／展示 | PASS | test_mlb_runner_compatibility.MlbRunnerCompatibilityTest.test_missing_optional_store_key_does_not_raise_key_error | 已存快照表格／安全摘要 |
| T12 | 2026-09-12 | MLB | automatic／展示 | PASS | test_mlb_runner_compatibility.MlbRunnerCompatibilityTest.test_odds_failure_keeps_official_schedule_as_pass_snapshot | PASS／保留已存資料 |
| T13 | 2026-09-12 | Football | automatic／展示 | PASS | test_v7_dates.DatesTest.test_espn_filters_by_taiwan_date_across_utc_midnight | 已存快照表格／安全摘要 |
| T14 | 2026-09-12 | Football | automatic／展示 | PASS | test_v7_integration.V7IntegrationTest.test_accent_aliases | 已存快照表格／安全摘要 |
| T15 | 2026-09-12 | Football | manual | PASS | test_v7_integration.V7IntegrationTest.test_football_manual_snapshot_wins_and_member_sql_is_read_only | 人工版本優先 |
| T16 | 2026-09-12 | Football | automatic／展示 | PASS | test_v7_integration.V7IntegrationTest.test_football_table_layout_and_input_immutability | 已存快照表格／安全摘要 |
| T17 | 2026-09-12 | Football | automatic／展示 | PASS | test_v7_integration.V7IntegrationTest.test_forecast_is_only_projection_of_saved_model | 已存快照表格／安全摘要 |
| T18 | 2026-09-12 | Football | automatic／展示 | PASS | test_v7_integration.V7IntegrationTest.test_member_does_not_compute_forecast | 已存快照表格／安全摘要 |
| T19 | 2026-09-12 | MLB | automatic／展示 | PASS | test_v7_integration.V7IntegrationTest.test_mlb_failure_preserves_existing_auto_and_football | PASS／保留已存資料 |
| T20 | 2026-09-12 | Football | automatic／展示 | PASS | test_v7_integration.V7IntegrationTest.test_odds_auth_diagnostics_do_not_expose_secret | 已存快照表格／安全摘要 |
| T21 | 2026-09-12 | Football | automatic／展示 | PASS | test_v7_integration.V7IntegrationTest.test_ordered_team_matching_rejects_reversed_market_sides | 已存快照表格／安全摘要 |

實際外部查詢：MLB 2026-09-13 台灣日期回傳15場，耗時14.2秒。ClubElo HTTP與HTTPS皆逾時，尚未恢復驗證。

領域保護：MLB 與 Football 各6個 Base Model／盤口／推薦與結算函式 AST 與v5相同。

未完成驗收：真實API認證、真實盤口產生推薦、ClubElo恢復、完整Streamlit瀏覽器部署。未知球隊中文對照保留原名。