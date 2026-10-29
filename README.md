# 🇩🇪 德语词汇自动提取工具

从 YouTube 视频自动提取德语词汇，整理成 Excel 词汇库。

---

## 快速开始

### 第一步：安装 Python 依赖

打开终端（macOS: Terminal / Windows: 命令提示符），运行：

```bash
pip install youtube-transcript-api google-genai openpyxl
```

### 第二步：获取免费 Gemini API Key

1. 访问 https://aistudio.google.com/app/apikey
2. 登录 Google 账号 → 点击「Create API Key」
3. 复制生成的 Key（格式类似 `AIza...`）
4. **完全免费**，每天可处理约 1500 个视频

### 第三步：运行工具

```bash
python german_vocab.py
```

---

## 使用方法

1. 把 Key 填进本文件夹里的 `config.json`（只需填一次）：

   ```json
   {
     "api_key": "AIza...你的Key...",
     "output_path": "/Users/你的用户名/Desktop/German_Vocabulary.xlsx"
   }
   ```

2. 启动程序，在界面里设置 Excel 输出路径（默认读取 `config.json` 里的路径）
3. 粘贴 YouTube 链接 → 点击「▶ 提取词汇」
4. 等待约 15 秒 → 词汇自动追加到 Excel 文件

---

## Excel 词汇库格式

| 列名 | 说明 |
|------|------|
| 词汇 | 名词带冠词（如 der Bahnhof），动词用不定式 |
| 词性 | n./v./adj./adv./part. 等 |
| 中文释义 | 中文翻译 |
| 英文释义 | 英文翻译（辅助理解） |
| 例句 | 直接来自视频字幕的原文例句 |
| 例句翻译 | 例句中文翻译 |
| 小品词/搭配 | 相关小品词或固定搭配（重点！） |
| 难度等级 | CEFR 等级：A1/A2/B1/B2/C1 |
| 备注 | 语气、用法补充说明 |
| 视频主题 | AI 猜测的视频主题 |
| 视频链接 | 原始 YouTube 链接 |
| 添加日期 | 处理日期 |

---

## 常见问题

**Q: 字幕获取失败怎么办？**
A: 确认视频有公开字幕（非仅版权字幕）。可尝试不同视频。

**Q: 字幕是英文不是德语？**
A: 工具优先获取德语字幕，若视频本身没有德语字幕则退而获取英文字幕，提取效果会稍差。

**Q: 如何在 Excel 里过滤特定难度？**
A: 在「难度等级」列使用 Excel 筛选功能即可。

**Q: 小品词(Partikel)是什么？**
A: 德语中表达语气的词，如 doch、mal、ja、eben、halt 等。工具会特别标注其语气含义。

---

## 技术说明

- **字幕获取**: `youtube-transcript-api`（免费，无需 API Key）
- **AI 分析**: Google Gemini 1.5 Flash（免费 tier 每天 1500 次）
- **Excel 操作**: `openpyxl`
- API Key 保存在：本文件夹下的 `config.json`（和 `german_vocab.py` 放在一起，直接双击就能打开编辑）
