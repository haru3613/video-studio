# README 首屏研究：首次訪客路徑的參考

查核日期：2026-09-22。以下只讀取各專案擁有的公開 README（直接連至當時的 `main`），沒有以星數、下載量或外部評論推論轉換效果。引文均為原文，且每一來源少於 25 個英文詞。

此研究使用的訪客路徑是：**GitHub 首頁 → 理解用途 → 看見可驗證的成果 → 選 CLI 或 MCP → 安裝 → 做第一支片 → 審片／匯出**。觀察是來源明示的內容；「可移植推論」是針對 Video Studio README 的設計建議，並非來源聲稱的效果。

## 1. Remotion

來源：[Remotion README](https://github.com/remotion-dev/remotion/blob/main/README.md)（查核：2026-09-22）

**觀察**

* 首屏先放動畫品牌圖，再用一句話定義產品："Video tools for the agent era." 接著以「agentically / interactively / programmatically」三種建立影片的方式，讓讀者自行對應自己的工作方式。
* 它接著列出自動化情境（設計系統、批次 render、應用程式），因此回答「規模擴大後能做什麼」。
* 讀者若已有 Node.js，第一個行動只有一條 `npx create-video@latest`；沒有 Node.js 則明確指向安裝頁。
* 首屏沒有一段具體成片的 GIF／影片案例；動畫 logo 是品牌視覺，不能當作「已產出影片」的證據。
* 授權限制沒有隱藏：README 明確要求讀者檢查特殊授權與公司使用情況。

| 訪客問題 | README 中可觀察到的回答 | 可移植推論給 Video Studio |
| --- | --- | --- |
| 這是什麼？ | 面向 agent 時代的影片工具，且以三個建立方式描述。 | 第一屏用一句具體成果描述：本機把自備旁白、SRT 與素材製成可審可匯出的影片；避免以「workflow orchestration」作為唯一說明。 |
| 適合我嗎？ | 三條路徑讓 agent、互動編輯與程式化使用者辨識自己。 | 並列「在終端機操作（人或有 shell 的 agent）」與「把工具接到 MCP client」。以操作方式分流，兩者都能用於 agent 製片。 |
| 第一步做什麼？ | 已具備前置條件者得到一條命令；其他人得到特定說明頁。 | 讓每個入口各有一條可複製、已標示前置條件的安裝／初始化命令。 |
| 有哪些界限？ | 公開說明授權條件。 | 在首屏附近明寫「沒有雲端服務、不提供 API key、不會自動合成或替換你的旁白」，把資料與責任邊界變成選擇依據。 |

## 2. Playwright MCP

來源：[microsoft/playwright-mcp README](https://github.com/microsoft/playwright-mcp/blob/main/README.md)（查核：2026-09-22）

**觀察**

* 開頭先定義 MCP server 與其能力，再把同一能力的 CLI 與 MCP 分別適合的情境說清楚；它沒有假裝兩者是同一個入口。
* README 把選擇依據落在可觀察的工作差異：CLI 的精簡指令／context 成本，和 MCP 的持續狀態、檢視與迭代能力。
* 「Getting started」先給大多數 client 可用的一份標準設定；各 client 的細節收在可展開區塊，Codex 也有獨立設定例。
* 需求（Node 18+、MCP client）在設定之前列出；長篇 option 表留在初始設定之後。
* README 沒有提供一個端到端的自動化成果 demo；它的首個可驗證結果是「client 裡已註冊此 MCP」。

| 訪客問題 | README 中可觀察到的回答 | 可移植推論給 Video Studio |
| --- | --- | --- |
| CLI 與 MCP 哪個該選？ | 以各自適合的 agent 工作迴圈回答。 | 放一張簡短比較表：終端機或可執行 shell 的 agent 可選 CLI；偏好 MCP 工具連接時選本機 stdio。兩者共用持久任務、審片、重試與專案格式；私人 HTTP 是進階部署方式。 |
| 要準備什麼？ | 在配置前列出 Node 與 client。 | 首屏 Quick start 前先列 OS、FFmpeg、Node／Rust（如實際需要）和「不需雲端帳號」；把平台差異連到細節頁。 |
| 如何開始？ | 有一份可直接貼上的標準 config，客戶端特例收合。 | MCP 預設顯示 stdio 的最小設定；HTTP MCP 收在第二層並明示需要自行設定驗證與儲存空間。 |
| 成功時看什麼？ | server 可被 client 安裝／使用。 | 指定可核對的成功訊號，例如 MCP client 列出工具；製片範例則以 job 成功及 final.mp4 可播放作為終點，不假設泛用 status 會顯示某個 ready 狀態。 |

## 3. OpenCut

來源：[OpenCut README](https://github.com/OpenCut-app/OpenCut/blob/main/README.md)（查核：2026-09-22）

**觀察**

* 首屏以 logo、產品名和一句「free and open source video editor」定義可使用的平台。
* 它把專案正處於重寫的狀態放在安裝指令之前，並明說現有正式網站仍使用 classic 版本；這是狀態與可用版本的明確區分。
* 「What's coming」包含 MCP、headless 與 scripting 等能力，但同段同時標示為未來項目，避免把 roadmap 當成現成功能。
* 開發命令是為貢獻者啟動各服務；它不是一般創作者製作第一支片的導覽。
* 因為重寫中，README 清楚表示尚未準備接受外部貢獻，並改提供 Discord／issue 作為下一步。

| 訪客問題 | README 中可觀察到的回答 | 可移植推論給 Video Studio |
| --- | --- | --- |
| 現在能用什麼？ | 現況、classic 與重寫版的邊界被直接寫出。 | 清楚區分「今天可完成的完整離線路徑」與尚未支援的內容；不要把設想中的雲端、供應商或 GUI 寫成現在可用。 |
| 這會在哪裡運作？ | 產品平台範圍在標題旁立即可見。 | 立即說明「自架、本機優先；CLI、stdio MCP、驗證 HTTP MCP」，並附到相應安裝段落的錨點。 |
| 我是使用者還是貢獻者？ | 開發設定與貢獻狀態分開說。 | 使用者 Quick start 應先於開發者 setup；把 `scripts/verify`、架構與貢獻規則移到後段，避免把製片入口誤認成開發環境。 |

## 跨案例可採用的編排原則（設計推論）

三份 README 都先回答「此刻能做什麼」，再給首次行動，最後才展開選項、開發或社群資訊。對 Video Studio，建議首屏依下列順序服務真實訪客問題：

1. **一句成品承諾與一個實際成品證據。** 放入可播放的短 demo／GIF 或一張有連結的成片封面；旁邊以一行說明它使用的是自備旁白、SRT 與動態素材。這能同時回答「成品長什麼樣」與「是否真的能跑完」。若 demo 使用示範聲音，必須標註，避免誤導為語音品質保證。
2. **兩條可選入口。** CLI 與 MCP 並列，說明何時選哪個，且各自放最小的第一個指令／設定。不要要求讀者先讀完整架構才判斷入口。
3. **第一支片的可複製路徑。** 以單一小範例展示：放入音檔與 SRT → 套用 project spec 與素材 → render → 本機審片 → export；每步附預期輸出或檔案位置。這是 README 應直接驗證的流程，不只是列能力。
4. **一眼可見的邊界。** 沒有雲端服務；使用者控制聲音與 provider；HTTP MCP 是自架且需自行驗證；素材、render 與輸出留在自己的環境。連到詳細安全與部署文件即可，避免在首屏塞入全部設定。
5. **其餘細節漸進展開。** 系統需求、私有 HTTP 部署、可替換 TTS provider、品質 gates、開發／貢獻放到第二層連結或可收合區塊；每段仍回指下一個可完成的動作。

這些是資訊結構假設，尚未做 A/B 測試或宣稱能提高 adoption。實作後可用全新 clone 跑一次兩個 Quick start，記錄從 clone 到可審片／匯出的命令與耗時，作為 README 是否忠於產品的驗收。
