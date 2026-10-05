# Agent Sprite Forge 大更新稽核與實作

日期：2026-10-05。範圍：這個 repo 的 sprite、map、video 技能與處理工具，
加上新的 API 生成入口。這是本機工作樹的更新紀錄；不是已發布的 GitHub release。

## 建議的方向

把 Forge 定位成 **agent 可以執行的遊戲美術製作流程**：

```text
玩法／相機／美術規格
  → 核准的角色、場景、道具原畫
  → 按用途選：靜態圖／sprite sheet／AI 影片／程式特效
  → 去背、固定座標、動作切段、地圖幾何與碰撞
  → 遊戲可讀取的素材、metadata、預覽與 QA
```

AI 影片值得正式加入，但不應把所有元素都改成影片。角色待機、流暢走路、
怪物呼吸、樹葉、水面、火光適合試；精準像素逐格動作、地形、UI 和可即時
調整的粒子特效仍各有更合適的方法。地圖本體宜保持清晰和穩定，局部動畫
放在獨立圖層。遊戲中是否命中、造成多少傷害，仍由遊戲規則決定。

## 已確認的問題與修正

| 範圍 | 舊問題 | 這次修正 |
| --- | --- | --- |
| 技能入口 | Sprite/map 指令過長；工具名稱與 magenta 路徑寫死 | 短入口＋按需參考文件；host tool／API 可選 |
| 影片生成 | 宣稱只支援 Grok Build，Codex/Claude 必須拒絕 | 原生工具、正式 API、現有影片三條路 |
| 透明圖片 | RGBA 用自己當 mask 再貼一次，透明度被重複相乘 | 保留 straight alpha；新增明確 native-alpha 路徑 |
| Sprite 品質 | pixel art 強制平滑；connected-component 篩選不精確 | 可選 nearest；修正最大元件與雜點過濾 |
| 動畫定位 | 每幀 bbox 裁切再各自縮放，造成大小抽動和跳躍失真 | 同一 clip 固定 envelope／尺度；保留來源座標與 root |
| Sprite／map QC | 拒絕的素材可能已寫到完成目錄 | 先驗證再發布；拒絕混用舊輸出 |
| Terrain | 重複 row index、零尺寸、矩形靜默裁成方形 | 提前驗證；裁切需明示，保留原生透明 |
| Props | slug 重複覆蓋、格線餘數被忽略、透明紫色可能被刪 | 先驗證命名／尺寸，選定去背模式 |
| 地圖預覽 | 前景／角色可能被忽略；像素素材被模糊縮放 | 分層合成與可選取樣；區分視覺、碰撞與遮擋 |
| 手機素材 | 「能播放 MP4」被誤認成「有透明」 | VP9 alpha、packed-alpha H.264、poster／PNG fallback 與 runtime 契約 |
| API 工作 | 沒有可恢復 job／請求紀錄 | dry-run、單次付費提交、保存 ID／hash、GET-only resume |

本次參考了 repo 中已有流程、同機已改進的技能版本，以及暮燈渡實際使用的
sourceSize/sourceAnchor、局部背景影片和手機素材契約。沒有把遊戲的硬編碼
尺寸、角色數或機器路徑搬成所有專案的規定。

## 四個技能各自負責什麼

| 技能 | 邊界 |
| --- | --- |
| `generate2dsprite` | 原畫、離散姿勢、角色一致性、透明圖、固定動作／atlas |
| `generate2dmap` | 玩法幾何、場景結構、props、捲軸層、碰撞／遮擋／轉場 |
| `video2dsprite` | 動作影片加工、固定定位、透明封裝、循環診斷、runtime handoff |
| `generate2dmedia` | 選定 API 的請求、生成、狀態追蹤與下載；不假裝完成遊戲 QA |

不增加遊戲 runtime 對生成服務的依賴。玩家端使用做好並驗證的素材；API key
只存在製作端。若要即時生成的 infinite game，是另一個具有延遲、成本與內容
管理需求的系統，不應混進這個資產工具的預設流程。

## API 調研與落地

本次已加入 OpenAI Images、xAI Imagine Image 與 xAI Imagine Video 的轉接器。
Google Veo／Gemini video、Runway 目前是調研候選，沒有冒稱已實作。
完整來源、日期、價格樣本與能力限制見
[provider-survey.md](../skills/generate2dmedia/references/provider-survey.md)。

xAI full 1.5 的首尾幀控制可以用來試循環；同一張圖放在首尾仍需檢查中間
動作、速度與接縫。API 與 Grok Build 原生 tool 的參數不同，訂閱額度也不能
當成 API 的免費額度。新 CLI 不暗中換模型或付費重試。

新 API 使用範例與中斷恢復見
[api-usage.md](../skills/generate2dmedia/references/api-usage.md)。

## 地圖應怎麼做

先決定可走區、入口出口、鏡頭、角色尺度、橋與平台，再生成對應的美術。

- **RPG 探索**：可編輯 tilemap 或地形底＋獨立 props；碰撞取樹幹／建築 footprint，
  不是整張透明圖的邊框。角色後方與前方遮擋分開處理。
- **2D 卷軸**：平台是碰撞幾何；遠、中、近景根據相機活動範圍計算覆蓋，
  不用「一律兩張畫面寬」猜測。
- **HD-2D 戰鬥**：穩定高解析背景＋局部水、火、霧、樹葉動畫；人物站立區與 UI
  安全區先規劃。整張背景影片是可選方案，需接受輸出解析度與壓縮代價。
- **跨地圖探索**：chunk 接口要定入口寬度、方向、spawn／trigger 區與碰撞；
  圖片接近並不代表遊戲能連通。

目前工具能生成／檢查素材與場景資料，**不是通用地圖編輯器或完整遊戲引擎**。
Unity、Godot、Three.js、Phaser 的實際碰撞與播放整合仍需在目標遊戲驗證。

## 相容性與遷移

- 保留原有主要 CLI；sprite 原有 `chroma_key`／`lanczos` 預設仍可用。
  新 RGBA 建議明確指定 `native_alpha`；像素素材選 `nearest`。
- 影片 sample 預設改用固定 envelope；只有重現旧結果才用
  `--registration legacy-per-frame`。正式影片封裝從尚未縮放的 clean frames 開始。
- `sourceSize`／`sourceAnchor` 描述原畫，不隨手機版編碼縮小。裁切後用
  `sourceRect` 對應回原畫空間。
- Packed MP4 要經 compositor 還原 alpha，不能直接當透明 `<video>` 疊上去。
  附帶 Canvas2D 範例便於驗證；多角色場景需共享 WebGL／decoder 與生命週期管理。
- 保留四個 sibling skill 目錄，避免只複製一個入口卻漏掉共用處理器。
- 這次未覆寫使用者全域安裝的 skills，也未修改暮燈渡遊戲或部署網站。

## 驗證與下一步

驗證明細見 [validation-2026-10-05.md](validation-2026-10-05.md)。
本輪有離線 API 合約測試、處理器回歸測試、真實 ffmpeg encode/decode 與舊示範
素材端到端加工；沒有花費 API 額度、沒有新模型畫質／速度排行榜，也沒有
實機 iPhone FPS 結果。

公開發布前最有價值的下一步是固定四個相同輸入的 showcase：角色走路、怪物
攻擊、樹木待機、局部水／火。各供應商記錄「含重試的每個合格 clip 成本」，
並同時展示遊戲尺寸、透明邊緣、首尾接縫與手機 frame time。這比只列更大模型
名稱或更多支援平台，更能讓開發者判斷 Forge 是否適合自己的遊戲。
