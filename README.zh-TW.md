# Agent Sprite Forge

語言：[English](./README.md) | [繁體中文](./README.zh-TW.md) | [简体中文](./README.zh-CN.md) | [日本語](./README.ja.md) | [한국어](./README.ko.md)

<p align="center">
  <img src="./src/banner.png" alt="Agent Sprite Forge banner" width="900" />
</p>

<!-- PROMO -->

## 一張原畫 → 整套可進遊戲的 sprite

<p align="center">
  <strong>核准一張生成的原畫，就拿到角色的每一個動作：去背、循環、計時都做好，直接匯出給你的引擎。</strong>
</p>

<p align="center">
  給 <strong>Claude Code</strong>（plugin）、<strong>Codex</strong> 與 <strong>Grok</strong> 用的 agent skills。Agent 用你手上最好的生圖路徑做出一張原畫（master still），再從這張原畫為每個動作各生成一支圖生影片（image-to-video），接著由 deterministic Python 工具逐一把關、去背、對位、挑循環、重新計時、後製（finish）並打包，交給 Godot、Aseprite 或網頁遊戲。
</p>

<table>
  <tr>
    <td align="center" width="30%">
      <img src="./src/v040/aria-master.png" alt="Aria 的核准原畫：短黑髮、青綠斗篷、紅色長圍巾的年輕女劍士，側面朝右" width="250" />
      <br />
      <strong>1. 一張核准的原畫</strong>
      <br />
      <sub>由本機 Codex CLI（<code>image_gen</code>）生成，三張候選中的第 1 張。</sub>
    </td>
    <td align="center" width="70%">
      <img src="./src/v040/aria-set.gif" alt="Aria 的六個動作以真實時間播放：idle、走路、跑步循環，攻擊、跳躍、受擊單次動作" width="600" />
      <br />
      <strong>2. 六個動作，每個動作一支圖生影片</strong>
      <br />
      <sub>Grok 圖生影片（本機 CLI）→ soft matte 去背 → 對位 → 用量測挑循環 → 單次動作重新計時到遊戲速度 → HD 後製加顏色鎖定。真實時間播放。</sub>
    </td>
  </tr>
</table>

<p align="center">
  <img src="./src/v040/aria-in-scene.gif" alt="Aria 跑過生成的草原背景，停下來刺出一劍，再繼續往前跑" width="860" />
  <br />
  <em>3. 放進場景：打包好的跑步、攻擊與 idle，疊在生成的背景上。跑速是從著地腳量出來的，所以腳不會滑。每一幀都來自 2026-10-06 的實機執行，沒有任何示意圖。</em>
</p>

<p align="center">
  <a href="#04-新功能大升級">新功能</a> ·
  <a href="#快速開始">快速開始</a> ·
  <a href="#生成路徑與供應商">生成路徑與供應商</a> ·
  <a href="#流程原畫--整套動作--後製--匯出">流程</a> ·
  <a href="#地圖">地圖</a> ·
  <a href="#showcase用-agent-sprite-forge-做的遊戲">Showcase</a> ·
  <a href="#工具">工具</a>
</p>

## 0.4 新功能：大升級

0.4 以前，角色動畫要從生圖產生的 sprite sheet 把幀切出來，或手動把單一支影片去背。0.4 把一張核准的原畫變成整套動作：每個動作一支圖生影片，再加上一條從每支影片到引擎、每一步都有量測的流程。用真實輸出量測的升級前後：

<table>
  <tr>
    <td align="center" width="50%">
      <img src="./src/v040/fox-old-vs-new-4x.gif" alt="舊版從一張生圖 sheet 切出來的像素狐狸跑步，旁邊是 0.4 新版像素狐狸跑步，4 倍 nearest 放大，每幀 80 ms" width="420" />
      <br />
      <strong>像素後製：48x64 的狐狸跑步</strong>
      <br />
      舊版：一張生圖 sheet 切成 8 格（每幀 594–709 色，半透明 alpha）。新版：原畫 → Grok 影片 → 像素後製（每幀 16–19 色、二值 alpha、選擇性描邊）。4 倍 nearest 放大，兩邊都是 80 ms。
    </td>
    <td align="center" width="50%">
      <img src="./src/validation/grok-old-vs-new-fringe-2x.png" alt="同一支 Grok 影片同一幀的 2 倍放大：舊 keyer 留下洋紅色外框，0.4 的 soft matte 邊緣乾淨" width="420" />
      <br />
      <strong>Soft matte 去背：沒有紫邊</strong>
      <br />
      同一支 Grok 影片、同一幀、2 倍放大。紫邊 0.704 → 0，漏出的 key 像素 7,780 → 0，145 幀去背時間 271 秒 → 44 秒。
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <img src="./src/v040/attack-before-after.gif" alt="Aria 的攻擊：左邊是生成器的慢動作速度，右邊是自動重新計時後的遊戲速度" width="420" />
      <br />
      <strong>單次動作重新計時到遊戲速度</strong>
      <br />
      圖生影片會把一個快速攻擊拍成慢動作。<code>retime --auto-oneshot</code> 剪掉停格、釘住命中點：4.1 秒 → 0.7 秒，命中點在 292 ms。
    </td>
    <td align="center" width="50%">
      <img src="./src/v040/colourlock-before-after.gif" alt="Aria 的跑步循環，左邊沒有顏色鎖定、右邊有；沒鎖定時靴子在橄欖綠與暗紅之間閃爍" width="420" />
      <br />
      <strong>HD 顏色鎖定</strong>
      <br />
      影片模型在快速擺動的四肢上會讓顏色漂移。顏色鎖定把每個設計色固定在原畫的顏色上（OKLab 色度，亮度不動），靴子就不再閃色。
    </td>
  </tr>
</table>

| 實機量測 | 升級前 | 0.4 |
|---|---:|---:|
| 像素狐狸（48x64，8 幀）：每幀顏色數 | 594–709 | **16–19** |
| 像素狐狸：alpha 階數 | 199 | **2** |
| 像素狐狸：每幀孤立像素（平均） | 732 | **75** |
| 去背：紫邊（外圈溢色，平均） | 0.704 | **0** |
| 去背：145 幀中漏出的 key 像素 | 7,780 | **0** |
| 去背：145 幀所需時間 | 271 秒 | **44 秒** |
| 攻擊：單次動作長度 | 4.1 秒 | **0.7 秒** |
| 顏色鎖定：每對相鄰幀的色相翻轉（跑步循環） | 352 | **170** |
| QC：被誤判淘汰的好 take | 24 支中 18 支 | **24 支中 0 支** |

改了什麼：

- **生圖優先。** 每張圖、每支影片都走同一個指令 `route_media.py`：有設定 API key 就用（OpenAI、Google Gemini、xAI、BytePlus、fal.ai），沒有就用你已登入的 Codex 或 Grok CLI，最後才是程式繪圖（code art）。見 [生成路徑與供應商](#生成路徑與供應商)。
- **每個角色一張原畫。** `master_still.py` 寫 prompt、生成多張候選、用「修改」修好差一點的那張，再核准成一張原畫（`master.json`）。
- **從這張原畫做出整套動作。** `sprite_set.py` 為每個動作生成一支圖生影片，逐支檢查（面積、腳、身分、縮放、轉身、出框、背景、多餘物件、動作量、結束姿勢、滲色、時間長度），不合格就帶修正條款重拍，然後 soft matte 去背、對位、挑循環、單次動作重新計時、後製並打包。
- **預設 HD 後製，需要時可選清晰的像素後製。** 每個角色只有一個身體尺度，取自原畫。像素後製另外做清晰縮圖、最多 32 色且色階拉開的調色盤、選擇性描邊與二值 alpha。
- 整套動作都套 **顏色鎖定**，**QC 依 42 支實機標記 take 重新校正**。
- **引擎匯出。** animation.json 3.0 加 PNG atlas、VP9-alpha WebM、packed-alpha MP4，並全部解碼驗證；Godot SpriteFrames 與 Sprite3D；Aseprite JSON；地圖匯出 Tiled、Godot、LDtk。
- **Claude Code plugin**，以及 Codex、Grok 安裝。Windows 上的問題也修好了：Codex 沙箱讀得到的執行資料夾、被鎖住檔案的清理、cp1252/cp950 主控台。

完整清單見 [CHANGELOG](./CHANGELOG.md)。實機紀錄（包含弱點）：[docs/validation-2026-10-06.md](./docs/validation-2026-10-06.md)。

## 快速開始

### 1. 安裝

**Claude Code（plugin）**

```bash
claude plugin marketplace add 0x0funky/agent-sprite-forge --sparse .claude-plugin skills
claude plugin install agent-sprite-forge@agent-sprite-forge
python -m pip install "numpy>=1.26" "Pillow>=10.1" "scipy>=1.11"
```

`--sparse` 只 checkout plugin manifest 與 skills，不會下載 showcase 媒體。

**Codex**

```bash
git clone https://github.com/0x0funky/agent-sprite-forge.git
cd agent-sprite-forge
python -m pip install -r requirements.txt
python tools/install_skills.py --apply --host codex
```

**Grok**：在同一個 clone 裡執行 `python tools/install_skills.py --apply --host grok`（裝到 `~/.grok/skills`）。`--host agents` 裝到 `~/.agents/skills`，`--dest <資料夾>` 可裝到任何 skills 資料夾。

`--apply` 會先備份已安裝的版本，再寫入 sha256 manifest；之後用 `python tools/install_skills.py --check --host codex` 檢查 drift。影片功能需要 `PATH` 上有 ffmpeg 5.1+。安裝後開一個新的 agent session，skills 才會載入。

可選：設定一把 API key（見 [設定 API key](#設定-api-key)）。沒有 key 時，agent 會用你已登入的 Codex 或 Grok CLI。

### 2. 做出原畫（master still）

```text
Use $generate2dsprite to make a master still of Aria, a young swordswoman hero: short dark hair, a teal hooded cloak, a long red scarf and one steel sword. Side view, facing right, HD.
```

Agent 會執行 `master_still.py generate --takes 3`（每張候選都經過 `route_media.py`），把候選並排給你看，用 `master_still.py edit`（「THE ONLY CHANGE: ...」）修好差一點的那張，再核准其中一張：原畫會被去背、依角色類別補邊到固定構圖（hero 在 1024x1024 上身高 788 px），並記錄在 `master.json`。

<p align="center">
  <img src="./src/v040/aria-master-takes.png" alt="Aria 的三張原畫候選並排在洋紅色 key 背景上，核准的是第 1 張" width="720" />
</p>

### 3. 做出整套 sprite

```text
Use $video2dsprite to animate the approved Aria master: idle, walk, run, attack, jump and hurt. Package them for Godot and the web.
```

背後實際執行的是（`<skill-dir>` 是安裝好的 `video2dsprite` 資料夾）：

```bash
python "<skill-dir>/scripts/sprite_set.py" plan --master art/aria-master/master.json --output-dir art/aria-set
python "<skill-dir>/scripts/sprite_set.py" run --plan art/aria-set/set_plan.json
python "<skill-dir>/scripts/sprite_set.py" review --plan art/aria-set/set_plan.json
python "<skill-dir>/scripts/sprite_set.py" accept --plan art/aria-set/set_plan.json --action run --take 2
python "<skill-dir>/scripts/sprite_set.py" report --plan art/aria-set/set_plan.json
```

`run` 中斷後可以接著跑，同一支影片絕不重複生成。Agent 會先看過每一張 review sheet 才核准動作，因為數字看不出武器變了或臉被遮住。每個動作最後都是一個驗證過的 package；`export_engine.py` 再加上 Godot 與 Aseprite 檔案。

## 生成路徑與供應商

每一張生成的圖、每一支影片都經過 `generate2dmedia/scripts/route_media.py`。它依下面的順序嘗試，並回報實際用了哪條路徑：

| 順序 | 路徑 | 圖片 | 影片 | 何時使用 |
|---|---|---|---|---|
| 1 | **API**：你設定的 key | OpenAI、Google Gemini、xAI、BytePlus Seedream、fal.ai（參考圖編輯） | xAI、BytePlus Seedance、fal.ai（Kling v3、Veo 3.1、Luma、MiniMax、Wan、Vidu、LTX） | 只要設定了 key 就用。設定 key 就代表你同意，所以不會每次再問 |
| 2 | **本機 CLI**：你自己已登入的 Codex 或 Grok | Codex `image_gen`（最多 8 張參考圖）；Grok 單次生圖或改圖 | Grok ACP 模式的圖生影片（6 或 10 秒） | 沒有 key，或每個 API 路徑都因帳號原因拒絕（沒有 key、認證、額度、速率限制）。用的是你的訂閱額度，從不用 API key |
| 3 | **程式繪圖**（`codeart2d`） | 程式繪製的像素 sprite、向量圖、特效、tiles | 骨架動畫 | 最後手段：只有完全沒有路徑（`no-route`，exit 3）或你明確要求程式繪圖時才用，並一律標明是程式繪製 |

`python "<generate2dmedia>/scripts/route_media.py" resolve --kind image` 會印出這次會走的路徑；`forge_doctor.py` 列出哪些 key 已設定（只顯示有或沒有，絕不顯示 key）與實際的順序。被內容審核拒絕、或可能已經花了錢的結果，絕不會換一條路徑重試。

### 支援的供應商

| 供應商 | Key 變數 | 圖片 | 影片 | 能否固定最後一幀 | 備註 |
|---|---|---|---|---|---|
| OpenAI | `OPENAI_API_KEY` | 生成與編輯，16 張參考圖，原生透明背景 | 無 | 不適用 | 預設 `gpt-image-2.5-sunburst` |
| Google Gemini | `GOOGLE_API_KEY` 或 `GEMINI_API_KEY` | 生成與編輯，14 張參考圖 | 無 | 不適用 | Pro 模型給 hero 原畫用；有 SynthID 浮水印 |
| xAI | `XAI_API_KEY` | 生成與編輯，5 張參考圖 | 1–15 秒，480p–1080p | 可以（1.5 在 480p 或 720p），最多 4 個 keyframe | Lite 模型做草稿 |
| BytePlus ModelArk | `ARK_API_KEY` | Seedream 生成與編輯，10 張參考圖 | Seedance，4–15 秒 | 可以 | 需先啟用模型；不接受真人臉 |
| fal.ai | `FAL_KEY` | 參考圖編輯（Nano Banana 2 與 Pro、Seedream 5、GPT Image 2） | Kling v3、Veo 3.1、Luma Ray 3.2、MiniMax H3、Wan 3.0、Vidu Q3、LTX-2.5 | 每個精選模型都可以 | Veo 與 LTX 會把原畫補邊成 16:9，並記錄裁切框 |

送出前，每個模型的限制都會先對照 [capabilities.json](./skills/generate2dmedia/references/capabilities.json) 檢查；模型接不了的請求會附上原因跳過，或附註說明後調整（例如把影片長度調到模型支援的長度）。每支影片發布時都去掉音軌。詳見 [route-media.md](./skills/generate2dmedia/references/route-media.md)、[api-usage.md](./skills/generate2dmedia/references/api-usage.md)、[provider-survey.md](./skills/generate2dmedia/references/provider-survey.md)、[cli-routes.md](./skills/generate2dmedia/references/cli-routes.md)。

### 設定 API key

每個供應商只讀自己的 key，來源是環境變數，或使用者設定資料夾裡的一個 JSON 檔（絕不放在專案裡）；兩者都有時以環境變數為準。

| 系統 | 設定檔 |
|---|---|
| Windows | `%APPDATA%\agent-sprite-forge\config.json` |
| macOS、Linux | `~/.config/agent-sprite-forge/config.json`（或 `$XDG_CONFIG_HOME/agent-sprite-forge/config.json`） |

```json
{
  "OPENAI_API_KEY": "sk-...",
  "GEMINI_API_KEY": "...",
  "XAI_API_KEY": "xai-...",
  "ARK_API_KEY": "...",
  "FAL_KEY": "key-id:key-secret",
  "providers": {"order": {"image": ["gemini", "openai"], "video": ["byteplus"]}}
}
```

每個欄位都是選填。`providers.order` 把你偏好的供應商排在前面；`models` 可依供應商與等級指定模型。Key 只在行程內讀取：不會被印出、記錄、寫入、放進命令列或交給子行程，也絕不會拿別家供應商的 key 來用。在 macOS 與 Linux 上請把檔案設成私有（`chmod 600`）。

### 花費紀錄

每次呼叫（付費或本機）都會在 `<project>/.forge/ledger.jsonl` 寫一行，含預估金額、供應商的 job id 與產出檔的 sha256；每個輸出資料夾都保留一份 `job.json`。除非你自己設定，否則沒有上限：`--budget-usd`、`--max-calls`、`FORGE_MAX_PAID_REQUESTS`，本機路徑用 `FORGE_SESSION_IMAGES` / `FORGE_SESSION_VIDEOS`。`media_ledger.py summary` 顯示總計。

## 流程：原畫 → 整套動作 → 後製 → 匯出

```text
master still        one clip per action      cut                     finish                 package and export
master_still.py --> sprite_set.py run    --> gait_loop.py select --> finish_frames.py   --> engine_export.py package + verify
generate, edit,     route_media.py video,    (loops), retime.py      hd (default) or        export_engine.py (Godot, Aseprite)
approve             gates, retakes, soft     --auto-oneshot          pixel, colour lock
                    matte, registration      (one-shots)             to the master
```

| 步驟 | 工具 | 做什麼 |
|---|---|---|
| 原畫 | `master_still.py` | 候選經過 `route_media.py`、用修改修正，再 `approve`：去背、補邊到固定構圖，產生 `master.json`，裡面的身分描述會被每一個動作 prompt 重複使用 |
| 影片 | `sprite_set.py run` | 每個動作從原畫生成一支圖生影片，畫布依動作而定（跳躍 3:4 留頭頂空間，攻擊 16:9）；路徑支援時，idle 與攻擊會固定最後一幀 |
| 把關 | `sprite_set.py` | 面積、腳、身分、縮放、轉身、出框、背景、多餘物件、動作量、結束姿勢、滲色、時間長度；最多 3 次 take，每次帶修正條款，全部不合格時保留最好的可用片段 |
| 去背與對位 | `video2dsprite.py process`、`register_clip.py` | 每個角色一份 matte profile 的 soft matte；用記錄下來的輸入轉換反推回去，身體尺度不會忽大忽小 |
| 剪輯 | `gait_loop.py`、`retime.py` | 走路、跑步、idle 循環由量測挑選；單次動作重新計時到遊戲長度並釘住命中點（攻擊 0.7 秒、跳躍 0.9 秒、受擊 0.5 秒） |
| 後製 | `finish_frames.py` | HD：每個角色一個尺度、premultiplied 面積縮圖、乾淨的 alpha、顏色鎖定。像素：清晰縮圖、共用且色階拉開的調色盤、選擇性描邊、二值 alpha，可選固定格（`--canvas 48x64`） |
| 打包 | `engine_export.py` | animation.json 3.0、PNG atlas、WebM alpha、packed MP4、poster；`verify` 會解碼每一個檔案 |
| 審閱 | `sprite_set.py review`、`accept`、`report` | 給 agent 看的 contact sheet、成品條與角色身高對照；報告列出路徑、take、QC 數字、循環與時間 |

### 引擎匯出

| 目標 | 拿到什麼 | 工具 |
|---|---|---|
| 網頁與任何引擎 | `animation.json` 3.0（整數 ms 的時長、事件、錨點）、PNG atlas、VP9-alpha WebM、給 iPhone 的 packed-alpha H.264 MP4、poster；`forge-runtime.mjs` 與 WebGL packed-alpha compositor | `engine_export.py package`，再 `verify` |
| Godot 4 | SpriteFrames `.tres` 搭配 AnimatedSprite2D `.tscn`；AnimatedSprite3D package | `export_engine.py --target godot-spriteframes`、`godot-sprite3d` |
| Aseprite、Phaser、PixiJS | 含 frame tag、事件與 pivot 的 Aseprite JSON array atlas | `export_engine.py --target aseprite-json` |
| 地圖 | Tiled 1.10、Godot 4.3+（TileMapLayer 場景）、LDtk 1.5.3 | `export_tiled.py`、`export_godot.py`、`export_ldtk.py` |

<p align="center">
  <img src="./src/v040/fox-new-sheet-4x.png" alt="新版像素狐狸跑步的 8 幀 sprite sheet，每格 48x64，以 4 倍顯示" width="768" />
  <br />
  <em>像素狐狸的實際交付：8 幀、每格 48x64、每幀 80 ms，附 Godot、Aseprite 與網頁 package（sheet 以 4 倍顯示）。</em>
</p>

> **尚未在編輯器裡驗證匯入。** 匯出工具證明的是解析層級的來回一致，Tiled 匯出會重新算繪並比對到 0 px 差異；還沒有人在編輯器裡開過 0.4.0 的 Godot、LDtk 或 Aseprite 匯出檔。

## 地圖

`generate2dmap` 做的是地圖而不是單一 sprite：手繪風分層地圖（只有地面的底圖、佈置好的參考圖、prop pack，再擷取出有錨點的透明道具）、tiles 與 autotiles、橫向捲軸關卡與 HD-2D 戰鬥背景。碰撞、可達性與出口都由資料推導（`map_bundle.v2`、`map_nav.py`），可匯出 Tiled、Godot 4 與 LDtk，並有可以走動的單檔 HTML 預覽。

<table>
  <tr>
    <td align="center" width="33%">
      <img src="./src/cyber-canal-base.png" alt="只有地面的賽博龐克運河底圖" width="260" />
      <br />
      <strong>只有地面的底圖</strong>
    </td>
    <td align="center" width="33%">
      <img src="./src/cyber-canal-dressed-reference.png" alt="佈置好的賽博龐克運河參考圖" width="260" />
      <br />
      <strong>佈置好的參考圖</strong>
    </td>
    <td align="center" width="33%">
      <img src="./src/cyber-canal-prop-pack.png" alt="生成的 3x3 賽博龐克運河 prop pack" width="260" />
      <br />
      <strong>3x3 prop pack</strong>
    </td>
  </tr>
</table>

<p align="center">
  <img src="./src/cyber-canal-layered-preview.png" alt="分層的賽博龐克運河地圖，道具依 y 排序" width="760" />
  <br />
  <strong>分層地圖：有錨點的道具依各自的地面線排序</strong>
</p>

## Showcase：用 Agent Sprite Forge 做的遊戲

以下是用 Codex 搭配先前版本的 skills 組出來的原創遊戲與角色，展示完整閉環：生成素材、結構化的場景資料，以及可玩的 prototype。

<table>
  <tr>
    <td align="center" width="50%">
      <img src="./src/summon-survivors-game-preview1.png" alt="Summon Survivors Unity WebGL 遊戲畫面" width="420" />
      <br />
      <strong>Summon Survivors — Unity WebGL</strong>
      <br />
      生成的地圖、主角 sheet、召喚獸與進化、敵人、Boss、掉落物、HUD、特效、升級選項，以及 WebGL 版本。
      <br />
      <a href="https://summon-survivors.vercel.app/">試玩</a> · <a href="https://drive.google.com/file/d/1TL7qRX95przTToZILVQ1EFwEXm3flB6t/view?usp=sharing">製作過程對話</a>
    </td>
    <td align="center" width="50%">
      <img src="./src/kingdomrush-forest-pass.png" alt="Forest Pass Defense Godot 塔防地圖" width="420" />
      <br />
      <strong>Forest Pass Defense — Godot 塔防</strong>
      <br />
      Godot 4 prototype：地圖、分離的道具、塔位、塔、敵人 sheet、Boss 與飛行敵人、波次、HUD、建造／升級／出售流程、投射物與鎖定規則。
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <img src="./src/godot-editor.png" alt="Generate2DMap 的 Godot 編輯器場景" width="420" />
      <br />
      <strong>可編輯的 RPG 地圖 — Godot TileMap</strong>
      <br />
      生成的 tileset 與 prop sheet 接成可編輯的 <code>TileMapLayer</code>、<code>Sprite2D</code> 道具、遇敵草叢 <code>Area2D</code>、<code>StaticBody2D</code> 碰撞、出口、metadata，以及除錯用的玩家與攝影機。
    </td>
    <td align="center" width="50%">
      <img src="./src/neon-breach.png" alt="Neon Breach 賽博龐克橫向捲軸" width="420" />
      <br />
      <strong>Neon Breach — 賽博龐克橫向捲軸</strong>
      <br />
      以生成的角色、攻擊、地圖與遊戲素材做成的可玩橫向捲軸 prototype。
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <img src="./src/pokemonlike2.png" alt="Sengoku Era JavaScript RPG 初始夥伴選擇" width="420" />
      <br />
      <strong>Sengoku Era — JavaScript 怪獸 RPG</strong>
      <br />
      瀏覽器 RPG prototype，有生成的角色、初始夥伴選擇、地圖流程與戰鬥 UI。
      <br />
      <a href="https://sengoku-era.vercel.app/">試玩</a>
    </td>
    <td align="center" width="50%">
      <img src="./src/pokemonlike.png" alt="Sengoku Era JavaScript RPG 戰鬥畫面" width="420" />
      <br />
      <strong>初始夥伴選擇與戰鬥循環</strong>
      <br />
      用 skills 做出的 sprite、怪獸、戰鬥與地圖素材組成的小型 JavaScript 遊戲。
    </td>
  </tr>
</table>

<details>
<summary>更多 Godot 塔防輸出</summary>

<table>
  <tr>
    <td align="center" width="40%">
      <img src="./src/kingdomrush-enemy-roster.png" alt="Forest Pass Defense 敵人一覽" width="320" />
      <br />
      <strong>敵人一覽，含飛行與 Boss 單位</strong>
    </td>
    <td align="center" width="30%">
      <img src="./src/kingdomrush-tower-icons.png" alt="Forest Pass Defense 塔的圖示" width="260" />
      <br />
      <strong>塔的陣容</strong>
    </td>
    <td align="center" width="30%">
      <img src="./src/kingdomrush-hud-icons.png" alt="Forest Pass Defense HUD 圖示" width="260" />
      <br />
      <strong>HUD 與遊戲圖示</strong>
    </td>
  </tr>
</table>

- `scenes/ForestPass.tscn`：底圖、分離的道具、敵人路徑、塔位與 HUD 節點。
- 六個塔系，各有生成的塔圖與升級階段。
- 地面單位、飛行單位與 Boss 的敵人動畫 sheet。
- 波次、難度、塔目錄、碰撞、路線與塔位 metadata。
- 在 Godot 中接好建造、升級、出售、投射物與鎖定行為。

</details>

<details>
<summary>更多 Unity survivors-like 輸出</summary>

<table>
  <tr>
    <td align="center" width="50%">
      <img src="./src/summon-survivors-game-preview1.png" alt="Summon Survivors Unity WebGL 畫面：召喚獸、敵人、掉落物、HUD 與目標" width="420" />
      <br />
      <strong>召喚獸、敵人、掉落物、HUD 與目標流程</strong>
    </td>
    <td align="center" width="50%">
      <img src="./src/summon-survivors-game-preview2-levelup.png" alt="Summon Survivors Unity WebGL 升級選單" width="420" />
      <br />
      <strong>升級選項：解鎖召喚獸、訓練、屬性與回復</strong>
    </td>
  </tr>
</table>

- 可玩場景：`Assets/Survivors/Scenes/SummonSurvivors.unity`。
- `SurvivorContentDatabase.asset` 連結生成的主角、召喚獸、敵人、掉落物、HUD 與特效 sprite。
- 初始召喚獸選擇、生存目標、經驗與金幣掉落、升級選項、召喚獸訓練與進化流程。
- 敵人生成壓力、Boss 時機、投射物攻擊、範圍傷害、血條與計分。
- `Builds/WebGL` 下的 WebGL 輸出與 Vercel 部署設定。

</details>

<details>
<summary>Godot 可編輯 TileMap：草原的各個圖層</summary>

<table>
  <tr>
    <td align="center" width="50%">
      <img src="./src/godot-meadow-layered-preview.png" alt="Godot 草原分層 RPG 地圖預覽" width="360" />
      <br />
      <strong>分層地圖預覽</strong>
    </td>
    <td align="center" width="50%">
      <img src="./src/godot-meadow-debug-preview.png" alt="Godot 草原除錯預覽，含碰撞與區域" width="360" />
      <br />
      <strong>碰撞與區域除錯疊圖</strong>
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <img src="./src/godot-meadow-tileset.png" alt="生成的 Godot 草原 tileset atlas" width="360" />
      <br />
      <strong>生成的 tileset atlas</strong>
    </td>
    <td align="center" width="50%">
      <img src="./src/godot-meadow-prop-pack.png" alt="生成的 3x3 草原 prop pack" width="360" />
      <br />
      <strong>生成的 3x3 prop pack</strong>
    </td>
  </tr>
</table>

</details>

### 特效與 sheet sprite

特效、圖示與道具仍然走 sprite sheet：施法、投射物與命中特效都來自 `$generate2dsprite` 的 sheet。

<table>
  <tr>
    <td align="center" width="50%">
      <img src="./src/cast.gif" alt="火法師施法動畫" width="150" />
      <br />
      <strong>施法</strong>
    </td>
    <td align="center" width="50%">
      <img src="./src/projectile.gif" alt="火法師投射物動畫" width="150" />
      <br />
      <strong>投射物</strong>
    </td>
  </tr>
</table>

<details>
<summary>較早的角色輸出（0.3）：四方向走路 sheet 與 Ryo 影片案例</summary>

0.4 之前，角色是從生圖 sheet 切出來的；0.4 改成從一張原畫讓角色動起來。

<table>
  <tr>
    <td align="center" width="25%"><img src="./src/down.gif" alt="武士往下走" width="132" /><br /><strong>下</strong></td>
    <td align="center" width="25%"><img src="./src/left.gif" alt="武士往左走" width="132" /><br /><strong>左</strong></td>
    <td align="center" width="25%"><img src="./src/right.gif" alt="武士往右走" width="132" /><br /><strong>右</strong></td>
    <td align="center" width="25%"><img src="./src/up.gif" alt="武士往上走" width="132" /><br /><strong>上</strong></td>
  </tr>
</table>

0.3 的影片流程用在 Ryo 身上（原畫 → 6 秒圖生影片 → chroma key → 16 幀 strip）；0.4 keyer 在這支影片上的數字見 [CHANGELOG](./CHANGELOG.md)。

| 原畫 | 動態（圖生影片） | Sprite 結果（16 幀） |
| --- | --- | --- |
| <img src="./src/video2dsprite-ryo/base.png" alt="洋紅背景上的 Ryo 原畫" width="200" /> | <img src="./src/video2dsprite-ryo/run-6s-preview.gif" alt="Ryo 6 秒跑步影片預覽" width="220" /><br />[下載 MP4](./src/video2dsprite-ryo/run-6s.mp4) | <img src="./src/video2dsprite-ryo/preview-16.gif" alt="Ryo 16 幀跑步循環" width="150" /> |

<p align="center">
  <img src="./src/video2dsprite-ryo/strip-16.png" alt="Ryo 16 幀跑步 strip" width="720" />
  <br />
  Skill 簡介短片（MP4）：<a href="./src/video2dsprite-ryo/intro.mp4">intro.mp4</a>
</p>

</details>

### 程式繪圖：最後手段的備援

完全沒有生圖路徑、或你明確要求程式繪圖時，`codeart2d` 會依 spec 用程式畫出來，不用任何生圖模型、不耗額度，並會明確標示。下面這些由 [`skills/codeart2d/examples`](./skills/codeart2d/examples) 算繪而成。

<table>
  <tr>
    <td align="center" width="25%">
      <img src="./src/codeart/hero-walk.gif" alt="程式繪製的像素主角走路循環" width="160" />
      <br />
      <sub><code>rig_animate.py</code>：FK/IK 走路，腳步踩實</sub>
    </td>
    <td align="center" width="45%">
      <img src="./src/codeart/fx-set.gif" alt="程式繪製的斬擊、命中環、塵土與投射物特效" width="360" />
      <br />
      <sub><code>fx_build.py</code>：斬擊、命中、塵土與投射物，含命中事件</sub>
    </td>
    <td align="center" width="30%">
      <img src="./src/codeart/meadow-layout.png" alt="程式產生的俯視草原地圖，有道路、池塘、房屋與樹" width="260" />
      <br />
      <sub><code>autotile_build.py</code> + <code>layout_build.py</code>：seam 證明過的 tiles，每個出口都走得到</sub>
    </td>
  </tr>
</table>

## 包含的 skills

五個 skill 資料夾請放在一起：它們用相對路徑呼叫彼此的腳本。

| Skill | 用途 | 主要輸出 |
| --- | --- | --- |
| [`generate2dsprite`](./skills/generate2dsprite) | 原畫、角色、生物、道具、圖示與特效；特效、圖示與道具的 sheet；把任何來源的幀打包 | `master.json`、對位好的幀、含 tick 與事件的 clip、調色盤、QA、Aseprite/Godot 匯出 |
| [`video2dsprite`](./skills/video2dsprite) | 從一張原畫做出整套 sprite（`sprite_set.py`），或處理一支現成影片 | 經過把關、soft matte 去背、對位、挑循環或重新計時、後製好的幀；animation.json 3.0、WebM alpha、packed MP4、PNG 備援 |
| [`generate2dmap`](./skills/generate2dmap) | 俯視與橫向捲軸地圖、tiles、道具組、視差、HD-2D 背景 | map_bundle.v2、碰撞與導航檢查、Tiled/Godot/LDtk 匯出、HTML 預覽、場景循環 |
| [`generate2dmedia`](./skills/generate2dmedia) | 所有生成圖片與影片的路徑（`route_media.py`）、五家 API 供應商、本機 Codex/Grok CLI 路徑、能力檢查 | 附收據的媒體、ledger 紀錄、路徑驗證紀錄 |
| [`codeart2d`](./skills/codeart2d) | 程式繪製的像素 sprite、扁平向量圖、特效、autotiles、layout、視差：最後手段，或你要求時才用 | 調色盤精確的幀、clip、fx.v1 runtime、seam 證明過的 tileset、可玩 bundle、`codeart-meta.json` |

在 Codex 裡，`codeart2d` 與 `generate2dmedia` 只能被明確呼叫：由 sprite、video 與 map skills 轉過去。

## 工具

在專案根目錄用 `python "<skill-dir>/scripts/<tool>.py" ...` 執行。每個工具都有 `--help`、輸出到新的資料夾（絕不覆蓋）、印出一行 JSON，失敗時 exit 1。路由表在各 SKILL.md 裡。

| Skill | 工具 | 功能 |
| --- | --- | --- |
| generate2dsprite | `master_still.py` | `prompt`、`generate --takes N`、`edit`、`pad`、`approve`：每個角色一張核准的原畫，`master.json` |
| | `generate2dsprite.py process` | 去背（soft 或 hard）、切格，並在同一個取樣網格上對位 sheet；含 QA 的 pipeline-meta v2 |
| | `sheet_qc.py` | `spill` 在切格前找出跨格的部件；`frames` 檢查身分、遠近腿交替與漂移 |
| | `scale_frames.py` | 每個動作一個尺度與根點；畫布會擴大，不會截掉 |
| | `plan_guide.py`、`make_anchor_layout.py`、`make_layout_guide.py` | 給生圖工具用的 sheet 規劃、姿勢指引與固定尺度範本 |
| | `build_animation_clips.py` | Clips v2：60 Hz tick、事件、轉場、hit-stop、review sheet 與 lint |
| | `assemble_frames.py` | 無損打包整幀；ownership 切格、循環接縫、環境淡入淡出 |
| | `palette_tool.py`、`pixel_reduce.py` | OKLab 調色盤、鎖色、變體、不閃爍的 clip 量化；整數像素網格縮減 |
| | `export_engine.py` | Aseprite JSON、Godot SpriteFrames 與 AnimatedSprite3D（尚未在編輯器驗證匯入） |
| video2dsprite | `sprite_set.py` | `plan`、`run`、`review`、`accept`、`retake`、`report`：從一張原畫為每個動作做一支影片，把關、重拍、後製與打包 |
| | `finish_frames.py`、`colour_lock.py` | HD（預設）或像素後製、共用調色盤、角色身高對照；顏色鎖定到原畫與其量測 |
| | `video2dsprite.py` | `key-plan`、`triage`、soft matte `process`/`clean`、`package`、`verify`、`doctor` |
| | `prepare_i2v_input.py`、`register_clip.py` | 建構式對位：以記錄的轉換放置輸入，再用一次反向轉換拉回，並做 take QC |
| | `gait_loop.py`、`retime.py`、`animation_review.py` | 量測挑選走路／跑步／idle 循環與步幅；單次動作自動重新計時，以及 tick 上的 impact/hold 計時；分類候選 |
| | `engine_export.py`、`validate_animation.py` | animation.json 3.0 加 WebM alpha、packed MP4 與行動裝置分級，前面有殘留閘門；解碼驗證；契約檢查 |
| | `references/runtime/forge-runtime.mjs`、`packed-alpha-webgl.mjs` | 依距離驅動的走路、hit-stop、轉場；WebGL packed-alpha compositor |
| generate2dmap | `extract_prop_pack.py` | 有錨點、腳印與 despill 的透明道具（prop_pack.v2） |
| | `extract_terrain_tiles.py`、`extract_platform_strip.py` | 地形填充、覆蓋層、等角／六角與 Wang 列；平台頂部的表面與接縫 QC |
| | `compose_layered_preview.py`、`validate_parallax.py`、`conform_background.py` | 依地面線排序的預覽與稽核疊圖；視差覆蓋與接縫；把畫作配合螢幕 |
| | `map_bundle.py`、`map_nav.py` | map_bundle.v2 驗證；由資料推導碰撞、可達性與傳送點 |
| | `export_tiled.py`、`export_godot.py`、`export_ldtk.py` | Tiled 1.10（重新算繪驗證）、Godot 4.3+ 與 LDtk 1.5.3（僅解析層級） |
| | `validate_chunks.py`、`validate_layout.py` | 房間區塊接口；橫向捲軸的跳躍、斜坡與平台 |
| | `build_scene_preview.py`、`references/runtime/map-runtime.mjs` | 含路線檢查的可走動單檔 HTML 預覽；與 map_nav 一致的 JS 碰撞 |
| | `scene_layout_guide.py`、`validate_stage.py`、`extract_scene_lights.py`、`edit_locality_check.py` | HD-2D 舞台指引、可適應長寬比的戰鬥站位、光源與氛圍、變體局部修改檢查 |
| | `build_motion_mask.py`、`scene_motion.py` | 靜態背景上的遮罩動態；GOP 對齊且解碼後檢查接縫的循環 |
| generate2dmedia | `route_media.py` | `image`、`video`、`resolve`：先 API key，再本機 CLI，最後 `no-route` |
| | `media_providers.py` | 五家 API adapter（OpenAI、Gemini、xAI、BytePlus、fal.ai）共用一個介面；`list` 列出模型 |
| | `forge_doctor.py` | 能力檢查、已設定的 key（有／沒有）與路徑順序；`--verify-route` |
| | `cli_media.py` | 本機 Codex/Grok CLI 的生圖、改圖與影片路徑；`resume`、`adopt --codex-thread`、`batch` |
| | `generate_media.py`、`media_ledger.py` | 單次付費 API 呼叫，含 dry run、上限與收據；ledger 總計與結算 |
| codeart2d | `render_pixelspec.py`、`pixel_qa.py` | PixelSpec 轉成調色盤精確的幀與 clip；像素圖 QA |
| | `svg_render.py`、`rig_animate.py` | 可攜 SVG 的 `render`、`lint` 與渲染器 `doctor`；含 FK、雙骨 IK 與地面約束的 SVG 骨架 |
| | `fx_build.py`、`fx_verify.mjs` | 六種含命中事件的特效預設與 fx.v1 runtime；runtime 檢查器 |
| | `autotile_build.py`、`layout_build.py`、`parallax_build.py`、`ambient_bake.py` | Seam 證明過的 tileset；可玩的俯視 layout；週期性視差層；背景上的環境循環 |
| repository | `tools/install_skills.py`、`tools/vendor_sync.py`、`tools/check_links.py` | 有防護的安裝與 drift 檢查；共用模組副本同步；README/文件連結檢查 |

## 需求

| 需要 | 安裝 |
| --- | --- |
| 所有 skills | Python 3.10+，`python -m pip install -r requirements.txt`（numpy>=1.26、Pillow>=10.1、scipy>=1.11；scipy 只是加速，有完全一致的 numpy 備援） |
| 影片、整套 sprite、打包與場景循環 | `PATH` 上的 ffmpeg 5.1+，需含 libvpx-vp9 與 libx264 |
| 生圖與生影片（擇一） | OpenAI、Gemini、xAI、BytePlus 或 fal.ai 的 API key（見 [設定 API key](#設定-api-key)），或你自己已登入的 Codex CLI 或 Grok CLI |
| codeart2d 的 SVG 圖 | `python -m pip install -r requirements-codeart.txt`（resvg-py>=0.5,<0.6），或 resvg-js CLI，或 Chrome/Edge。PixelSpec sprite 不需要額外安裝 |
| JS runtime、`fx_verify.mjs`、場景預覽檢查 | node 22（選用）；`build_scene_preview.py --verify` 有 playwright 時也會用到 |
| 貢獻者 | `python -m pip install -r requirements-dev.txt`，再執行 `python -m pytest -q` 與 `node --test "tests/js/*.test.mjs"` |

## 建議 prompt

### 一個角色，從頭到尾

```text
Use $generate2dsprite to make a master still of a fox ranger for a 2D platformer: orange fur, teal tunic, brown boots, side view facing right. Show me three takes.
```

```text
Use $video2dsprite to turn the approved fox master into idle, run, attack and hurt, pixel finish in 48x64 cells at 80 ms, and export it for Godot and Aseprite.
```

### Sprite 與特效

```text
Use $generate2dsprite to create a wizard spell bundle with cast, projectile and impact sprites.
```

```text
Use $video2dsprite with my existing side-view hero PNG as the master. Make a run loop and an attack, and report the routes and QC numbers.
```

### 地圖

```text
Use $generate2dmap to create a small top-down village with a pond, roads to three exits, collision and a walkable HTML preview, then export it to Tiled.
```

```text
Use $generate2dmap to create an HD-2D battle plate for a harbour at night with lantern lights and moving water.
```

<details>
<summary>Showcase prototype 用過的整個遊戲 prompt</summary>

```text
use $generate2dsprite to create a 2D side-scrolling action game. It should include attack mechanics, map elements, and all the essential features. I would like you to design it, and all the necessary assets should be created using this skill. It needs to be an actually playable game, with a cyberpunk story setting.
```

```text
Use $generate2dsprite to create a 2D monster-collecting RPG. You only need to build one scene for now. It must include a starter monster selection mechanic, a battle screen, and all basic gameplay functions. I would like you to design all the elements and the story, and you can also decide which game engine to use. Use this skill to create any assets you need. The story should be set in the Sengoku period.
```

</details>

## 注意事項與限制

- 腳本不是創意大腦，數字型 QA 也從不單獨核准解剖結構或動作：agent 會看 review sheet，原畫由你核准。
- 圖生影片模型在快速擺動的四肢與跳躍上會漂移。跑步與跳躍通常需要重拍；把關與修正條款就是為此存在，實機紀錄裡寫了實際花了幾次。
- 已證明與未證明的事項見 [docs/known-limitations.md](./docs/known-limitations.md)；實機結果見 [docs/validation-2026-10-06.md](./docs/validation-2026-10-06.md)。
- 0.4.0 匯出檔在引擎與編輯器中的匯入尚未驗證（見 [引擎匯出](#引擎匯出)）。

## 生成素材與授權

下方的 MIT 授權涵蓋本 repository 的程式碼與文件，不涵蓋你用它生成的東西。圖片或影片模型產生的圖與影片，受該供應商條款約束；程式繪圖則來自你或你的 agent 寫的 spec。請勿描摹或用 prompt 生成你不擁有的角色或真人。商業專案請使用你掌握權利的原創角色或 IP，並確認各供應商的條款。

## Repository 結構

```text
agent-sprite-forge/
  .claude-plugin/        plugin.json, marketplace.json (Claude Code)
  skills/
    generate2dsprite/    SKILL.md, agents/openai.yaml, references/, scripts/
    video2dsprite/
    generate2dmap/
    generate2dmedia/
    codeart2d/           examples/ with every spec shown above
  shared/                canonical shared modules and JSON schemas (vendored into skills)
  tools/                 install_skills.py, vendor_sync.py, check_links.py
  tests/                 pytest and node suites, fixtures with provenance
  docs/                  validation records, known limitations, audit
  src/                   README media (src/v040: the 0.4 showcase)
  CHANGELOG.md
```

## Star History

<a href="https://www.star-history.com/?repos=0x0funky%2Fagent-sprite-forge&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=0x0funky/agent-sprite-forge&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=0x0funky/agent-sprite-forge&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=0x0funky/agent-sprite-forge&type=date&legend=top-left" />
 </picture>
</a>

## 授權

MIT，見 [LICENSE](./LICENSE)。
