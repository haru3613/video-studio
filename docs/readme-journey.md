# Video Studio：首次訪客到第一支影片的走查

日期：2026-09-22。查核基準：`3e22520`。角色假設：會使用 AI coding agent、能執行終端機指令，第一次看到 Video Studio，希望做一支有旁白的解說短片；尚未準備測試用的音檔與 SRT。

這是模擬新手的認知走查，不是真實受測者訪談或轉換率研究。內心對話是設計假設；頁面、文件與 CLI 回應是本輪查核證據。

## 查核範圍

實際操作了 GitHub 專案首頁 → README → 安裝段落 → user-media 指南 → 回到審片／匯出段落 → MCP 段落 → template guide。以 Chrome 桌面介面拍攝並保存 8 張原始截圖；in-app browser 在本機不可用，因此使用 Chrome。Chrome 已登入維護者帳號，帳號提示與編輯按鈕不代表匿名訪客會看到的畫面，也不作為產品缺陷。

從目前來源建置 CLI，實際檢查文件出現的指令名稱及必要參數。沒有重新安裝 runtime、連接新的 MCP client、製作新影片或操作審片 UI。後半段評估的是文件能否引導使用者完成任務，不宣稱這些產品功能執行失敗。沒有使用過去 dogfood 畫面當作本輪的新手操作證據。

完整圖文走查另存於本機 audit 資料夾 `video-studio-readme-audit-20260922`；含帳號介面的截圖不放進公開 repo。比較研究見 [readme-examples.md](readme-examples.md)。

## 逐步旅程

| 步驟 | 模擬的新手內心對話 | 觀察到的內容 | 判斷與下一步 |
| --- | --- | --- | --- |
| 1. 抵達專案頁 | 「Video Studio 是剪輯器、AI 生片工具，還是給工程師的套件？」 | About 使用 self-hosted、CLI、MCP；上方以檔案列表為主，沒有產品畫面或成品預覽入口。截圖 01。 | 定位不夠具體。About 和 README 第一行應共同說明『讓 agent 使用自己的配音與素材製作影片』。不把 GitHub 固定的檔案列表當成可重設的產品版面。 |
| 2. 理解用途、尋找成果 | 「它到底能幫我做哪一種影片？做出來會像什麼？」 | 第一屏列出 artifacts、production contracts、render receipts 等術語；未展示影片或審片頁。截圖 02。 | 缺少採用理由與可見成果。先給一支可播放的短片、同一支片的輸入清單和審片畫面，再介紹工作流能力。 |
| 3. 選擇操作方式 | 「我平常已經用 agent，需要 CLI 還是 MCP？一定得自己架伺服器？」 | 開頭並列 CLI、stdio、HTTP；詳細 MCP 說明位於 backup/restore 之後，只有 executable 路徑，沒有初次連接例與第一個任務。截圖 07。 | 選擇成本偏高。CLI 可供人或有 shell 的 agent 使用；MCP 是另一種工具連接方式。兩者共用持久任務、審片、重試。先介紹本機 stdio，私人 HTTP 部署放進階指南。 |
| 4. 決定是否安裝 | 「我還沒看過成片，就要裝 Rust、uv、Node、FFmpeg、Chrome，Mac 還提到 Xcode？」 | 安裝段落先要求 setup、完整 verify、install；接著解釋不可變 runtime 和私有 Haru launcher。截圖 03。 | 承諾成果之前要求大量投入。前置條件要誠實保留，但把『使用者安裝』和『開發者完整驗證』分開說；目前 install 會再呼叫 setup，使用者不應自行猜三條命令是否都必要。 |
| 5. 準備第一支影片 | 「voice.wav 和 captions.srt 我要去哪拿？我只是想先試試。」 | 指南要求已安裝 CLI、自備音檔/SRT；JSON 範例可用，但沒有一份可立即拿來跑的成片同款素材包。截圖 04。範例連結帶到 Remotion 引擎層的技術音與 template 操作，截圖 08。 | 首次體驗需要現成 sample 和『我有自己的素材』兩條路。試用 sample 應走同一套公開 CLI/MCP 流程，並給出預期成品；引擎煙霧測試保留給開發者。 |
| 6. 開始渲染、知道是否完成 | 「lease_id 放哪？job ID 怎麼查？它回覆了，所以做完了嗎？」 | 準備/渲染範例含 lease、idempotency key、tools_root 占位值；後續只提命令名稱，缺少完整 polling 命令與成功輸出例。截圖 05。 | 有能力但導覽中斷。保留保護機制，用同一個專案名與可複製完整命令串，明示哪些值由上一個回應取得，以及『已送出』和『已成功』的可見差異。 |
| 7. 審片、下載成品 | 「成品在哪？我怎麼拿到 MP4？」 | user-media 要讀者回 README；前頁使用 `my-video`，README 的 status 路徑改成 `demo`。匯出段落提 MCP 工具名 `export_delivery`，缺少完整的 CLI export 例。截圖 06。 | 明確的教學銜接問題。一路使用相同專案名，展示 UI 啟動、驗證碼取得方式、檔案位置、`delivery export` 命令與輸出目錄。審片 UI 本身本輪未操作。 |
| 8. 修改並決定持續使用 | 「我能只換第二個畫面嗎？agent 怎麼收到我的意見？重跑會弄丟舊版嗎？」 | 文件說明保留舊成品及重試，但沒有串成『留一次時間點回饋 → agent 讀取 → 修改 → 再審』的初次體驗。來源為 user-media 的 Retry 區及 README。 | 這是值得前移的核心價值。讓 sample 教學最後包含一次小修改，展示版本前後對照與失敗復原。此階段是文件推演，不是本輪 UI 測試結果。 |

來源：[README](../README.md)、[自備素材指南](user-media.md)、[模板指南](../templates/narrated/remotion/README.md)、[CLI 定義](../pipeline/src/cli.rs)、[安裝腳本](../scripts/install)、[setup](../scripts/setup)。

## 實際檢查到的指令落差

以下是本輪建置後的 parser 查核，不會啟動 MCP server 或修改任何影片專案。

- 執行文件提到的 `video-studio job status`：exit 2，缺少 `--project-root` 和 `--job-id`。這個名稱是合法子指令，但文件仍要求讀者自行查參數。
- 把文中的 MCP 工具名讀成 CLI 子指令，執行 `video-studio export_delivery --help`：exit 2，unrecognized subcommand。這是可預期的誤讀，並非聲稱 README 的 shell code block 寫了這條錯誤命令。
- `video-studio delivery export --help`：exit 0，列出 project root、owner、lease ID、idempotency key 等必要參數。README 應直接提供這條正確路徑的完整範例。
- `video-studio job status --help`：exit 0。CLI 有提供可查詢的說明；README 可以把這份能力接成更連續的入門路徑。

## 最值得修正的順序

### P0：讓讀者看懂成果並走完一次

1. 第一屏換成具體成果描述，並提供真實成品預覽、審片介面截圖與輸入清單。
2. 從首頁直接給『用 CLI』和『連接 MCP』兩個入口；兩者都能給 AI agent 使用。
3. 把第一次製片集中成一份從安裝到匯出的教學。統一 `my-video`，補齊 PATH、workspace、樣本、lease/job ID、poll、UI、export、release lease 等相依步驟。
4. 補一份有權分享、能產生展示片的範例素材；公開影片／素材需要另行準備，這次沒有上傳或發布。

### P1：減少不必要的前置閱讀

- 把 Haru 歷史相容性、receipt 格式、備份內部細節放到進階文件。首頁只保留對使用者有意義的結果，例如失敗時保留上一版。
- 重新區分使用者安裝與開發者驗證。目前仍是 source-built 安裝，不寫成已有 binary release，也不保證『一分鐘開始』。
- MCP 入口加上經實測的最小 client 設定、workspace 初始化、工具可見的成功訊號與第一個 prompt。HTTP 私人部署另連詳細指南。
- 在首屏下方說清楚輸入：正式製作主要自備音檔與 SRT；可匯入自選服務產出的聲音，內建 TTS 是選配。

### P2：第一次成功後的理由

- 加一段短示範：agent 製片 → 瀏覽器留時間點回饋 → agent 修改 → 下載新版。
- 加上『換自己的素材』和『從失敗工作恢復』的後續任務。
- 再考慮提供簡化初始化命令或範例套件下載。這些是待實作的改善方向，不寫成今天已有的指令。

## 建議的新 README 順序

1. 一句用途 + 三行說明 + 成品預覽 + CLI/MCP 入口。
2. 你提供什麼、你會拿到什麼。
3. 製片、審片、修改的完整迴圈。
4. 使用哪個入口（CLI / 本機 MCP；私人 HTTP 另連）。
5. 第一支影片，先用現成範例，再換自己的聲音和素材。
6. 使用邊界：開發中、自架、前置條件、TTS 選配、依賴授權連結。
7. 進階指南、貢獻和開發者資訊。

GitHub 對 README 的建議本來就包括用途、為什麼有用、如何開始與如何求助；此處的排序是針對 Video Studio 的設計判斷，沒有轉換率實驗支持。來源：[GitHub README 指南](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-readmes)。

## 首屏文案草稿

下面是可以討論的英文文案，尚未替換正式 README，也沒有將不存在的 demo/download 連結偽裝成可用入口。

> **Video Studio**
>
> **Make narrated videos with your AI agent.**
>
> Bring your voice, captions, images, and clips. Render a video on your own machine, review it in your browser, and send changes back to your agent.
>
> Use the CLI or connect through MCP. Keep your choice of voice and media providers.

推薦的繁體中文表達：

> **讓你的 AI agent，把配音與素材做成影片。**
>
> 用自己的音檔、字幕、圖片和片段製片，在本機網頁審片，再讓 agent 依回饋修改。

文案旁邊應有兩個真實入口：CLI quickstart、MCP setup。可播放範例準備好後再加 Watch the demo。影片示範要顯示成品、輸入與修改過程；純技術測試音不能代替配音品質展示。示範中的自然旁白同樣要有清楚的素材來源／使用權，不把某一個付費 TTS 服務當成必要條件。

不要承諾現在沒有的任意一鍵生片、圖形時間軸剪輯、雲端帳號、無前置條件安裝、未測試 client 相容性或安裝時間。Remotion 是渲染基礎；Video Studio 可主打的是 agent 可操作的製作、審片、修改、重試與交付流程。

## 怎樣知道這次改善有效

找未讀過專案的目標使用者，先只給 GitHub 網址，不在旁補充說明。第一輪先觀察，而不預設通過率：

- 看完首屏能否用自己的話說出用途、必要輸入與輸出。
- 能否找到一支成品，並分辨哪些素材是使用者提供的。
- 能否選出適合自己的 CLI/MCP 入口。
- 依同一份教學產出範例 MP4，並指出最後檔案在哪。
- 能否換一個素材、提出一次修改並找到新版。

記錄首次卡住的步驟、查閱的頁面、錯誤訊息、需要旁人補充的資訊和實際時間。只有取得這些資料後，才談轉換或完成率改善。

## 可及性與查核限制

現有 GitHub Markdown 有標題層級及可複製程式碼，這些是有用的基礎。本輪只檢查桌面視覺與可存取性樹，沒有做手機、鍵盤全流程、螢幕閱讀器或 WCAG 合規測試。新增 demo 應附字幕、文字版內容與有意義的預覽替代文字；不依靠自動播放或只有聲音的說明。現有大量術語與跨頁銜接也是認知負擔，應在內容層修正。
