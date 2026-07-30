# wanna_play 🏀🗳️
wanna play? A Telegram Bot for initiating polls and fetching weather maps!

## 🌟 新功能介紹 (Windy 氣象整合)
現在，原有的 `/wea` 指令已被重構並升級！直接輸入 `/wea` 時，Bot 會透過 ipinfo 查詢執行主機的公網 IP 與位置；輸入 `/wea taipei`、`/wea 東京` 或 `/wea Tokyo, JP` 時，則會透過 Open-Meteo Geocoding 查詢指定城市的經緯度。取得座標後，Bot 會在背景啟動 headless Chromium 瀏覽器，平行載入該城市的 **Windy 累積雨量預報圖** 與 **即時雷達回波圖**，最後以相簿形式發送兩張天氣圖。

### 技術亮點：
1. **異步平行分頁載入**：擷取雙圖僅需 10 秒。
2. **自動移除 GDPR / Cookie 橫幅**：截圖乾淨無廣告遮擋。
3. **內建中文字型安裝**：解決地圖上中文地名出現亂碼、空白方塊的問題。
4. **網路逾時與多用戶排隊鎖機制**：大幅提升伺服器執行 headless 瀏覽器時的資源安全性。

---

## 🚀 運行與構建說明

因為加入了 Playwright 自動化瀏覽器環境，首次重啟時需要重新進行本機 Docker Image 構建（加上 `--build`）：

```bash
# 停止、重新構建並在背景啟動
docker compose down
docker compose up -d --build
```
